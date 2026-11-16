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


def get_recent_scans(output_dir: Path, limit: int = 50) -> list[dict]:
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

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self.serve_dashboard()
        elif path == "/api/status":
            self.serve_status()
        elif path == "/api/scans":
            self.send_json(get_recent_scans(self.settings.output))
        elif path.startswith("/files/"):
            self.serve_file(path[7:])
        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/sort":
            self.trigger_sort()
        else:
            self.send_error(404, "Not Found")

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
        except OSError:
            self.send_error(500, "Error reading file")

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
  <title>Brother DS-940DW Scan Hub</title>
  <style>
    :root {{
      --bg: #090a0c;
      --surface: #111317;
      --surface-subtle: #16181d;
      --border: #22252c;
      --border-subtle: #191b20;
      --text: #f0f2f5;
      --text-secondary: #8b919d;
      --text-muted: #555964;
      --accent: #e1e4ea;
      --accent-muted: #2c2f38;
      --status-green: #30d158;
      --status-amber: #ff9f0a;
      --status-muted: #666a75;
      --cat-bills: rgba(56, 189, 248, 0.12);
      --cat-bills-text: #7dd3fc;
      --cat-tax: rgba(168, 85, 247, 0.12);
      --cat-tax-text: #c084fc;
      --cat-health: rgba(52, 211, 153, 0.12);
      --cat-health-text: #6ee7b7;
      --cat-ins: rgba(251, 146, 60, 0.12);
      --cat-ins-text: #fdba74;
      --cat-rec: rgba(244, 114, 182, 0.12);
      --cat-rec-text: #f472b6;
      --cat-other: rgba(148, 163, 184, 0.12);
      --cat-other-text: #94a3b8;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
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
      margin-bottom: 2rem;
      padding-bottom: 1.25rem;
      border-bottom: 1px solid var(--border-subtle);
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }}
    .brand-icon {{
      width: 28px;
      height: 28px;
      color: var(--text);
    }}
    .brand-title {{
      font-size: 1.25rem;
      font-weight: 600;
      letter-spacing: -0.015em;
    }}
    .brand-tag {{
      font-size: 0.75rem;
      font-weight: 500;
      color: var(--text-secondary);
      background: var(--surface-subtle);
      border: 1px solid var(--border);
      padding: 0.15rem 0.5rem;
      border-radius: 4px;
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
    }}
    .status-dot {{
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--status-muted);
    }}
    .status-dot.online {{ background: var(--status-green); box-shadow: 0 0 8px rgba(48, 209, 88, 0.4); }}
    .status-dot.active {{ background: var(--status-amber); box-shadow: 0 0 8px rgba(255, 159, 10, 0.5); animation: pulse 1.5s infinite ease-in-out; }}
    .status-dot.offline {{ background: var(--status-muted); }}

    @keyframes pulse {{
      0%, 100% {{ opacity: 1; }}
      50% {{ opacity: 0.4; }}
    }}

    /* Hardware Telemetry Strip */
    .telemetry-card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 1.25rem 1.5rem;
      margin-bottom: 2rem;
      display: grid;
      grid-template-columns: 1.4fr 1fr 1fr 1.6fr;
      gap: 1.5rem;
    }}
    @media (max-width: 768px) {{
      .telemetry-card {{ grid-template-columns: 1fr 1fr; gap: 1rem; }}
    }}
    .metric-item {{ display: flex; flex-direction: column; gap: 0.25rem; }}
    .metric-label {{ font-size: 0.75rem; color: var(--text-secondary); font-weight: 500; }}
    .metric-val {{ font-size: 0.95rem; font-weight: 600; color: var(--text); }}
    .metric-sub {{ font-size: 0.75rem; color: var(--text-muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}

    /* Section & Toolbar */
    .section-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 1rem;
    }}
    .section-title {{
      font-size: 1rem;
      font-weight: 600;
      letter-spacing: -0.01em;
      color: var(--text);
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }}
    .count-badge {{
      font-size: 0.75rem;
      font-weight: 500;
      color: var(--text-muted);
      background: var(--surface-subtle);
      border: 1px solid var(--border-subtle);
      padding: 0.1rem 0.45rem;
      border-radius: 4px;
    }}
    .btn {{
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      background: var(--accent);
      color: #090a0c;
      border: 1px solid transparent;
      padding: 0.45rem 0.85rem;
      border-radius: 6px;
      font-size: 0.8rem;
      font-weight: 500;
      cursor: pointer;
      transition: background 120ms ease, opacity 120ms ease;
    }}
    .btn:hover {{ opacity: 0.92; }}
    .btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
    .btn svg {{ width: 14px; height: 14px; }}

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
      font-size: 0.75rem;
      border-bottom: 1px solid var(--border);
    }}
    tbody tr {{
      border-bottom: 1px solid var(--border-subtle);
      transition: background 120ms ease;
    }}
    tbody tr:last-child {{ border-bottom: none; }}
    tbody tr:hover {{ background: rgba(255, 255, 255, 0.02); }}
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
      gap: 0.5rem;
    }}
    .doc-link:hover {{ text-decoration: underline; color: #fff; }}
    .doc-icon {{ width: 16px; height: 16px; color: var(--text-secondary); flex-shrink: 0; }}
    
    .cat-pill {{
      display: inline-block;
      font-size: 0.75rem;
      font-weight: 500;
      padding: 0.15rem 0.55rem;
      border-radius: 4px;
    }}
    .cat-Bills {{ background: var(--cat-bills); color: var(--cat-bills-text); }}
    .cat-Taxes {{ background: var(--cat-tax); color: var(--cat-tax-text); }}
    .cat-Medical {{ background: var(--cat-health); color: var(--cat-health-text); }}
    .cat-Insurance {{ background: var(--cat-ins); color: var(--cat-ins-text); }}
    .cat-Receipts {{ background: var(--cat-rec); color: var(--cat-rec-text); }}
    .cat-Personal {{ background: var(--cat-other); color: var(--cat-other-text); }}
    .cat-Inbox {{ background: var(--surface-subtle); color: var(--text-secondary); }}

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
      opacity: 0.6;
    }}
    .empty-title {{
      font-size: 0.9rem;
      font-weight: 500;
      color: var(--text-secondary);
      margin-bottom: 0.25rem;
    }}
    .empty-desc {{
      font-size: 0.8rem;
      color: var(--text-muted);
    }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="brand">
        <svg class="brand-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
          <path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/>
          <path d="M6 9V3a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v6"/>
          <rect x="6" y="14" width="12" height="8" rx="1"/>
        </svg>
        <div>
          <span class="brand-title">Brother DS-940DW</span>
          <span class="brand-tag">Scan Hub</span>
        </div>
      </div>
      <div class="status-pill">
        <span id="status-dot" class="status-dot offline"></span>
        <span id="status-label">Standby</span>
      </div>
    </header>

    <div class="telemetry-card">
      <div class="metric-item">
        <span class="metric-label">Scanner Link</span>
        <span class="metric-val" id="scanner-state">Connecting</span>
        <span class="metric-sub" id="scanner-ip">{self.settings.scanner_ip}:8080</span>
      </div>
      <div class="metric-item">
        <span class="metric-label">Uptime</span>
        <span class="metric-val" id="uptime">--</span>
        <span class="metric-sub">launchd daemon</span>
      </div>
      <div class="metric-item">
        <span class="metric-label">Memory</span>
        <span class="metric-val" id="ram">-- MB</span>
        <span class="metric-sub">Apple Neural Engine</span>
      </div>
      <div class="metric-item">
        <span class="metric-label">Storage</span>
        <span class="metric-val" style="font-size: 0.82rem; font-family: ui-monospace, Menlo, monospace;" id="path">{self.settings.output}</span>
        <span class="metric-sub">iCloud Tracked</span>
      </div>
    </div>

    <div class="section-header">
      <div class="section-title">
        <span>Recent Scans</span>
        <span class="count-badge" id="scan-count">0</span>
      </div>
      <button class="btn" id="sort-btn" onclick="triggerSort()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="23 4 23 10 17 10"/>
          <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
        </svg>
        <span>Organize Mail</span>
      </button>
    </div>

    <div class="table-card">
      <table>
        <thead>
          <tr>
            <th>Document</th>
            <th>Category</th>
            <th>Size</th>
            <th>Scanned Date</th>
          </tr>
        </thead>
        <tbody id="scans-table">
          <tr><td colspan="4" class="empty-state">Loading archives...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <script>
    function getCategoryClass(cat) {{
      if (cat.includes('Bills')) return 'cat-Bills';
      if (cat.includes('Tax')) return 'cat-Taxes';
      if (cat.includes('Medical')) return 'cat-Medical';
      if (cat.includes('Insurance')) return 'cat-Insurance';
      if (cat.includes('Receipt')) return 'cat-Receipts';
      if (cat.includes('Personal')) return 'cat-Personal';
      return 'cat-Inbox';
    }}

    async function updateStatus() {{
      try {{
        const res = await fetch('/api/status');
        const data = await res.json();
        
        const dot = document.getElementById('status-dot');
        const label = document.getElementById('status-label');
        const stateEl = document.getElementById('scanner-state');

        dot.className = 'status-dot';
        if (data.state === 'waiting_for_paper') {{
          dot.classList.add('online');
          label.textContent = 'Ready for paper';
          stateEl.textContent = 'Ready (Idle)';
        }} else if (data.state === 'scanning') {{
          dot.classList.add('active');
          label.textContent = 'Scanning...';
          stateEl.textContent = 'Scanning Duplex';
        }} else if (data.state === 'waiting_for_next_sheet') {{
          dot.classList.add('active');
          label.textContent = `Batch countdown (${{data.timeout_remaining || ''}}s)`;
          stateEl.textContent = `Collation Window (${{data.timeout_remaining || ''}}s)`;
        }} else {{
          dot.classList.add('offline');
          label.textContent = data.state === 'scanner_offline' ? 'Offline (Sleep)' : data.state.replace(/_/g, ' ');
          stateEl.textContent = data.state === 'scanner_offline' ? 'Standby (Sleep)' : data.state;
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
        const scans = await res.json();
        const tbody = document.getElementById('scans-table');
        document.getElementById('scan-count').textContent = scans.length;

        if (scans.length === 0) {{
          tbody.innerHTML = `
            <tr>
              <td colspan="4" class="empty-state">
                <svg class="empty-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                  <polyline points="14 2 14 8 20 8"/>
                </svg>
                <div class="empty-title">No documents in queue</div>
                <div class="empty-desc">Insert paper into the feeder to automatically scan.</div>
              </td>
            </tr>
          `;
          return;
        }}

        tbody.innerHTML = scans.map(s => `
          <tr>
            <td>
              <a class="doc-link" href="/files/${{encodeURIComponent(s.relative_path)}}" target="_blank">
                <svg class="doc-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                  <polyline points="14 2 14 8 20 8"/>
                </svg>
                <span>${{s.filename}}</span>
              </a>
            </td>
            <td><span class="cat-pill ${{getCategoryClass(s.category)}}">${{s.category}}</span></td>
            <td style="color: var(--text-secondary);">${{s.size_kb}} KB</td>
            <td style="color: var(--text-muted);">${{s.date_str}}</td>
          </tr>
        `).join('');
      }} catch (e) {{
        console.error(e);
      }}
    }}

    async function triggerSort() {{
      const btn = document.getElementById('sort-btn');
      const origText = btn.innerHTML;
      btn.innerHTML = '<span>Sorting...</span>';
      btn.disabled = true;
      try {{
        const res = await fetch('/api/sort', {{ method: 'POST' }});
        const data = await res.json();
        await loadScans();
      }} catch (e) {{
        console.error(e);
      }} finally {{
        btn.innerHTML = origText;
        btn.disabled = false;
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
