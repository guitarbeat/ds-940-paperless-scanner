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
      --bg: #0b0c0f;
      --surface: #13151a;
      --surface-subtle: #171920;
      --surface-hover: #1b1e26;
      --surface-elevated: #222631;
      --border: rgba(255, 255, 255, 0.075);
      --border-subtle: rgba(255, 255, 255, 0.04);
      --border-focus: rgba(255, 255, 255, 0.25);
      --text: #f2f4f7;
      --text-secondary: #9aa1ad;
      --text-muted: #5e6573;
      --accent: #f2f4f7;
      --accent-subtle: rgba(255, 255, 255, 0.06);
      --accent-btn-text: #0b0c0f;
      --status-ready: #34c759;
      --status-active: #f59e0b;
      --status-standby: #64748b;
      --status-danger: #ef4444;
      --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    ::selection {{ background: rgba(255, 255, 255, 0.14); color: #fff; }}
    ::-webkit-scrollbar {{ width: 6px; height: 6px; }}
    ::-webkit-scrollbar-track {{ background: transparent; }}
    ::-webkit-scrollbar-thumb {{ background: rgba(255, 255, 255, 0.1); border-radius: 3px; }}
    ::-webkit-scrollbar-thumb:hover {{ background: rgba(255, 255, 255, 0.2); }}

    body {{
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display", "Inter", system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding: 3rem 1.5rem 4rem;
      font-variant-numeric: tabular-nums;
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
      letter-spacing: -0.011em;
    }}
    .container {{ max-width: 1040px; margin: 0 auto; }}

    /* Header */
    header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 2rem;
      padding-bottom: 1.5rem;
      border-bottom: 1px solid var(--border-subtle);
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 0.85rem;
    }}
    .brand-icon {{
      width: 28px;
      height: 28px;
      padding: 5px;
      background: var(--surface-subtle);
      border: 1px solid var(--border);
      border-radius: 7px;
      color: var(--text-secondary);
      flex-shrink: 0;
    }}
    .brand-text {{
      display: flex;
      align-items: baseline;
      gap: 0.65rem;
      flex-wrap: wrap;
    }}
    .brand-title {{
      font-size: 1.125rem;
      font-weight: 600;
      letter-spacing: -0.02em;
      color: var(--text);
    }}
    .brand-badge {{
      font-size: 0.7rem;
      font-weight: 500;
      color: var(--text-muted);
      letter-spacing: 0.02em;
    }}

    /* Header Right: Status + Controls */
    .header-actions {{
      display: flex;
      align-items: center;
      gap: 0.6rem;
    }}
    .status-pill {{
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      font-size: 0.78rem;
      font-weight: 500;
      padding: 0.35rem 0.8rem;
      border-radius: 9999px;
      background: var(--surface);
      border: 1px solid var(--border);
      color: var(--text-secondary);
      transition: all 180ms var(--ease-out);
    }}
    .status-dot {{
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--status-standby);
      transition: background-color 200ms ease, box-shadow 200ms ease;
    }}
    .status-dot.online {{ background: var(--status-ready); box-shadow: 0 0 8px rgba(52, 199, 89, 0.4); }}
    .status-dot.active {{ background: var(--status-active); box-shadow: 0 0 8px rgba(245, 158, 11, 0.5); animation: pulse 1.6s infinite ease-in-out; }}
    .status-dot.offline {{ background: var(--status-standby); }}

    @keyframes pulse {{
      0%, 100% {{ opacity: 1; transform: scale(1); }}
      50% {{ opacity: 0.4; transform: scale(0.9); }}
    }}

    .icon-btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 30px;
      height: 30px;
      border-radius: 7px;
      background: var(--surface);
      border: 1px solid var(--border);
      color: var(--text-muted);
      cursor: pointer;
      transition: all 150ms var(--ease-out);
    }}
    .icon-btn:hover {{ color: var(--text); border-color: rgba(255, 255, 255, 0.15); background: var(--surface-hover); }}
    .icon-btn:active {{ transform: scale(0.96); }}

    /* Collation Banner */
    .collation-banner {{
      display: none;
      background: rgba(245, 158, 11, 0.08);
      border: 1px solid rgba(245, 158, 11, 0.22);
      border-radius: 9px;
      padding: 0.85rem 1.25rem;
      margin-bottom: 1.75rem;
      position: relative;
      overflow: hidden;
      animation: fadeIn 200ms var(--ease-out);
    }}
    .collation-banner.visible {{ display: flex; align-items: center; justify-content: space-between; }}
    .collation-info {{ display: flex; align-items: center; gap: 0.75rem; }}
    .collation-icon {{ width: 17px; height: 17px; color: var(--status-active); flex-shrink: 0; }}
    .collation-text {{ font-size: 0.82rem; font-weight: 500; color: #fbbf24; }}
    .collation-timer {{ font-size: 0.8rem; font-weight: 600; font-family: ui-monospace, Menlo, monospace; color: var(--text); }}
    .collation-bar {{
      position: absolute;
      bottom: 0;
      left: 0;
      height: 2px;
      background: var(--status-active);
      transition: width 1s linear;
    }}

    /* Hardware Telemetry Strip (Precision Instrument Look) */
    .telemetry-card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 1.1rem 1.4rem;
      margin-bottom: 2rem;
      display: grid;
      grid-template-columns: 1.2fr 1fr 1fr 1.8fr;
      gap: 1.25rem;
      box-shadow: 0 2px 12px rgba(0, 0, 0, 0.2);
    }}
    @media (max-width: 820px) {{
      .telemetry-card {{ grid-template-columns: 1fr 1fr; gap: 1rem; }}
    }}
    .metric-item {{ display: flex; flex-direction: column; gap: 0.2rem; }}
    .metric-label {{ font-size: 0.68rem; color: var(--text-muted); font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; }}
    .metric-val {{ font-size: 0.9rem; font-weight: 500; color: var(--text); }}
    .metric-sub {{ font-size: 0.72rem; color: var(--text-muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .path-row {{ display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; }}
    .copy-pill {{
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      background: none;
      border: none;
      color: var(--text-muted);
      cursor: pointer;
      padding: 2px 6px;
      border-radius: 4px;
      transition: all 120ms ease;
    }}
    .copy-pill:hover {{ color: var(--text); background: var(--surface-subtle); }}
    .copy-pill:active {{ transform: scale(0.96); }}

    /* Section Header & Toolbar */
    .toolbar-row {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 0.85rem;
      gap: 1rem;
      flex-wrap: wrap;
    }}
    .section-title {{
      font-size: 0.92rem;
      font-weight: 600;
      color: var(--text);
      display: flex;
      align-items: center;
      gap: 0.55rem;
      letter-spacing: -0.01em;
    }}
    .count-badge {{
      font-size: 0.7rem;
      font-weight: 500;
      color: var(--text-muted);
      background: var(--surface-subtle);
      border: 1px solid var(--border-subtle);
      padding: 0.08rem 0.45rem;
      border-radius: 9999px;
    }}
    .toolbar-actions {{ display: flex; align-items: center; gap: 0.6rem; }}
    
    .search-wrapper {{
      position: relative;
      display: flex;
      align-items: center;
    }}
    .search-input {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 7px;
      padding: 0.45rem 2.2rem 0.45rem 2.1rem;
      font-size: 0.8rem;
      color: var(--text);
      width: 240px;
      outline: none;
      transition: all 150ms var(--ease-out);
    }}
    .search-input:focus {{
      border-color: var(--border-focus);
      background: var(--surface-hover);
      box-shadow: 0 0 0 1px var(--border-focus);
      width: 270px;
    }}
    .search-input::placeholder {{ color: var(--text-muted); }}
    .search-icon {{
      position: absolute;
      left: 0.65rem;
      width: 14px;
      height: 14px;
      color: var(--text-muted);
      pointer-events: none;
    }}
    .search-kbd {{
      position: absolute;
      right: 0.55rem;
      font-size: 0.65rem;
      font-family: ui-monospace, Menlo, monospace;
      color: var(--text-muted);
      background: var(--surface-subtle);
      border: 1px solid var(--border);
      padding: 0.1rem 0.35rem;
      border-radius: 4px;
      pointer-events: none;
    }}

    /* Category Filter Bar */
    .filter-bar {{
      display: flex;
      gap: 0.4rem;
      overflow-x: auto;
      margin-bottom: 1rem;
      padding-bottom: 0.35rem;
    }}
    .filter-pill {{
      font-size: 0.75rem;
      font-weight: 500;
      padding: 0.28rem 0.72rem;
      border-radius: 9999px;
      background: var(--surface);
      border: 1px solid var(--border);
      color: var(--text-secondary);
      cursor: pointer;
      white-space: nowrap;
      transition: all 140ms var(--ease-out);
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
    }}
    .filter-pill:hover {{
      color: var(--text);
      border-color: rgba(255, 255, 255, 0.15);
      background: var(--surface-hover);
      transform: translateY(-1px);
    }}
    .filter-pill:active {{ transform: scale(0.97); }}
    .filter-pill.active {{
      background: var(--accent-subtle);
      color: var(--text);
      border-color: rgba(255, 255, 255, 0.22);
      font-weight: 600;
    }}

    /* Document Archive List */
    .table-card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      overflow: hidden;
      box-shadow: 0 4px 20px rgba(0, 0, 0, 0.25);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.83rem;
      text-align: left;
    }}
    thead th {{
      padding: 0.8rem 1.25rem;
      background: var(--surface-subtle);
      color: var(--text-muted);
      font-weight: 600;
      font-size: 0.68rem;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      border-bottom: 1px solid var(--border);
    }}
    tbody tr {{
      border-bottom: 1px solid var(--border-subtle);
      transition: background 120ms ease;
    }}
    tbody tr:last-child {{ border-bottom: none; }}
    tbody tr:hover {{
      background: var(--surface-hover);
    }}
    td {{
      padding: 0.85rem 1.25rem;
      vertical-align: middle;
    }}
    
    .doc-link {{
      color: var(--text);
      text-decoration: none;
      font-weight: 500;
      display: inline-flex;
      align-items: center;
      gap: 0.6rem;
      cursor: pointer;
      transition: color 120ms ease;
    }}
    .doc-link:hover {{ color: #fff; }}
    .doc-icon {{
      width: 16px;
      height: 16px;
      color: var(--text-muted);
      flex-shrink: 0;
      transition: color 120ms ease;
    }}
    .doc-link:hover .doc-icon {{ color: var(--text-secondary); }}
    .doc-name {{ letter-spacing: -0.01em; }}
    .doc-ext {{ color: var(--text-muted); font-weight: 400; }}

    .cat-pill {{
      display: inline-block;
      font-size: 0.7rem;
      font-weight: 500;
      padding: 0.18rem 0.55rem;
      border-radius: 5px;
      letter-spacing: 0.01em;
      white-space: nowrap;
    }}

    .actions-cell {{ text-align: right; }}
    .action-btn {{
      display: inline-flex;
      align-items: center;
      gap: 0.25rem;
      background: none;
      border: none;
      color: var(--text-muted);
      text-decoration: none;
      font-size: 0.74rem;
      font-weight: 500;
      padding: 0.25rem 0.5rem;
      border-radius: 5px;
      cursor: pointer;
      transition: all 120ms ease;
    }}
    .action-btn:hover {{
      color: var(--text);
      background: var(--surface-subtle);
    }}
    .action-btn:active {{ transform: scale(0.96); }}
    .action-btn.action-delete:hover {{
      color: #ff6961;
      background: rgba(239, 68, 68, 0.08);
    }}

    .empty-state {{
      padding: 4rem 1.5rem;
      text-align: center;
      color: var(--text-muted);
    }}
    .empty-icon {{
      width: 36px;
      height: 36px;
      margin: 0 auto 0.85rem;
      color: var(--text-muted);
      opacity: 0.4;
    }}
    .empty-title {{
      font-size: 0.9rem;
      font-weight: 500;
      color: var(--text-secondary);
      margin-bottom: 0.35rem;
    }}
    .empty-desc {{
      font-size: 0.78rem;
      color: var(--text-muted);
      max-width: 320px;
      margin: 0 auto;
    }}

    /* QuickLook Drawer / Modal */
    .preview-backdrop {{
      position: fixed;
      top: 0;
      left: 0;
      right: 0;
      bottom: 0;
      background: rgba(4, 5, 8, 0.78);
      backdrop-filter: blur(10px);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 1000;
      padding: 1.5rem;
      opacity: 0;
      transition: opacity 160ms var(--ease-out);
    }}
    .preview-backdrop.open {{
      display: flex;
      opacity: 1;
    }}
    .preview-modal {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      width: 100%;
      max-width: 960px;
      height: 86vh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      box-shadow: 0 24px 60px rgba(0, 0, 0, 0.65);
      transform: scale(0.98);
      transition: transform 180ms var(--ease-out);
    }}
    .preview-backdrop.open .preview-modal {{
      transform: scale(1);
    }}
    .preview-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 0.85rem 1.25rem;
      border-bottom: 1px solid var(--border);
      background: var(--surface-subtle);
    }}
    .preview-header-left {{
      display: flex;
      align-items: center;
      gap: 0.65rem;
      overflow: hidden;
    }}
    .preview-title {{
      font-size: 0.88rem;
      font-weight: 600;
      color: var(--text);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      max-width: 650px;
    }}
    .preview-meta-pill {{
      font-size: 0.7rem;
      color: var(--text-muted);
      background: var(--surface);
      border: 1px solid var(--border);
      padding: 0.15rem 0.5rem;
      border-radius: 4px;
      white-space: nowrap;
    }}
    .preview-actions {{
      display: flex;
      align-items: center;
      gap: 0.4rem;
    }}
    .preview-frame {{
      flex: 1;
      width: 100%;
      height: 100%;
      border: none;
      background: #14161b;
    }}

    /* Toast Notification */
    .toast {{
      position: fixed;
      bottom: 2rem;
      right: 2rem;
      background: var(--surface-elevated);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 0.7rem 1.2rem;
      border-radius: 8px;
      font-size: 0.8rem;
      font-weight: 500;
      box-shadow: 0 12px 30px rgba(0, 0, 0, 0.45);
      transform: translateY(20px);
      opacity: 0;
      transition: all 180ms var(--ease-out);
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
        <svg class="brand-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <rect x="2" y="4" width="20" height="16" rx="2"/>
          <path d="M6 12h12"/>
          <path d="M6 8h12"/>
        </svg>
        <div class="brand-text">
          <span class="brand-title">Brother DS-940DW</span>
          <span class="brand-badge">AirScan • Duplex</span>
        </div>
      </div>
      <div class="header-actions">
        <div class="status-pill">
          <span id="status-dot" class="status-dot offline"></span>
          <span id="status-label">Standby</span>
        </div>
        <button class="icon-btn" onclick="refreshAll()" title="Refresh status and documents">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="23 4 23 10 17 10"/>
            <polyline points="1 20 1 14 7 14"/>
            <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
          </svg>
        </button>
      </div>
    </header>

    <div id="collation-banner" class="collation-banner">
      <div class="collation-info">
        <svg class="collation-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="10"/>
          <polyline points="12 6 12 12 16 14"/>
        </svg>
        <span class="collation-text" id="collation-msg">Multi-sheet batch active. Insert next sheet...</span>
      </div>
      <span class="collation-timer" id="collation-time">8s remaining</span>
      <div id="collation-bar" class="collation-bar" style="width: 100%;"></div>
    </div>

    <div class="telemetry-card">
      <div class="metric-item">
        <span class="metric-label">Scanner</span>
        <span class="metric-val" id="scanner-state">Connecting</span>
        <span class="metric-sub" id="scanner-ip">{self.settings.scanner_ip}:8080</span>
      </div>
      <div class="metric-item">
        <span class="metric-label">Uptime</span>
        <span class="metric-val" id="uptime">--</span>
      </div>
      <div class="metric-item">
        <span class="metric-label">Memory RSS</span>
        <span class="metric-val" id="ram">-- MB</span>
      </div>
      <div class="metric-item">
        <span class="metric-label">Destination</span>
        <div class="path-row">
          <span class="metric-val" style="font-size: 0.82rem; font-family: ui-monospace, Menlo, monospace;" id="path">{self.settings.output}</span>
          <button class="copy-pill" id="copy-btn" onclick="copyPath()" title="Copy folder path">
            <svg id="copy-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
              <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
            </svg>
          </button>
        </div>
      </div>
    </div>

    <div class="toolbar-row">
      <div class="section-title">
        <span>Recent Scans</span>
        <span class="count-badge" id="scan-count">0</span>
      </div>
      <div class="toolbar-actions">
        <div class="search-wrapper">
          <svg class="search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="11" cy="11" r="8"/>
            <line x1="21" y1="21" x2="16.65" y2="16.65"/>
          </svg>
          <input type="text" id="search-box" class="search-input" placeholder="Search documents..." oninput="filterScans()">
          <kbd class="search-kbd">/</kbd>
        </div>
      </div>
    </div>

    <div class="filter-bar" id="category-filters">
      <span class="filter-pill active" onclick="setCategoryFilter('all', this)">All (0)</span>
    </div>

    <div class="table-card">
      <table>
        <thead>
          <tr>
            <th>Document</th>
            <th>Category</th>
            <th title="Date extracted from document content">Doc Date</th>
            <th title="Timestamp physically scanned">Scanned</th>
            <th>Size</th>
            <th class="actions-cell">Actions</th>
          </tr>
        </thead>
        <tbody id="scans-table">
          <tr><td colspan="6" class="empty-state">Loading documents...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <div id="preview-modal" class="preview-backdrop" onclick="closePreview(event)">
    <div class="preview-modal" onclick="event.stopPropagation()">
      <div class="preview-header">
        <div class="preview-header-left">
          <span class="preview-title" id="preview-filename">Document Preview</span>
          <span class="preview-meta-pill" id="preview-doc-date" style="display:none;"></span>
          <span class="preview-meta-pill" id="preview-scan-date" style="display:none;"></span>
        </div>
        <div class="preview-actions">
          <a class="icon-btn" id="preview-download" href="#" download="" title="Download PDF">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
              <polyline points="7 10 12 15 17 10"/>
              <line x1="12" y1="15" x2="12" y2="3"/>
            </svg>
          </a>
          <button class="icon-btn" onclick="closePreview()" title="Close (Esc)">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
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

    function showToast(msg) {{
      const t = document.getElementById('toast');
      t.textContent = msg;
      t.classList.add('show');
      setTimeout(() => t.classList.remove('show'), 2800);
    }}

    function copyPath() {{
      const p = document.getElementById('path').textContent;
      navigator.clipboard.writeText(p);
      const icon = document.getElementById('copy-icon');
      if (icon) {{
        icon.innerHTML = '<polyline points="20 6 9 17 4 12"/>';
        setTimeout(() => {{
          icon.innerHTML = '<rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>';
        }}, 1800);
      }}
      showToast('Path copied to clipboard');
    }}

    function getCategoryBadge(cat) {{
      const c = (cat || 'Inbox').trim();
      const lower = c.toLowerCase();
      let bg = 'rgba(255, 255, 255, 0.05)';
      let text = 'var(--text-secondary)';
      let border = 'rgba(255, 255, 255, 0.08)';

      if (lower.includes('legal') || lower.includes('citation')) {{
        bg = 'rgba(99, 102, 241, 0.12)';
        text = '#a5b4fc';
        border = 'rgba(99, 102, 241, 0.22)';
      }} else if (lower.includes('auto') || lower.includes('vehicle')) {{
        bg = 'rgba(245, 158, 11, 0.12)';
        text = '#fcd34d';
        border = 'rgba(245, 158, 11, 0.22)';
      }} else if (lower.includes('health') || lower.includes('medical')) {{
        bg = 'rgba(20, 184, 166, 0.12)';
        text = '#5eead4';
        border = 'rgba(20, 184, 166, 0.22)';
      }} else if (lower.includes('bank') || lower.includes('finance')) {{
        bg = 'rgba(16, 185, 129, 0.12)';
        text = '#6ee7b7';
        border = 'rgba(16, 185, 129, 0.22)';
      }} else if (lower.includes('tax')) {{
        bg = 'rgba(244, 63, 94, 0.12)';
        text = '#fda4af';
        border = 'rgba(244, 63, 94, 0.22)';
      }} else if (lower.includes('utilit')) {{
        bg = 'rgba(14, 165, 233, 0.12)';
        text = '#7dd3fc';
        border = 'rgba(14, 165, 233, 0.22)';
      }}
      return `<span class="cat-pill" style="background:${{bg}};color:${{text}};border:1px solid ${{border}};">${{c}}</span>`;
    }}

    function updateCategoryFilters() {{
      const counts = {{}};
      allScans.forEach(s => {{
        const cat = s.category || 'Inbox';
        counts[cat] = (counts[cat] || 0) + 1;
      }});

      const filterBar = document.getElementById('category-filters');
      const cats = Object.keys(counts).sort();

      let html = `<span class="filter-pill ${{currentCategory === 'all' ? 'active' : ''}}" onclick="setCategoryFilter('all', this)">All (${{allScans.length}})</span>`;

      cats.forEach(cat => {{
        const isActive = currentCategory.toLowerCase() === cat.toLowerCase();
        const escCat = cat.replace(/'/g, "\\'");
        html += `<span class="filter-pill ${{isActive ? 'active' : ''}}" onclick="setCategoryFilter('${{escCat}}', this)">${{cat}} (${{counts[cat]}})</span>`;
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

    function closePreview(e) {{
      const modal = document.getElementById('preview-modal');
      const frame = document.getElementById('preview-iframe');
      modal.classList.remove('open');
      frame.src = 'about:blank';
    }}

    document.addEventListener('keydown', e => {{
      if (e.key === 'Escape') closePreview();
      if (e.key === '/' && document.activeElement.tagName !== 'INPUT') {{
        e.preventDefault();
        document.getElementById('search-box').focus();
      }}
    }});

    function setCategoryFilter(cat, el) {{
      currentCategory = cat;
      updateCategoryFilters();
      renderScans();
    }}

    function filterScans() {{
      renderScans();
    }}

    function refreshAll() {{
      updateStatus();
      loadScans();
      showToast('Refreshed');
    }}

    function renderScans() {{
      const query = document.getElementById('search-box').value.toLowerCase().trim();
      const tbody = document.getElementById('scans-table');

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

      document.getElementById('scan-count').textContent = filtered.length;

      if (filtered.length === 0) {{
        tbody.innerHTML = `
          <tr>
            <td colspan="6" class="empty-state">
              <svg class="empty-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                <polyline points="14 2 14 8 20 8"/>
              </svg>
              <div class="empty-title">No documents found</div>
              <div class="empty-desc">Feed a sheet into the Brother scanner to capture, OCR, and organize.</div>
            </td>
          </tr>
        `;
        return;
      }}

      tbody.innerHTML = filtered.map(s => {{
        const baseName = s.filename.endsWith('.pdf') ? s.filename.slice(0, -4) : s.filename;
        const escRel = s.relative_path.replace(/'/g, "\\'");
        const escName = s.filename.replace(/'/g, "\\'");
        const escDocDate = (s.doc_date || '').replace(/'/g, "\\'");
        const escScanDate = (s.scan_date || '').replace(/'/g, "\\'");

        return `
          <tr>
            <td>
              <a class="doc-link" onclick="openPreview('${{escRel}}', '${{escName}}', '${{escDocDate}}', '${{escScanDate}}')">
                <svg class="doc-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                  <polyline points="14 2 14 8 20 8"/>
                </svg>
                <span class="doc-name">${{baseName}}<span class="doc-ext">.pdf</span></span>
              </a>
            </td>
            <td>${{getCategoryBadge(s.category)}}</td>
            <td style="color: var(--text-secondary); font-variant-numeric: tabular-nums;">${{s.doc_date ? `<span style="font-weight: 500; color: var(--text);">${{s.doc_date}}</span>` : '<span style="color: var(--text-muted); opacity: 0.4;">—</span>'}}</td>
            <td style="color: var(--text-muted); font-variant-numeric: tabular-nums; font-size: 0.78rem;">${{s.scan_date || '—'}}</td>
            <td style="color: var(--text-muted); font-size: 0.78rem;">${{s.size_kb}} KB</td>
            <td class="actions-cell">
              <button class="action-btn" onclick="openPreview('${{escRel}}', '${{escName}}', '${{escDocDate}}', '${{escScanDate}}')">Preview</button>
              <a class="action-btn" href="/files/${{encodeURIComponent(s.relative_path)}}" download="${{s.filename}}">Download</a>
              <button class="action-btn action-delete" onclick="confirmDelete('${{escRel}}', '${{escName}}')">Delete</button>
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
