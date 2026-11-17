"""Lightweight monitoring and management web dashboard for DS-940DW scanner."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
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

                    files.append({
                        "filename": fname,
                        "relative_path": str(rel_path),
                        "category": category,
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
        if parsed.path in ("/", "/index.html", "/api/status", "/api/scans", "/api/categories") or parsed.path.startswith("/files/"):
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
        elif path == "/api/categories":
            ledger = TaxonomyLedger(self.settings.output)
            self.send_json(ledger.get_active_categories())
        elif path.startswith("/files/"):
            self.serve_file(path[7:])
        else:
            self.send_error(404, "Not Found")

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
      --bg-page: #f6f7f9;
      --bg-surface: #ffffff;
      --bg-subtle: #f0f2f5;
      --bg-hover: #e8ecf1;
      --bg-selected: #e2e8f0;
      --border: #dbe0e7;
      --border-subtle: #ebedf2;
      --border-focus: #2563eb;
      --text: #191c21;
      --text-muted: #5e6674;
      --text-dim: #848f9f;
      --accent: #1d4ed8;
      --accent-hover: #1e40af;
      --accent-soft: rgba(29, 78, 216, 0.08);
      --status-ready: #16a34a;
      --status-active: #d97706;
      --status-standby: #64748b;
      --status-danger: #dc2626;
      --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.04);
      --shadow-md: 0 4px 14px rgba(0, 0, 0, 0.08);
      --shadow-lg: 0 10px 25px rgba(0, 0, 0, 0.12);
      --ease: cubic-bezier(0.16, 1, 0.3, 1);
      --ease-out: cubic-bezier(0.23, 1, 0.32, 1);
      --ease-press: cubic-bezier(0.4, 0, 0.2, 1);
    }}

    [data-theme="dark"] {{
      --bg-page: #0f1115;
      --bg-surface: #171920;
      --bg-subtle: #1f232d;
      --bg-hover: #262c38;
      --bg-selected: #243147;
      --border: #2c3342;
      --border-subtle: #202531;
      --border-focus: #3b82f6;
      --text: #f0f2f5;
      --text-muted: #9aa1ad;
      --text-dim: #656d79;
      --accent: #60a5fa;
      --accent-hover: #3b82f6;
      --accent-soft: rgba(96, 165, 250, 0.12);
      --status-ready: #22c55e;
      --status-active: #f59e0b;
      --status-standby: #94a3b8;
      --status-danger: #ef4444;
      --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.25);
      --shadow-md: 0 4px 14px rgba(0, 0, 0, 0.4);
      --shadow-lg: 0 10px 25px rgba(0, 0, 0, 0.5);
    }}

    @media (prefers-color-scheme: dark) {{
      :root:not([data-theme="light"]) {{
        --bg-page: #0f1115;
        --bg-surface: #171920;
        --bg-subtle: #1f232d;
        --bg-hover: #262c38;
        --bg-selected: #243147;
        --border: #2c3342;
        --border-subtle: #202531;
        --border-focus: #3b82f6;
        --text: #f0f2f5;
        --text-muted: #9aa1ad;
        --text-dim: #656d79;
        --accent: #60a5fa;
        --accent-hover: #3b82f6;
        --accent-soft: rgba(96, 165, 250, 0.12);
        --status-ready: #22c55e;
        --status-active: #f59e0b;
        --status-standby: #94a3b8;
        --status-danger: #ef4444;
        --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.25);
        --shadow-md: 0 4px 14px rgba(0, 0, 0, 0.4);
        --shadow-lg: 0 10px 25px rgba(0, 0, 0, 0.5);
      }}
    }}

    @media (prefers-reduced-motion: reduce) {{
      *, ::before, ::after {{
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.01ms !important;
        scroll-behavior: auto !important;
      }}
    }}

    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    ::selection {{ background: var(--accent-soft); color: var(--accent); }}

    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      background: var(--bg-page);
      color: var(--text);
      line-height: 1.45;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      font-variant-numeric: tabular-nums;
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
    }}

    button, a, input {{
      font-family: inherit;
    }}

    button {{
      cursor: pointer;
      user-select: none;
    }}

    button:active,
    .btn-strip:active,
    .btn-icon:active,
    .btn-action:active,
    .btn-copy:active,
    .category-chip:active,
    .btn-row-action:active,
    .btn-alert-action:active {{
      transform: scale(0.96);
    }}

    h1, h2, h3, .brand-title, .strip-title, .ledger-title, .inspector-title, .zero-title {{
      text-wrap: balance;
    }}

    p, .strip-detail, .zero-desc, .doc-meta-row {{
      text-wrap: pretty;
    }}

    :focus-visible {{
      outline: 2px solid var(--border-focus);
      outline-offset: 2px;
    }}

    /* Unified Header & Hardware Status */
    header.app-header {{
      background: var(--bg-surface);
      border-bottom: 1px solid var(--border);
      position: sticky;
      top: 0;
      z-index: 100;
      display: flex;
      flex-direction: column;
      padding: 0;
      transition: border-bottom-color 180ms var(--ease), background-color 180ms var(--ease);
    }}

    .header-primary-row {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 0.6rem 1.25rem;
      gap: 0.75rem 1rem;
      background: var(--bg-surface);
      flex-wrap: wrap;
    }}

    .header-brand-status {{
      display: flex;
      align-items: center;
      gap: 0.75rem;
      flex-shrink: 0;
      min-width: 0;
    }}

    .brand-wrap {{
      display: flex;
      align-items: center;
      gap: 0.6rem;
      flex-shrink: 0;
    }}

    .brand-logo {{
      width: 22px;
      height: 22px;
      color: var(--text);
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
    }}

    .brand-title {{
      font-size: 0.95rem;
      font-weight: 600;
      letter-spacing: -0.015em;
      color: var(--text);
      white-space: nowrap;
      flex-shrink: 0;
    }}

    .header-divider {{
      width: 1px;
      height: 20px;
      background: var(--border);
      flex-shrink: 0;
    }}

    .header-status-indicator {{
      display: flex;
      align-items: center;
      gap: 0.55rem;
      flex-shrink: 0;
      min-width: 0;
    }}

    .strip-beacon {{
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--status-ready);
      flex-shrink: 0;
      transition: background-color 160ms ease, box-shadow 160ms ease;
    }}

    .status-dot {{
      display: none;
    }}

    .strip-title {{
      font-size: 0.9rem;
      font-weight: 600;
      letter-spacing: -0.015em;
      color: var(--text);
      line-height: 1.2;
      white-space: nowrap;
      flex-shrink: 0;
    }}

    .strip-badge {{
      font-size: 0.68rem;
      font-weight: 600;
      padding: 0.12rem 0.45rem;
      border-radius: 4px;
      background: var(--bg-subtle);
      border: 1px solid var(--border);
      color: var(--text-muted);
      letter-spacing: 0.02em;
      text-transform: uppercase;
      white-space: nowrap;
      flex-shrink: 0;
    }}

    .strip-sleep-pill {{
      font-size: 0.72rem;
      font-weight: 600;
      font-variant-numeric: tabular-nums;
      color: var(--text-muted);
      background: var(--bg-surface);
      border: 1px solid var(--border);
      padding: 0.22rem 0.55rem;
      border-radius: 4px;
      white-space: nowrap;
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      flex-shrink: 0;
    }}

    .header-session-actions {{
      display: flex;
      align-items: center;
      gap: 0.55rem;
      flex-shrink: 0;
      margin-left: auto;
    }}

    .header-global-actions {{
      display: flex;
      align-items: center;
      gap: 0.55rem;
      flex-shrink: 0;
    }}

    .strip-countdown-wrap {{
      display: none;
      align-items: center;
      gap: 0.35rem;
      background: rgba(217, 119, 6, 0.1);
      border: 1px solid rgba(217, 119, 6, 0.3);
      padding: 0.15rem 0.5rem;
      border-radius: 4px;
      font-size: 0.72rem;
      color: var(--status-active);
      flex-shrink: 0;
    }}

    .countdown-pulse-dot {{
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--status-active);
      animation: pulse 1s infinite;
    }}

    .countdown-label {{
      font-weight: 500;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      font-size: 0.65rem;
    }}

    @keyframes pulse {{
      0%, 100% {{ opacity: 1; }}
      50% {{ opacity: 0.3; }}
    }}

    .strip-countdown-num {{
      font-size: 0.88rem;
      font-weight: 600;
      font-variant-numeric: tabular-nums;
      color: var(--status-active);
    }}

    .strip-actions {{
      display: flex;
      align-items: center;
      gap: 0.45rem;
      flex-shrink: 0;
    }}

    .strip-btn-group {{
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      flex-shrink: 0;
    }}

    .btn-strip {{
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      font-size: 0.74rem;
      font-weight: 500;
      color: var(--text-muted);
      background: var(--bg-surface);
      border: 1px solid var(--border);
      padding: 0.24rem 0.6rem;
      border-radius: 5px;
      cursor: pointer;
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
      border-color: var(--accent-hover);
      color: #ffffff;
    }}

    .btn-strip-accent {{
      background: var(--accent-soft);
      border-color: rgba(37, 99, 235, 0.25);
      color: var(--accent);
      font-weight: 600;
    }}

    .btn-strip-accent:hover {{
      background: var(--accent);
      border-color: var(--accent);
      color: #ffffff;
    }}

    .sort-spinner {{
      display: inline-block;
      width: 11px;
      height: 11px;
      border: 2px solid currentColor;
      border-right-color: transparent;
      border-radius: 50%;
      animation: spin 0.6s linear infinite;
    }}

    @keyframes spin {{
      to {{ transform: rotate(360deg); }}
    }}

    .header-actions {{
      display: flex;
      align-items: center;
      gap: 0.4rem;
    }}

    .btn-icon {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 28px;
      height: 28px;
      border-radius: 5px;
      background: var(--bg-surface);
      border: 1px solid var(--border);
      color: var(--text-muted);
      transition: background-color 120ms var(--ease), border-color 120ms var(--ease), color 120ms var(--ease), transform 80ms var(--ease-press);
    }}

    .btn-icon:hover {{
      background: var(--bg-hover);
      color: var(--text);
      border-color: var(--border-focus);
    }}

    .btn-icon.active {{
      background: var(--bg-selected);
      border-color: var(--border-focus);
      color: var(--accent);
    }}

    /* Hardware Alert Banner */
    .alert-banner {{
      background: rgba(220, 38, 38, 0.08);
      border-bottom: 1px solid rgba(220, 38, 38, 0.25);
      animation: alertSlideDown 160ms var(--ease-out);
    }}

    [data-theme="dark"] .alert-banner {{
      background: rgba(239, 68, 68, 0.12);
      border-bottom-color: rgba(239, 68, 68, 0.3);
    }}

    @keyframes alertSlideDown {{
      from {{ transform: translateY(-8px); opacity: 0; }}
      to {{ transform: translateY(0); opacity: 1; }}
    }}

    .alert-container {{
      max-width: 1680px;
      margin: 0 auto;
      padding: 0.65rem 1.25rem;
      display: flex;
      align-items: center;
      gap: 1rem;
      flex-wrap: wrap;
    }}

    .alert-icon-wrap {{
      width: 32px;
      height: 32px;
      border-radius: 6px;
      background: rgba(220, 38, 38, 0.15);
      color: var(--status-danger);
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
    }}

    .alert-body {{
      flex: 1;
      min-width: 260px;
      display: flex;
      flex-direction: column;
      gap: 0.2rem;
    }}

    .alert-heading {{
      font-size: 0.86rem;
      font-weight: 600;
      color: var(--status-danger);
    }}

    .alert-steps {{
      font-size: 0.78rem;
      color: var(--text);
      display: flex;
      align-items: center;
      gap: 0.5rem;
      flex-wrap: wrap;
    }}

    .alert-step {{
      display: inline-flex;
      align-items: center;
      gap: 0.25rem;
    }}

    .alert-step-divider {{
      color: var(--text-dim);
      font-size: 0.72rem;
    }}

    .btn-alert-action {{
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      font-size: 0.76rem;
      font-weight: 600;
      background: var(--status-danger);
      border: 1px solid var(--status-danger);
      color: #ffffff;
      padding: 0.32rem 0.7rem;
      border-radius: 5px;
      cursor: pointer;
      transition: background-color 100ms var(--ease), transform 80ms var(--ease-press);
    }}

    .btn-alert-action:hover {{
      background: #b91c1c;
      border-color: #b91c1c;
    }}

    /* Collapsible Telemetry & Diagnostics Drawer */
    .telemetry-drawer {{
      border-top: 1px solid var(--border-subtle);
      background: var(--bg-subtle);
      animation: drawerSlideDown 140ms var(--ease-out);
    }}

    @keyframes drawerSlideDown {{
      from {{ opacity: 0; transform: translateY(-4px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}

    .telemetry-content {{
      max-width: 1680px;
      margin: 0 auto;
      padding: 0.85rem 1.25rem;
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
    }}

    .telemetry-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 0.75rem;
    }}

    .tele-card {{
      background: var(--bg-surface);
      border: 1px solid var(--border-subtle);
      border-radius: 6px;
      padding: 0.65rem 0.85rem;
      display: flex;
      flex-direction: column;
      gap: 0.35rem;
      min-width: 0;
      overflow: hidden;
      box-shadow: var(--shadow-sm);
    }}

    .tele-card-wide {{
      grid-column: span 2;
    }}

    @media (max-width: 768px) {{
      .tele-card-wide {{
        grid-column: span 1;
      }}
    }}

    .tele-card-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.5rem;
      min-width: 0;
    }}

    .tele-card-label {{
      font-size: 0.68rem;
      font-weight: 600;
      color: var(--text-dim);
      text-transform: uppercase;
      letter-spacing: 0.04em;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      min-width: 0;
    }}

    .tele-card-val {{
      font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
      font-size: 0.78rem;
      font-weight: 500;
      color: var(--text);
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      min-width: 0;
    }}

    .tele-endpoint-link {{
      color: var(--accent);
      text-decoration: none;
      transition: color 100ms var(--ease);
    }}

    .tele-endpoint-link:hover {{
      text-decoration: underline;
    }}

    .tele-path-val {{
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}

    .btn-tele-link {{
      display: inline-flex;
      align-items: center;
      gap: 0.25rem;
      font-size: 0.68rem;
      font-weight: 600;
      color: var(--accent);
      text-decoration: none;
      padding: 0.12rem 0.45rem;
      border-radius: 4px;
      background: var(--accent-soft);
      white-space: nowrap;
      flex-shrink: 0;
      transition: background-color 100ms var(--ease), color 100ms var(--ease);
    }}

    .btn-tele-link:hover {{
      background: var(--accent);
      color: #ffffff;
    }}

    .telemetry-footer {{
      padding-top: 0.45rem;
      border-top: 1px solid var(--border-subtle);
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}

    .strip-detail {{
      font-size: 0.74rem;
      color: var(--text-muted);
      line-height: 1.35;
    }}

    .btn-copy {{
      position: relative;
      background: var(--bg-surface);
      border: 1px solid var(--border);
      color: var(--text-muted);
      display: inline-flex;
      align-items: center;
      gap: 0.3rem;
      padding: 0.15rem 0.45rem;
      border-radius: 4px;
      cursor: pointer;
      font-size: 0.68rem;
      font-weight: 500;
      white-space: nowrap;
      flex-shrink: 0;
      transition: background-color 100ms var(--ease), border-color 100ms var(--ease), color 100ms var(--ease), transform 80ms var(--ease-press);
    }}

    .btn-copy:hover {{
      background: var(--bg-hover);
      color: var(--text);
      border-color: var(--border-focus);
    }}

    .strip-progress-bar {{
      position: absolute;
      bottom: 0;
      left: 0;
      height: 3px;
      width: 0%;
      background: var(--status-active);
      box-shadow: 0 0 6px rgba(217, 119, 6, 0.4);
      transition: width 0.7s linear;
    }}

    /* State modifier styles for header */
    .app-header.state-ready .strip-beacon {{ background: var(--status-ready); }}
    .app-header.state-scanning .strip-beacon {{ background: var(--accent); }}
    .app-header.state-collation .strip-beacon {{ background: var(--status-active); }}
    .app-header.state-processing .strip-beacon {{ background: #6366f1; }}
    .app-header.state-filed .strip-beacon {{ background: var(--status-ready); }}
    .app-header.state-warning .strip-beacon {{ background: var(--status-danger); }}
    .app-header.state-standby .strip-beacon {{ background: var(--status-standby); }}

    /* Main Workbench */
    main.workbench {{
      max-width: 1680px;
      width: 100%;
      margin: 0 auto;
      padding: 1rem 1.25rem 1.5rem;
      flex: 1;
      display: flex;
      flex-direction: column;
      gap: 1rem;
      min-height: 0;
    }}

    /* Master-Detail Split Grid */
    .split-layout {{
      display: grid;
      grid-template-columns: clamp(340px, 28vw, 420px) minmax(0, 1fr);
      gap: 1.25rem;
      flex: 1;
      min-height: calc(100vh - 100px);
    }}

    /* Left Pane: Ledger */
    .ledger-pane {{
      background: var(--bg-surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      box-shadow: var(--shadow-sm);
      min-width: 0;
    }}

    .ledger-header {{
      padding: 0.85rem 1rem;
      border-bottom: 1px solid var(--border);
      background: var(--bg-surface);
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
    }}

    .ledger-title-row {{
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}

    .ledger-title-wrap {{
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }}

    .ledger-title {{
      font-size: 0.92rem;
      font-weight: 600;
      color: var(--text);
    }}

    .ledger-count {{
      font-size: 0.72rem;
      font-weight: 500;
      color: var(--text-muted);
      background: var(--bg-subtle);
      border: 1px solid var(--border-subtle);
      padding: 0.1rem 0.45rem;
      border-radius: 4px;
    }}

    .ledger-shortcuts {{
      display: flex;
      align-items: center;
      gap: 0.3rem;
      font-size: 0.68rem;
      color: var(--text-dim);
    }}

    .ledger-shortcuts kbd {{
      background: var(--bg-subtle);
      border: 1px solid var(--border-subtle);
      padding: 0.05rem 0.25rem;
      border-radius: 3px;
      font-size: 0.62rem;
      color: var(--text-muted);
    }}

    .search-row {{
      position: relative;
      display: flex;
      align-items: center;
    }}

    .search-input {{
      width: 100%;
      background: var(--bg-page);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 0.45rem 3.4rem 0.45rem 2.1rem;
      font-size: 0.8rem;
      color: var(--text);
      transition: background-color 120ms var(--ease), border-color 120ms var(--ease), box-shadow 120ms var(--ease);
    }}

    .search-input:focus {{
      background: var(--bg-surface);
      border-color: var(--border-focus);
      box-shadow: 0 0 0 3px var(--accent-soft);
    }}

    .search-icon {{
      position: absolute;
      left: 0.65rem;
      width: 14px;
      height: 14px;
      color: var(--text-dim);
      pointer-events: none;
    }}

    .search-clear-btn {{
      position: absolute;
      right: 1.85rem;
      background: none;
      border: none;
      color: var(--text-dim);
      font-size: 0.75rem;
      cursor: pointer;
      padding: 0.15rem 0.35rem;
      line-height: 1;
      border-radius: 3px;
    }}

    .search-clear-btn:hover {{
      color: var(--text);
    }}

    .search-kbd {{
      position: absolute;
      right: 0.6rem;
      font-size: 0.64rem;
      color: var(--text-dim);
      background: var(--bg-surface);
      border: 1px solid var(--border);
      padding: 0.1rem 0.35rem;
      border-radius: 3px;
      pointer-events: none;
    }}

    /* Category Chips with Horizontal Scroll Gradient */
    .category-bar-wrap {{
      position: relative;
      overflow: hidden;
    }}

    .category-bar {{
      display: flex;
      gap: 0.35rem;
      overflow-x: auto;
      padding-bottom: 0.2rem;
      scrollbar-width: none;
      -webkit-overflow-scrolling: touch;
      mask-image: linear-gradient(to right, black 88%, transparent 100%);
      -webkit-mask-image: linear-gradient(to right, black 88%, transparent 100%);
    }}

    .category-bar::-webkit-scrollbar {{
      display: none;
    }}

    .category-chip {{
      font-size: 0.73rem;
      font-weight: 500;
      padding: 0.22rem 0.55rem;
      border-radius: 4px;
      background: transparent;
      border: 1px solid transparent;
      color: var(--text-muted);
      white-space: nowrap;
      transition: background-color 100ms var(--ease), border-color 100ms var(--ease), color 100ms var(--ease), transform 80ms var(--ease-press);
      flex-shrink: 0;
    }}

    .category-chip:hover {{
      background: var(--bg-subtle);
      color: var(--text);
    }}

    .category-chip.active {{
      background: var(--bg-subtle);
      border-color: var(--border);
      color: var(--text);
      font-weight: 600;
    }}

    /* Document Feed List */
    .doc-list {{
      flex: 1;
      overflow-y: auto;
    }}

    .doc-item {{
      padding: 0.75rem 0.95rem;
      border-bottom: 1px solid var(--border-subtle);
      display: flex;
      align-items: flex-start;
      gap: 0.65rem;
      cursor: pointer;
      background: var(--bg-surface);
      position: relative;
      transition: background-color 90ms var(--ease-out), transform 80ms var(--ease-press);
    }}

    .doc-item:hover {{
      background: var(--bg-hover);
    }}

    .doc-item:active {{
      transform: scale(0.985);
    }}

    .doc-item.selected {{
      background: var(--bg-selected);
      box-shadow: inset 3px 0 0 var(--accent);
    }}

    .doc-icon-wrap {{
      color: var(--text-muted);
      margin-top: 0.15rem;
      flex-shrink: 0;
    }}

    .doc-item.selected .doc-icon-wrap {{
      color: var(--accent);
    }}

    .doc-body {{
      flex: 1;
      min-width: 0;
      display: flex;
      flex-direction: column;
      gap: 0.3rem;
    }}

    .doc-title-row {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 0.5rem;
      min-width: 0;
    }}

    .doc-title {{
      font-size: 0.86rem;
      font-weight: 600;
      letter-spacing: -0.01em;
      color: var(--text);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      flex: 1;
      min-width: 0;
    }}

    .doc-size {{
      font-size: 0.7rem;
      font-weight: 500;
      color: var(--text-dim);
      font-variant-numeric: tabular-nums;
      flex-shrink: 0;
      white-space: nowrap;
    }}

    .doc-meta-row {{
      display: flex;
      align-items: center;
      gap: 0.45rem;
      flex-wrap: wrap;
      font-size: 0.72rem;
      color: var(--text-muted);
      min-width: 0;
    }}

    .meta-tag {{
      display: inline-flex;
      align-items: center;
      gap: 0.25rem;
      white-space: nowrap;
    }}

    /* Semantic Category Tag Badges */
    .category-tag {{
      font-size: 0.68rem;
      font-weight: 600;
      padding: 0.1rem 0.45rem;
      border-radius: 4px;
      white-space: nowrap;
      flex-shrink: 0;
      letter-spacing: 0.01em;
      transition: background-color 100ms var(--ease);
    }}

    .cat-inbox {{
      background: var(--bg-subtle);
      border: 1px solid var(--border-subtle);
      color: var(--text-muted);
    }}

    .cat-legal {{
      background: rgba(217, 119, 6, 0.1);
      border: 1px solid rgba(217, 119, 6, 0.25);
      color: #b45309;
    }}
    [data-theme="dark"] .cat-legal {{
      background: rgba(245, 158, 11, 0.12);
      border-color: rgba(245, 158, 11, 0.3);
      color: #fbbf24;
    }}

    .cat-finance {{
      background: rgba(16, 185, 129, 0.1);
      border: 1px solid rgba(16, 185, 129, 0.25);
      color: #047857;
    }}
    [data-theme="dark"] .cat-finance {{
      background: rgba(52, 211, 153, 0.12);
      border-color: rgba(52, 211, 153, 0.3);
      color: #34d399;
    }}

    .cat-auto {{
      background: rgba(139, 92, 246, 0.1);
      border: 1px solid rgba(139, 92, 246, 0.25);
      color: #6d28d9;
    }}
    [data-theme="dark"] .cat-auto {{
      background: rgba(167, 139, 250, 0.12);
      border-color: rgba(167, 139, 250, 0.3);
      color: #a78bfa;
    }}

    .cat-medical {{
      background: rgba(14, 165, 233, 0.1);
      border: 1px solid rgba(14, 165, 233, 0.25);
      color: #0369a1;
    }}
    [data-theme="dark"] .cat-medical {{
      background: rgba(56, 189, 248, 0.12);
      border-color: rgba(56, 189, 248, 0.3);
      color: #38bdf8;
    }}

    .cat-bills {{
      background: rgba(249, 115, 22, 0.1);
      border: 1px solid rgba(249, 115, 22, 0.25);
      color: #c2410c;
    }}
    [data-theme="dark"] .cat-bills {{
      background: rgba(251, 146, 60, 0.12);
      border-color: rgba(251, 146, 60, 0.3);
      color: #fb923c;
    }}

    .cat-taxes {{
      background: rgba(225, 29, 72, 0.1);
      border: 1px solid rgba(225, 29, 72, 0.25);
      color: #be123c;
    }}
    [data-theme="dark"] .cat-taxes {{
      background: rgba(251, 113, 133, 0.12);
      border-color: rgba(251, 113, 133, 0.3);
      color: #fb7185;
    }}

    .doc-actions {{
      margin-left: 0.25rem;
      flex-shrink: 0;
    }}

    .btn-row-action {{
      position: relative;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 26px;
      height: 26px;
      border: none;
      background: transparent;
      border-radius: 4px;
      color: var(--text-dim);
      flex-shrink: 0;
      transition: background-color 100ms var(--ease), color 100ms var(--ease), transform 80ms var(--ease-press);
    }}

    .btn-row-action::before {{
      content: "";
      position: absolute;
      inset: -4px;
    }}

    .btn-row-action:hover {{
      color: var(--status-danger);
      background: rgba(220, 38, 38, 0.08);
    }}

    /* Right Pane: Document Inspector */
    .inspector-pane {{
      background: var(--bg-surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      box-shadow: var(--shadow-sm);
      min-width: 0;
    }}

    .inspector-header {{
      padding: 0.65rem 1rem;
      border-bottom: 1px solid var(--border);
      background: var(--bg-surface);
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 0.75rem;
      min-width: 0;
    }}

    .inspector-info {{
      display: flex;
      flex-direction: column;
      gap: 0.25rem;
      min-width: 0;
      flex: 1;
    }}

    .inspector-title {{
      font-size: 0.92rem;
      font-weight: 600;
      color: var(--text);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}

    .inspector-meta {{
      display: flex;
      align-items: center;
      gap: 0.5rem;
      font-size: 0.73rem;
      color: var(--text-muted);
      flex-wrap: wrap;
    }}

    .inspector-actions {{
      display: flex;
      align-items: center;
      gap: 0.45rem;
      flex-shrink: 0;
    }}

    .btn-action {{
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      font-size: 0.76rem;
      font-weight: 500;
      color: var(--text-muted);
      background: var(--bg-surface);
      border: 1px solid var(--border);
      padding: 0.32rem 0.65rem;
      border-radius: 5px;
      text-decoration: none;
      transition: background-color 100ms var(--ease), border-color 100ms var(--ease), color 100ms var(--ease), transform 80ms var(--ease-press);
    }}

    .btn-action:hover {{
      background: var(--bg-hover);
      color: var(--text);
      border-color: var(--border-focus);
    }}

    .btn-action.btn-danger:hover {{
      color: var(--status-danger);
      border-color: var(--status-danger);
      background: rgba(220, 38, 38, 0.06);
    }}

    .viewer-frame {{
      flex: 1;
      width: 100%;
      height: 100%;
      min-height: 560px;
      border: none;
      background: var(--bg-subtle);
    }}

    /* Zero State */
    .zero-state {{
      flex: 1;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 3rem 1.5rem;
      text-align: center;
      color: var(--text-muted);
    }}

    .zero-icon {{
      width: 36px;
      height: 36px;
      color: var(--text-dim);
      margin-bottom: 0.85rem;
    }}

    .zero-title {{
      font-size: 0.9rem;
      font-weight: 600;
      color: var(--text);
      margin-bottom: 0.25rem;
    }}

    .zero-desc {{
      font-size: 0.78rem;
      color: var(--text-muted);
      max-width: 320px;
      line-height: 1.4;
    }}

    /* Toast */
    .toast {{
      position: fixed;
      bottom: 2.5rem;
      right: 2rem;
      background: var(--bg-surface);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 0.55rem 1rem;
      border-radius: 6px;
      font-size: 0.78rem;
      font-weight: 500;
      box-shadow: var(--shadow-md);
      transform: translateY(8px) scale(0.96);
      opacity: 0;
      pointer-events: none;
      transition: opacity 160ms var(--ease-out), transform 160ms var(--ease-out);
      z-index: 1000;
    }}

    .toast.show {{
      transform: translateY(0) scale(1);
      opacity: 1;
    }}

    /* Accessible Confirmation Modal */
    .modal-backdrop {{
      position: fixed;
      inset: 0;
      background: rgba(15, 17, 21, 0.55);
      backdrop-filter: blur(4px);
      -webkit-backdrop-filter: blur(4px);
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 1000;
      padding: 1rem;
      animation: fadeIn 150ms var(--ease-out);
    }}

    @keyframes fadeIn {{
      from {{ opacity: 0; }}
      to {{ opacity: 1; }}
    }}

    .modal-card {{
      background: var(--bg-surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      box-shadow: var(--shadow-lg);
      max-width: 440px;
      width: 100%;
      padding: 1.4rem;
      display: flex;
      gap: 1.1rem;
      align-items: flex-start;
      animation: modalSlideIn 180ms var(--ease-out);
    }}

    @keyframes modalSlideIn {{
      from {{ transform: scale(0.96) translateY(6px); opacity: 0; }}
      to {{ transform: scale(1) translateY(0); opacity: 1; }}
    }}

    .modal-icon-wrap {{
      width: 40px;
      height: 40px;
      border-radius: 50%;
      background: rgba(220, 38, 38, 0.1);
      color: var(--status-danger);
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
    }}

    .modal-content {{
      flex: 1;
      min-width: 0;
    }}

    .modal-title {{
      font-size: 1rem;
      font-weight: 600;
      color: var(--text);
      margin: 0 0 0.35rem;
    }}

    .modal-desc {{
      font-size: 0.8rem;
      color: var(--text-muted);
      line-height: 1.4;
      margin: 0 0 0.65rem;
    }}

    .modal-filename {{
      font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
      font-size: 0.74rem;
      color: var(--text);
      background: var(--bg-subtle);
      border: 1px solid var(--border-subtle);
      padding: 0.35rem 0.6rem;
      border-radius: 4px;
      margin-bottom: 1.1rem;
      word-break: break-all;
    }}

    .modal-actions {{
      display: flex;
      justify-content: flex-end;
      gap: 0.55rem;
    }}

    .btn-modal {{
      font-size: 0.78rem;
      font-weight: 500;
      padding: 0.38rem 0.85rem;
      border-radius: 5px;
      cursor: pointer;
      transition: background-color 100ms var(--ease), border-color 100ms var(--ease), transform 80ms var(--ease-press);
    }}

    .btn-modal-cancel {{
      background: var(--bg-surface);
      border: 1px solid var(--border);
      color: var(--text-muted);
    }}

    .btn-modal-cancel:hover {{
      background: var(--bg-hover);
      color: var(--text);
    }}

    .btn-modal-danger {{
      background: var(--status-danger);
      border: 1px solid var(--status-danger);
      color: #ffffff;
      font-weight: 600;
    }}

    .btn-modal-danger:hover {{
      background: #b91c1c;
      border-color: #b91c1c;
    }}

    /* Responsive Breakpoints & Progressive Disclosure */
    @media (max-width: 960px) {{
      .btn-row-action {{
        opacity: 1;
      }}
      .header-primary-row {{
        padding: 0.55rem 1rem 0.45rem;
        gap: 0.75rem;
      }}
      .brand-title {{
        display: none;
      }}
      .header-divider {{
        display: none;
      }}
      .split-layout {{
        grid-template-columns: 1fr;
      }}
      .inspector-pane {{
        position: fixed;
        top: 0;
        left: 0;
        right: 0;
        bottom: 0;
        z-index: 200;
        border-radius: 0;
        display: none;
      }}
      .inspector-pane.mobile-open {{
        display: flex;
      }}
      .btn-close-mobile {{
        display: inline-flex !important;
      }}
    }}

    /* Mobile Viewports (<680px) */
    @media (max-width: 680px) {{
      .header-primary-row {{
        flex-wrap: wrap;
        padding: 0.5rem 0.85rem 0.55rem;
        gap: 0.45rem 0.65rem;
      }}
      .header-brand-status {{
        order: 1;
        flex: 1 1 auto;
        min-width: 0;
      }}
      .header-global-actions {{
        order: 2;
        flex-shrink: 0;
        margin-left: auto;
      }}
      .header-session-actions {{
        display: none;
      }}
      .header-session-actions.has-actions {{
        display: flex;
        order: 3;
        flex: 1 1 100%;
        margin-left: 0;
        justify-content: space-between;
        padding-top: 0.45rem;
        border-top: 1px solid var(--border-subtle);
      }}
      .strip-actions {{
        width: 100%;
        justify-content: space-between;
      }}
      #btn-trigger-sort span {{
        display: none;
      }}
    }}

    @media (max-width: 440px) {{
      .strip-badge {{
        display: none;
      }}
      .btn-strip {{
        padding: 0.28rem 0.65rem;
      }}
    }}

    @media (min-width: 961px) {{
      .btn-close-mobile {{
        display: none !important;
      }}
      .btn-row-action {{
        opacity: 0;
      }}
      .doc-item:hover .btn-row-action,
      .doc-item:focus-within .btn-row-action {{
        opacity: 1;
      }}
    }}
  </style>
</head>
<body>
  <header class="app-header state-ready" id="live-status-strip" role="region" aria-live="polite">
    <!-- Row 1: Primary Brand, Hardware State & Immediate Actions -->
    <div class="header-primary-row">
      <div class="header-brand-status">
        <div class="brand-wrap">
          <div class="brand-logo" aria-hidden="true">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <rect x="2" y="4" width="20" height="16" rx="2"/>
              <path d="M6 12h12"/>
              <path d="M6 8h12"/>
            </svg>
          </div>
          <h1 class="brand-title">Brother DS-940DW</h1>
        </div>

        <div class="header-divider" aria-hidden="true"></div>

        <div class="header-status-indicator">
          <span id="strip-beacon" class="strip-beacon"></span>
          <span id="status-dot" style="display:none;"></span>
          <span id="status-label" style="display:none;"></span>
          <h2 id="strip-title" class="strip-title">Ready for paper</h2>
          <span id="strip-badge" class="strip-badge">Duplex 300 DPI</span>
        </div>
      </div>

      <div class="header-session-actions" id="header-session-actions">
        <div id="strip-countdown-wrap" class="strip-countdown-wrap">
          <span class="countdown-pulse-dot"></span>
          <span class="countdown-label">Batch:</span>
          <span id="strip-countdown-num" class="strip-countdown-num">8.0s</span>
        </div>
        <div id="strip-actions" class="strip-actions"></div>
      </div>

      <div class="header-global-actions">
        <button class="btn-strip btn-strip-accent" id="btn-trigger-sort" onclick="triggerSortScans()" title="Sort and organize unfiled scans with Gemini AI">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>
          </svg>
          <span>Organize</span>
        </button>

        <div class="header-divider" aria-hidden="true"></div>

        <div class="header-actions">
          <button class="btn-icon" id="tele-toggle-btn" onclick="toggleTelemetry()" title="Toggle System Diagnostics HUD (Scanner IP, RAM, Uptime, Archive)" aria-label="System Diagnostics">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="10"/>
              <line x1="12" y1="16" x2="12" y2="12"/>
              <line x1="12" y1="8" x2="12.01" y2="8"/>
            </svg>
          </button>
          <button class="btn-icon" id="theme-toggle" onclick="toggleTheme()" title="Switch color theme" aria-label="Switch color theme">
            <svg id="theme-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
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
          <button class="btn-icon" onclick="refreshAll()" title="Refresh documents and status" aria-label="Refresh">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="23 4 23 10 17 10"/>
              <polyline points="1 20 1 14 7 14"/>
              <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
            </svg>
          </button>
        </div>
      </div>
    </div>

    <!-- Collapsible Telemetry & Diagnostics Drawer -->
    <div id="telemetry-drawer" class="telemetry-drawer" style="display:none;">
      <div class="telemetry-content">
        <div class="telemetry-grid">
          <div class="tele-card">
            <div class="tele-card-header">
              <span class="tele-card-label">Scanner Network Endpoint</span>
              <a id="diag-scanner-link" href="http://{self.settings.scanner_ip}:8080/" target="_blank" rel="noopener" class="btn-tele-link" title="Open Brother Web Admin">
                <span>Web Admin ↗</span>
              </a>
            </div>
            <a id="diag-scanner" class="tele-card-val tele-endpoint-link" href="http://{self.settings.scanner_ip}:8080/" target="_blank" rel="noopener" title="Open Brother Web Admin">{self.settings.scanner_ip}:8080</a>
          </div>
          <div class="tele-card">
            <div class="tele-card-header">
              <span class="tele-card-label">Daemon Engine State</span>
            </div>
            <span id="diag-state" class="tele-card-val">Connecting</span>
          </div>
          <div class="tele-card">
            <div class="tele-card-header">
              <span class="tele-card-label">Process Uptime</span>
            </div>
            <span id="diag-uptime" class="tele-card-val">--</span>
          </div>
          <div class="tele-card">
            <div class="tele-card-header">
              <span class="tele-card-label">Resident Memory (RSS)</span>
            </div>
            <span id="diag-ram" class="tele-card-val">-- MB</span>
          </div>
          <div class="tele-card tele-card-wide">
            <div class="tele-card-header">
              <span class="tele-card-label">Local Archive Destination</span>
              <button class="btn-copy" onclick="copyPath()" title="Copy folder path" aria-label="Copy folder path">
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
                  <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
                </svg>
                <span>Copy Path</span>
              </button>
            </div>
            <span id="diag-path" class="tele-card-val tele-path-val">{self.settings.output}</span>
          </div>
        </div>
        <div class="telemetry-footer">
          <span id="strip-detail" class="strip-detail">Brother DS-940DW ready · Insert document into feeder to scan</span>
        </div>
      </div>
    </div>

    <div id="strip-progress-bar" class="strip-progress-bar"></div>
  </header>

  <!-- Hardware Alert Banner (for Paper Jams, Feed Errors, Blocked Path) -->
  <div id="hardware-alert-banner" class="alert-banner" style="display:none;" role="alert">
    <div class="alert-container">
      <div class="alert-icon-wrap" aria-hidden="true">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
          <line x1="12" y1="9" x2="12" y2="13"/>
          <line x1="12" y1="17" x2="12.01" y2="17"/>
        </svg>
      </div>
      <div class="alert-body">
        <div class="alert-heading" id="alert-heading">Paper Feed Error / Jam Detected</div>
        <div class="alert-steps" id="alert-steps">
          <span class="alert-step"><b>1.</b> Gently pull paper out of scanner path</span>
          <span class="alert-step-divider">→</span>
          <span class="alert-step"><b>2.</b> Press <b>Power</b> once to clear flashing red <b>(!)</b></span>
          <span class="alert-step-divider">→</span>
          <span class="alert-step"><b>3.</b> Re-insert sheet straight into slot</span>
        </div>
      </div>
      <div class="alert-actions">
        <button class="btn-alert-action" onclick="refreshAll()" title="Re-check scanner status">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="23 4 23 10 17 10"/>
            <polyline points="1 20 1 14 7 14"/>
            <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
          </svg>
          <span>Check Hardware</span>
        </button>
      </div>
    </div>
  </div>

  <main class="workbench">
    <!-- Master-Detail Split Grid -->
    <div class="split-layout">
      <!-- Master: Recent Scans Ledger -->
      <section class="ledger-pane" aria-labelledby="ledger-heading">
        <div class="ledger-header">
          <div class="ledger-title-row">
            <div class="ledger-title-wrap">
              <h2 id="ledger-heading" class="ledger-title">Recent Scans</h2>
              <span class="ledger-count" id="scan-count">0</span>
            </div>
            <div class="ledger-shortcuts"><kbd>↑</kbd><kbd>↓</kbd> <span>nav</span> · <kbd>/</kbd> <span>search</span></div>
          </div>

          <div class="search-row">
            <svg class="search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <circle cx="11" cy="11" r="8"/>
              <line x1="21" y1="21" x2="16.65" y2="16.65"/>
            </svg>
            <input type="search" id="search-box" class="search-input" placeholder="Search archive..." oninput="onSearchInput()" aria-label="Search archive">
            <button class="search-clear-btn" id="search-clear" onclick="clearSearch()" style="display:none;" title="Clear search" aria-label="Clear search">✕</button>
            <kbd class="search-kbd">/</kbd>
          </div>

          <div class="category-bar-wrap">
            <nav class="category-bar" id="category-filters" aria-label="Filter by category">
              <button class="category-chip active" onclick="setCategoryFilter('all', this)">All</button>
            </nav>
          </div>
        </div>

        <div class="doc-list" id="doc-list" role="listbox" aria-label="Scanned documents">
          <div class="zero-state">
            <div class="zero-title">Loading documents...</div>
          </div>
        </div>
      </section>

      <!-- Detail: Live Inspector Pane -->
      <section class="inspector-pane" id="inspector-pane" aria-label="Document Preview and Inspector">
        <div class="inspector-header">
          <div class="inspector-info">
            <div class="inspector-title" id="inspector-filename">Select a document</div>
            <div class="inspector-meta" id="inspector-meta">
              <span id="meta-category" class="meta-tag category-tag cat-inbox" style="display:none;"></span>
              <span id="meta-doc-date" class="meta-tag" style="display:none;"></span>
              <span id="meta-scan-date" class="meta-tag" style="display:none;"></span>
              <span id="meta-size" class="meta-tag" style="display:none;"></span>
            </div>
          </div>

          <div class="inspector-actions">
            <a class="btn-action" id="btn-download" href="#" download="" title="Download PDF" style="display:none;">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                <polyline points="7 10 12 15 17 10"/>
                <line x1="12" y1="15" x2="12" y2="3"/>
              </svg>
              <span>Download</span>
            </a>
            <a class="btn-action" id="btn-open-external" href="#" target="_blank" rel="noopener" title="Open in new window" style="display:none;">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>
                <polyline points="15 3 21 3 21 9"/>
                <line x1="10" y1="14" x2="21" y2="3"/>
              </svg>
              <span>Open</span>
            </a>
            <button class="btn-action btn-danger" id="btn-delete-doc" onclick="deleteActiveDoc()" title="Delete document" style="display:none;">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polyline points="3 6 5 6 21 6"/>
                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
              </svg>
              <span>Delete</span>
            </button>
            <button class="btn-action btn-close-mobile" onclick="closeMobileInspector()" title="Close viewer">
              <span>Close</span>
            </button>
          </div>
        </div>

        <iframe class="viewer-frame" id="viewer-frame" src="about:blank" title="Document PDF Preview"></iframe>

        <div class="zero-state" id="inspector-zero" style="display:flex;">
          <svg class="zero-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
            <polyline points="14 2 14 8 20 8"/>
          </svg>
          <div class="zero-title">Ready for paper</div>
          <div class="zero-desc">Select any document in the ledger to inspect, or feed paper into the DS-940DW to scan automatically.</div>
        </div>
      </section>
    </div>
  </main>

  <div id="toast" class="toast" role="alert">Notification</div>

  <!-- Accessible Deletion Confirmation Modal -->
  <div id="confirm-modal" class="modal-backdrop" style="display:none;" role="dialog" aria-modal="true" aria-labelledby="modal-title">
    <div class="modal-card">
      <div class="modal-icon-wrap" aria-hidden="true">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="3 6 5 6 21 6"/>
          <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
        </svg>
      </div>
      <div class="modal-content">
        <h3 id="modal-title" class="modal-title">Delete Document?</h3>
        <p class="modal-desc">This action cannot be undone. The PDF will be permanently removed from your archive:</p>
        <div id="modal-filename" class="modal-filename">document.pdf</div>
        <div class="modal-actions">
          <button class="btn-modal btn-modal-cancel" id="modal-cancel-btn" onclick="closeConfirmModal()">Cancel</button>
          <button class="btn-modal btn-modal-danger" id="modal-confirm-btn" onclick="executePendingDelete()">Delete Document</button>
        </div>
      </div>
    </div>
  </div>

  <script>
    let allScans = [];
    let currentCategory = 'all';
    let selectedIndex = -1;
    let activeDoc = null;

    function showToast(msg) {{
      const t = document.getElementById('toast');
      t.textContent = msg;
      t.classList.add('show');
      setTimeout(() => t.classList.remove('show'), 2200);
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

      // Unfiled brother scanner convention: brother-YYYYMMDD-HHMMSS-hash
      const unfiledMatch = base.match(/^brother-([0-9]{{4}})([0-9]{{2}})([0-9]{{2}})-([0-9]{{2}})([0-9]{{2}})([0-9]{{2}})-[a-f0-9]+$/);
      if (unfiledMatch) {{
        const y = parseInt(unfiledMatch[1]);
        const m = parseInt(unfiledMatch[2]) - 1;
        const d = parseInt(unfiledMatch[3]);
        const hh = parseInt(unfiledMatch[4]);
        const mm = parseInt(unfiledMatch[5]);
        const date = new Date(y, m, d, hh, mm);
        const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
        const month = months[date.getMonth()] || 'Scan';
        let hour = date.getHours();
        const ampm = hour >= 12 ? 'PM' : 'AM';
        hour = hour % 12 || 12;
        const minStr = String(date.getMinutes()).padStart(2, '0');
        return `Unfiled Scan (${{month}} ${{d}}, ${{hour}}:${{minStr}} ${{ampm}})`;
      }}

      const dateMatch = base.match(/^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}_(.*)$/);
      if (dateMatch) {{
        return dateMatch[1].replace(/_/g, ' · ').replace(/-/g, ' ');
      }}
      return base.replace(/_/g, ' · ').replace(/-/g, ' ');
    }}

    /* Search Input & Clear */
    function onSearchInput() {{
      const val = document.getElementById('search-box').value;
      const clearBtn = document.getElementById('search-clear');
      if (clearBtn) clearBtn.style.display = val ? 'block' : 'none';
      filterScans();
    }}

    function clearSearch() {{
      const box = document.getElementById('search-box');
      box.value = '';
      const clearBtn = document.getElementById('search-clear');
      if (clearBtn) clearBtn.style.display = 'none';
      filterScans();
      box.focus();
    }}

    /* Telemetry Drawer Management */
    function initTelemetryDrawer() {{
      const open = localStorage.getItem('ds940-telemetry-open') === 'true';
      setTelemetryDrawerOpen(open);
    }}

    function setTelemetryDrawerOpen(open) {{
      const drawer = document.getElementById('telemetry-drawer');
      const btn = document.getElementById('tele-toggle-btn');
      if (!drawer) return;
      drawer.style.display = open ? 'block' : 'none';
      if (btn) btn.classList.toggle('active', open);
      localStorage.setItem('ds940-telemetry-open', open ? 'true' : 'false');
    }}

    function toggleTelemetry() {{
      const drawer = document.getElementById('telemetry-drawer');
      const isOpen = drawer && drawer.style.display !== 'none';
      setTelemetryDrawerOpen(!isOpen);
    }}

    /* AI Sort Trigger */
    async function triggerSortScans() {{
      const btn = document.getElementById('btn-trigger-sort');
      if (btn) {{
        btn.disabled = true;
        btn.innerHTML = '<span class="sort-spinner"></span> <span>Organizing...</span>';
      }}
      try {{
        const res = await fetch('/api/sort', {{ method: 'POST' }});
        const data = await res.json();
        if (data.status === 'ok') {{
          const count = data.moved_count || 0;
          showToast(`Organized ${{count}} document${{count === 1 ? '' : 's'}}`);
          await loadScans();
          await updateStatus();
        }} else {{
          showToast('Sort failed: ' + (data.message || 'Error'));
        }}
      }} catch (e) {{
        showToast('Sort error: ' + e);
      }} finally {{
        if (btn) {{
          btn.disabled = false;
          btn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg> <span>Organize</span>';
        }}
      }}
    }}

    /* Category Filtering */
    function updateCategoryFilters() {{
      const counts = {{}};
      allScans.forEach(s => {{
        const cat = s.category || 'Inbox';
        counts[cat] = (counts[cat] || 0) + 1;
      }});

      const filterBar = document.getElementById('category-filters');
      const cats = Object.keys(counts).sort();

      let html = `<button class="category-chip ${{currentCategory === 'all' ? 'active' : ''}}" onclick="setCategoryFilter('all', this)">All (${{allScans.length}})</button>`;

      cats.forEach(cat => {{
        const isActive = currentCategory.toLowerCase() === cat.toLowerCase();
        const escCat = cat.replace(/'/g, "\\'");
        html += `<button class="category-chip ${{isActive ? 'active' : ''}}" onclick="setCategoryFilter('${{escCat}}', this)">${{cat}} (${{counts[cat]}})</button>`;
      }});

      filterBar.innerHTML = html;
    }}

    function setCategoryFilter(cat, el) {{
      currentCategory = cat;
      selectedIndex = -1;
      updateCategoryFilters();
      renderScans();
    }}

    function filterScans() {{
      selectedIndex = -1;
      renderScans();
    }}

    function getFilteredScans() {{
      const query = document.getElementById('search-box').value.toLowerCase().trim();
      let filtered = allScans;
      if (currentCategory !== 'all') {{
        filtered = filtered.filter(s => (s.category || 'Inbox').toLowerCase() === currentCategory.toLowerCase());
      }}
      if (query) {{
        filtered = filtered.filter(s => 
          s.filename.toLowerCase().includes(query) || 
          (s.category && s.category.toLowerCase().includes(query)) ||
          (s.doc_date && s.doc_date.toLowerCase().includes(query)) ||
          (s.scan_date && s.scan_date.toLowerCase().includes(query))
        );
      }}
      return filtered;
    }}

    function selectDocument(doc, index) {{
      activeDoc = doc;
      selectedIndex = index;
      
      const frame = document.getElementById('viewer-frame');
      const zero = document.getElementById('inspector-zero');
      const filenameEl = document.getElementById('inspector-filename');
      const catEl = document.getElementById('meta-category');
      const docDateEl = document.getElementById('meta-doc-date');
      const scanDateEl = document.getElementById('meta-scan-date');
      const sizeEl = document.getElementById('meta-size');
      const dlBtn = document.getElementById('btn-download');
      const openBtn = document.getElementById('btn-open-external');
      const delBtn = document.getElementById('btn-delete-doc');
      const pane = document.getElementById('inspector-pane');

      if (!doc) {{
        frame.src = 'about:blank';
        frame.style.display = 'none';
        zero.style.display = 'flex';
        filenameEl.textContent = 'Select a document';
        catEl.style.display = 'none';
        docDateEl.style.display = 'none';
        scanDateEl.style.display = 'none';
        sizeEl.style.display = 'none';
        dlBtn.style.display = 'none';
        openBtn.style.display = 'none';
        delBtn.style.display = 'none';
        pane.classList.remove('mobile-open');
        return;
      }}

      const fileUrl = '/files/' + doc.relative_path.split('/').map(encodeURIComponent).join('/');
      frame.src = fileUrl;
      frame.style.display = 'block';
      zero.style.display = 'none';

      filenameEl.textContent = parseDisplayTitle(doc.filename);
      filenameEl.title = doc.filename;

      const cat = doc.category || 'Inbox';
      catEl.textContent = cat;
      catEl.className = 'meta-tag category-tag ' + getCategoryClass(cat);
      catEl.style.display = 'inline-block';

      docDateEl.textContent = 'Doc Date: ' + (doc.doc_date || 'Not detected');
      docDateEl.style.display = doc.doc_date ? 'inline-block' : 'none';

      scanDateEl.textContent = 'Scanned: ' + (doc.scan_date || 'Recent');
      scanDateEl.style.display = 'inline-block';

      sizeEl.textContent = doc.size_kb + ' KB';
      sizeEl.style.display = 'inline-block';

      dlBtn.href = fileUrl;
      dlBtn.download = doc.filename;
      dlBtn.style.display = 'inline-flex';

      openBtn.href = fileUrl;
      openBtn.style.display = 'inline-flex';

      delBtn.style.display = 'inline-flex';

      pane.classList.add('mobile-open');
      highlightSelectedRow();
    }}

    function closeMobileInspector() {{
      document.getElementById('inspector-pane').classList.remove('mobile-open');
    }}

    function highlightSelectedRow() {{
      const items = document.querySelectorAll('.doc-item');
      items.forEach((item, idx) => {{
        if (idx === selectedIndex) {{
          item.classList.add('selected');
          item.setAttribute('aria-selected', 'true');
          item.scrollIntoView({{ block: 'nearest', behavior: 'smooth' }});
        }} else {{
          item.classList.remove('selected');
          item.setAttribute('aria-selected', 'false');
        }}
      }});
    }}

    function renderScans() {{
      const filtered = getFilteredScans();
      const listContainer = document.getElementById('doc-list');
      document.getElementById('scan-count').textContent = filtered.length;

      if (filtered.length === 0) {{
        const query = document.getElementById('search-box').value.trim();
        const escQuery = query.replace(/"/g, '&quot;');
        listContainer.innerHTML = `
          <div class="zero-state">
            <svg class="zero-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
              <circle cx="11" cy="11" r="8"/>
              <line x1="21" y1="21" x2="16.65" y2="16.65"/>
            </svg>
            <div class="zero-title">${{query ? `No documents matching "${{escQuery}}"` : 'No documents in this view'}}</div>
            <div class="zero-desc">${{query ? 'Check your spelling or clear your search to see all documents.' : 'Feed a new document into the Brother scanner to populate this archive.'}}</div>
            ${{query ? '<button class="btn-action" onclick="clearSearch()" style="margin-top:0.75rem;">Clear search</button>' : ''}}
          </div>
        `;
        selectDocument(null, -1);
        return;
      }}

      listContainer.innerHTML = filtered.map((s, idx) => {{
        const headline = parseDisplayTitle(s.filename);
        const isSelected = activeDoc && activeDoc.relative_path === s.relative_path;
        const escRel = s.relative_path.replace(/'/g, "\\'");
        const escName = s.filename.replace(/'/g, "\\'");
        const cat = s.category || 'Inbox';
        const catClass = getCategoryClass(cat);
        const docDateStr = s.doc_date ? `📅 ${{s.doc_date}}` : '';

        return `
          <div class="doc-item ${{isSelected ? 'selected' : ''}}" 
               tabindex="0" 
               role="option" 
               aria-selected="${{isSelected ? 'true' : 'false'}}"
               onclick="selectDocument(allScans.find(x => x.relative_path === '${{escRel}}'), ${{idx}})"
               onkeydown="if(event.key === 'Enter' || event.key === ' ') {{ selectDocument(allScans.find(x => x.relative_path === '${{escRel}}'), ${{idx}}); event.preventDefault(); }}">
            <div class="doc-icon-wrap" aria-hidden="true">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                <polyline points="14 2 14 8 20 8"/>
              </svg>
            </div>
            <div class="doc-body">
              <div class="doc-title-row">
                <span class="doc-title" title="${{s.filename}}">${{headline}}</span>
                <span class="doc-size">${{s.size_kb}} KB</span>
              </div>
              <div class="doc-meta-row">
                <span class="category-tag ${{catClass}}">${{cat}}</span>
                ${{docDateStr ? `<span class="meta-tag" title="Document Date">Doc Date: ${{s.doc_date}}</span>` : ''}}
                <span class="meta-tag" title="Scan Timestamp">Scanned: ${{s.scan_date || 'Recent'}}</span>
              </div>
            </div>
            <div class="doc-actions" onclick="event.stopPropagation()">
              <button class="btn-row-action" onclick="confirmDelete('${{escRel}}', '${{escName}}')" title="Delete document" aria-label="Delete ${{headline}}">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <polyline points="3 6 5 6 21 6"/>
                  <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
                </svg>
              </button>
            </div>
          </div>
        `;
      }}).join('');

      // Auto-select the first document on desktop if none selected
      if (selectedIndex === -1 && filtered.length > 0 && window.innerWidth > 960) {{
        selectDocument(filtered[0], 0);
      }}
    }}

    function deleteActiveDoc() {{
      if (!activeDoc) return;
      confirmDelete(activeDoc.relative_path, activeDoc.filename);
    }}

    let pendingDelete = null;

    function confirmDelete(relPath, filename) {{
      pendingDelete = {{ relPath, filename }};
      const modal = document.getElementById('confirm-modal');
      const nameEl = document.getElementById('modal-filename');
      const cancelBtn = document.getElementById('modal-cancel-btn');
      if (nameEl) nameEl.textContent = filename;
      if (modal) modal.style.display = 'flex';
      if (cancelBtn) cancelBtn.focus();
    }}

    function closeConfirmModal() {{
      const modal = document.getElementById('confirm-modal');
      if (modal) modal.style.display = 'none';
      pendingDelete = null;
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
        
        const dot = document.getElementById('status-dot');
        const label = document.getElementById('status-label');
        const diagState = document.getElementById('diag-state');
        
        const strip = document.getElementById('live-status-strip');
        const stripTitle = document.getElementById('strip-title');
        const stripBadge = document.getElementById('strip-badge');
        const stripDetail = document.getElementById('strip-detail');
        const countdownWrap = document.getElementById('strip-countdown-wrap');
        const countdownNum = document.getElementById('strip-countdown-num');
        const stripActions = document.getElementById('strip-actions');
        const progressBar = document.getElementById('strip-progress-bar');
        const alertBanner = document.getElementById('hardware-alert-banner');

        const state = data.state || 'unknown';
        const scannerIp = data.scanner_ip || 'network';

        const diagScanner = document.getElementById('diag-scanner');
        if (diagScanner) {{
          diagScanner.textContent = `${{scannerIp}}:8080`;
          diagScanner.href = `http://${{scannerIp}}:8080/`;
        }}
        const diagScannerLink = document.getElementById('diag-scanner-link');
        if (diagScannerLink) diagScannerLink.href = `http://${{scannerIp}}:8080/`;

        if (dot) dot.className = 'status-dot';
        strip.className = 'app-header';
        progressBar.style.width = '0%';
        countdownWrap.style.display = 'none';
        if (stripActions) stripActions.innerHTML = '';
        if (alertBanner) alertBanner.style.display = 'none';

        let isActiveState = false;

        if (state === 'waiting_for_paper') {{
          if (dot) dot.classList.add('online');
          if (label) label.textContent = 'Ready for paper';
          if (diagState) diagState.textContent = 'Ready (Idle)';

          strip.classList.add('state-ready');
          stripTitle.textContent = 'Ready for paper';
          stripBadge.textContent = 'Duplex 300 DPI';

          const idleRem = data.idle_remaining;
          if (idleRem !== undefined && idleRem !== null && idleRem > 0) {{
            const m = Math.floor(idleRem / 60);
            const s = Math.floor(idleRem % 60);
            const timeStr = m > 0 ? `${{m}}m ${{s}}s` : `${{s}}s`;
            stripDetail.textContent = `Brother DS-940DW ready at ${{scannerIp}} · Sleeping in ${{timeStr}} to save battery`;
            if (stripActions) {{
              stripActions.innerHTML = `<span class="strip-sleep-pill" title="Battery saver countdown">🌙 ${{timeStr}}</span><div class="strip-btn-group"><button class="btn-strip" onclick="sessionAction(\\'extend\\')">+5m</button><button class="btn-strip" onclick="sessionAction(\\'pause\\')">Sleep Now</button></div>`;
            }}
          }} else {{
            stripDetail.textContent = `Brother DS-940DW ready at ${{scannerIp}} · Insert document into feeder to scan`;
            if (stripActions) stripActions.innerHTML = '';
          }}
        }} else if (state === 'battery_saver_dormant') {{
          if (dot) dot.classList.add('offline');
          if (label) label.textContent = 'Standby (Sleeping)';
          if (diagState) diagState.textContent = 'Battery Saver Standby';

          strip.classList.add('state-standby');
          stripTitle.textContent = 'Battery Saver Standby';
          stripBadge.textContent = 'Sleeping';
          stripDetail.textContent = 'Scanner is sleeping to conserve battery · Press Power on scanner or click Wake to scan';
          if (stripActions) {{
            stripActions.innerHTML = '<button class="btn-strip btn-strip-primary" onclick="sessionAction(\\'wake\\')">Wake Session</button>';
          }}
        }} else if (state === 'scanning') {{
          isActiveState = true;
          const sheet = data.sheet || 1;
          if (dot) dot.classList.add('active');
          if (label) label.textContent = `Scanning Sheet ${{sheet}}...`;
          if (diagState) diagState.textContent = `Scanning Duplex (Sheet ${{sheet}})`;

          strip.classList.add('state-scanning');
          stripTitle.textContent = `Scanning Sheet ${{sheet}}...`;
          stripBadge.textContent = 'Duplex 300 DPI';
          stripDetail.textContent = 'Drawing paper through feed rollers · Capturing front and back pages in 24-bit color';
        }} else if (state === 'waiting_for_next_sheet') {{
          isActiveState = true;
          const sheet = data.sheet || 1;
          const pages = data.pages_so_far || (sheet * 2);
          const remaining = (data.timeout_remaining !== undefined && data.timeout_remaining !== null) ? data.timeout_remaining : 8.0;

          if (dot) dot.classList.add('active');
          if (label) label.textContent = `Batch (${{remaining}}s)`;
          if (diagState) diagState.textContent = `Collation (${{remaining}}s)`;

          strip.classList.add('state-collation');
          stripTitle.textContent = `Insert Sheet ${{sheet + 1}} to Append`;
          stripBadge.textContent = 'Batch Window';
          stripDetail.textContent = `Sheet ${{sheet}} captured (${{pages}} pages total) · Insert next sheet within ${{remaining}}s to append to this document`;

          countdownWrap.style.display = 'flex';
          countdownNum.textContent = `${{remaining}}s`;
          const pct = Math.max(0, Math.min(100, (remaining / 8.0) * 100));
          progressBar.style.width = `${{pct}}%`;
        }} else if (state === 'assembling_pdf') {{
          isActiveState = true;
          if (dot) dot.classList.add('active');
          if (label) label.textContent = 'Compiling PDF...';
          if (diagState) diagState.textContent = 'Compiling PDF';

          strip.classList.add('state-processing');
          stripTitle.textContent = 'Compiling PDF Document...';
          stripBadge.textContent = 'Processing';
          stripDetail.textContent = 'Pruning blank sides with ink detection and assembling searchable PDF pages';
        }} else if (state === 'filing_document') {{
          isActiveState = true;
          if (dot) dot.classList.add('active');
          if (label) label.textContent = 'Filing Document...';
          if (diagState) diagState.textContent = 'Filing Document';

          strip.classList.add('state-processing');
          stripTitle.textContent = 'Filing Document...';
          stripBadge.textContent = 'Vision & Tags';
          stripDetail.textContent = 'Running Apple Vision OCR, AI categorization, and writing native macOS Finder tags';
        }} else if (state === 'filed') {{
          isActiveState = true;
          const dest = data.destination || 'Document.pdf';
          const filename = dest.split('/').pop();
          const cat = data.category || 'Inbox';
          const pages = data.pages ? `${{data.pages}} page${{data.pages > 1 ? 's' : ''}}` : '';

          if (dot) dot.classList.add('online');
          if (label) label.textContent = `Filed: ${{filename}}`;
          if (diagState) diagState.textContent = `Filed (${{filename}})`;

          strip.classList.add('state-filed');
          stripTitle.textContent = `Filed: ${{filename}}`;
          stripBadge.textContent = cat;
          stripDetail.textContent = `Organized into ${{cat}}${{pages ? ' · ' + pages : ''}} · Ready for next document`;

          if (lastFiledDestination !== dest) {{
            lastFiledDestination = dest;
            loadScans();
          }}
        }} else if (state === 'scanner_offline') {{
          if (dot) dot.classList.add('offline');
          if (label) label.textContent = 'Standby (Sleep)';
          if (diagState) diagState.textContent = 'Standby (Sleep)';

          strip.classList.add('state-standby');
          stripTitle.textContent = 'Standby (Sleeping)';
          stripBadge.textContent = 'Wi-Fi Sleep';
          stripDetail.textContent = `Scanner is sleeping at ${{scannerIp}} · Press Power on scanner or click Wake to scan`;
          if (stripActions) {{
            stripActions.innerHTML = '<button class="btn-strip btn-strip-primary" onclick="sessionAction(\\'wake\\')">Wake Session</button>';
          }}
        }} else if (state === 'paper_blocked' || state === 'paper_jammed') {{
          if (dot) dot.classList.add('offline');
          if (label) label.textContent = 'Feeder Jammed';
          if (diagState) diagState.textContent = 'Paper Jammed / Feed Error';

          strip.classList.add('state-warning');
          stripTitle.textContent = 'Paper Jam / Feed Error';
          stripBadge.textContent = 'Clear Paper & Press Power';
          stripDetail.textContent = `Feeder jam detected at ${{scannerIp}} · Follow recovery instructions in alert banner below`;
          if (stripActions) {{
            stripActions.innerHTML = '<button class="btn-strip btn-strip-primary" onclick="refreshAll()">Check Hardware</button>';
          }}
          if (alertBanner) {{
            alertBanner.style.display = 'block';
          }}
        }} else {{
          if (dot) dot.classList.add('offline');
          const cleanState = state.replace(/_/g, ' ');
          if (label) label.textContent = cleanState;
          if (diagState) diagState.textContent = state;

          strip.classList.add('state-standby');
          stripTitle.textContent = cleanState;
          stripBadge.textContent = 'Status';
          stripDetail.textContent = `Current hardware state: ${{state}}`;
        }}

        const diagUptime = document.getElementById('diag-uptime');
        if (diagUptime) diagUptime.textContent = data.uptime || '--';
        const diagRam = document.getElementById('diag-ram');
        if (diagRam) diagRam.textContent = (data.memory_mb || '0.0') + ' MB';

        const sessionActionsWrap = document.getElementById('header-session-actions');
        const hasSessionContent = (stripActions && stripActions.children.length > 0) || (countdownWrap && countdownWrap.style.display === 'flex');
        if (sessionActionsWrap) {{
          sessionActionsWrap.classList.toggle('has-actions', Boolean(hasSessionContent));
        }}

        setPollingInterval(isActiveState ? 750 : 2500);
      }} catch (e) {{
        console.error('updateStatus error:', e);
      }}
    }}

    async function loadScans() {{
      try {{
        const res = await fetch('/api/scans');
        allScans = await res.json();
        updateCategoryFilters();
        renderScans();
      }} catch (e) {{
        console.error(e);
      }}
    }}

    function refreshAll() {{
      updateStatus();
      loadScans();
      showToast('Archive refreshed');
    }}

    /* Keyboard navigation */
    document.addEventListener('keydown', e => {{
      const isInput = document.activeElement.tagName === 'INPUT';
      const modal = document.getElementById('confirm-modal');
      const isModalOpen = modal && modal.style.display !== 'none';

      if (e.key === 'Escape') {{
        if (isModalOpen) {{
          closeConfirmModal();
          return;
        }}
        closeMobileInspector();
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
      }} else if (e.key.toLowerCase() === 'i' && !isInput) {{
        e.preventDefault();
        toggleTelemetry();
      }} else if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && !isInput) {{
        e.preventDefault();
        const filtered = getFilteredScans();
        if (!filtered.length) return;
        if (e.key === 'ArrowDown') {{
          selectedIndex = Math.min(filtered.length - 1, selectedIndex + 1);
        }} else {{
          selectedIndex = Math.max(0, selectedIndex - 1);
        }}
        selectDocument(filtered[selectedIndex], selectedIndex);
      }}
    }});

    initTheme();
    initTelemetryDrawer();
    updateStatus();
    loadScans();
    setPollingInterval(2500);
    setInterval(loadScans, 10000);
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
