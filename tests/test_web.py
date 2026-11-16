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
            self.assertIn("Doc Date", html)
            self.assertIn("Scanned", html)

    def test_categories_endpoint(self):
        url = f"http://127.0.0.1:{self.port}/api/categories"
        with urllib.request.urlopen(url, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            cats = json.loads(resp.read().decode("utf-8"))
            self.assertIsInstance(cats, list)

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


if __name__ == "__main__":
    unittest.main()
