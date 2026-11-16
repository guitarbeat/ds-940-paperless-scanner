"""Lightweight monitoring and management web dashboard for DS-940DW scanner."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
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

    files = []
    for root, _, filenames in os.walk(output_dir):
        for fname in filenames:
            if fname.lower().endswith(".pdf") and not fname.startswith("."):
                full_path = Path(root) / fname
                try:
                    stat = full_path.stat()
                    rel_path = full_path.relative_to(output_dir)
                    category = rel_path.parent.name if rel_path.parent.name else "Inbox"
                    files.append({
                        "filename": fname,
                        "relative_path": str(rel_path),
                        "category": category,
                        "size_bytes": stat.st_size,
                        "size_kb": round(stat.st_size / 1024, 1),
                        "modified": stat.st_mtime,
                        "date_str": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)),
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
      --bg: #090a0d;
      --surface: #121418;
      --surface-subtle: #171920;
      --surface-elevated: #1d2028;
      --border: #222630;
      --border-subtle: #181b22;
      --text: #f0f2f5;
      --text-secondary: #8a919e;
      --text-muted: #565d6c;
      --accent: #f0f2f5;
      --accent-btn-text: #090a0d;
      --status-green: #30d158;
      --status-amber: #ff9f0a;
      --status-muted: #626875;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    ::selection {{ background: rgba(255, 255, 255, 0.15); color: #fff; }}
    ::-webkit-scrollbar {{ width: 8px; height: 8px; }}
    ::-webkit-scrollbar-track {{ background: var(--bg); }}
    ::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 4px; }}
    ::-webkit-scrollbar-thumb:hover {{ background: var(--text-muted); }}

    body {{
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display", system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding: 2.5rem 1.5rem;
      font-variant-numeric: tabular-nums;
      -webkit-font-smoothing: antialiased;
    }}
    .container {{ max-width: 1040px; margin: 0 auto; }}

    /* Header */
    header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 1.75rem;
      padding-bottom: 1.25rem;
      border-bottom: 1px solid var(--border-subtle);
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }}
    .brand-title {{
      font-size: 1.15rem;
      font-weight: 600;
      letter-spacing: -0.015em;
    }}

    /* Status Indicator */
    .status-pill {{
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      font-size: 0.8rem;
      font-weight: 500;
      padding: 0.35rem 0.75rem;
      border-radius: 9999px;
      background: var(--surface);
      border: 1px solid var(--border);
      color: var(--text-secondary);
      transition: border-color 200ms ease;
    }}
    .status-dot {{
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--status-muted);
      transition: background-color 200ms ease, box-shadow 200ms ease;
    }}
    .status-dot.online {{ background: var(--status-green); box-shadow: 0 0 8px rgba(48, 209, 88, 0.4); }}
    .status-dot.active {{ background: var(--status-amber); box-shadow: 0 0 8px rgba(255, 159, 10, 0.5); animation: pulse 1.5s infinite ease-in-out; }}
    .status-dot.offline {{ background: var(--status-muted); }}

    @keyframes pulse {{
      0%, 100% {{ opacity: 1; }}
      50% {{ opacity: 0.35; }}
    }}

    /* Collation Banner */
    .collation-banner {{
      display: none;
      background: rgba(255, 159, 10, 0.08);
      border: 1px solid rgba(255, 159, 10, 0.25);
      border-radius: 8px;
      padding: 0.85rem 1.25rem;
      margin-bottom: 1.5rem;
      position: relative;
      overflow: hidden;
    }}
    .collation-banner.visible {{ display: flex; align-items: center; justify-content: space-between; }}
    .collation-info {{ display: flex; align-items: center; gap: 0.75rem; }}
    .collation-icon {{ width: 18px; height: 18px; color: var(--status-amber); flex-shrink: 0; }}
    .collation-text {{ font-size: 0.85rem; font-weight: 500; color: #ffbe53; }}
    .collation-timer {{ font-size: 0.82rem; font-weight: 600; font-family: ui-monospace, Menlo, monospace; color: var(--text); }}
    .collation-bar {{
      position: absolute;
      bottom: 0;
      left: 0;
      height: 2px;
      background: var(--status-amber);
      transition: width 1s linear;
    }}

    /* Hardware Telemetry Strip */
    .telemetry-card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 1.15rem 1.4rem;
      margin-bottom: 1.75rem;
      display: grid;
      grid-template-columns: 1.3fr 1fr 1fr 1.7fr;
      gap: 1.25rem;
    }}
    @media (max-width: 768px) {{
      .telemetry-card {{ grid-template-columns: 1fr 1fr; gap: 1rem; }}
    }}
    .metric-item {{ display: flex; flex-direction: column; gap: 0.2rem; }}
    .metric-label {{ font-size: 0.72rem; color: var(--text-secondary); font-weight: 500; letter-spacing: 0.01em; }}
    .metric-val {{ font-size: 0.92rem; font-weight: 600; color: var(--text); }}
    .metric-sub {{ font-size: 0.72rem; color: var(--text-muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .path-row {{ display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; }}
    .copy-btn {{
      background: none;
      border: none;
      color: var(--text-muted);
      cursor: pointer;
      padding: 2px;
      border-radius: 3px;
      transition: color 150ms ease;
    }}
    .copy-btn:hover {{ color: var(--text); }}

    /* Section & Toolbar */
    .toolbar-row {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 1rem;
      gap: 1rem;
      flex-wrap: wrap;
    }}
    .section-title {{
      font-size: 0.95rem;
      font-weight: 600;
      color: var(--text);
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }}
    .count-badge {{
      font-size: 0.72rem;
      font-weight: 500;
      color: var(--text-muted);
      background: var(--surface-subtle);
      border: 1px solid var(--border-subtle);
      padding: 0.1rem 0.45rem;
      border-radius: 4px;
    }}
    .toolbar-actions {{ display: flex; align-items: center; gap: 0.6rem; }}
    
    .search-input {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 0.4rem 0.75rem 0.4rem 2rem;
      font-size: 0.8rem;
      color: var(--text);
      width: 220px;
      outline: none;
      transition: border-color 150ms ease;
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='%238a919e' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='11' cy='11' r='8'%3E%3C/circle%3E%3Cline x1='21' y1='21' x2='16.65' y2='16.65'%3E%3C/line%3E%3C/svg%3E");
      background-repeat: no-repeat;
      background-position: 8px center;
    }}
    .search-input:focus {{ border-color: var(--text-secondary); }}
    .search-input::placeholder {{ color: var(--text-muted); }}

    .btn {{
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      background: var(--accent);
      color: var(--accent-btn-text);
      border: 1px solid transparent;
      padding: 0.42rem 0.85rem;
      border-radius: 6px;
      font-size: 0.8rem;
      font-weight: 500;
      cursor: pointer;
      transition: opacity 120ms ease;
    }}
    .btn:hover {{ opacity: 0.92; }}
    .btn:disabled {{ opacity: 0.45; cursor: not-allowed; }}
    .btn svg {{ width: 14px; height: 14px; }}

    /* Category Filter Pills */
    .filter-bar {{
      display: flex;
      gap: 0.4rem;
      overflow-x: auto;
      margin-bottom: 0.85rem;
      padding-bottom: 0.25rem;
    }}
    .filter-pill {{
      font-size: 0.75rem;
      font-weight: 500;
      padding: 0.25rem 0.65rem;
      border-radius: 9999px;
      background: var(--surface);
      border: 1px solid var(--border);
      color: var(--text-secondary);
      cursor: pointer;
      white-space: nowrap;
      transition: all 120ms ease;
    }}
    .filter-pill:hover {{ color: var(--text); border-color: var(--text-muted); }}
    .filter-pill.active {{
      background: var(--accent);
      color: var(--accent-btn-text);
      border-color: var(--accent);
    }}

    /* Document Archive List */
    .table-card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      overflow: hidden;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.85rem;
      text-align: left;
    }}
    thead th {{
      padding: 0.75rem 1.25rem;
      background: var(--surface-subtle);
      color: var(--text-secondary);
      font-weight: 500;
      font-size: 0.72rem;
      border-bottom: 1px solid var(--border);
    }}
    tbody tr {{
      border-bottom: 1px solid var(--border-subtle);
      transition: background 120ms ease;
    }}
    tbody tr:last-child {{ border-bottom: none; }}
    tbody tr:hover {{ background: rgba(255, 255, 255, 0.02); }}
    td {{
      padding: 0.8rem 1.25rem;
      vertical-align: middle;
    }}
    .doc-link {{
      color: var(--text);
      text-decoration: none;
      font-weight: 500;
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      cursor: pointer;
    }}
    .doc-link:hover {{ color: #fff; text-decoration: underline; }}
    .doc-icon {{ width: 16px; height: 16px; color: var(--text-secondary); flex-shrink: 0; }}
    
    .cat-pill {{
      display: inline-block;
      font-size: 0.72rem;
      font-weight: 500;
      padding: 0.15rem 0.55rem;
      border-radius: 4px;
      letter-spacing: 0.01em;
    }}

    .actions-cell {{ text-align: right; }}
    .action-link {{
      color: var(--text-muted);
      text-decoration: none;
      font-size: 0.75rem;
      margin-left: 0.75rem;
      transition: color 120ms ease;
    }}
    .action-link:hover {{ color: var(--text); }}
    .action-link.action-delete:hover {{ color: #ff453a; }}

    .empty-state {{
      padding: 3.5rem 1rem;
      text-align: center;
      color: var(--text-muted);
    }}
    .empty-icon {{
      width: 32px;
      height: 32px;
      margin: 0 auto 0.75rem;
      color: var(--text-muted);
      opacity: 0.5;
    }}
    .empty-title {{
      font-size: 0.9rem;
      font-weight: 500;
      color: var(--text-secondary);
      margin-bottom: 0.25rem;
    }}
    .empty-desc {{ font-size: 0.8rem; color: var(--text-muted); }}

    /* QuickLook Drawer / Modal */
    .preview-backdrop {{
      position: fixed;
      top: 0;
      left: 0;
      right: 0;
      bottom: 0;
      background: rgba(0, 0, 0, 0.7);
      backdrop-filter: blur(4px);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 1000;
      padding: 1.5rem;
    }}
    .preview-backdrop.open {{ display: flex; }}
    .preview-modal {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      width: 100%;
      max-width: 900px;
      height: 85vh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      box-shadow: 0 20px 40px rgba(0, 0, 0, 0.5);
    }}
    .preview-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 0.85rem 1.25rem;
      border-bottom: 1px solid var(--border);
      background: var(--surface-subtle);
    }}
    .preview-title {{ font-size: 0.9rem; font-weight: 600; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 700px; }}
    .close-btn {{
      background: none;
      border: none;
      color: var(--text-secondary);
      cursor: pointer;
      font-size: 1.25rem;
      line-height: 1;
      padding: 0.25rem;
    }}
    .close-btn:hover {{ color: #fff; }}
    .preview-frame {{
      flex: 1;
      width: 100%;
      height: 100%;
      border: none;
      background: #1e1e1e;
    }}

    /* Toast Notification */
    .toast {{
      position: fixed;
      bottom: 1.5rem;
      right: 1.5rem;
      background: var(--surface-elevated);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 0.75rem 1.25rem;
      border-radius: 8px;
      font-size: 0.82rem;
      font-weight: 500;
      box-shadow: 0 10px 25px rgba(0, 0, 0, 0.4);
      transform: translateY(100px);
      opacity: 0;
      transition: all 200ms cubic-bezier(0.16, 1, 0.3, 1);
      z-index: 2000;
    }}
    .toast.show {{
      transform: translateY(0);
      opacity: 1;
    }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="brand">
        <span class="brand-title">Brother DS-940DW</span>
      </div>
      <div class="status-pill">
        <span id="status-dot" class="status-dot offline"></span>
        <span id="status-label">Standby</span>
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
        <span class="metric-sub" id="scanner-ip">{self.settings.scanner_ip}</span>
      </div>
      <div class="metric-item">
        <span class="metric-label">Uptime</span>
        <span class="metric-val" id="uptime">--</span>
      </div>
      <div class="metric-item">
        <span class="metric-label">RAM</span>
        <span class="metric-val" id="ram">-- MB</span>
      </div>
      <div class="metric-item">
        <span class="metric-label">Path</span>
        <div class="path-row">
          <span class="metric-val" style="font-size: 0.82rem; font-family: ui-monospace, Menlo, monospace;" id="path">{self.settings.output}</span>
          <button class="copy-btn" onclick="copyPath()" title="Copy folder path">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
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
        <input type="text" id="search-box" class="search-input" placeholder="Search... (press /)" oninput="filterScans()">
        <button class="btn" id="sort-btn" onclick="triggerSort()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="23 4 23 10 17 10"/>
            <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
          </svg>
          <span>Organize</span>
        </button>
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
            <th>Size</th>
            <th>Date</th>
            <th class="actions-cell">Actions</th>
          </tr>
        </thead>
        <tbody id="scans-table">
          <tr><td colspan="5" class="empty-state">Loading...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <div id="preview-modal" class="preview-backdrop" onclick="closePreview(event)">
    <div class="preview-modal" onclick="event.stopPropagation()">
      <div class="preview-header">
        <span class="preview-title" id="preview-filename">Document Preview</span>
        <button class="close-btn" onclick="closePreview()">&times;</button>
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
      setTimeout(() => t.classList.remove('show'), 3500);
    }}

    function copyPath() {{
      const p = document.getElementById('path').textContent;
      navigator.clipboard.writeText(p);
      showToast('Path copied to clipboard');
    }}

    function getCategoryStyle(cat) {{
      if (!cat || cat === 'Inbox') {{
        return 'background: var(--surface-subtle); color: var(--text-secondary); border: 1px solid var(--border-subtle);';
      }}
      let hash = 0;
      for (let i = 0; i < cat.length; i++) hash = cat.charCodeAt(i) + ((hash << 5) - hash);
      const h = Math.abs(hash) % 360;
      return `background: hsla(${{h}}, 65%, 50%, 0.12); color: hsl(${{h}}, 80%, 75%); border: 1px solid hsla(${{h}}, 65%, 50%, 0.25);`;
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

    function openPreview(relPath, filename) {{
      const modal = document.getElementById('preview-modal');
      const frame = document.getElementById('preview-iframe');
      const title = document.getElementById('preview-filename');
      title.textContent = filename;
      frame.src = '/files/' + encodeURIComponent(relPath);
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

    function renderScans() {{
      const query = document.getElementById('search-box').value.toLowerCase().trim();
      const tbody = document.getElementById('scans-table');

      let filtered = allScans;
      if (currentCategory !== 'all') {{
        filtered = filtered.filter(s => s.category.toLowerCase().includes(currentCategory.toLowerCase()));
      }}
      if (query) {{
        filtered = filtered.filter(s => s.filename.toLowerCase().includes(query) || s.category.toLowerCase().includes(query));
      }}

      document.getElementById('scan-count').textContent = filtered.length;

      if (filtered.length === 0) {{
        tbody.innerHTML = `
          <tr>
            <td colspan="5" class="empty-state">
              <svg class="empty-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                <polyline points="14 2 14 8 20 8"/>
              </svg>
              <div class="empty-title">No documents</div>
              <div class="empty-desc">Feed a sheet into the scanner to capture.</div>
            </td>
          </tr>
        `;
        return;
      }}

      tbody.innerHTML = filtered.map(s => `
        <tr>
          <td>
            <a class="doc-link" onclick="openPreview('${{s.relative_path}}', '${{s.filename}}')">
              <svg class="doc-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                <polyline points="14 2 14 8 20 8"/>
              </svg>
              <span>${{s.filename}}</span>
            </a>
          </td>
          <td><span class="cat-pill" style="${{getCategoryStyle(s.category)}}">${{s.category}}</span></td>
          <td style="color: var(--text-secondary);">${{s.size_kb}} KB</td>
          <td style="color: var(--text-muted);">${{s.date_str}}</td>
          <td class="actions-cell">
            <a class="action-link" onclick="openPreview('${{s.relative_path}}', '${{s.filename}}')">Preview</a>
            <a class="action-link" href="/files/${{encodeURIComponent(s.relative_path)}}" download="${{s.filename}}">Download</a>
            <a class="action-link action-delete" onclick="confirmDelete('${{s.relative_path}}', '${{s.filename}}')">Delete</a>
          </td>
        </tr>
      `).join('');
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
          label.textContent = `Batch window (${{data.timeout_remaining || ''}}s)`;
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

    async function triggerSort() {{
      const btn = document.getElementById('sort-btn');
      const origHtml = btn.innerHTML;
      btn.innerHTML = '<span>Sorting...</span>';
      btn.disabled = true;
      try {{
        const res = await fetch('/api/sort', {{ method: 'POST' }});
        const data = await res.json();
        showToast(`Organized ${{data.moved_count}} document${{data.moved_count === 1 ? '' : 's'}}`);
        await loadScans();
      }} catch (e) {{
        showToast('Sorting failed: ' + e);
      }} finally {{
        btn.innerHTML = origHtml;
        btn.disabled = false;
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
