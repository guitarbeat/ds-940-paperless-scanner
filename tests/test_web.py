from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.request

from ds940.config import Settings
from ds940.web import run_web_server


class WebDashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmppath = Path(self.tmpdir.name)
        self.work = self.tmppath / "work"
        self.work.mkdir()
        self.output = self.tmppath / "output"
        self.output.mkdir()

        # Write dummy status and scan
        (self.work / "auto-status.json").write_text(
            json.dumps({"state": "waiting_for_paper", "updated": time.time()}),
            encoding="utf-8",
        )
        dummy_pdf = self.output / "test_doc.pdf"
        dummy_pdf.write_bytes(b"%PDF-1.4 sample")

        bills_dir = self.output / "Bills"
        bills_dir.mkdir(parents=True, exist_ok=True)
        dated_pdf = bills_dir / "2026-10-02_Austin-Energy_Bill.pdf"
        dated_pdf.write_bytes(b"%PDF-1.4 sample bill")

        self.settings = Settings(
            scanner_ip="192.168.0.25",
            work=self.work,
            output=self.output,
            paper_size="a4",
            web_port=0,
            web_bind_ip="127.0.0.1",
        )
        self.stop_event = threading.Event()
        self.server, self.thread = run_web_server(self.settings, self.stop_event)
        self.port = self.server.server_address[1]
        time.sleep(0.1)

    def tearDown(self):
        self.stop_event.set()
        time.sleep(0.1)

    def test_status_endpoint(self):
        url = f"http://127.0.0.1:{self.port}/api/status"
        with urllib.request.urlopen(url, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["state"], "waiting_for_paper")
            self.assertEqual(data["scanner_ip"], "192.168.0.25")
            self.assertIn("memory_mb", data)
            self.assertIn("uptime", data)

    def test_status_endpoint_extended_states(self):
        (self.work / "auto-status.json").write_text(
            json.dumps({
                "state": "filed",
                "destination": "Medical/2026-11-17_Labcorp_Invoice.pdf",
                "category": "Medical & Health",
                "pages": 2,
                "raw_pages": 4,
                "discarded_blank_pages": 2,
                "completed_at": 1794900000.0,
                "updated": time.time(),
            }),
            encoding="utf-8",
        )
        url = f"http://127.0.0.1:{self.port}/api/status"
        with urllib.request.urlopen(url, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["state"], "filed")
            self.assertEqual(data["destination"], "Medical/2026-11-17_Labcorp_Invoice.pdf")
            self.assertEqual(data["category"], "Medical & Health")
            self.assertEqual(data["pages"], 2)
            self.assertEqual(data["raw_pages"], 4)
            self.assertEqual(data["discarded_blank_pages"], 2)

    def test_scans_endpoint(self):
        url = f"http://127.0.0.1:{self.port}/api/scans"
        with urllib.request.urlopen(url, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            scans = json.loads(resp.read().decode("utf-8"))
            self.assertIsInstance(scans, list)
            self.assertEqual(len(scans), 2)
            
            by_name = {s["filename"]: s for s in scans}
            self.assertIn("test_doc.pdf", by_name)
            self.assertIn("2026-10-02_Austin-Energy_Bill.pdf", by_name)

            self.assertIsNone(by_name["test_doc.pdf"]["doc_date"])
            self.assertIsNotNone(by_name["test_doc.pdf"]["scan_date"])

            self.assertEqual(by_name["2026-10-02_Austin-Energy_Bill.pdf"]["doc_date"], "2026-10-02")
            self.assertIsNotNone(by_name["2026-10-02_Austin-Energy_Bill.pdf"]["scan_date"])
            self.assertEqual(by_name["2026-10-02_Austin-Energy_Bill.pdf"]["category"], "Bills")

    def test_dashboard_html(self):
        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            html = resp.read().decode("utf-8")
            self.assertIn("Brother DS-940DW", html)
            self.assertIn("Recent Scans", html)
            self.assertIn("live-status-strip", html)
            self.assertIn("strip-title", html)
            self.assertIn("header-session-actions", html)
            self.assertIn("header-global-actions", html)
            self.assertIn("Doc Date", html)
            self.assertIn("Scanned", html)
            self.assertIn("finder-window", html)
            self.assertIn("finder-sidebar", html)
            self.assertIn("finder-breadcrumbs", html)

    def test_thumbnail_endpoint(self):
        import hashlib
        # 1. Non-existent file returns 404
        url_404 = f"http://127.0.0.1:{self.port}/thumbs/nonexistent.pdf"
        try:
            with urllib.request.urlopen(url_404, timeout=3.0) as resp:
                pass
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

        # 2. Path traversal returns 403
        url_403 = f"http://127.0.0.1:{self.port}/thumbs/../../etc/passwd"
        try:
            with urllib.request.urlopen(url_403, timeout=3.0) as resp:
                pass
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 403)

        # 3. Cached thumbnail returns 200 with image/png
        cache_dir = self.work / "thumbnails"
        cache_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = (self.output / "test_doc.pdf").resolve()
        st = pdf_path.stat()
        cache_key = hashlib.md5(f"{pdf_path}_{st.st_mtime}_{st.st_size}".encode()).hexdigest()
        (cache_dir / f"{cache_key}.png").write_bytes(b"\x89PNG\r\n\x1a\nfake-png-data")

        url_thumb = f"http://127.0.0.1:{self.port}/thumbs/test_doc.pdf"
        with urllib.request.urlopen(url_thumb, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "image/png")
            self.assertEqual(resp.read(), b"\x89PNG\r\n\x1a\nfake-png-data")

    def test_categories_endpoint(self):
        url = f"http://127.0.0.1:{self.port}/api/categories"
        with urllib.request.urlopen(url, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            cats = json.loads(resp.read().decode("utf-8"))
            self.assertIsInstance(cats, list)

    def test_folders_and_scan_metadata_endpoint(self):
        # Verify /api/folders endpoint
        url = f"http://127.0.0.1:{self.port}/api/folders"
        with urllib.request.urlopen(url, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            folders = json.loads(resp.read().decode("utf-8"))
            self.assertIsInstance(folders, list)
            names = [f["name"] for f in folders]
            self.assertIn("Inbox", names)
            self.assertIn("Bills", names)

        # Verify /api/scans contains folder and is_unfiled
        scans_url = f"http://127.0.0.1:{self.port}/api/scans"
        with urllib.request.urlopen(scans_url, timeout=3.0) as resp:
            scans = json.loads(resp.read().decode("utf-8"))
            by_name = {s["filename"]: s for s in scans}
            self.assertTrue(by_name["test_doc.pdf"]["is_unfiled"])
            self.assertEqual(by_name["test_doc.pdf"]["folder"], "Inbox")
            self.assertFalse(by_name["2026-10-02_Austin-Energy_Bill.pdf"]["is_unfiled"])
            self.assertEqual(by_name["2026-10-02_Austin-Energy_Bill.pdf"]["folder"], "Bills")

    def test_delete_endpoint(self):
        url = f"http://127.0.0.1:{self.port}/api/delete"
        req = urllib.request.Request(
            url,
            data=json.dumps({"path": "test_doc.pdf"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["status"], "ok")
        self.assertFalse((self.output / "test_doc.pdf").exists())

    def test_session_lifecycle_endpoints(self):
        # 1. Test status includes session fields
        status_url = f"http://127.0.0.1:{self.port}/api/status"
        with urllib.request.urlopen(status_url, timeout=3.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("session_active", data)
            self.assertIn("idle_remaining", data)
            self.assertIn("idle_timeout", data)

        # 2. Test pause endpoint
        pause_url = f"http://127.0.0.1:{self.port}/api/session/pause"
        req = urllib.request.Request(pause_url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["status"], "ok")
            self.assertFalse(data["session_active"])

        # 3. Test wake endpoint
        wake_url = f"http://127.0.0.1:{self.port}/api/session/wake"
        req = urllib.request.Request(wake_url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["status"], "ok")
            self.assertTrue(data["session_active"])
            self.assertGreater(data["idle_remaining"], 0)

        # 4. Test extend endpoint
        extend_url = f"http://127.0.0.1:{self.port}/api/session/extend"
        req = urllib.request.Request(extend_url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["status"], "ok")
            self.assertTrue(data["session_active"])

    def test_dashboard_has_preview_toggle(self):
        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            html = resp.read().decode("utf-8")
            self.assertIn("btn-toggle-preview", html)
            self.assertIn("toggleThumbnailPreview", html)
            self.assertIn("ds940-show-thumbnails", html)


if __name__ == "__main__":
    unittest.main()
