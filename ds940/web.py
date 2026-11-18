"""Lightweight monitoring and management web dashboard for DS-940DW scanner."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse

from .config import Settings
from .taxonomy import TaxonomyLedger

LOG = logging.getLogger(__name__)


def get_process_metrics() -> dict:
    rss_mb = 0.0
    try:
        import resource
        import sys
        maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss_mb = round(maxrss / (1024 * 1024), 1) if sys.platform == "darwin" else round(maxrss / 1024, 1)
    except Exception:
        pass
    return {"rss_mb": rss_mb}


def get_recent_scans(output_dir: Path, limit: int = 100) -> list[dict]:
    if not output_dir.exists():
        return []

    log_map: dict[str, str] = {}
    sort_log = output_dir / "SORT_LOG.md"
    if sort_log.exists():
        try:
            for line in sort_log.read_text(encoding="utf-8").splitlines():
                m = re.search(r"\*\*([^\*]+)\*\*:\s*Moved\s*`([^`]+)`\s*→\s*`([^`]+)`", line)
                if m:
                    log_ts, src_name, dst_rel = m.groups()
                    src_m = re.search(r"brother-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})", src_name)
                    if src_m:
                        y, mo, d, h, mi, s = src_m.groups()
                        scan_date_str = f"{y}-{mo}-{d} {h}:{mi}"
                    else:
                        scan_date_str = log_ts[:16]
                    log_map[dst_rel.strip()] = scan_date_str
                    log_map[Path(dst_rel.strip()).name] = scan_date_str
        except Exception:
            pass

    files = []
    for root, dirs, filenames in os.walk(output_dir):
        # Prevent traversal into hidden directories (e.g. .staging, .trash, .git)
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fname in filenames:
            if fname.lower().endswith(".pdf") and not fname.startswith("."):
                full_path = Path(root) / fname
                try:
                    stat = full_path.stat()
                    rel_path = full_path.relative_to(output_dir)
                    category = rel_path.parent.name if rel_path.parent.name else "Inbox"

                    # 1. Document Date (e.g. 2026-10-02 from 2026-10-02_Austin-Municipal-Court_...)
                    doc_date_match = re.match(r"^(\d{4}-\d{2}-\d{2})", fname)
                    doc_date = doc_date_match.group(1) if doc_date_match else None

                    # 2. Scan Date (when it was scanned by Brother DS-940DW)
                    scan_date = None
                    birth = getattr(stat, "st_birthtime", None)
                    if birth is not None and birth > 0:
                        scan_date = time.strftime("%Y-%m-%d %H:%M", time.localtime(birth))
                    elif str(rel_path) in log_map:
                        scan_date = log_map[str(rel_path)]
                    elif fname in log_map:
                        scan_date = log_map[fname]
                    else:
                        src_m = re.search(r"brother-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})", fname)
                        if src_m:
                            y, mo, d, h, mi, s = src_m.groups()
                            scan_date = f"{y}-{mo}-{d} {h}:{mi}"
                        else:
                            scan_date = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))

                    is_unfiled = (category == "Inbox")
                    files.append({
                        "filename": fname,
                        "relative_path": str(rel_path),
                        "category": category,
                        "folder": category,
                        "is_unfiled": is_unfiled,
                        "size_bytes": stat.st_size,
                        "size_kb": round(stat.st_size / 1024, 1),
                        "modified": stat.st_mtime,
                        "doc_date": doc_date,
                        "scan_date": scan_date,
                        "date_str": scan_date,
                    })
                except OSError:
                    continue

    files.sort(key=lambda x: x["modified"], reverse=True)
    return files[:limit]


class DashboardHandler(BaseHTTPRequestHandler):
    settings: Settings
    session: Any = None
    start_time: float = time.time()

    def log_message(self, format, *args):
        pass

    def send_json(self, data: dict | list, status: int = 200):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html", "/api/status", "/api/scans", "/api/categories", "/api/folders") or parsed.path.startswith("/files/"):
            self.send_response(200)
            self.end_headers()
        else:
            self.send_error(404, "Not Found")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self.serve_dashboard()
        elif path == "/api/status":
            self.serve_status()
        elif path == "/api/scans":
            self.send_json(get_recent_scans(self.settings.output))
        elif path == "/api/folders":
            self.serve_folders()
        elif path == "/api/categories":
            ledger = TaxonomyLedger(self.settings.output)
            self.send_json(ledger.get_active_categories())
        elif path.startswith("/files/"):
            self.serve_file(path[7:])
        elif path.startswith("/thumbs/"):
            self.serve_thumbnail(path[8:])
        else:
            self.send_error(404, "Not Found")

    def serve_folders(self):
        output_dir = self.settings.output
        folders = []
        if output_dir.exists():
            root_pdfs = [p for p in output_dir.glob("*.pdf") if p.is_file()]
            folders.append({"name": "Inbox", "count": len(root_pdfs), "is_root": True})
            for sub in sorted(output_dir.iterdir()):
                if sub.is_dir() and not sub.name.startswith("."):
                    sub_pdfs = [p for p in sub.glob("*.pdf") if p.is_file()]
                    folders.append({"name": sub.name, "count": len(sub_pdfs), "is_root": False})
        self.send_json(folders)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/sort":
            self.trigger_sort()
        elif parsed.path == "/api/delete":
            self.handle_delete()
        elif parsed.path == "/api/session/wake":
            self.handle_session_action("wake")
        elif parsed.path == "/api/session/extend":
            self.handle_session_action("extend")
        elif parsed.path == "/api/session/pause":
            self.handle_session_action("pause")
        else:
            self.send_error(404, "Not Found")

    def handle_session_action(self, action: str):
        try:
            from .session import SessionManager
            idle_timeout = getattr(self.settings, "idle_timeout", 180.0)
            mgr = self.session or SessionManager(self.settings.work, idle_timeout=idle_timeout)
            result = mgr.write_command(action, duration=300.0)
            self.send_json(result)
        except Exception as err:
            LOG.exception("Session action error (%s): %s", action, err)
            self.send_json({"status": "error", "message": str(err)}, status=500)

    def handle_delete(self):
        try:
            content_len = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(content_len).decode("utf-8"))
            rel_path = payload.get("path", "").lstrip("/")
            if not rel_path:
                self.send_json({"status": "error", "message": "Missing path"}, status=400)
                return

            target_path = (self.settings.output / rel_path).resolve()
            output_resolved = self.settings.output.resolve()
            target_path.relative_to(output_resolved)

            if target_path.is_file():
                target_path.unlink()
                self.send_json({"status": "ok", "deleted": rel_path})
            else:
                self.send_json({"status": "error", "message": "File not found"}, status=404)
        except (ValueError, OSError) as err:
            self.send_json({"status": "error", "message": str(err)}, status=400)

    def trigger_sort(self):
        try:
            import importlib.util
            script_path = Path(__file__).resolve().parent.parent / "scripts" / "sort_scans.py"
            if not script_path.exists():
                raise FileNotFoundError(f"Sort script not found at {script_path}")
            spec = importlib.util.spec_from_file_location("ds940_sort_scans", script_path)
            if not spec or not spec.loader:
                raise ImportError(f"Could not load spec for {script_path}")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            results = mod.sort_scans(self.settings.output, dry_run=False, api_key=self.settings.gemini_api_key or None)
            self.send_json({"status": "ok", "moved_count": len(results), "items": results})
        except Exception as err:
            LOG.exception("Sort error: %s", err)
            self.send_json({"status": "error", "message": str(err)}, status=500)

    def serve_file(self, rel_path: str):
        safe_rel = urllib.parse.unquote(rel_path).lstrip("/")
        target_path = (self.settings.output / safe_rel).resolve()
        try:
            output_resolved = self.settings.output.resolve()
            target_path.relative_to(output_resolved)
        except ValueError:
            self.send_error(403, "Access denied")
            return

        if not target_path.is_file():
            self.send_error(404, "File not found")
            return

        try:
            data = target_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'inline; filename="{target_path.name}"')
            self.end_headers()
            self.wfile.write(data)
        except OSError as err:
            LOG.exception("Error serving file %s: %s", target_path, err)
            self.send_error(500, f"Error reading file: {err}")

    def serve_thumbnail(self, rel_path: str):
        safe_rel = urllib.parse.unquote(rel_path).lstrip("/")
        target_path = (self.settings.output / safe_rel).resolve()
        try:
            output_resolved = self.settings.output.resolve()
            target_path.relative_to(output_resolved)
        except ValueError:
            self.send_error(403, "Access denied")
            return

        if not target_path.is_file():
            self.send_error(404, "File not found")
            return

        cache_dir = self.settings.work / "thumbnails"
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            st = target_path.stat()
            cache_key = hashlib.md5(f"{target_path}_{st.st_mtime}_{st.st_size}".encode()).hexdigest()
            thumb_path = cache_dir / f"{cache_key}.png"

            if not thumb_path.exists():
                qlmanage_bin = shutil.which("qlmanage") or "/usr/bin/qlmanage"
                if os.path.exists(qlmanage_bin) and target_path.stat().st_size > 500:
                    with tempfile.TemporaryDirectory() as tmp:
                        cmd = [qlmanage_bin, "-t", "-s", "256", "-o", tmp, str(target_path)]
                        try:
                            subprocess.run(cmd, capture_output=True, timeout=2.5)
                            generated = list(Path(tmp).glob("*.png"))
                            if generated:
                                shutil.copyfile(generated[0], thumb_path)
                        except subprocess.TimeoutExpired:
                            LOG.warning("qlmanage timed out generating thumbnail for %s", target_path)

            if thumb_path.exists():
                data = thumb_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(data)
                return
        except Exception as err:
            LOG.warning("Thumbnail generation error for %s: %s", target_path, err)

        self.send_error(404, "Thumbnail not available")

    def serve_status(self):
        status_file = self.settings.work / "auto-status.json"
        state = "unknown"
        updated = 0
        raw = {}
        if status_file.exists():
            try:
                raw = json.loads(status_file.read_text(encoding="utf-8"))
                state = raw.get("state", "unknown")
                updated = raw.get("updated", 0)
            except Exception:
                pass

        last_scan_file = self.settings.work / "last-scan.json"
        last_scan = None
        if last_scan_file.exists():
            try:
                last_scan = json.loads(last_scan_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        metrics = get_process_metrics()
        uptime_sec = int(time.time() - self.start_time)
        hours, remainder = divmod(uptime_sec, 3600)
        minutes, seconds = divmod(remainder, 60)

        session_active = raw.get("session_active", True)
        idle_rem = raw.get("idle_remaining")
        idle_tot = raw.get("idle_timeout", getattr(self.settings, "idle_timeout", 180.0))
        if self.session is not None:
            session_active = self.session.is_active()
            idle_rem = round(self.session.get_remaining(), 1)

        data = {
            "state": state,
            "scanner_ip": raw.get("scanner_ip", self.settings.scanner_ip),
            "output_dir": str(self.settings.output),
            "uptime": f"{hours}h {minutes}m {seconds}s",
            "uptime_seconds": uptime_sec,
            "memory_mb": metrics.get("rss_mb", 0.0),
            "updated": updated,
            "timeout_remaining": raw.get("timeout_remaining") if isinstance(raw, dict) else None,
            "sheet": raw.get("sheet") if isinstance(raw, dict) else None,
            "pages_so_far": raw.get("pages_so_far") if isinstance(raw, dict) else None,
            "destination": raw.get("destination") if isinstance(raw, dict) else None,
            "category": raw.get("category") if isinstance(raw, dict) else None,
            "pages": raw.get("pages") if isinstance(raw, dict) else None,
            "raw_pages": raw.get("raw_pages") if isinstance(raw, dict) else None,
            "discarded_blank_pages": raw.get("discarded_blank_pages") if isinstance(raw, dict) else None,
            "completed_at": raw.get("completed_at") if isinstance(raw, dict) else None,
            "session_active": session_active,
            "idle_remaining": idle_rem,
            "idle_timeout": idle_tot,
            "last_scan": last_scan,
        }
        self.send_json(data)

    def serve_dashboard(self):
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Brother DS-940DW</title>
  <style>
    :root {{
      --bg-page: #e8ecf2;
      --bg-window: #ffffff;
      --bg-sidebar: #f0f3f8;
      --bg-surface: #ffffff;
      --bg-subtle: #f4f6fa;
      --bg-hover: #e6ebf4;
      --bg-selected: #007aff;
      --bg-selected-text: #ffffff;
      --border: #d0d7e2;
      --border-subtle: #e2e8f0;
      --border-focus: #007aff;
      --text: #1d2129;
      --text-muted: #5a6474;
      --text-dim: #8892a2;
      --accent: #007aff;
      --accent-hover: #0062cc;
      --accent-soft: rgba(0, 122, 255, 0.12);
      --status-ready: #34c759;
      --status-active: #ff9500;
      --status-standby: #8e8e93;
      --status-danger: #ff3b30;
      --shadow-window: 0 16px 44px rgba(0, 0, 0, 0.16), 0 0 0 1px rgba(0, 0, 0, 0.08);
      --shadow-sm: 0 1px 3px rgba(0, 0, 0, 0.06);
      --ease: cubic-bezier(0.16, 1, 0.3, 1);
      --ease-press: cubic-bezier(0.4, 0, 0.2, 1);
    }}

    [data-theme="dark"] {{
      --bg-page: #0d0f14;
      --bg-window: #191c24;
      --bg-sidebar: #13151c;
      --bg-surface: #1e222c;
      --bg-subtle: #242936;
      --bg-hover: #2a3140;
      --bg-selected: #0a84ff;
      --bg-selected-text: #ffffff;
      --border: #2e3546;
      --border-subtle: #252b38;
      --border-focus: #0a84ff;
      --text: #f2f4f8;
      --text-muted: #9aa4b6;
      --text-dim: #6d788c;
      --accent: #0a84ff;
      --accent-hover: #409cff;
      --accent-soft: rgba(10, 132, 255, 0.16);
      --status-ready: #30d158;
      --status-active: #ff9f0a;
      --status-standby: #98989d;
      --status-danger: #ff453a;
      --shadow-window: 0 20px 50px rgba(0, 0, 0, 0.5), 0 0 0 1px rgba(255, 255, 255, 0.08);
      --shadow-sm: 0 1px 3px rgba(0, 0, 0, 0.3);
    }}

    @media (prefers-color-scheme: dark) {{
      :root:not([data-theme="light"]) {{
        --bg-page: #0d0f14;
        --bg-window: #191c24;
        --bg-sidebar: #13151c;
        --bg-surface: #1e222c;
        --bg-subtle: #242936;
        --bg-hover: #2a3140;
        --bg-selected: #0a84ff;
        --bg-selected-text: #ffffff;
        --border: #2e3546;
        --border-subtle: #252b38;
        --border-focus: #0a84ff;
        --text: #f2f4f8;
        --text-muted: #9aa4b6;
        --text-dim: #6d788c;
        --accent: #0a84ff;
        --accent-hover: #409cff;
        --accent-soft: rgba(10, 132, 255, 0.16);
        --status-ready: #30d158;
        --status-active: #ff9f0a;
        --status-standby: #98989d;
        --status-danger: #ff453a;
        --shadow-window: 0 20px 50px rgba(0, 0, 0, 0.5), 0 0 0 1px rgba(255, 255, 255, 0.08);
        --shadow-sm: 0 1px 3px rgba(0, 0, 0, 0.3);
      }}
    }}

    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    ::selection {{ background: var(--accent-soft); color: var(--accent); }}

    body {{
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro", "Helvetica Neue", Arial, sans-serif;
      background: var(--bg-page);
      color: var(--text);
      line-height: 1.45;
      min-height: 100vh;
      padding: 1.25rem;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: flex-start;
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
    }}

    /* Main macOS Finder Window Shell */
    .finder-window {{
      width: 100%;
      max-width: 1680px;
      min-height: calc(100vh - 2.5rem);
      background: var(--bg-window);
      border-radius: 11px;
      box-shadow: var(--shadow-window);
      display: flex;
      flex-direction: column;
      overflow: hidden;
      border: 1px solid var(--border);
      position: relative;
    }}

    /* macOS Finder Window Titlebar / Toolbar */
    .finder-titlebar {{
      height: 48px;
      background: var(--bg-surface);
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 0.85rem;
      gap: 0.5rem;
      user-select: none;
      flex-shrink: 0;
      position: relative;
      min-width: 0;
      overflow: hidden;
    }}

    .titlebar-left {{
      display: flex;
      align-items: center;
      gap: 0.65rem;
      flex-shrink: 1;
      min-width: 0;
    }}


    .finder-brand {{
      display: flex;
      align-items: center;
      gap: 0.45rem;
      font-weight: 600;
      font-size: 0.88rem;
      color: var(--text);
      letter-spacing: -0.01em;
      flex-shrink: 0;
    }}

    .titlebar-divider {{
      width: 1px;
      height: 18px;
      background: var(--border);
      flex-shrink: 0;
    }}

    /* Toolbar Status Strip */
    .finder-status-strip {{
      display: flex;
      align-items: center;
      gap: 0.45rem;
      font-size: 0.82rem;
      color: var(--text);
      min-width: 0;
      flex-shrink: 1;
    }}

    .strip-beacon {{
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--status-ready);
      transition: background-color 160ms ease;
      flex-shrink: 0;
    }}

    .strip-title {{
      font-size: 0.82rem;
      font-weight: 600;
      color: var(--text);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      min-width: 0;
    }}

    .strip-badge {{
      font-size: 0.68rem;
      font-weight: 600;
      padding: 0.1rem 0.45rem;
      border-radius: 4px;
      background: var(--bg-subtle);
      border: 1px solid var(--border);
      color: var(--text-muted);
      letter-spacing: 0.02em;
      text-transform: uppercase;
      white-space: nowrap;
      flex-shrink: 0;
    }}

    .titlebar-center {{
      display: flex;
      align-items: center;
      gap: 0.4rem;
      flex-shrink: 0;
      white-space: nowrap;
    }}

    .strip-actions {{
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      flex-wrap: nowrap;
      white-space: nowrap;
    }}

    .strip-sleep-pill {{
      font-size: 0.72rem;
      font-weight: 600;
      font-variant-numeric: tabular-nums;
      color: var(--text-muted);
      background: var(--bg-subtle);
      border: 1px solid var(--border);
      padding: 0.2rem 0.5rem;
      border-radius: 4px;
      white-space: nowrap;
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      flex-shrink: 0;
    }}

    .strip-btn-group {{
      display: inline-flex;
      align-items: center;
      gap: 0.3rem;
      flex-wrap: nowrap;
      white-space: nowrap;
    }}

    .btn-strip {{
      display: inline-flex;
      align-items: center;
      gap: 0.3rem;
      font-size: 0.73rem;
      font-weight: 500;
      color: var(--text-muted);
      background: var(--bg-surface);
      border: 1px solid var(--border);
      padding: 0.22rem 0.55rem;
      border-radius: 4px;
      cursor: pointer;
      white-space: nowrap;
      flex-shrink: 0;
      transition: background-color 100ms var(--ease), border-color 100ms var(--ease), color 100ms var(--ease), transform 80ms var(--ease-press);
    }}

    .btn-strip:hover {{
      background: var(--bg-hover);
      color: var(--text);
      border-color: var(--border-focus);
    }}

    .btn-strip-primary {{
      background: var(--accent);
      border-color: var(--accent);
      color: #ffffff;
      font-weight: 600;
    }}

    .btn-strip-primary:hover {{
      background: var(--accent-hover);
      color: #ffffff;
    }}

    .btn-strip-accent {{
      background: var(--accent-soft);
      border-color: rgba(0, 122, 255, 0.25);
      color: var(--accent);
      font-weight: 600;
    }}

    .btn-strip-accent:hover {{
      background: var(--accent);
      color: #ffffff;
    }}

    .titlebar-right {{
      display: flex;
      align-items: center;
      gap: 0.35rem;
      flex-shrink: 0;
      margin-left: auto;
    }}

    .view-toggle-group {{
      display: inline-flex;
      background: var(--bg-subtle);
      border: 1px solid var(--border);
      border-radius: 5px;
      padding: 1px;
    }}

    .btn-view-toggle {{
      background: transparent;
      border: none;
      width: 26px;
      height: 24px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 4px;
      color: var(--text-dim);
      cursor: pointer;
      transition: background-color 100ms var(--ease), color 100ms var(--ease);
    }}

    .btn-view-toggle:hover {{
      color: var(--text);
    }}

    .btn-view-toggle.active {{
      background: var(--bg-surface);
      color: var(--text);
      box-shadow: 0 1px 2px rgba(0,0,0,0.1);
    }}

    .btn-icon {{
      width: 26px;
      height: 26px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 4px;
      background: var(--bg-surface);
      border: 1px solid var(--border);
      color: var(--text-muted);
      cursor: pointer;
      transition: background-color 100ms var(--ease), border-color 100ms var(--ease);
    }}

    .btn-icon:hover {{
      background: var(--bg-hover);
      color: var(--text);
      border-color: var(--border-focus);
    }}

    .btn-icon.active {{
      background: var(--accent-soft);
      border-color: var(--accent);
      color: var(--accent);
    }}

    /* Hardware Alert Banner */
    .alert-banner {{
      background: rgba(255, 59, 48, 0.08);
      border-bottom: 1px solid rgba(255, 59, 48, 0.25);
      padding: 0.6rem 1rem;
      display: flex;
      align-items: center;
      gap: 0.75rem;
      animation: alertSlideDown 160ms var(--ease);
    }}

    @keyframes alertSlideDown {{
      from {{ transform: translateY(-8px); opacity: 0; }}
      to {{ transform: translateY(0); opacity: 1; }}
    }}

    .alert-icon {{
      color: var(--status-danger);
      flex-shrink: 0;
    }}

    .alert-body {{
      flex: 1;
      font-size: 0.78rem;
      color: var(--text);
      display: flex;
      align-items: center;
      gap: 0.5rem;
      flex-wrap: wrap;
    }}

    .alert-title {{
      font-weight: 600;
      color: var(--status-danger);
    }}

    .btn-alert-action {{
      font-size: 0.72rem;
      font-weight: 600;
      padding: 0.22rem 0.55rem;
      border-radius: 4px;
      background: var(--status-danger);
      color: #fff;
      border: none;
      cursor: pointer;
    }}

    /* 3-Pane Finder Body Layout */
    .finder-body {{
      flex: 1;
      display: grid;
      grid-template-columns: 210px minmax(0, 1fr) clamp(340px, 32vw, 440px);
      min-height: 0;
      position: relative;
    }}

    .finder-body.quicklook-collapsed {{
      grid-template-columns: 210px minmax(0, 1fr);
    }}

    /* Left Source List Sidebar */
    .finder-sidebar {{
      background: var(--bg-sidebar);
      border-right: 1px solid var(--border);
      padding: 0.75rem 0.5rem;
      display: flex;
      flex-direction: column;
      gap: 1rem;
      overflow-y: auto;
      user-select: none;
    }}

    .sidebar-section-title {{
      font-size: 0.68rem;
      font-weight: 700;
      color: var(--text-dim);
      letter-spacing: 0.05em;
      text-transform: uppercase;
      padding: 0 0.5rem 0.25rem;
    }}

    .sidebar-nav {{
      display: flex;
      flex-direction: column;
      gap: 2px;
    }}

    .sidebar-item {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0.35rem 0.6rem;
      border-radius: 5px;
      font-size: 0.8rem;
      font-weight: 500;
      color: var(--text);
      cursor: pointer;
      text-decoration: none;
      transition: background-color 80ms var(--ease), color 80ms var(--ease);
    }}

    .sidebar-item:hover {{
      background: var(--bg-hover);
    }}

    .sidebar-item.active {{
      background: var(--bg-selected);
      color: var(--bg-selected-text);
      font-weight: 600;
    }}

    .sidebar-item.active .sidebar-count {{
      background: rgba(255, 255, 255, 0.25);
      color: #ffffff;
    }}

    .sidebar-item.active .sidebar-icon {{
      color: #ffffff;
    }}

    .sidebar-item-label {{
      display: flex;
      align-items: center;
      gap: 0.5rem;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}

    .sidebar-icon {{
      width: 16px;
      height: 16px;
      flex-shrink: 0;
      color: var(--accent);
    }}

    .sidebar-count {{
      font-size: 0.68rem;
      font-weight: 600;
      background: var(--bg-subtle);
      border: 1px solid var(--border-subtle);
      color: var(--text-muted);
      padding: 0.05rem 0.4rem;
      border-radius: 10px;
      font-variant-numeric: tabular-nums;
    }}


    /* Main Finder Browser Pane */
    .finder-main {{
      background: var(--bg-window);
      display: flex;
      flex-direction: column;
      min-width: 0;
      overflow: hidden;
    }}

    /* Path Breadcrumbs Bar */
    .finder-breadcrumbs {{
      height: 36px;
      border-bottom: 1px solid var(--border-subtle);
      padding: 0 1rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
      background: var(--bg-window);
      font-size: 0.76rem;
      color: var(--text-dim);
      flex-shrink: 0;
    }}

    .crumb-trail {{
      display: flex;
      align-items: center;
      gap: 0.4rem;
      min-width: 0;
      overflow: hidden;
      white-space: nowrap;
      text-overflow: ellipsis;
    }}


    .crumb-active {{
      font-weight: 600;
      color: var(--text);
    }}

    .search-box-wrap {{
      position: relative;
      width: 180px;
    }}

    .search-input {{
      width: 100%;
      background: var(--bg-subtle);
      border: 1px solid var(--border);
      border-radius: 5px;
      padding: 0.2rem 1.6rem 0.2rem 1.6rem;
      font-size: 0.74rem;
      color: var(--text);
    }}

    .search-input:focus {{
      outline: none;
      border-color: var(--border-focus);
      box-shadow: 0 0 0 2px var(--accent-soft);
      background: var(--bg-surface);
    }}

    .search-icon {{
      position: absolute;
      left: 0.45rem;
      top: 50%;
      transform: translateY(-50%);
      width: 12px;
      height: 12px;
      color: var(--text-dim);
      pointer-events: none;
    }}

    .search-kbd {{
      position: absolute;
      right: 0.45rem;
      top: 50%;
      transform: translateY(-50%);
      font-size: 0.6rem;
      color: var(--text-dim);
      background: var(--bg-surface);
      border: 1px solid var(--border);
      border-radius: 3px;
      padding: 0 3px;
      pointer-events: none;
    }}

    /* Browser Content Area: Icon Grid */
    .finder-content {{
      flex: 1;
      overflow-y: auto;
      padding: 1.25rem;
    }}

    .finder-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(135px, 1fr));
      gap: 1.25rem 1rem;
      align-items: start;
    }}

    .finder-icon-card {{
      display: flex;
      flex-direction: column;
      align-items: center;
      text-align: center;
      padding: 0.6rem 0.45rem;
      border-radius: 6px;
      cursor: pointer;
      user-select: none;
      position: relative;
      transition: background-color 80ms var(--ease);
    }}

    .finder-icon-card:hover {{
      background: var(--bg-hover);
    }}

    .finder-icon-card.selected {{
      background: var(--accent-soft);
      outline: 2px solid var(--accent);
      outline-offset: -1px;
    }}

    .finder-doc-icon {{
      width: 58px;
      height: 74px;
      position: relative;
      margin-bottom: 0.45rem;
      display: flex;
      align-items: center;
      justify-content: center;
    }}

    .doc-thumbnail-img {{
      width: 56px;
      height: 72px;
      object-fit: cover;
      object-position: top;
      background: #ffffff;
      border-radius: 4px;
      border: 1px solid var(--border);
      box-shadow: 0 2px 5px rgba(0,0,0,0.12);
      transition: transform 120ms var(--ease), box-shadow 120ms var(--ease);
      display: block;
    }}

    .finder-icon-card:hover .doc-thumbnail-img {{
      transform: translateY(-2px);
      box-shadow: 0 4px 10px rgba(0,0,0,0.18);
    }}

    .doc-fallback-wrap {{
      width: 56px;
      height: 68px;
      position: relative;
      filter: drop-shadow(0 2px 4px rgba(0,0,0,0.12));
    }}

    .list-name-col {{
      display: inline-flex;
      align-items: center;
      gap: 0.55rem;
      font-weight: 600;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      min-width: 0;
    }}

    .list-thumb-img {{
      width: 18px;
      height: 23px;
      object-fit: cover;
      object-position: top;
      background: #ffffff;
      border-radius: 2px;
      border: 1px solid var(--border);
      flex-shrink: 0;
      box-shadow: 0 1px 3px rgba(0,0,0,0.1);
    }}

    .doc-page-svg {{
      width: 100%;
      height: 100%;
    }}

    .doc-page-badge {{
      position: absolute;
      bottom: 6px;
      left: 50%;
      transform: translateX(-50%);
      font-size: 0.6rem;
      font-weight: 700;
      background: #ff3b30;
      color: #fff;
      padding: 0.05rem 0.35rem;
      border-radius: 3px;
      letter-spacing: 0.02em;
    }}

    .doc-card-title {{
      font-size: 0.74rem;
      font-weight: 500;
      color: var(--text);
      line-height: 1.25;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
      text-overflow: ellipsis;
      word-break: break-word;
      max-width: 100%;
      margin-bottom: 0.2rem;
    }}

    .doc-card-meta {{
      font-size: 0.66rem;
      color: var(--text-dim);
      font-variant-numeric: tabular-nums;
    }}

    /* Compact List View */
    .finder-list {{
      display: none;
      flex-direction: column;
      width: 100%;
      border-collapse: collapse;
      font-size: 0.76rem;
    }}

    .finder-list-header {{
      display: grid;
      grid-template-columns: minmax(200px, 2fr) 140px 110px 90px 120px 40px;
      padding: 0.35rem 0.75rem;
      font-size: 0.68rem;
      font-weight: 600;
      color: var(--text-dim);
      border-bottom: 1px solid var(--border);
      user-select: none;
    }}

    .finder-list-row {{
      display: grid;
      grid-template-columns: minmax(200px, 2fr) 140px 110px 90px 120px 40px;
      padding: 0.45rem 0.75rem;
      align-items: center;
      border-bottom: 1px solid var(--border-subtle);
      cursor: pointer;
      transition: background-color 70ms var(--ease);
    }}

    .finder-list-row:hover {{
      background: var(--bg-hover);
    }}

    .finder-list-row.selected {{
      background: var(--bg-selected);
      color: var(--bg-selected-text);
    }}

    .finder-list-row.selected .doc-card-meta,
    .finder-list-row.selected .meta-tag {{
      color: #ffffff;
    }}

    /* Right Quick Look Inspector Pane */
    .quick-look-pane {{
      background: var(--bg-surface);
      border-left: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      overflow: hidden;
      min-width: 0;
    }}

    .quick-look-header {{
      padding: 0.65rem 0.85rem;
      border-bottom: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      gap: 0.4rem;
      background: var(--bg-surface);
    }}

    .quick-look-title-row {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 0.5rem;
    }}

    .quick-look-title {{
      font-size: 0.86rem;
      font-weight: 600;
      color: var(--text);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      flex: 1;
    }}

    .quick-look-meta {{
      display: flex;
      align-items: center;
      gap: 0.45rem;
      flex-wrap: wrap;
      font-size: 0.72rem;
      color: var(--text-muted);
    }}

    .quick-look-actions {{
      display: flex;
      align-items: center;
      gap: 0.35rem;
      margin-top: 0.2rem;
    }}

    .btn-action {{
      display: inline-flex;
      align-items: center;
      gap: 0.3rem;
      font-size: 0.72rem;
      font-weight: 500;
      padding: 0.25rem 0.55rem;
      border-radius: 4px;
      background: var(--bg-subtle);
      border: 1px solid var(--border);
      color: var(--text);
      text-decoration: none;
      cursor: pointer;
    }}

    .btn-action:hover {{
      background: var(--bg-hover);
      border-color: var(--border-focus);
    }}

    .btn-action.btn-danger:hover {{
      background: rgba(255, 59, 48, 0.1);
      color: var(--status-danger);
      border-color: var(--status-danger);
    }}

    .quick-look-preview {{
      flex: 1;
      width: 100%;
      height: 100%;
      border: none;
      background: var(--bg-subtle);
    }}

    .quick-look-zero {{
      flex: 1;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 2rem 1.5rem;
      text-align: center;
      color: var(--text-muted);
      gap: 0.5rem;
    }}

    /* Finder Statusbar Footer */
    .finder-statusbar {{
      min-height: 28px;
      height: 28px;
      background: var(--bg-surface);
      border-top: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 0.85rem;
      font-size: 0.7rem;
      color: var(--text-dim);
      user-select: none;
      flex-shrink: 0;
      gap: 0.5rem;
      overflow: hidden;
    }}

    .statusbar-left {{
      display: flex;
      align-items: center;
      gap: 0.5rem;
      min-width: 0;
      overflow: hidden;
      white-space: nowrap;
      text-overflow: ellipsis;
    }}

    .statusbar-right {{
      display: flex;
      align-items: center;
      gap: 0.5rem;
      flex-shrink: 0;
      margin-left: auto;
    }}

    .statusbar-item {{
      display: inline-flex;
      align-items: center;
      gap: 0.25rem;
      white-space: nowrap;
      flex-shrink: 0;
    }}

    .statusbar-link {{
      color: var(--accent);
      text-decoration: none;
      display: inline-flex;
      align-items: center;
    }}

    .statusbar-link:hover {{
      text-decoration: underline;
    }}

    #diag-scanner {{
      max-width: 170px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      display: inline-block;
      vertical-align: bottom;
    }}

    .statusbar-path {{
      max-width: 220px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      display: inline-block;
      vertical-align: bottom;
    }}

    .btn-copy {{
      background: transparent;
      border: 1px solid var(--border);
      color: var(--text-muted);
      padding: 0.05rem 0.35rem;
      border-radius: 3px;
      font-size: 0.65rem;
      cursor: pointer;
      flex-shrink: 0;
    }}

    /* Responsive Breakpoints */
    @media (max-width: 1200px) {{
      .strip-badge {{
        display: none !important;
      }}
    }}

    @media (max-width: 1024px) {{
      .finder-body {{
        grid-template-columns: 190px minmax(0, 1fr);
      }}
      .quick-look-pane {{
        position: fixed;
        right: 0;
        top: 0;
        bottom: 0;
        width: 440px;
        max-width: 100vw;
        z-index: 200;
        box-shadow: -10px 0 30px rgba(0,0,0,0.25);
        display: none;
      }}
      .quick-look-pane.mobile-open {{
        display: flex;
      }}
      .btn-close-mobile {{
        display: inline-flex !important;
      }}
      .finder-brand, .titlebar-divider {{
        display: none !important;
      }}
    }}

    @media (max-width: 820px) {{
      #btn-trigger-sort .btn-text {{
        display: none !important;
      }}
      #btn-trigger-sort {{
        padding: 0.22rem 0.45rem;
      }}
      .statusbar-path, .statusbar-archive-label {{
        display: none !important;
      }}
      #diag-scanner {{
        max-width: 120px;
      }}
    }}

    @media (max-width: 768px) {{
      body {{
        padding: 0.35rem;
      }}
      .finder-window {{
        border-radius: 8px;
        min-height: calc(100vh - 0.7rem);
      }}
      .finder-body {{
        grid-template-columns: 1fr;
      }}
      .finder-sidebar {{
        display: none;
      }}
      .titlebar-center {{
        display: none !important;
      }}
      .quick-look-pane {{
        width: 100vw;
      }}
    }}

    @media (max-width: 480px) {{
      .finder-titlebar {{
        padding: 0 0.5rem;
        gap: 0.35rem;
      }}
      #strip-title {{
        max-width: 100px;
      }}
      .search-kbd {{
        display: none !important;
      }}
      .search-box-wrap {{
        max-width: 130px;
        min-width: 90px;
      }}
      .search-input {{
        padding: 0.2rem 0.5rem 0.2rem 1.4rem;
        font-size: 0.72rem;
      }}
      .finder-breadcrumbs {{
        padding: 0 0.5rem;
      }}
      .finder-statusbar {{
        padding: 0 0.5rem;
        font-size: 0.67rem;
      }}
      #statusbar-summary {{
        max-width: 80px;
        overflow: hidden;
        text-overflow: ellipsis;
      }}
      #diag-scanner {{
        max-width: 85px;
      }}
    }}
  </style>
</head>
<body>
  <!-- Main Finder Window -->
  <div class="finder-window" id="finder-window" role="application" aria-label="macOS Finder Document Workspace">
    <!-- Window Titlebar / Toolbar -->
    <header class="finder-titlebar" id="live-status-strip" role="region" aria-live="polite">
      <div class="titlebar-left">
        <div class="finder-brand">
          <span class="finder-brand-name">Brother DS-940DW</span>
        </div>
        <div class="titlebar-divider" aria-hidden="true"></div>
        <div class="finder-status-strip">
          <span id="strip-beacon" class="strip-beacon"></span>
          <h2 id="strip-title" class="strip-title">Ready for paper</h2>
          <span id="strip-badge" class="strip-badge">Duplex 300 DPI</span>
        </div>
      </div>

      <div class="titlebar-center header-session-actions" id="header-session-actions">
        <div id="strip-countdown-wrap" class="strip-countdown-wrap" style="display:none;">
          <span class="countdown-pulse-dot"></span>
          <span class="countdown-label">Batch:</span>
          <span id="strip-countdown-num" class="strip-countdown-num">8.0s</span>
        </div>
        <div id="strip-actions" class="strip-actions"></div>
      </div>

      <div class="titlebar-right header-global-actions" id="header-global-actions">
        <button class="btn-strip btn-strip-accent" id="btn-trigger-sort" onclick="triggerSortScans()" title="Sort and organize unfiled scans with Gemini AI">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>
          </svg>
          <span class="btn-text">Organize</span>
        </button>

        <div class="view-toggle-group" id="view-toggle" role="group" aria-label="View Mode">
          <button class="btn-view-toggle active" id="view-toggle-grid" onclick="setViewMode('grid')" title="Icon Grid View" aria-label="Icon Grid View">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="3" y="3" width="7" height="7"/>
              <rect x="14" y="3" width="7" height="7"/>
              <rect x="14" y="14" width="7" height="7"/>
              <rect x="3" y="14" width="7" height="7"/>
            </svg>
          </button>
          <button class="btn-view-toggle" id="view-toggle-list" onclick="setViewMode('list')" title="List View" aria-label="List View">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="8" y1="6" x2="21" y2="6"/>
              <line x1="8" y1="12" x2="21" y2="12"/>
              <line x1="8" y1="18" x2="21" y2="18"/>
              <line x1="3" y1="6" x2="3.01" y2="6"/>
              <line x1="3" y1="12" x2="3.01" y2="12"/>
              <line x1="3" y1="18" x2="3.01" y2="18"/>
            </svg>
          </button>
        </div>

        <button class="btn-icon" id="btn-toggle-quicklook" onclick="toggleQuickLook()" title="Toggle Quick Look Preview (Spacebar)" aria-label="Toggle Quick Look">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
            <circle cx="12" cy="12" r="3"/>
          </svg>
        </button>

        <button class="btn-icon" id="theme-toggle" onclick="toggleTheme()" title="Switch color theme" aria-label="Switch color theme">
          <svg id="theme-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="12" cy="12" r="5"/>
            <line x1="12" y1="1" x2="12" y2="3"/>
            <line x1="12" y1="21" x2="12" y2="23"/>
            <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/>
            <line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/>
            <line x1="1" y1="12" x2="3" y2="12"/>
            <line x1="21" y1="12" x2="23" y2="12"/>
            <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/>
            <line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>
          </svg>
        </button>

        <button class="btn-icon" onclick="refreshAll()" title="Refresh archive and hardware status" aria-label="Refresh">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="23 4 23 10 17 10"/>
            <polyline points="1 20 1 14 7 14"/>
            <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
          </svg>
        </button>
      </div>

      <div id="strip-progress-bar" class="strip-progress-bar" style="position:absolute; bottom:0; left:0; height:2px; width:0; background:var(--status-active); transition: width 0.7s linear;"></div>
    </header>

    <!-- In-Page Hardware Alert Banner (for Paper Jams, Feed Errors) -->
    <div id="hardware-alert-banner" class="alert-banner" style="display:none;" role="alert">
      <div class="alert-icon" aria-hidden="true">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
          <line x1="12" y1="9" x2="12" y2="13"/>
          <line x1="12" y1="17" x2="12.01" y2="17"/>
        </svg>
      </div>
      <div class="alert-body">
        <span class="alert-title" id="alert-heading">Paper Feed Error / Jam Detected:</span>
        <span><b>1.</b> Clear sheet</span>
        <span>→</span>
        <span><b>2.</b> Press <b>Power</b> once to clear red (!)</span>
        <span>→</span>
        <span><b>3.</b> Re-insert sheet straight</span>
      </div>
      <button class="btn-alert-action" onclick="refreshAll()">Check Hardware</button>
    </div>

    <!-- 3-Pane Finder Body -->
    <div class="finder-body" id="finder-body">
      <!-- Left: Source List Sidebar -->
      <aside class="finder-sidebar" id="finder-sidebar" aria-label="Finder Sidebar">
        <div class="sidebar-section">
          <div class="sidebar-section-title">Favorites</div>
          <nav class="sidebar-nav" id="sidebar-favorites">
            <a class="sidebar-item active" data-folder="all" onclick="selectFolder('all', this)">
              <span class="sidebar-item-label">
                <svg class="sidebar-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
                </svg>
                <span>Recent Scans</span>
              </span>
              <span class="sidebar-count" id="sidebar-count-all">0</span>
            </a>
            <a class="sidebar-item" data-folder="Inbox" onclick="selectFolder('Inbox', this)">
              <span class="sidebar-item-label">
                <svg class="sidebar-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/>
                  <path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>
                </svg>
                <span>Inbox / Unfiled</span>
              </span>
              <span class="sidebar-count" id="sidebar-count-inbox">0</span>
            </a>
          </nav>
        </div>

        <div class="sidebar-section">
          <div class="sidebar-section-title">Folders</div>
          <nav class="sidebar-nav" id="sidebar-folders">
            <!-- Dynamically populated from /api/folders -->
          </nav>
        </div>
      </aside>

      <!-- Center: Main Browser Area -->
      <main class="finder-main" id="finder-main" aria-label="Filesystem Browser">
        <!-- Breadcrumbs & Search -->
        <div class="finder-breadcrumbs" id="finder-breadcrumbs">
          <div class="crumb-trail">
            <span class="crumb-active" id="crumb-folder">Recent Scans</span>
            <span class="doc-size" id="scan-count" style="margin-left:0.4rem;">0 items</span>
          </div>

          <div class="search-box-wrap">
            <svg class="search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <circle cx="11" cy="11" r="8"/>
              <line x1="21" y1="21" x2="16.65" y2="16.65"/>
            </svg>
            <input type="search" id="search-box" class="search-input" placeholder="Search files..." oninput="onSearchInput()" aria-label="Search files">
            <kbd class="search-kbd">/</kbd>
          </div>
        </div>

        <!-- Browser Content -->
        <div class="finder-content" id="doc-list" role="listbox" aria-label="Documents">
          <!-- Icon Grid View (Default) -->
          <div class="finder-grid" id="finder-grid">
            <!-- Rendered by JS -->
          </div>

          <!-- List View (Optional Toggle) -->
          <div class="finder-list" id="finder-list">
            <div class="finder-list-header">
              <span>Name</span>
              <span>Scanned</span>
              <span>Doc Date</span>
              <span>Size</span>
              <span>Category</span>
              <span></span>
            </div>
            <div id="finder-list-body"></div>
          </div>
        </div>
      </main>

      <!-- Right: macOS Quick Look Inspector -->
      <section class="quick-look-pane" id="quick-look-pane" aria-label="Quick Look Document Inspector">
        <div class="quick-look-header">
          <div class="quick-look-title-row">
            <h3 class="quick-look-title" id="inspector-filename">Select a document</h3>
            <button class="btn-icon btn-close-mobile" onclick="toggleQuickLook()" title="Close Quick Look" style="display:none;">✕</button>
          </div>

          <div class="quick-look-meta" id="inspector-meta">
            <span id="meta-category" class="category-tag cat-inbox" style="display:none;"></span>
            <span id="meta-doc-date" class="meta-tag" style="display:none;"></span>
            <span id="meta-scan-date" class="meta-tag" style="display:none;"></span>
            <span id="meta-size" class="meta-tag" style="display:none;"></span>
          </div>

          <div class="quick-look-actions">
            <a class="btn-action" id="btn-download" href="#" download="" title="Download PDF" style="display:none;">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                <polyline points="7 10 12 15 17 10"/>
                <line x1="12" y1="15" x2="12" y2="3"/>
              </svg>
              <span>Download</span>
            </a>
            <a class="btn-action" id="btn-open-external" href="#" target="_blank" rel="noopener" title="Open in new window" style="display:none;">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>
                <polyline points="15 3 21 3 21 9"/>
                <line x1="10" y1="14" x2="21" y2="3"/>
              </svg>
              <span>Open</span>
            </a>
            <button class="btn-action btn-danger" id="btn-delete-doc" onclick="deleteActiveDoc()" title="Delete document" style="display:none;">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polyline points="3 6 5 6 21 6"/>
                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
              </svg>
              <span>Delete</span>
            </button>
          </div>
        </div>

        <iframe class="quick-look-preview" id="viewer-frame" src="about:blank" title="PDF Document Preview"></iframe>

        <div class="quick-look-zero" id="inspector-zero">
          <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="color:var(--text-dim);">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
            <polyline points="14 2 14 8 20 8"/>
          </svg>
          <div style="font-weight:600; font-size:0.86rem; color:var(--text);">Quick Look</div>
          <div style="font-size:0.75rem;">Select any file and tap <b>Spacebar</b> to preview.</div>
        </div>
      </section>
    </div>

    <!-- Finder Statusbar Footer -->
    <footer class="finder-statusbar diagnostics-footer" aria-label="Finder Statusbar">
      <div class="statusbar-left">
        <span class="statusbar-item" id="statusbar-summary">0 items, 0 MB</span>
        <span>·</span>
        <span class="statusbar-item" title="Scanner Network Endpoint">
          <span>Scanner:</span>
          <a id="diag-scanner" class="statusbar-link" href="http://{self.settings.scanner_ip}:8080/" target="_blank" rel="noopener" title="Open Brother Web Admin">{self.settings.scanner_ip}:8080 ↗</a>
        </span>
        <span>·</span>
        <span class="statusbar-item" title="Daemon Engine State">
          <span>State:</span>
          <span id="diag-state">Connecting</span>
        </span>
      </div>

      <div class="statusbar-right">
        <span class="statusbar-item" title="Local Archive Destination">
          <span class="statusbar-archive-label">Archive:</span>
          <span id="diag-path" class="statusbar-path">{self.settings.output}</span>
          <button class="btn-copy" onclick="copyPath()" title="Copy folder path" aria-label="Copy folder path">Copy</button>
        </span>
      </div>
    </footer>
  </div>

  <div id="toast" class="toast" role="alert" style="position:fixed; bottom:2rem; right:2rem; background:var(--bg-surface); border:1px solid var(--border); padding:0.5rem 0.9rem; border-radius:6px; font-size:0.76rem; font-weight:500; box-shadow:0 8px 24px rgba(0,0,0,0.15); opacity:0; pointer-events:none; transition:opacity 160ms var(--ease);">Notification</div>

  <!-- Deletion Confirmation Modal -->
  <div id="confirm-modal" class="modal-backdrop" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.45); backdrop-filter:blur(3px); -webkit-backdrop-filter:blur(3px); z-index:1000; align-items:center; justify-content:center; padding:1rem;">
    <div class="modal-card" style="background:var(--bg-surface); border:1px solid var(--border); border-radius:10px; box-shadow:var(--shadow-window); max-width:400px; width:100%; padding:1.25rem;">
      <h3 id="modal-title" style="font-size:0.95rem; font-weight:600; margin-bottom:0.35rem;">Move to Trash?</h3>
      <p style="font-size:0.78rem; color:var(--text-muted); line-height:1.4; margin-bottom:0.6rem;">This PDF will be permanently removed from your Mac Mini archive:</p>
      <div id="modal-filename" style="font-family:monospace; font-size:0.72rem; background:var(--bg-subtle); padding:0.3rem 0.5rem; border-radius:4px; margin-bottom:1rem; word-break:break-all;">document.pdf</div>
      <div style="display:flex; justify-content:flex-end; gap:0.5rem;">
        <button class="btn-strip" onclick="closeConfirmModal()">Cancel</button>
        <button class="btn-strip btn-strip-primary" style="background:var(--status-danger); border-color:var(--status-danger);" onclick="executePendingDelete()">Delete Document</button>
      </div>
    </div>
  </div>

  <script>
    let allScans = [];
    let currentFolder = 'all';
    let currentViewMode = localStorage.getItem('ds940-view-mode') || 'grid';
    let isQuickLookOpen = true;
    let selectedIndex = -1;
    let activeDoc = null;
    let pendingDelete = null;

    function showToast(msg) {{
      const t = document.getElementById('toast');
      t.textContent = msg;
      t.style.opacity = '1';
      setTimeout(() => t.style.opacity = '0', 2200);
    }}

    function copyPath() {{
      const p = document.getElementById('diag-path').textContent;
      navigator.clipboard.writeText(p);
      showToast('Path copied to clipboard');
    }}

    /* Theme Management */
    function initTheme() {{
      const saved = localStorage.getItem('ds940-theme') || 'system';
      applyTheme(saved);
    }}

    function applyTheme(theme) {{
      if (theme === 'dark') {{
        document.documentElement.setAttribute('data-theme', 'dark');
      }} else if (theme === 'light') {{
        document.documentElement.setAttribute('data-theme', 'light');
      }} else {{
        document.documentElement.removeAttribute('data-theme');
      }}
      localStorage.setItem('ds940-theme', theme);
    }}

    function toggleTheme() {{
      const current = document.documentElement.getAttribute('data-theme');
      if (!current) {{
        const isDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
        applyTheme(isDark ? 'light' : 'dark');
      }} else if (current === 'dark') {{
        applyTheme('light');
      }} else {{
        applyTheme('dark');
      }}
    }}

    /* View Mode Switcher (Grid vs List) */
    function setViewMode(mode) {{
      currentViewMode = mode;
      localStorage.setItem('ds940-view-mode', mode);
      document.getElementById('view-toggle-grid').classList.toggle('active', mode === 'grid');
      document.getElementById('view-toggle-list').classList.toggle('active', mode === 'list');
      document.getElementById('finder-grid').style.display = mode === 'grid' ? 'grid' : 'none';
      document.getElementById('finder-list').style.display = mode === 'list' ? 'flex' : 'none';
    }}

    /* Quick Look Toggle */
    function toggleQuickLook() {{
      isQuickLookOpen = !isQuickLookOpen;
      const pane = document.getElementById('quick-look-pane');
      const body = document.getElementById('finder-body');
      const btn = document.getElementById('btn-toggle-quicklook');
      
      btn.classList.toggle('active', isQuickLookOpen);
      if (window.innerWidth <= 1100) {{
        pane.classList.toggle('mobile-open', isQuickLookOpen);
      }} else {{
        body.classList.toggle('quicklook-collapsed', !isQuickLookOpen);
        pane.style.display = isQuickLookOpen ? 'flex' : 'none';
      }}
    }}

    /* Category Semantic Color Helpers */
    function getCategoryClass(cat) {{
      if (!cat) return 'cat-inbox';
      const c = cat.toLowerCase();
      if (c.includes('legal') || c.includes('citation')) return 'cat-legal';
      if (c.includes('finance') || c.includes('bank')) return 'cat-finance';
      if (c.includes('auto') || c.includes('vehicle')) return 'cat-auto';
      if (c.includes('medical') || c.includes('health')) return 'cat-medical';
      if (c.includes('bill')) return 'cat-bills';
      if (c.includes('tax')) return 'cat-taxes';
      return 'cat-inbox';
    }}

    /* Document Title Parser */
    function parseDisplayTitle(filename) {{
      const base = filename.endsWith('.pdf') ? filename.slice(0, -4) : filename;
      const unfiledMatch = base.match(/^brother-([0-9]{{4}})([0-9]{{2}})([0-9]{{2}})-([0-9]{{2}})([0-9]{{2}})([0-9]{{2}})-[a-f0-9]+$/);
      if (unfiledMatch) {{
        const y = parseInt(unfiledMatch[1]);
        const m = parseInt(unfiledMatch[2]) - 1;
        const d = parseInt(unfiledMatch[3]);
        const hh = parseInt(unfiledMatch[4]);
        const mm = parseInt(unfiledMatch[5]);
        const dt = new Date(y, m, d, hh, mm);
        const mon = dt.toLocaleString('en-US', {{ month: 'short' }});
        const h12 = hh % 12 || 12;
        const ampm = hh >= 12 ? 'PM' : 'AM';
        const minStr = String(mm).padStart(2, '0');
        return `Unfiled Scan (${{mon}} ${{d}}, ${{h12}}:${{minStr}} ${{ampm}})`;
      }}
      return base.replace(/^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}_/, '').replace(/_/g, ' · ').replace(/-/g, ' ');
    }}

    /* Filtering */
    function getFilteredScans() {{
      const q = (document.getElementById('search-box').value || '').trim().toLowerCase();
      return allScans.filter(doc => {{
        if (currentFolder === 'Inbox' && !doc.is_unfiled) return false;
        if (currentFolder !== 'all' && currentFolder !== 'Inbox' && doc.folder !== currentFolder) return false;
        if (!q) return true;
        const matchTitle = (doc.filename || '').toLowerCase().includes(q);
        const matchCat = (doc.category || '').toLowerCase().includes(q);
        const matchDocDate = (doc.doc_date || '').toLowerCase().includes(q);
        const matchScanDate = (doc.scan_date || '').toLowerCase().includes(q);
        return matchTitle || matchCat || matchDocDate || matchScanDate;
      }});
    }}

    function selectFolder(folder, el) {{
      currentFolder = folder;
      document.querySelectorAll('.sidebar-item').forEach(i => i.classList.remove('active'));
      if (el) el.classList.add('active');
      document.getElementById('crumb-folder').textContent = folder === 'all' ? 'Recent Scans' : folder;
      renderScans();
    }}

    function onSearchInput() {{
      renderScans();
    }}

    function renderScans() {{
      const filtered = getFilteredScans();
      const grid = document.getElementById('finder-grid');
      const listBody = document.getElementById('finder-list-body');
      const countEl = document.getElementById('scan-count');
      const statusbarSummary = document.getElementById('statusbar-summary');

      countEl.textContent = `${{filtered.length}} item${{filtered.length === 1 ? '' : 's'}}`;
      
      const totalBytes = filtered.reduce((acc, x) => acc + (x.size_bytes || 0), 0);
      const totalMb = (totalBytes / (1024 * 1024)).toFixed(1);
      statusbarSummary.textContent = `${{filtered.length}} items, ${{totalMb}} MB`;

      if (filtered.length === 0) {{
        grid.innerHTML = '<div style="grid-column: 1/-1; padding: 3rem; text-align: center; color: var(--text-dim); font-size: 0.85rem;">No documents found in this folder.</div>';
        listBody.innerHTML = '<div style="padding: 2rem; text-align: center; color: var(--text-dim);">No documents found.</div>';
        return;
      }}

      // Render Grid
      let gridHtml = '';
      let listHtml = '';

      filtered.forEach((doc, idx) => {{
        const title = parseDisplayTitle(doc.filename);
        const isSelected = activeDoc && (activeDoc.relative_path === doc.relative_path);
        const kb = doc.size_kb ? `${{doc.size_kb}} KB` : '';
        const date = doc.doc_date || doc.scan_date || '';

        gridHtml += `
          <div class="finder-icon-card ${{isSelected ? 'selected' : ''}}"
               tabindex="0"
               onclick="selectDocument(allScans.find(x => x.relative_path === '${{encodeURIComponent(doc.relative_path)}}'), ${{idx}})"
               ondblclick="openDocumentExternal('${{encodeURIComponent(doc.relative_path)}}')">
            <div class="finder-doc-icon">
              <img class="doc-thumbnail-img"
                   src="/thumbs/${{encodeURIComponent(doc.relative_path)}}"
                   alt="${{doc.filename}}"
                   loading="lazy"
                   onerror="this.style.display='none'; this.nextElementSibling.style.display='block';">
              <div class="doc-fallback-wrap" style="display:none;">
                <svg class="doc-page-svg" viewBox="0 0 56 68" fill="none" xmlns="http://www.w3.org/2000/svg">
                  <path d="M0 4C0 1.79086 1.79086 0 4 0H38L56 18V64C56 66.2091 54.2091 68 52 68H4C1.79086 68 0 66.2091 0 64V4Z" fill="var(--bg-surface)"/>
                  <path d="M0 4C0 1.79086 1.79086 0 4 0H38L56 18V64C56 66.2091 54.2091 68 52 68H4C1.79086 68 0 66.2091 0 64V4Z" stroke="var(--border)" stroke-width="1.5"/>
                  <path d="M38 0V14C38 16.2091 39.7909 18 42 18H56" fill="var(--bg-subtle)" stroke="var(--border)" stroke-width="1.5"/>
                  <line x1="10" y1="26" x2="46" y2="26" stroke="var(--border)" stroke-width="1.5" stroke-linecap="round"/>
                  <line x1="10" y1="34" x2="46" y2="34" stroke="var(--border)" stroke-width="1.5" stroke-linecap="round"/>
                  <line x1="10" y1="42" x2="34" y2="42" stroke="var(--border)" stroke-width="1.5" stroke-linecap="round"/>
                </svg>
                <span class="doc-page-badge">PDF</span>
              </div>
            </div>
            <div class="doc-card-title" title="${{doc.filename}}">${{title}}</div>
            <div class="doc-card-meta">${{date}} · ${{kb}}</div>
          </div>
        `;

        listHtml += `
          <div class="finder-list-row ${{isSelected ? 'selected' : ''}}"
               onclick="selectDocument(allScans.find(x => x.relative_path === '${{encodeURIComponent(doc.relative_path)}}'), ${{idx}})"
               ondblclick="openDocumentExternal('${{encodeURIComponent(doc.relative_path)}}')">
            <div class="list-name-col">
              <img class="list-thumb-img" src="/thumbs/${{encodeURIComponent(doc.relative_path)}}" alt="" loading="lazy" onerror="this.style.display='none';">
              <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${{title}}</span>
            </div>
            <div class="doc-card-meta">${{doc.scan_date || '--'}}</div>
            <div class="doc-card-meta">${{doc.doc_date || '--'}}</div>
            <div class="doc-card-meta">${{kb}}</div>
            <div><span class="category-tag ${{getCategoryClass(doc.category)}}">${{doc.category || 'Inbox'}}</span></div>
            <button class="btn-copy" onclick="event.stopPropagation(); triggerDeleteModal('${{encodeURIComponent(doc.relative_path)}}', '${{doc.filename}}')">✕</button>
          </div>
        `;
      }});

      grid.innerHTML = gridHtml;
      listBody.innerHTML = listHtml;
    }}

    function selectDocument(doc, index) {{
      activeDoc = doc;
      selectedIndex = index;

      const pane = document.getElementById('quick-look-pane');
      const filename = document.getElementById('inspector-filename');
      const metaCat = document.getElementById('meta-category');
      const metaDocDate = document.getElementById('meta-doc-date');
      const metaScanDate = document.getElementById('meta-scan-date');
      const metaSize = document.getElementById('meta-size');
      const btnDownload = document.getElementById('btn-download');
      const btnOpen = document.getElementById('btn-open-external');
      const btnDelete = document.getElementById('btn-delete-doc');
      const frame = document.getElementById('viewer-frame');
      const zero = document.getElementById('inspector-zero');

      if (!doc) {{
        filename.textContent = 'Select a document';
        metaCat.style.display = 'none';
        metaDocDate.style.display = 'none';
        metaScanDate.style.display = 'none';
        metaSize.style.display = 'none';
        btnDownload.style.display = 'none';
        btnOpen.style.display = 'none';
        btnDelete.style.display = 'none';
        frame.style.display = 'none';
        frame.src = 'about:blank';
        zero.style.display = 'flex';
        return;
      }}

      filename.textContent = parseDisplayTitle(doc.filename);
      filename.title = doc.filename;

      metaCat.textContent = doc.category || 'Inbox';
      metaCat.className = 'category-tag ' + getCategoryClass(doc.category);
      metaCat.style.display = 'inline-flex';

      if (doc.doc_date) {{
        metaDocDate.textContent = 'Doc Date: ' + doc.doc_date;
        metaDocDate.style.display = 'inline-flex';
      }} else {{
        metaDocDate.style.display = 'none';
      }}

      if (doc.scan_date) {{
        metaScanDate.textContent = 'Scanned: ' + doc.scan_date;
        metaScanDate.style.display = 'inline-flex';
      }} else {{
        metaScanDate.style.display = 'none';
      }}

      metaSize.textContent = (doc.size_kb || 0) + ' KB';
      metaSize.style.display = 'inline-flex';

      const fileUrl = '/files/' + encodeURIComponent(doc.relative_path);
      btnDownload.href = fileUrl;
      btnDownload.download = doc.filename;
      btnDownload.style.display = 'inline-flex';

      btnOpen.href = fileUrl;
      btnOpen.style.display = 'inline-flex';
      btnDelete.style.display = 'inline-flex';

      frame.src = fileUrl + '#toolbar=0&navpanes=0';
      frame.style.display = 'block';
      zero.style.display = 'none';

      if (!isQuickLookOpen) {{
        toggleQuickLook();
      }}

      // Update selection styling in grid and list
      document.querySelectorAll('.finder-icon-card').forEach((el, i) => el.classList.toggle('selected', i === index));
      document.querySelectorAll('.finder-list-row').forEach((el, i) => el.classList.toggle('selected', i === index));
    }}

    function openDocumentExternal(relPath) {{
      window.open('/files/' + relPath, '_blank');
    }}

    function triggerDeleteModal(relPath, fname) {{
      pendingDelete = {{ relPath: decodeURIComponent(relPath), filename: fname }};
      document.getElementById('modal-filename').textContent = fname;
      document.getElementById('confirm-modal').style.display = 'flex';
    }}

    function closeConfirmModal() {{
      document.getElementById('confirm-modal').style.display = 'none';
      pendingDelete = null;
    }}

    function deleteActiveDoc() {{
      if (!activeDoc) return;
      triggerDeleteModal(encodeURIComponent(activeDoc.relative_path), activeDoc.filename);
    }}

    async function executePendingDelete() {{
      if (!pendingDelete) return;
      const {{ relPath, filename }} = pendingDelete;
      closeConfirmModal();

      try {{
        const res = await fetch('/api/delete', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ path: relPath }})
        }});
        const data = await res.json();
        if (data.status === 'ok') {{
          showToast('Deleted ' + filename);
          if (activeDoc && (activeDoc.relative_path === relPath || activeDoc.filename === filename)) {{
            activeDoc = null;
            selectedIndex = -1;
            selectDocument(null, -1);
          }}
          await loadScans();
          await loadFolders();
        }} else {{
          showToast('Failed to delete: ' + (data.message || 'Error'));
        }}
      }} catch (e) {{
        showToast('Delete error: ' + e);
      }}
    }}

    async function sessionAction(action) {{
      try {{
        const res = await fetch(`/api/session/${{action}}`, {{ method: 'POST' }});
        const data = await res.json();
        if (data.status === 'ok') {{
          if (action === 'wake') {{
            showToast('Scanning session activated (5m)');
          }} else if (action === 'extend') {{
            showToast('Session extended (+5m)');
          }} else if (action === 'pause') {{
            showToast('Scanner set to sleep');
          }}
          await updateStatus();
        }} else {{
          showToast('Session update failed: ' + (data.message || 'Error'));
        }}
      }} catch (e) {{
        showToast('Session action error: ' + e);
      }}
    }}

    async function triggerSortScans() {{
      const btn = document.getElementById('btn-trigger-sort');
      btn.disabled = true;
      showToast('⚡ Organizing unfiled scans with Gemini AI...');
      try {{
        const res = await fetch('/api/sort', {{ method: 'POST' }});
        const data = await res.json();
        showToast(data.message || 'Organize complete');
        await loadScans();
        await loadFolders();
      }} catch (e) {{
        showToast('Organize error: ' + e);
      }} finally {{
        btn.disabled = false;
      }}
    }}

    let pollTimer = null;
    let currentPollInterval = 2500;
    let lastFiledDestination = null;

    function setPollingInterval(intervalMs) {{
      if (intervalMs === currentPollInterval && pollTimer !== null) return;
      currentPollInterval = intervalMs;
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = setInterval(updateStatus, currentPollInterval);
    }}

    async function updateStatus() {{
      try {{
        const res = await fetch('/api/status');
        const data = await res.json();
        
        const diagState = document.getElementById('diag-state');
        
        const strip = document.getElementById('live-status-strip');
        const stripTitle = document.getElementById('strip-title');
        const stripBadge = document.getElementById('strip-badge');
        const countdownWrap = document.getElementById('strip-countdown-wrap');
        const countdownNum = document.getElementById('strip-countdown-num');
        const stripActions = document.getElementById('strip-actions');
        const progressBar = document.getElementById('strip-progress-bar');
        const alertBanner = document.getElementById('hardware-alert-banner');

        const state = data.state || 'unknown';
        const scannerIp = data.scanner_ip || 'network';

        const diagScanner = document.getElementById('diag-scanner');
        if (diagScanner) {{
          diagScanner.textContent = `${{scannerIp}}:8080 ↗`;
          diagScanner.href = `http://${{scannerIp}}:8080/`;
        }}

        progressBar.style.width = '0%';
        countdownWrap.style.display = 'none';
        if (stripActions) stripActions.innerHTML = '';
        if (alertBanner) alertBanner.style.display = 'none';

        let isActiveState = false;

        if (state === 'waiting_for_paper') {{
          if (dot) dot.classList.add('online');
          if (label) label.textContent = 'Ready for paper';
          if (diagState) diagState.textContent = 'Ready (Idle)';

          stripTitle.textContent = 'Ready for paper';
          stripBadge.textContent = 'Duplex 300 DPI';

          const idleRem = data.idle_remaining;
          if (idleRem !== undefined && idleRem !== null && idleRem > 0) {{
            const m = Math.floor(idleRem / 60);
            const s = Math.floor(idleRem % 60);
            const timeStr = m > 0 ? `${{m}}m ${{s}}s` : `${{s}}s`;
            if (stripDetail) stripDetail.textContent = `Brother DS-940DW ready at ${{scannerIp}} · Sleeping in ${{timeStr}} to save battery`;
            if (stripActions) {{
              stripActions.innerHTML = `<span class="strip-sleep-pill" title="Battery saver countdown">🌙 ${{timeStr}}</span><div class="strip-btn-group"><button class="btn-strip" onclick="sessionAction(\\'extend\\')">+5m</button><button class="btn-strip" onclick="sessionAction(\\'pause\\')">Sleep Now</button></div>`;
            }}
          }} else {{
            if (stripDetail) stripDetail.textContent = `Brother DS-940DW ready at ${{scannerIp}} · Insert document into feeder to scan`;
            if (stripActions) stripActions.innerHTML = '';
          }}
        }} else if (state === 'battery_saver_dormant') {{
          if (dot) dot.classList.add('offline');
          if (label) label.textContent = 'Standby (Sleeping)';
          if (diagState) diagState.textContent = 'Battery Saver Standby';

          stripTitle.textContent = 'Battery Saver Standby';
          stripBadge.textContent = 'Sleeping';
          if (stripDetail) stripDetail.textContent = 'Scanner is sleeping to conserve battery · Press Power on scanner or click Wake to scan';
          if (stripActions) {{
            stripActions.innerHTML = '<button class="btn-strip btn-strip-primary" onclick="sessionAction(\\'wake\\')">Wake Session</button>';
          }}
        }} else if (state === 'scanning') {{
          isActiveState = true;
          const sheet = data.sheet || 1;
          if (dot) dot.classList.add('active');
          if (label) label.textContent = `Scanning Sheet ${{sheet}}...`;
          if (diagState) diagState.textContent = `Scanning Duplex (Sheet ${{sheet}})`;

          stripTitle.textContent = `Scanning Sheet ${{sheet}}...`;
          stripBadge.textContent = 'Duplex 300 DPI';
          if (stripDetail) stripDetail.textContent = 'Drawing paper through feed rollers · Capturing front and back pages in 24-bit color';
        }} else if (state === 'waiting_for_next_sheet') {{
          isActiveState = true;
          const sheet = data.sheet || 1;
          const pages = data.pages_so_far || (sheet * 2);
          const remaining = (data.timeout_remaining !== undefined && data.timeout_remaining !== null) ? data.timeout_remaining : 8.0;

          if (dot) dot.classList.add('active');
          if (label) label.textContent = `Batch (${{remaining}}s)`;
          if (diagState) diagState.textContent = `Collation (${{remaining}}s)`;

          stripTitle.textContent = `Insert Sheet ${{sheet + 1}} to Append`;
          stripBadge.textContent = 'Batch Window';
          if (stripDetail) stripDetail.textContent = `Sheet ${{sheet}} captured (${{pages}} pages total) · Insert next sheet within ${{remaining}}s to append to this document`;

          countdownWrap.style.display = 'flex';
          countdownNum.textContent = `${{remaining}}s`;
          const pct = Math.max(0, Math.min(100, (remaining / 8.0) * 100));
          progressBar.style.width = `${{pct}}%`;
        }} else if (state === 'assembling_pdf') {{
          isActiveState = true;
          if (dot) dot.classList.add('active');
          if (label) label.textContent = 'Compiling PDF...';
          if (diagState) diagState.textContent = 'Compiling PDF';

          stripTitle.textContent = 'Compiling PDF Document...';
          stripBadge.textContent = 'Processing';
          if (stripDetail) stripDetail.textContent = 'Pruning blank sides with ink detection and assembling searchable PDF pages';
        }} else if (state === 'filing_document') {{
          isActiveState = true;
          if (dot) dot.classList.add('active');
          if (label) label.textContent = 'Filing Document...';
          if (diagState) diagState.textContent = 'Filing Document';

          stripTitle.textContent = 'Filing Document...';
          stripBadge.textContent = 'Vision & Tags';
          if (stripDetail) stripDetail.textContent = 'Running Apple Vision OCR, AI categorization, and writing native macOS Finder tags';
        }} else if (state === 'filed') {{
          isActiveState = true;
          const dest = data.destination || 'Document.pdf';
          const filename = dest.split('/').pop();
          const cat = data.category || 'Inbox';
          const pages = data.pages ? `${{data.pages}} page${{data.pages > 1 ? 's' : ''}}` : '';

          if (dot) dot.classList.add('online');
          if (label) label.textContent = `Filed: ${{filename}}`;
          if (diagState) diagState.textContent = `Filed (${{filename}})`;

          stripTitle.textContent = `Filed: ${{filename}}`;
          stripBadge.textContent = cat;
          if (stripDetail) stripDetail.textContent = `Organized into ${{cat}}${{pages ? ' · ' + pages : ''}} · Ready for next document`;

          if (lastFiledDestination !== dest) {{
            lastFiledDestination = dest;
            loadScans();
            loadFolders();
          }}
        }} else if (state === 'scanner_offline') {{
          if (dot) dot.classList.add('offline');
          if (label) label.textContent = 'Standby (Sleep)';
          if (diagState) diagState.textContent = 'Standby (Sleep)';

          stripTitle.textContent = 'Standby (Sleeping)';
          stripBadge.textContent = 'Wi-Fi Sleep';
          if (stripDetail) stripDetail.textContent = `Scanner is sleeping at ${{scannerIp}} · Press Power on scanner or click Wake to scan`;
          if (stripActions) {{
            stripActions.innerHTML = '<button class="btn-strip btn-strip-primary" onclick="sessionAction(\\'wake\\')">Wake Session</button>';
          }}
        }} else if (state === 'paper_blocked' || state === 'paper_jammed') {{
          if (dot) dot.classList.add('offline');
          if (label) label.textContent = 'Feeder Jammed';
          if (diagState) diagState.textContent = 'Paper Jammed / Feed Error';

          stripTitle.textContent = 'Paper Jam / Feed Error';
          stripBadge.textContent = 'Clear Paper & Press Power';
          if (stripDetail) stripDetail.textContent = `Feeder jam at ${{scannerIp}} · Follow recovery instructions above`;
          if (stripActions) {{
            stripActions.innerHTML = '<button class="btn-strip btn-strip-primary" onclick="refreshAll()">Check Hardware</button>';
          }}
          if (alertBanner) {{
            alertBanner.style.display = 'flex';
          }}
        }} else {{
          if (dot) dot.classList.add('offline');
          const cleanState = state.replace(/_/g, ' ');
          if (label) label.textContent = cleanState;
          if (diagState) diagState.textContent = state;

          stripTitle.textContent = cleanState;
          stripBadge.textContent = 'Status';
          if (stripDetail) stripDetail.textContent = `Current hardware state: ${{state}}`;
        }}

        setPollingInterval(isActiveState ? 750 : 2500);
      }} catch (e) {{
        console.error('updateStatus error:', e);
      }}
    }}

    async function loadFolders() {{
      try {{
        const res = await fetch('/api/folders');
        const folders = await res.json();
        const container = document.getElementById('sidebar-folders');
        let html = '';

        let inboxCount = 0;
        let totalCount = 0;

        folders.forEach(f => {{
          totalCount += f.count;
          if (f.name === 'Inbox') {{
            inboxCount = f.count;
            return;
          }}
          const isActive = currentFolder === f.name;
          html += `
            <a class="sidebar-item ${{isActive ? 'active' : ''}}" data-folder="${{f.name}}" onclick="selectFolder('${{f.name}}', this)">
              <span class="sidebar-item-label">
                <svg class="sidebar-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
                </svg>
                <span>${{f.name}}</span>
              </span>
              <span class="sidebar-count">${{f.count}}</span>
            </a>
          `;
        }});

        container.innerHTML = html;
        document.getElementById('sidebar-count-inbox').textContent = inboxCount;
        document.getElementById('sidebar-count-all').textContent = totalCount;
      }} catch (e) {{
        console.error('loadFolders error:', e);
      }}
    }}

    async function loadScans() {{
      try {{
        const res = await fetch('/api/scans');
        allScans = await res.json();
        renderScans();
        if (selectedIndex === -1 && allScans.length > 0) {{
          selectDocument(allScans[0], 0);
        }}
      }} catch (e) {{
        console.error(e);
      }}
    }}

    function refreshAll() {{
      updateStatus();
      loadScans();
      loadFolders();
      showToast('Finder archive refreshed');
    }}

    /* Native Keyboard Listeners: Spacebar for Quick Look, Arrows for Grid Navigation */
    document.addEventListener('keydown', e => {{
      const isInput = document.activeElement.tagName === 'INPUT';
      const modal = document.getElementById('confirm-modal');
      const isModalOpen = modal && modal.style.display !== 'none';

      if (e.key === 'Escape') {{
        if (isModalOpen) {{
          closeConfirmModal();
          return;
        }}
        if (isInput) {{
          document.activeElement.blur();
        }}
      }} else if (e.key === 'Enter' && isModalOpen) {{
        e.preventDefault();
        executePendingDelete();
      }} else if (isModalOpen) {{
        return;
      }} else if (e.key === '/' && !isInput) {{
        e.preventDefault();
        document.getElementById('search-box').focus();
      }} else if ((e.key === ' ' || e.code === 'Space') && !isInput) {{
        e.preventDefault();
        toggleQuickLook();
      }} else if ((e.key === 'ArrowDown' || e.key === 'ArrowUp' || e.key === 'ArrowRight' || e.key === 'ArrowLeft') && !isInput) {{
        e.preventDefault();
        const filtered = getFilteredScans();
        if (!filtered.length) return;
        if (e.key === 'ArrowDown' || e.key === 'ArrowRight') {{
          selectedIndex = Math.min(filtered.length - 1, selectedIndex + 1);
        }} else {{
          selectedIndex = Math.max(0, selectedIndex - 1);
        }}
        selectDocument(filtered[selectedIndex], selectedIndex);
      }}
    }});

    initTheme();
    setViewMode(currentViewMode);
    updateStatus();
    loadFolders();
    loadScans();
    setPollingInterval(2500);
    setInterval(loadScans, 10000);
    setInterval(loadFolders, 10000);
  </script>
</body>
</html>"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_web_server(settings: Settings, stop_event: threading.Event | None = None, session=None):
    class CustomServer(ThreadingHTTPServer):
        allow_reuse_address = True

    DashboardHandler.settings = settings
    DashboardHandler.session = session
    DashboardHandler.start_time = time.time()

    server = CustomServer((settings.web_bind_ip, settings.web_port), DashboardHandler)
    server.timeout = 0.5

    def serve_loop():
        LOG.info("Dashboard web server running at http://%s:%d", settings.web_bind_ip, settings.web_port)
        while stop_event is None or not stop_event.is_set():
            server.handle_request()
        server.server_close()

    thread = threading.Thread(target=serve_loop, daemon=True, name="web-dashboard")
    thread.start()
    return server, thread
