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
      --bg: #0c0e12;
      --surface: #14171d;
      --surface-subtle: #191c23;
      --surface-hover: #1c2028;
      --surface-active: #222631;
      --border: rgba(255, 255, 255, 0.065);
      --border-subtle: rgba(255, 255, 255, 0.035);
      --border-focus: rgba(255, 255, 255, 0.22);
      --text: #f0f2f5;
      --text-secondary: #9aa1ad;
      --text-muted: #5e6674;
      --accent: #f0f2f5;
      --status-ready: #34c759;
      --status-active: #f59e0b;
      --status-standby: #64748b;
      --status-danger: #ef4444;
      --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    ::selection {{ background: rgba(255, 255, 255, 0.12); color: #fff; }}
    ::-webkit-scrollbar {{ width: 5px; height: 5px; }}
    ::-webkit-scrollbar-track {{ background: transparent; }}
    ::-webkit-scrollbar-thumb {{ background: rgba(255, 255, 255, 0.08); border-radius: 3px; }}
    ::-webkit-scrollbar-thumb:hover {{ background: rgba(255, 255, 255, 0.18); }}

    body {{
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display", "Inter", system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding: 2.5rem 1.5rem 4rem;
      font-variant-numeric: tabular-nums;
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
      letter-spacing: -0.011em;
    }}
    .container {{ max-width: 980px; margin: 0 auto; }}

    /* Header */
    header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 1.25rem;
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }}
    .brand-mark {{
      width: 22px;
      height: 22px;
      color: var(--text-secondary);
      flex-shrink: 0;
    }}
    .brand-title {{
      font-size: 1.05rem;
      font-weight: 600;
      letter-spacing: -0.02em;
      color: var(--text);
    }}
    .header-right {{
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }}
    .status-badge {{
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      font-size: 0.75rem;
      font-weight: 500;
      color: var(--text-secondary);
      background: var(--surface);
      border: 1px solid var(--border);
      padding: 0.28rem 0.65rem;
      border-radius: 9999px;
    }}
    .status-dot {{
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--status-standby);
      transition: background-color 200ms ease;
    }}
    .status-dot.online {{ background: var(--status-ready); }}
    .status-dot.active {{ background: var(--status-active); animation: softPulse 1.6s infinite ease-in-out; }}
    .status-dot.offline {{ background: var(--status-standby); }}

    @keyframes softPulse {{
      0%, 100% {{ opacity: 1; }}
      50% {{ opacity: 0.35; }}
    }}

    .icon-action {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 28px;
      height: 28px;
      border-radius: 6px;
      background: var(--surface);
      border: 1px solid var(--border);
      color: var(--text-muted);
      cursor: pointer;
      transition: all 120ms var(--ease-out);
    }}
    .icon-action:hover {{ color: var(--text); background: var(--surface-hover); border-color: rgba(255, 255, 255, 0.14); }}
    .icon-action:active {{ transform: scale(0.96); }}

    /* Discreet Single-Line Telemetry Strip */
    .telemetry-strip {{
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 0.5rem 0.85rem;
      font-size: 0.72rem;
      color: var(--text-muted);
      padding: 0.5rem 0.85rem;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 7px;
      margin-bottom: 2rem;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    }}
    .telemetry-item {{
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
    }}
    .telemetry-label {{ color: var(--text-muted); opacity: 0.75; }}
    .telemetry-value {{ color: var(--text-secondary); }}
    .telemetry-sep {{ opacity: 0.3; }}
    .path-copy {{
      background: none;
      border: none;
      color: var(--text-muted);
      cursor: pointer;
      padding: 0 2px;
      margin-left: 2px;
      transition: color 120ms ease;
    }}
    .path-copy:hover {{ color: var(--text); }}

    /* Multi-Sheet Collation Banner */
    .collation-banner {{
      display: none;
      background: rgba(245, 158, 11, 0.08);
      border: 1px solid rgba(245, 158, 11, 0.2);
      border-radius: 8px;
      padding: 0.75rem 1rem;
      margin-bottom: 1.5rem;
      position: relative;
      overflow: hidden;
      animation: fadeIn 150ms var(--ease-out);
    }}
    .collation-banner.visible {{ display: flex; align-items: center; justify-content: space-between; }}
    .collation-msg {{ font-size: 0.8rem; font-weight: 500; color: #fbbf24; }}
    .collation-timer {{ font-size: 0.76rem; font-family: ui-monospace, Menlo, monospace; color: var(--text); font-weight: 600; }}
    .collation-progress {{
      position: absolute;
      bottom: 0;
      left: 0;
      height: 2px;
      background: var(--status-active);
      transition: width 1s linear;
    }}

    /* Main Section Toolbar */
    .archive-toolbar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 0.85rem;
      gap: 1rem;
      flex-wrap: wrap;
    }}
    .toolbar-title-wrap {{
      display: flex;
      align-items: baseline;
      gap: 0.5rem;
    }}
    .toolbar-title {{
      font-size: 0.95rem;
      font-weight: 600;
      letter-spacing: -0.015em;
      color: var(--text);
    }}
    .toolbar-count {{
      font-size: 0.72rem;
      color: var(--text-muted);
      font-variant-numeric: tabular-nums;
    }}
    .search-box-wrap {{
      position: relative;
      display: flex;
      align-items: center;
    }}
    .search-input {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 0.4rem 2rem 0.4rem 2rem;
      font-size: 0.78rem;
      color: var(--text);
      width: 210px;
      outline: none;
      transition: all 140ms var(--ease-out);
    }}
    .search-input:focus {{
      border-color: var(--border-focus);
      background: var(--surface-hover);
      box-shadow: 0 0 0 1px var(--border-focus);
      width: 250px;
    }}
    .search-input::placeholder {{ color: var(--text-muted); }}
    .search-icon-svg {{
      position: absolute;
      left: 0.65rem;
      width: 13px;
      height: 13px;
      color: var(--text-muted);
      pointer-events: none;
    }}
    .kbd-shortcut {{
      position: absolute;
      right: 0.5rem;
      font-size: 0.62rem;
      font-family: ui-monospace, Menlo, monospace;
      color: var(--text-muted);
      background: var(--surface-subtle);
      border: 1px solid var(--border);
      padding: 0.05rem 0.35rem;
      border-radius: 3px;
      pointer-events: none;
    }}

    /* Category Segmented Tabs */
    .filter-tabs {{
      display: flex;
      gap: 0.3rem;
      overflow-x: auto;
      margin-bottom: 0.85rem;
      padding-bottom: 0.2rem;
    }}
    .filter-tab {{
      font-size: 0.74rem;
      font-weight: 500;
      padding: 0.25rem 0.65rem;
      border-radius: 6px;
      background: transparent;
      border: 1px solid transparent;
      color: var(--text-muted);
      cursor: pointer;
      white-space: nowrap;
      transition: all 120ms var(--ease-out);
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
    }}
    .filter-tab:hover {{ color: var(--text-secondary); background: var(--surface); }}
    .filter-tab.active {{
      background: var(--surface);
      border-color: var(--border);
      color: var(--text);
      font-weight: 600;
    }}
    .filter-tab-count {{
      font-size: 0.68rem;
      opacity: 0.65;
    }}

    /* Document Ledger Table */
    .ledger-container {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 9px;
      overflow: hidden;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.82rem;
      text-align: left;
    }}
    thead th {{
      padding: 0.65rem 1.15rem;
      background: var(--surface-subtle);
      color: var(--text-muted);
      font-weight: 500;
      font-size: 0.68rem;
      letter-spacing: 0.03em;
      text-transform: uppercase;
      border-bottom: 1px solid var(--border);
    }}
    tbody tr {{
      border-bottom: 1px solid var(--border-subtle);
      cursor: pointer;
      transition: background 100ms ease;
    }}
    tbody tr:last-child {{ border-bottom: none; }}
    tbody tr:hover {{ background: var(--surface-hover); }}
    tbody tr.selected {{ background: var(--surface-active); outline: 1px solid rgba(255, 255, 255, 0.12); }}
    td {{
      padding: 0.75rem 1.15rem;
      vertical-align: middle;
    }}

    /* Document Cell */
    .doc-cell {{
      display: flex;
      align-items: center;
      gap: 0.75rem;
      min-width: 240px;
    }}
    .doc-glyph {{
      width: 16px;
      height: 16px;
      color: var(--text-muted);
      flex-shrink: 0;
      transition: color 100ms ease;
    }}
    tbody tr:hover .doc-glyph {{ color: var(--text-secondary); }}
    .doc-info {{
      display: flex;
      flex-direction: column;
      gap: 0.1rem;
      overflow: hidden;
    }}
    .doc-headline {{
      font-size: 0.84rem;
      font-weight: 500;
      color: var(--text);
      letter-spacing: -0.01em;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      max-width: 440px;
    }}
    .doc-subline {{
      font-size: 0.68rem;
      color: var(--text-muted);
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      max-width: 440px;
    }}

    /* Category Pill */
    .cat-badge {{
      display: inline-block;
      font-size: 0.68rem;
      font-weight: 500;
      padding: 0.15rem 0.5rem;
      border-radius: 4px;
      letter-spacing: 0.01em;
      white-space: nowrap;
    }}

    /* Dates & Sizes */
    .date-val {{
      font-size: 0.78rem;
      color: var(--text-secondary);
      font-variant-numeric: tabular-nums;
    }}
    .date-val.doc-date {{
      font-weight: 500;
      color: var(--text);
    }}
    .scan-date-val {{
      font-size: 0.74rem;
      color: var(--text-muted);
      font-variant-numeric: tabular-nums;
    }}
    .size-val {{
      font-size: 0.74rem;
      color: var(--text-muted);
      font-variant-numeric: tabular-nums;
    }}

    /* Action Buttons */
    .actions-col {{ text-align: right; white-space: nowrap; }}
    .row-btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 0.25rem 0.45rem;
      font-size: 0.72rem;
      font-weight: 500;
      color: var(--text-muted);
      background: none;
      border: none;
      border-radius: 4px;
      cursor: pointer;
      text-decoration: none;
      transition: all 100ms ease;
    }}
    .row-btn:hover {{ color: var(--text); background: var(--surface-subtle); }}
    .row-btn:active {{ transform: scale(0.96); }}
    .row-btn.row-delete:hover {{ color: #ff6b6b; background: rgba(239, 68, 68, 0.08); }}

    /* Empty State */
    .empty-state {{
      padding: 3.5rem 1.5rem;
      text-align: center;
      color: var(--text-muted);
    }}
    .empty-icon {{
      width: 28px;
      height: 28px;
      margin: 0 auto 0.65rem;
      color: var(--text-muted);
      opacity: 0.35;
    }}
    .empty-headline {{
      font-size: 0.85rem;
      font-weight: 500;
      color: var(--text-secondary);
      margin-bottom: 0.25rem;
    }}
    .empty-instruction {{
      font-size: 0.74rem;
      color: var(--text-muted);
    }}

    /* QuickLook Drawer / Modal */
    .preview-backdrop {{
      position: fixed;
      top: 0;
      left: 0;
      right: 0;
      bottom: 0;
      background: rgba(3, 4, 6, 0.8);
      backdrop-filter: blur(12px);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 1000;
      padding: 1.5rem;
      opacity: 0;
      transition: opacity 140ms var(--ease-out);
    }}
    .preview-backdrop.open {{
      display: flex;
      opacity: 1;
    }}
    .preview-modal {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      width: 100%;
      max-width: 940px;
      height: 86vh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      box-shadow: 0 20px 50px rgba(0, 0, 0, 0.65);
      transform: scale(0.97);
      transition: transform 160ms var(--ease-out);
    }}
    .preview-backdrop.open .preview-modal {{
      transform: scale(1);
    }}
    .preview-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 0.75rem 1.15rem;
      border-bottom: 1px solid var(--border);
      background: var(--surface-subtle);
    }}
    .preview-left {{
      display: flex;
      align-items: center;
      gap: 0.55rem;
      overflow: hidden;
    }}
    .preview-title {{
      font-size: 0.84rem;
      font-weight: 600;
      color: var(--text);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      max-width: 580px;
    }}
    .preview-pill {{
      font-size: 0.68rem;
      color: var(--text-muted);
      background: var(--surface);
      border: 1px solid var(--border);
      padding: 0.12rem 0.45rem;
      border-radius: 3px;
      white-space: nowrap;
    }}
    .preview-right {{
      display: flex;
      align-items: center;
      gap: 0.35rem;
    }}
    .preview-frame {{
      flex: 1;
      width: 100%;
      height: 100%;
      border: none;
      background: #121419;
    }}

    /* Toast */
    .toast {{
      position: fixed;
      bottom: 2rem;
      right: 2rem;
      background: var(--surface-hover);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 0.65rem 1.1rem;
      border-radius: 7px;
      font-size: 0.76rem;
      font-weight: 500;
      box-shadow: 0 10px 25px rgba(0, 0, 0, 0.4);
      transform: translateY(12px);
      opacity: 0;
      transition: all 140ms var(--ease-out);
      z-index: 2000;
      pointer-events: none;
    }}
    .toast.show {{
      transform: translateY(0);
      opacity: 1;
    }}

    @keyframes fadeIn {{
      from {{ opacity: 0; transform: translateY(-4px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="brand">
        <svg class="brand-mark" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <rect x="2" y="4" width="20" height="16" rx="2"/>
          <path d="M6 12h12"/>
          <path d="M6 8h12"/>
        </svg>
        <span class="brand-title">Brother DS-940DW</span>
      </div>
      <div class="header-right">
        <div class="status-badge">
          <span id="status-dot" class="status-dot offline"></span>
          <span id="status-label">Standby</span>
        </div>
        <button class="icon-action" onclick="refreshAll()" title="Refresh status and documents">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="23 4 23 10 17 10"/>
            <polyline points="1 20 1 14 7 14"/>
            <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
          </svg>
        </button>
      </div>
    </header>

    <!-- Discreet Hardware Telemetry Strip -->
    <div class="telemetry-strip">
      <div class="telemetry-item">
        <span class="telemetry-label">host</span>
        <span class="telemetry-value" id="scanner-ip">{self.settings.scanner_ip}:8080</span>
      </div>
      <span class="telemetry-sep">/</span>
      <div class="telemetry-item">
        <span class="telemetry-label">state</span>
        <span class="telemetry-value" id="scanner-state">Connecting</span>
      </div>
      <span class="telemetry-sep">/</span>
      <div class="telemetry-item">
        <span class="telemetry-label">uptime</span>
        <span class="telemetry-value" id="uptime">--</span>
      </div>
      <span class="telemetry-sep">/</span>
      <div class="telemetry-item">
        <span class="telemetry-label">rss</span>
        <span class="telemetry-value" id="ram">-- MB</span>
      </div>
      <span class="telemetry-sep">/</span>
      <div class="telemetry-item">
        <span class="telemetry-label">storage</span>
        <span class="telemetry-value" id="path">{self.settings.output}</span>
        <button class="path-copy" id="copy-btn" onclick="copyPath()" title="Copy folder path">
          <svg id="copy-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
            <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
          </svg>
        </button>
      </div>
    </div>

    <!-- Collation Notification Banner -->
    <div id="collation-banner" class="collation-banner">
      <span class="collation-msg" id="collation-msg">Multi-sheet batch active. Insert next sheet...</span>
      <span class="collation-timer" id="collation-time">8s</span>
      <div id="collation-bar" class="collation-progress" style="width: 100%;"></div>
    </div>

    <!-- Toolbar: Title & Search -->
    <div class="archive-toolbar">
      <div class="toolbar-title-wrap">
        <span class="toolbar-title">Recent Scans</span>
        <span class="toolbar-count" id="scan-count">0</span>
      </div>
      <div class="search-box-wrap">
        <svg class="search-icon-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="11" cy="11" r="8"/>
          <line x1="21" y1="21" x2="16.65" y2="16.65"/>
        </svg>
        <input type="text" id="search-box" class="search-input" placeholder="Search archive..." oninput="filterScans()">
        <kbd class="kbd-shortcut">/</kbd>
      </div>
    </div>

    <!-- Category Tabs -->
    <div class="filter-tabs" id="category-filters">
      <button class="filter-tab active" onclick="setCategoryFilter('all', this)">All</button>
    </div>

    <!-- Document Ledger Table -->
    <div class="ledger-container">
      <table>
        <thead>
          <tr>
            <th style="width: 48%;">Document</th>
            <th style="width: 16%;">Category</th>
            <th style="width: 13%;" title="Extracted document date">Doc Date</th>
            <th style="width: 13%;" title="Timestamp physically scanned">Scanned</th>
            <th style="width: 10%; text-align: right;">Size</th>
            <th class="actions-col" style="width: 10px;"></th>
          </tr>
        </thead>
        <tbody id="scans-table">
          <tr><td colspan="6" class="empty-state">Loading documents...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- macOS Style QuickLook Modal -->
  <div id="preview-modal" class="preview-backdrop" onclick="closePreview(event)">
    <div class="preview-modal" onclick="event.stopPropagation()">
      <div class="preview-header">
        <div class="preview-left">
          <span class="preview-title" id="preview-filename">Document Preview</span>
          <span class="preview-pill" id="preview-doc-date" style="display:none;"></span>
          <span class="preview-pill" id="preview-scan-date" style="display:none;"></span>
        </div>
        <div class="preview-right">
          <a class="icon-action" id="preview-download" href="#" download="" title="Download PDF">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
              <polyline points="7 10 12 15 17 10"/>
              <line x1="12" y1="15" x2="12" y2="3"/>
            </svg>
          </a>
          <button class="icon-action" onclick="closePreview()" title="Close (Esc or Space)">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="18" y1="6" x2="6" y2="18"/>
              <line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>
      </div>
      <iframe class="preview-frame" id="preview-iframe" src="about:blank"></iframe>
    </div>
  </div>

  <div id="toast" class="toast">Action completed</div>

  <script>
    let allScans = [];
    let currentCategory = 'all';
    let selectedIndex = -1;

    function showToast(msg) {{
      const t = document.getElementById('toast');
      t.textContent = msg;
      t.classList.add('show');
      setTimeout(() => t.classList.remove('show'), 2200);
    }}

    function copyPath() {{
      const p = document.getElementById('path').textContent;
      navigator.clipboard.writeText(p);
      const icon = document.getElementById('copy-icon');
      if (icon) {{
        icon.innerHTML = '<polyline points="20 6 9 17 4 12"/>';
        setTimeout(() => {{
          icon.innerHTML = '<rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>';
        }}, 1600);
      }}
      showToast('Path copied to clipboard');
    }}

    function getCategoryBadge(cat) {{
      const c = (cat || 'Inbox').trim();
      const lower = c.toLowerCase();
      let bg = 'rgba(255, 255, 255, 0.04)';
      let text = 'var(--text-secondary)';
      let border = 'rgba(255, 255, 255, 0.07)';

      if (lower.includes('legal') || lower.includes('citation')) {{
        bg = 'rgba(99, 102, 241, 0.08)';
        text = '#a5b4fc';
        border = 'rgba(99, 102, 241, 0.18)';
      }} else if (lower.includes('auto') || lower.includes('vehicle')) {{
        bg = 'rgba(245, 158, 11, 0.08)';
        text = '#fcd34d';
        border = 'rgba(245, 158, 11, 0.18)';
      }} else if (lower.includes('health') || lower.includes('medical')) {{
        bg = 'rgba(20, 184, 166, 0.08)';
        text = '#5eead4';
        border = 'rgba(20, 184, 166, 0.18)';
      }} else if (lower.includes('bank') || lower.includes('finance')) {{
        bg = 'rgba(16, 185, 129, 0.08)';
        text = '#6ee7b7';
        border = 'rgba(16, 185, 129, 0.18)';
      }} else if (lower.includes('tax')) {{
        bg = 'rgba(244, 63, 94, 0.08)';
        text = '#fda4af';
        border = 'rgba(244, 63, 94, 0.18)';
      }} else if (lower.includes('utilit')) {{
        bg = 'rgba(14, 165, 233, 0.08)';
        text = '#7dd3fc';
        border = 'rgba(14, 165, 233, 0.18)';
      }}
      return `<span class="cat-badge" style="background:${{bg}};color:${{text}};border:1px solid ${{border}};">${{c}}</span>`;
    }}

    function parseDisplayTitle(filename) {{
      const base = filename.endsWith('.pdf') ? filename.slice(0, -4) : filename;
      const dateMatch = base.match(/^\\d{{4}}-\\d{{2}}-\\d{{2}}_(.*)$/);
      if (dateMatch) {{
        return dateMatch[1].replace(/_/g, ' · ').replace(/-/g, ' ');
      }}
      return base.replace(/_/g, ' · ').replace(/-/g, ' ');
    }}

    function updateCategoryFilters() {{
      const counts = {{}};
      allScans.forEach(s => {{
        const cat = s.category || 'Inbox';
        counts[cat] = (counts[cat] || 0) + 1;
      }});

      const filterBar = document.getElementById('category-filters');
      const cats = Object.keys(counts).sort();

      let html = `<button class="filter-tab ${{currentCategory === 'all' ? 'active' : ''}}" onclick="setCategoryFilter('all', this)">All <span class="filter-tab-count">${{allScans.length}}</span></button>`;

      cats.forEach(cat => {{
        const isActive = currentCategory.toLowerCase() === cat.toLowerCase();
        const escCat = cat.replace(/'/g, "\\'");
        html += `<button class="filter-tab ${{isActive ? 'active' : ''}}" onclick="setCategoryFilter('${{escCat}}', this)">${{cat}} <span class="filter-tab-count">${{counts[cat]}}</span></button>`;
      }});

      filterBar.innerHTML = html;
    }}

    function openPreview(relPath, filename, docDate, scanDate) {{
      const modal = document.getElementById('preview-modal');
      const frame = document.getElementById('preview-iframe');
      const title = document.getElementById('preview-filename');
      const docDateEl = document.getElementById('preview-doc-date');
      const scanDateEl = document.getElementById('preview-scan-date');
      const dlBtn = document.getElementById('preview-download');

      title.textContent = filename;
      if (docDate) {{
        docDateEl.textContent = 'Doc: ' + docDate;
        docDateEl.style.display = 'inline-block';
      }} else {{
        docDateEl.style.display = 'none';
      }}

      if (scanDate) {{
        scanDateEl.textContent = 'Scanned: ' + scanDate;
        scanDateEl.style.display = 'inline-block';
      }} else {{
        scanDateEl.style.display = 'none';
      }}

      const fileUrl = '/files/' + encodeURIComponent(relPath);
      dlBtn.href = fileUrl;
      dlBtn.download = filename;
      frame.src = fileUrl;
      modal.classList.add('open');
    }}

    function closePreview() {{
      const modal = document.getElementById('preview-modal');
      const frame = document.getElementById('preview-iframe');
      modal.classList.remove('open');
      frame.src = 'about:blank';
    }}

    function isPreviewOpen() {{
      return document.getElementById('preview-modal').classList.contains('open');
    }}

    /* Desktop-grade Keyboard Interaction (Spacebar QuickLook, Arrow Keys, / search) */
    document.addEventListener('keydown', e => {{
      const isInput = document.activeElement.tagName === 'INPUT';
      
      if (e.key === 'Escape') {{
        if (isPreviewOpen()) {{
          closePreview();
        }} else if (isInput) {{
          document.activeElement.blur();
        }}
      }} else if (e.key === ' ' && !isInput) {{
        e.preventDefault();
        if (isPreviewOpen()) {{
          closePreview();
        }} else {{
          const filtered = getFilteredScans();
          const target = selectedIndex >= 0 && selectedIndex < filtered.length ? filtered[selectedIndex] : filtered[0];
          if (target) {{
            openPreview(target.relative_path, target.filename, target.doc_date, target.scan_date);
          }}
        }}
      }} else if (e.key === '/' && !isInput) {{
        e.preventDefault();
        document.getElementById('search-box').focus();
      }} else if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && !isInput && !isPreviewOpen()) {{
        e.preventDefault();
        const filtered = getFilteredScans();
        if (!filtered.length) return;
        if (e.key === 'ArrowDown') {{
          selectedIndex = Math.min(filtered.length - 1, selectedIndex + 1);
        }} else {{
          selectedIndex = Math.max(0, selectedIndex - 1);
        }}
        highlightSelectedRow();
      }}
    }});

    function highlightSelectedRow() {{
      const rows = document.querySelectorAll('#scans-table tr');
      rows.forEach((r, idx) => {{
        if (idx === selectedIndex) {{
          r.classList.add('selected');
          r.scrollIntoView({{ block: 'nearest', behavior: 'smooth' }});
        }} else {{
          r.classList.remove('selected');
        }}
      }});
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

    function refreshAll() {{
      updateStatus();
      loadScans();
      showToast('Archive refreshed');
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

    function renderScans() {{
      const filtered = getFilteredScans();
      const tbody = document.getElementById('scans-table');

      document.getElementById('scan-count').textContent = filtered.length;

      if (filtered.length === 0) {{
        tbody.innerHTML = `
          <tr>
            <td colspan="6" class="empty-state">
              <svg class="empty-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                <polyline points="14 2 14 8 20 8"/>
              </svg>
              <div class="empty-headline">No matching documents</div>
              <div class="empty-instruction">Feed a sheet into the scanner to capture, OCR, and organize.</div>
            </td>
          </tr>
        `;
        return;
      }}

      tbody.innerHTML = filtered.map((s, idx) => {{
        const headline = parseDisplayTitle(s.filename);
        const escRel = s.relative_path.replace(/'/g, "\\'");
        const escName = s.filename.replace(/'/g, "\\'");
        const escDocDate = (s.doc_date || '').replace(/'/g, "\\'");
        const escScanDate = (s.scan_date || '').replace(/'/g, "\\'");
        const isSelected = idx === selectedIndex ? 'selected' : '';

        return `
          <tr class="${{isSelected}}" onclick="openPreview('${{escRel}}', '${{escName}}', '${{escDocDate}}', '${{escScanDate}}')">
            <td>
              <div class="doc-cell">
                <svg class="doc-glyph" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                  <polyline points="14 2 14 8 20 8"/>
                </svg>
                <div class="doc-info">
                  <span class="doc-headline">${{headline}}</span>
                  <span class="doc-subline">${{s.filename}}</span>
                </div>
              </div>
            </td>
            <td>${{getCategoryBadge(s.category)}}</td>
            <td><span class="date-val doc-date">${{s.doc_date || '<span style="color:var(--text-muted);opacity:0.4;">—</span>'}}</span></td>
            <td><span class="scan-date-val">${{s.scan_date || '—'}}</span></td>
            <td style="text-align: right;"><span class="size-val">${{s.size_kb}} KB</span></td>
            <td class="actions-col" onclick="event.stopPropagation()">
              <button class="row-btn row-delete" onclick="confirmDelete('${{escRel}}', '${{escName}}')" title="Delete document">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <polyline points="3 6 5 6 21 6"/>
                  <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
                </svg>
              </button>
            </td>
          </tr>
        `;
      }}).join('');
    }}

    async function updateStatus() {{
      try {{
        const res = await fetch('/api/status');
        const data = await res.json();
        
        const dot = document.getElementById('status-dot');
        const label = document.getElementById('status-label');
        const stateEl = document.getElementById('scanner-state');
        const collationBanner = document.getElementById('collation-banner');
        const collationBar = document.getElementById('collation-bar');

        dot.className = 'status-dot';
        if (data.state === 'waiting_for_paper') {{
          dot.classList.add('online');
          label.textContent = 'Ready for paper';
          stateEl.textContent = 'Ready (Idle)';
          collationBanner.classList.remove('visible');
        }} else if (data.state === 'scanning') {{
          dot.classList.add('active');
          label.textContent = 'Scanning...';
          stateEl.textContent = 'Scanning Duplex';
          collationBanner.classList.remove('visible');
        }} else if (data.state === 'waiting_for_next_sheet') {{
          dot.classList.add('active');
          label.textContent = `Batch (${{data.timeout_remaining || ''}}s)`;
          stateEl.textContent = `Collation (${{data.timeout_remaining || ''}}s)`;
          
          collationBanner.classList.add('visible');
          document.getElementById('collation-msg').textContent = `Sheet ${{data.sheet || 1}} captured (${{data.pages_so_far || 2}} pages). Insert sheet ${{ (data.sheet || 1) + 1 }} to append...`;
          document.getElementById('collation-time').textContent = `${{data.timeout_remaining || 0}}s remaining`;
          const pct = Math.max(0, Math.min(100, ((data.timeout_remaining || 0) / 8.0) * 100));
          collationBar.style.width = pct + '%';
        }} else {{
          dot.classList.add('offline');
          label.textContent = data.state === 'scanner_offline' ? 'Offline (Sleep)' : data.state.replace(/_/g, ' ');
          stateEl.textContent = data.state === 'scanner_offline' ? 'Standby (Sleep)' : data.state;
          collationBanner.classList.remove('visible');
        }}

        document.getElementById('uptime').textContent = data.uptime;
        document.getElementById('ram').textContent = data.memory_mb + ' MB';
      }} catch (e) {{
        console.error(e);
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
          await loadScans();
        }} else {{
          showToast('Failed to delete: ' + (data.message || 'Error'));
        }}
      }} catch (e) {{
        showToast('Delete error: ' + e);
      }}
    }}

    updateStatus();
    loadScans();
    setInterval(updateStatus, 3000);
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
