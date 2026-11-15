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

from .ai import CATEGORIES
from .config import Settings

LOG = logging.getLogger(__name__)


def get_process_metrics() -> dict:
    """Retrieve memory and CPU metrics for the current process."""
    rss_mb = 0.0
    try:
        import resource
        # ru_maxrss is in bytes on macOS, KB on Linux
        maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        import sys
        if sys.platform == "darwin":
            rss_mb = round(maxrss / (1024 * 1024), 1)
        else:
            rss_mb = round(maxrss / 1024, 1)
    except Exception:
        pass
    return {"rss_mb": rss_mb}


def get_recent_scans(output_dir: Path, limit: int = 25) -> list[dict]:
    """Scan the output folder for recent PDF files."""
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
        # Quiet standard HTTP access logs to keep service log clean
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
            scans = get_recent_scans(self.settings.output)
            self.send_json(scans)
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
            LOG.exception("Error during web-triggered sort: %s", err)
            self.send_json({"status": "error", "message": str(err)}, status=500)

    def serve_file(self, rel_path: str):
        safe_rel = urllib.parse.unquote(rel_path).lstrip("/")
        target_path = (self.settings.output / safe_rel).resolve()
        # Security: verify path is within output directory
        if not str(target_path).startswith(str(self.settings.output.resolve())) or not target_path.is_file():
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
      --bg: #0f172a;
      --card-bg: #1e293b;
      --border: #334155;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --accent: #38bdf8;
      --success: #22c55e;
      --warning: #f59e0b;
      --danger: #ef4444;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding: 2rem 1rem;
    }}
    .container {{ max-width: 960px; margin: 0 auto; }}
    header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 2rem; border-bottom: 1px solid var(--border); padding-bottom: 1rem; }}
    h1 {{ font-size: 1.5rem; font-weight: 700; display: flex; align-items: center; gap: 0.5rem; }}
    .badge {{ font-size: 0.85rem; padding: 0.35rem 0.75rem; border-radius: 9999px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; }}
    .badge-online {{ background: rgba(34, 197, 94, 0.2); color: var(--success); border: 1px solid var(--success); }}
    .badge-offline {{ background: rgba(239, 68, 68, 0.2); color: var(--danger); border: 1px solid var(--danger); }}
    .badge-scanning {{ background: rgba(56, 189, 248, 0.2); color: var(--accent); border: 1px solid var(--accent); }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin-bottom: 2rem; }}
    .card {{ background: var(--card-bg); border: 1px solid var(--border); border-radius: 0.75rem; padding: 1.25rem; }}
    .card-label {{ font-size: 0.75rem; text-transform: uppercase; color: var(--text-muted); font-weight: 600; margin-bottom: 0.25rem; }}
    .card-value {{ font-size: 1.4rem; font-weight: 700; color: var(--text); }}
    .section-title {{ font-size: 1.15rem; font-weight: 600; margin-bottom: 1rem; display: flex; justify-content: space-between; align-items: center; }}
    .table-container {{ background: var(--card-bg); border: 1px solid var(--border); border-radius: 0.75rem; overflow-x: auto; margin-bottom: 2rem; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; text-align: left; }}
    th, td {{ padding: 0.75rem 1rem; border-bottom: 1px solid var(--border); }}
    th {{ background: rgba(255, 255, 255, 0.03); color: var(--text-muted); font-weight: 600; font-size: 0.8rem; text-transform: uppercase; }}
    tr:last-child td {{ border-bottom: none; }}
    a.file-link {{ color: var(--accent); text-decoration: none; font-weight: 500; }}
    a.file-link:hover {{ text-decoration: underline; }}
    .btn {{ background: var(--accent); color: #000; border: none; padding: 0.5rem 1rem; border-radius: 0.5rem; font-weight: 600; cursor: pointer; transition: opacity 0.2s; }}
    .btn:hover {{ opacity: 0.9; }}
    .btn-secondary {{ background: var(--border); color: var(--text); }}
    .tag {{ font-size: 0.75rem; padding: 0.2rem 0.5rem; border-radius: 0.35rem; background: var(--border); color: var(--text-muted); }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>📄 Brother DS-940DW Hub</h1>
      <span id="status-badge" class="badge badge-offline">Checking...</span>
    </header>

    <div class="grid">
      <div class="card">
        <div class="card-label">Scanner Host</div>
        <div class="card-value" id="scanner-ip">{self.settings.scanner_ip}</div>
      </div>
      <div class="card">
        <div class="card-label">Receiver Uptime</div>
        <div class="card-value" id="uptime">--</div>
      </div>
      <div class="card">
        <div class="card-label">RAM Consumption</div>
        <div class="card-value" id="ram">-- MB</div>
      </div>
      <div class="card">
        <div class="card-label">Filing Path</div>
        <div class="card-value" style="font-size: 1rem; word-break: break-all;" id="path">{self.settings.output}</div>
      </div>
    </div>

    <div class="section-title">
      <span>Recent Scans</span>
      <button class="btn btn-secondary" onclick="triggerSort()">✨ Run AI Sort</button>
    </div>

    <div class="table-container">
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
          <tr><td colspan="4" style="text-align: center; color: var(--text-muted);">Loading scans...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <script>
    async function updateStatus() {{
      try {{
        const res = await fetch('/api/status');
        const data = await res.json();
        
        const badge = document.getElementById('status-badge');
        badge.className = 'badge';
        if (data.state === 'waiting_for_paper') {{
          badge.textContent = 'Ready for Paper';
          badge.classList.add('badge-online');
        }} else if (data.state === 'scanning') {{
          badge.textContent = 'Scanning...';
          badge.classList.add('badge-scanning');
        }} else if (data.state === 'waiting_for_next_sheet') {{
          badge.textContent = `Next Sheet? (${{data.timeout_remaining || ''}}s)`;
          badge.classList.add('badge-scanning');
        }} else {{
          badge.textContent = data.state.replace(/_/g, ' ');
          badge.classList.add('badge-offline');
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
        if (scans.length === 0) {{
          tbody.innerHTML = '<tr><td colspan="4" style="text-align: center; color: var(--text-muted);">No scans found yet.</td></tr>';
          return;
        }}
        tbody.innerHTML = scans.map(s => `
          <tr>
            <td><a class="file-link" href="/files/${{encodeURIComponent(s.relative_path)}}" target="_blank">${{s.filename}}</a></td>
            <td><span class="tag">${{s.category}}</span></td>
            <td>${{s.size_kb}} KB</td>
            <td style="color: var(--text-muted);">${{s.date_str}}</td>
          </tr>
        `).join('');
      }} catch (e) {{
        console.error(e);
      }}
    }}

    async function triggerSort() {{
      const btn = event.target;
      btn.textContent = 'Sorting...';
      btn.disabled = true;
      try {{
        const res = await fetch('/api/sort', {{ method: 'POST' }});
        const data = await res.json();
        alert('Sort Complete! Moved ' + data.moved_count + ' document(s).');
        await loadScans();
      }} catch (e) {{
        alert('Sort failed: ' + e);
      }} finally {{
        btn.textContent = '✨ Run AI Sort';
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
    """Start the dashboard HTTP server in a background thread."""
    class CustomServer(ThreadingHTTPServer):
        allow_reuse_address = True

    DashboardHandler.settings = settings
    DashboardHandler.start_time = time.time()

    server = CustomServer((settings.web_bind_ip, settings.web_port), DashboardHandler)
    server.timeout = 0.5
    actual_port = server.server_address[1]

    def serve_loop():
        LOG.info("Dashboard web server running at http://%s:%d", settings.web_bind_ip, settings.web_port)
        while stop_event is None or not stop_event.is_set():
            server.handle_request()
        server.server_close()

    thread = threading.Thread(target=serve_loop, daemon=True, name="ds940-web")
    thread.start()
    return server, thread
