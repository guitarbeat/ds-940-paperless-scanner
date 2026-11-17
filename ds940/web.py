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
    for root, _, filenames in os.walk(output_dir):
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
        else:
            self.send_error(404, "Not Found")

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
        from ..scripts.sort_scans import sort_scans
        try:
            results = sort_scans(self.settings.output, dry_run=False, api_key=self.settings.gemini_api_key or None)
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

        data = {
            "state": state,
            "scanner_ip": self.settings.scanner_ip,
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
      --accent-soft: rgba(29, 78, 216, 0.08);
      --status-ready: #16a34a;
      --status-active: #d97706;
      --status-standby: #64748b;
      --status-danger: #dc2626;
      --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.04);
      --shadow-md: 0 4px 14px rgba(0, 0, 0, 0.08);
      --ease: cubic-bezier(0.16, 1, 0.3, 1);
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
      --accent-soft: rgba(96, 165, 250, 0.12);
      --status-ready: #22c55e;
      --status-active: #f59e0b;
      --status-standby: #94a3b8;
      --status-danger: #ef4444;
      --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.25);
      --shadow-md: 0 4px 14px rgba(0, 0, 0, 0.4);
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
        --accent-soft: rgba(96, 165, 250, 0.12);
        --status-ready: #22c55e;
        --status-active: #f59e0b;
        --status-standby: #94a3b8;
        --status-danger: #ef4444;
        --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.25);
        --shadow-md: 0 4px 14px rgba(0, 0, 0, 0.4);
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
    }}

    :focus-visible {{
      outline: 2px solid var(--border-focus);
      outline-offset: 2px;
    }}

    /* Header */
    header {{
      background: var(--bg-surface);
      border-bottom: 1px solid var(--border);
      padding: 0.75rem 1.5rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
      position: sticky;
      top: 0;
      z-index: 100;
    }}

    .brand-wrap {{
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }}

    .brand-logo {{
      width: 24px;
      height: 24px;
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
    }}

    .header-center {{
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }}

    .status-badge {{
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      font-size: 0.76rem;
      font-weight: 500;
      color: var(--text-muted);
      background: var(--bg-subtle);
      border: 1px solid var(--border);
      padding: 0.25rem 0.65rem;
      border-radius: 4px;
    }}

    .status-dot {{
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--status-standby);
      transition: background-color 160ms ease;
    }}

    .status-dot.online {{ background: var(--status-ready); }}
    .status-dot.active {{ background: var(--status-active); }}
    .status-dot.offline {{ background: var(--status-standby); }}

    .header-actions {{
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }}

    .btn-icon {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 32px;
      height: 32px;
      border-radius: 5px;
      background: var(--bg-surface);
      border: 1px solid var(--border);
      color: var(--text-muted);
      transition: all 120ms var(--ease);
    }}

    .btn-icon:hover {{
      background: var(--bg-hover);
      color: var(--text);
      border-color: var(--border-focus);
    }}

    /* Main Workbench */
    main.workbench {{
      max-width: 1560px;
      width: 100%;
      margin: 0 auto;
      padding: 1.25rem 1.5rem;
      flex: 1;
      display: flex;
      flex-direction: column;
      gap: 1rem;
    }}

    /* Live Hardware Status Strip */
    .status-strip {{
      background: var(--bg-surface);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 0.85rem 1.25rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
      position: relative;
      overflow: hidden;
      transition: border-color 180ms var(--ease), background-color 180ms var(--ease);
    }}

    .strip-main {{
      display: flex;
      align-items: center;
      gap: 0.9rem;
      flex: 1;
      min-width: 0;
    }}

    .strip-beacon {{
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--status-ready);
      flex-shrink: 0;
      transition: background-color 180ms ease;
    }}

    .strip-text-wrap {{
      display: flex;
      flex-direction: column;
      gap: 0.15rem;
      min-width: 0;
    }}

    .strip-headline-row {{
      display: flex;
      align-items: center;
      gap: 0.65rem;
      flex-wrap: wrap;
    }}

    .strip-title {{
      font-size: 1.05rem;
      font-weight: 600;
      letter-spacing: -0.015em;
      color: var(--text);
      line-height: 1.25;
    }}

    .strip-badge {{
      font-size: 0.72rem;
      font-weight: 600;
      padding: 0.15rem 0.45rem;
      border-radius: 3px;
      background: var(--bg-subtle);
      border: 1px solid var(--border);
      color: var(--text-muted);
      letter-spacing: 0.02em;
      text-transform: uppercase;
    }}

    .strip-detail {{
      font-size: 0.82rem;
      color: var(--text-muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}

    .strip-countdown-wrap {{
      display: none;
      align-items: center;
      gap: 0.5rem;
      margin-left: 1rem;
    }}

    .strip-countdown-num {{
      font-size: 0.95rem;
      font-weight: 600;
      font-variant-numeric: tabular-nums;
      color: var(--text);
    }}

    .strip-progress-bar {{
      position: absolute;
      bottom: 0;
      left: 0;
      height: 2px;
      width: 0%;
      background: var(--status-active);
      transition: width 0.7s linear;
    }}

    /* State modifier styles */
    .status-strip.state-ready {{
      border-color: var(--border);
    }}
    .status-strip.state-ready .strip-beacon {{
      background: var(--status-ready);
    }}

    .status-strip.state-scanning {{
      border-color: rgba(37, 99, 235, 0.35);
      background: rgba(37, 99, 235, 0.035);
    }}
    .status-strip.state-scanning .strip-beacon {{
      background: var(--accent);
    }}

    .status-strip.state-collation {{
      border-color: rgba(217, 119, 6, 0.4);
      background: rgba(217, 119, 6, 0.05);
    }}
    .status-strip.state-collation .strip-beacon {{
      background: var(--status-active);
    }}

    .status-strip.state-processing {{
      border-color: rgba(99, 102, 241, 0.35);
      background: rgba(99, 102, 241, 0.04);
    }}
    .status-strip.state-processing .strip-beacon {{
      background: #6366f1;
    }}

    .status-strip.state-filed {{
      border-color: rgba(22, 163, 74, 0.4);
      background: rgba(22, 163, 74, 0.05);
    }}
    .status-strip.state-filed .strip-beacon {{
      background: var(--status-ready);
    }}

    .status-strip.state-warning {{
      border-color: rgba(220, 38, 38, 0.4);
      background: rgba(220, 38, 38, 0.04);
    }}
    .status-strip.state-warning .strip-beacon {{
      background: var(--status-danger);
    }}

    .status-strip.state-standby {{
      border-color: var(--border);
      background: var(--bg-surface);
    }}
    .status-strip.state-standby .strip-beacon {{
      background: var(--status-standby);
    }}

    /* Master-Detail Split Grid */
    .split-layout {{
      display: grid;
      grid-template-columns: 480px 1fr;
      gap: 1.25rem;
      flex: 1;
      min-height: calc(100vh - 210px);
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

    .search-row {{
      position: relative;
      display: flex;
      align-items: center;
    }}

    .search-input {{
      width: 100%;
      background: var(--bg-page);
      border: 1px solid var(--border);
      border-radius: 5px;
      padding: 0.45rem 2rem 0.45rem 2.1rem;
      font-size: 0.8rem;
      color: var(--text);
      transition: all 120ms var(--ease);
    }}

    .search-input:focus {{
      background: var(--bg-surface);
      border-color: var(--border-focus);
    }}

    .search-icon {{
      position: absolute;
      left: 0.65rem;
      width: 14px;
      height: 14px;
      color: var(--text-dim);
      pointer-events: none;
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

    /* Category Chips */
    .category-bar {{
      display: flex;
      gap: 0.35rem;
      overflow-x: auto;
      padding-bottom: 0.1rem;
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
      transition: all 100ms var(--ease);
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
      divide-y: 1px solid var(--border-subtle);
    }}

    .doc-item {{
      padding: 0.75rem 1rem;
      border-bottom: 1px solid var(--border-subtle);
      display: flex;
      align-items: flex-start;
      gap: 0.75rem;
      cursor: pointer;
      background: var(--bg-surface);
      transition: background 80ms ease;
    }}

    .doc-item:hover {{
      background: var(--bg-hover);
    }}

    .doc-item.selected {{
      background: var(--bg-selected);
      border-left: 3px solid var(--accent);
      padding-left: calc(1rem - 3px);
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
      gap: 0.2rem;
    }}

    .doc-title-row {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 0.5rem;
    }}

    .doc-title {{
      font-size: 0.84rem;
      font-weight: 600;
      color: var(--text);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}

    .doc-size {{
      font-size: 0.7rem;
      color: var(--text-dim);
      flex-shrink: 0;
    }}

    .doc-meta-row {{
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 0.4rem 0.65rem;
      font-size: 0.72rem;
      color: var(--text-muted);
    }}

    .meta-tag {{
      display: inline-flex;
      align-items: center;
      gap: 0.25rem;
    }}

    .category-tag {{
      font-weight: 500;
      background: var(--bg-subtle);
      border: 1px solid var(--border-subtle);
      padding: 0.05rem 0.35rem;
      border-radius: 3px;
      color: var(--text-muted);
    }}

    .doc-actions {{
      margin-left: 0.25rem;
    }}

    .btn-row-action {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 26px;
      height: 26px;
      border: none;
      background: transparent;
      border-radius: 4px;
      color: var(--text-dim);
      transition: all 100ms ease;
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
    }}

    .inspector-header {{
      padding: 0.75rem 1.15rem;
      border-bottom: 1px solid var(--border);
      background: var(--bg-surface);
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 1rem;
    }}

    .inspector-info {{
      display: flex;
      flex-direction: column;
      gap: 0.2rem;
      min-width: 0;
    }}

    .inspector-title {{
      font-size: 0.9rem;
      font-weight: 600;
      color: var(--text);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}

    .inspector-meta {{
      display: flex;
      align-items: center;
      gap: 0.6rem;
      font-size: 0.74rem;
      color: var(--text-muted);
    }}

    .inspector-actions {{
      display: flex;
      align-items: center;
      gap: 0.5rem;
      flex-shrink: 0;
    }}

    .btn-action {{
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      font-size: 0.76rem;
      font-weight: 500;
      color: var(--text-muted);
      background: var(--bg-surface);
      border: 1px solid var(--border);
      padding: 0.35rem 0.65rem;
      border-radius: 5px;
      text-decoration: none;
      transition: all 100ms var(--ease);
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
      min-height: 540px;
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

    /* Technical Diagnostics Footer */
    footer.diagnostics {{
      background: var(--bg-surface);
      border-top: 1px solid var(--border);
      padding: 0.6rem 1.5rem;
      font-size: 0.72rem;
      color: var(--text-dim);
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 0.5rem 1.5rem;
    }}

    .diag-group {{
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 0.5rem 1rem;
    }}

    .diag-item {{
      display: inline-flex;
      align-items: center;
      gap: 0.3rem;
    }}

    .diag-label {{
      color: var(--text-dim);
    }}

    .diag-val {{
      color: var(--text-muted);
      font-weight: 500;
    }}

    .btn-copy {{
      background: none;
      border: none;
      color: var(--text-dim);
      display: inline-flex;
      align-items: center;
      padding: 0 2px;
      margin-left: 2px;
    }}

    .btn-copy:hover {{ color: var(--text); }}

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
      transform: translateY(10px);
      opacity: 0;
      pointer-events: none;
      transition: all 140ms var(--ease);
      z-index: 1000;
    }}

    .toast.show {{
      transform: translateY(0);
      opacity: 1;
    }}

    /* Mobile Drawer & Modal View */
    @media (max-width: 960px) {{
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

    @media (min-width: 961px) {{
      .btn-close-mobile {{
        display: none !important;
      }}
    }}
  </style>
</head>
<body>
  <header>
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

    <div class="header-center">
      <div class="status-badge" role="status">
        <span id="status-dot" class="status-dot offline"></span>
        <span id="status-label">Standby</span>
      </div>
    </div>

    <div class="header-actions">
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
  </header>

  <main class="workbench">
    <!-- Live Hardware Status Strip -->
    <div id="live-status-strip" class="status-strip state-ready" role="region" aria-live="polite">
      <div class="strip-main">
        <span id="strip-beacon" class="strip-beacon"></span>
        <div class="strip-text-wrap">
          <div class="strip-headline-row">
            <h2 id="strip-title" class="strip-title">Ready for paper</h2>
            <span id="strip-badge" class="strip-badge">Duplex 300 DPI</span>
          </div>
          <div id="strip-detail" class="strip-detail">Brother DS-940DW ready &bull; Insert document into feeder to scan</div>
        </div>
      </div>
      <div id="strip-countdown-wrap" class="strip-countdown-wrap">
        <span id="strip-countdown-num" class="strip-countdown-num">8.0s</span>
      </div>
      <div id="strip-progress-bar" class="strip-progress-bar"></div>
    </div>

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
          </div>

          <div class="search-row">
            <svg class="search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <circle cx="11" cy="11" r="8"/>
              <line x1="21" y1="21" x2="16.65" y2="16.65"/>
            </svg>
            <input type="search" id="search-box" class="search-input" placeholder="Search archive..." oninput="filterScans()" aria-label="Search archive">
            <kbd class="search-kbd">/</kbd>
          </div>

          <nav class="category-bar" id="category-filters" aria-label="Filter by category">
            <button class="category-chip active" onclick="setCategoryFilter('all', this)">All</button>
          </nav>
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
              <span id="meta-category" class="meta-tag category-tag" style="display:none;"></span>
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
            </a>
            <button class="btn-action btn-danger" id="btn-delete-doc" onclick="deleteActiveDoc()" title="Delete document" style="display:none;">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polyline points="3 6 5 6 21 6"/>
                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
              </svg>
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

  <!-- Diagnostics Footer -->
  <footer class="diagnostics">
    <div class="diag-group">
      <div class="diag-item">
        <span class="diag-label">Scanner:</span>
        <span class="diag-val" id="diag-scanner">{self.settings.scanner_ip}:8080</span>
      </div>
      <div class="diag-item">
        <span class="diag-label">State:</span>
        <span class="diag-val" id="diag-state">Connecting</span>
      </div>
      <div class="diag-item">
        <span class="diag-label">Uptime:</span>
        <span class="diag-val" id="diag-uptime">--</span>
      </div>
      <div class="diag-item">
        <span class="diag-label">RAM:</span>
        <span class="diag-val" id="diag-ram">-- MB</span>
      </div>
    </div>
    <div class="diag-group">
      <div class="diag-item">
        <span class="diag-label">Archive:</span>
        <span class="diag-val" id="diag-path">{self.settings.output}</span>
        <button class="btn-copy" onclick="copyPath()" title="Copy folder path" aria-label="Copy folder path">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
            <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
          </svg>
        </button>
      </div>
    </div>
  </footer>

  <div id="toast" class="toast" role="alert">Notification</div>

  <script>
    let allScans = [];
    let currentCategory = 'all';
    let selectedIndex = -1;
    let activeDoc = null;

    function showToast(msg) {{
      const t = document.getElementById('toast');
      t.textContent = msg;
      t.classList.add('show');
      setTimeout(() => t.classList.remove('show'), 2000);
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

    /* Document Title Parser */
    function parseDisplayTitle(filename) {{
      const base = filename.endsWith('.pdf') ? filename.slice(0, -4) : filename;
      const dateMatch = base.match(/^\\d{{4}}-\\d{{2}}-\\d{{2}}_(.*)$/);
      if (dateMatch) {{
        return dateMatch[1].replace(/_/g, ' · ').replace(/-/g, ' ');
      }}
      return base.replace(/_/g, ' · ').replace(/-/g, ' ');
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
        filtered = filtered.filter(s => s.category.toLowerCase().includes(currentCategory.toLowerCase()));
      }}
      if (query) {{
        filtered = filtered.filter(s => 
          s.filename.toLowerCase().includes(query) || 
          s.category.toLowerCase().includes(query) ||
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

      const fileUrl = '/files/' + encodeURIComponent(doc.relative_path);
      frame.src = fileUrl;
      frame.style.display = 'block';
      zero.style.display = 'none';

      filenameEl.textContent = parseDisplayTitle(doc.filename);
      filenameEl.title = doc.filename;

      catEl.textContent = doc.category || 'Inbox';
      catEl.style.display = 'inline-block';

      docDateEl.textContent = 'Doc Date: ' + (doc.doc_date || 'Not detected');
      docDateEl.style.display = 'inline-block';

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
        listContainer.innerHTML = `
          <div class="zero-state">
            <svg class="zero-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
              <circle cx="11" cy="11" r="8"/>
              <line x1="21" y1="21" x2="16.65" y2="16.65"/>
            </svg>
            <div class="zero-title">No matching documents</div>
            <div class="zero-desc">Adjust your search query or feed a new document into the scanner.</div>
          </div>
        `;
        selectDocument(null, -1);
        return;
      }}

      listContainer.innerHTML = filtered.map((s, idx) => {{
        const headline = parseDisplayTitle(s.filename);
        const isSelected = activeDoc && activeDoc.filename === s.filename;
        const escRel = s.relative_path.replace(/'/g, "\\'");
        const escName = s.filename.replace(/'/g, "\\'");

        return `
          <div class="doc-item ${{isSelected ? 'selected' : ''}}" 
               tabindex="0" 
               role="option" 
               aria-selected="${{isSelected ? 'true' : 'false'}}"
               onclick="selectDocument(allScans.find(x => x.filename === '${{escName}}'), ${{idx}})"
               onkeydown="if(event.key === 'Enter' || event.key === ' ') {{ selectDocument(allScans.find(x => x.filename === '${{escName}}'), ${{idx}}); event.preventDefault(); }}">
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
                <span class="category-tag">${{s.category || 'Inbox'}}</span>
                <span class="meta-tag">Doc Date: ${{s.doc_date || 'Not detected'}}</span>
                <span class="meta-tag">Scanned: ${{s.scan_date || 'Recent'}}</span>
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

    async function confirmDelete(relPath, filename) {{
      if (!confirm('Permanently delete ' + filename + '?')) return;
      try {{
        const res = await fetch('/api/delete', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ path: relPath }})
        }});
        const data = await res.json();
        if (data.status === 'ok') {{
          showToast('Deleted ' + filename);
          if (activeDoc && activeDoc.filename === filename) {{
            activeDoc = null;
            selectedIndex = -1;
          }}
          await loadScans();
        }} else {{
          showToast('Failed to delete: ' + (data.message || 'Error'));
        }}
      }} catch (e) {{
        showToast('Delete error: ' + e);
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
        const progressBar = document.getElementById('strip-progress-bar');

        const state = data.state || 'unknown';
        const scannerIp = data.scanner_ip || 'network';

        dot.className = 'status-dot';
        strip.className = 'status-strip';
        progressBar.style.width = '0%';
        countdownWrap.style.display = 'none';

        let isActiveState = false;

        if (state === 'waiting_for_paper') {{
          dot.classList.add('online');
          label.textContent = 'Ready for paper';
          diagState.textContent = 'Ready (Idle)';

          strip.classList.add('state-ready');
          stripTitle.textContent = 'Ready for paper';
          stripBadge.textContent = 'Duplex 300 DPI';
          stripDetail.textContent = `Brother DS-940DW ready at ${{scannerIp}} &bull; Insert document into feeder to scan`;
        }} else if (state === 'scanning') {{
          isActiveState = true;
          const sheet = data.sheet || 1;
          dot.classList.add('active');
          label.textContent = `Scanning Sheet ${{sheet}}...`;
          diagState.textContent = `Scanning Duplex (Sheet ${{sheet}})`;

          strip.classList.add('state-scanning');
          stripTitle.textContent = `Scanning Sheet ${{sheet}}...`;
          stripBadge.textContent = 'Duplex 300 DPI';
          stripDetail.textContent = 'Drawing paper through feed rollers &bull; Capturing front and back pages in 24-bit color';
        }} else if (state === 'waiting_for_next_sheet') {{
          isActiveState = true;
          const sheet = data.sheet || 1;
          const pages = data.pages_so_far || (sheet * 2);
          const remaining = (data.timeout_remaining !== undefined && data.timeout_remaining !== null) ? data.timeout_remaining : 8.0;

          dot.classList.add('active');
          label.textContent = `Batch (${{remaining}}s)`;
          diagState.textContent = `Collation (${{remaining}}s)`;

          strip.classList.add('state-collation');
          stripTitle.textContent = `Insert Sheet ${{sheet + 1}} to Append`;
          stripBadge.textContent = 'Batch Window';
          stripDetail.textContent = `Sheet ${{sheet}} captured (${{pages}} pages total) &bull; Insert next sheet within ${{remaining}}s to append to this document`;

          countdownWrap.style.display = 'flex';
          countdownNum.textContent = `${{remaining}}s`;
          const pct = Math.max(0, Math.min(100, (remaining / 8.0) * 100));
          progressBar.style.width = `${{pct}}%`;
        }} else if (state === 'assembling_pdf') {{
          isActiveState = true;
          dot.classList.add('active');
          label.textContent = 'Compiling PDF...';
          diagState.textContent = 'Compiling PDF';

          strip.classList.add('state-processing');
          stripTitle.textContent = 'Compiling PDF Document...';
          stripBadge.textContent = 'Processing';
          stripDetail.textContent = 'Pruning blank sides with ink detection and assembling searchable PDF pages';
        }} else if (state === 'filing_document') {{
          isActiveState = true;
          dot.classList.add('active');
          label.textContent = 'Filing Document...';
          diagState.textContent = 'Filing Document';

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

          dot.classList.add('online');
          label.textContent = `Filed: ${{filename}}`;
          diagState.textContent = `Filed (${{filename}})`;

          strip.classList.add('state-filed');
          stripTitle.textContent = `Filed: ${{filename}}`;
          stripBadge.textContent = cat;
          stripDetail.textContent = `Organized into ${{cat}}${{pages ? ' &bull; ' + pages : ''}} &bull; Ready for next document`;

          if (lastFiledDestination !== dest) {{
            lastFiledDestination = dest;
            loadScans();
          }}
        }} else if (state === 'scanner_offline') {{
          dot.classList.add('offline');
          label.textContent = 'Standby (Sleep)';
          diagState.textContent = 'Standby (Sleep)';

          strip.classList.add('state-standby');
          stripTitle.textContent = 'Standby (Sleeping)';
          stripBadge.textContent = 'Wi-Fi Sleep';
          stripDetail.textContent = `Scanner is sleeping at ${{scannerIp}} &bull; Automatically wakes when paper is inserted`;
        }} else if (state === 'paper_blocked' || state === 'paper_jammed') {{
          dot.classList.add('offline');
          label.textContent = 'Feeder Jammed';
          diagState.textContent = 'Paper Jammed';

          strip.classList.add('state-warning');
          stripTitle.textContent = 'Feeder Jammed';
          stripBadge.textContent = 'Action Required';
          stripDetail.textContent = 'Paper is jammed in the scanner path &bull; Please open cover and clear paper path';
        }} else {{
          dot.classList.add('offline');
          const cleanState = state.replace(/_/g, ' ');
          label.textContent = cleanState;
          diagState.textContent = state;

          strip.classList.add('state-standby');
          stripTitle.textContent = cleanState;
          stripBadge.textContent = 'Status';
          stripDetail.textContent = `Current hardware state: ${{state}}`;
        }}

        document.getElementById('diag-uptime').textContent = data.uptime || '--';
        document.getElementById('diag-ram').textContent = (data.memory_mb || '0.0') + ' MB';

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
      
      if (e.key === 'Escape') {{
        closeMobileInspector();
        if (isInput) document.activeElement.blur();
      }} else if (e.key === '/' && !isInput) {{
        e.preventDefault();
        document.getElementById('search-box').focus();
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


def run_web_server(settings: Settings, stop_event: threading.Event | None = None):
    class CustomServer(ThreadingHTTPServer):
        allow_reuse_address = True

    DashboardHandler.settings = settings
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
