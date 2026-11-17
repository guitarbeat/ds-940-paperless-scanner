from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from PIL import Image
try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

from ds940.escl import (
    EsclError,
    EsclScanner,
    PaperJammedError,
    ScannerBusyError,
    ScannerOfflineError,
    ScannerStatus,
)
from ds940.spool import ScanSpool
from ds940.worker import capture_if_ready_escl, retry_state_escl


def make_dummy_jpeg(path: Path, color=(255, 0, 0)):
    img = Image.new("RGB", (300, 300), color=color)
    img.save(path, "JPEG", dpi=(300, 300))
    return path


class WorkerEsclTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        root = Path(self.tmpdir.name)
        self.work = root / "work"
        self.work.mkdir()
        self.consume = root / "consume"
        self.consume.mkdir()
        self.spool = ScanSpool(self.work, self.consume)
        self.on_scan = Mock()
        self.scanner = MagicMock(spec=EsclScanner)

    def test_empty_feeder_does_not_scan(self):
        self.scanner.get_status.return_value = ScannerStatus(state="Idle", adf_state="ScannerAdfEmpty")
        result = capture_if_ready_escl(self.scanner, self.spool, "a4", self.on_scan)
        self.assertIsNone(result)
        self.assertEqual(list(self.consume.iterdir()), [])
        self.on_scan.assert_not_called()

    def test_jammed_feeder_raises(self):
        self.scanner.get_status.return_value = ScannerStatus(state="Stopped", adf_state="ScannerAdfJammed")
        with self.assertRaises(PaperJammedError):
            capture_if_ready_escl(self.scanner, self.spool, "a4", self.on_scan)
        self.assertEqual(list(self.consume.iterdir()), [])

    def test_loaded_feeder_scans_and_delivers_duplex_pdf(self):
        self.scanner.get_status.return_value = ScannerStatus(state="Idle", adf_state="ScannerAdfLoaded")
        self.scanner.start_scan_job.return_value = "http://192.168.0.25:8080/eSCL/ScanJobs/1"

        def mock_fetch_pages(job_url, folder, start_page=1, **kwargs):
            p1 = make_dummy_jpeg(folder / f"page-{start_page}.jpg", color=(255, 0, 0))
            p2 = make_dummy_jpeg(folder / f"page-{start_page + 1}.jpg", color=(0, 255, 0))
            return [p1, p2]

        self.scanner.fetch_job_pages.side_effect = mock_fetch_pages

        scan_id = capture_if_ready_escl(self.scanner, self.spool, "a4", self.on_scan)
        self.assertIsNotNone(scan_id)
        self.on_scan.assert_called_once()

        # Verify delivered file in output directory
        delivered = list(self.consume.glob("*.pdf"))
        self.assertEqual(len(delivered), 1)
        pdf_path = delivered[0]
        self.assertEqual(pdf_path.name, f"{scan_id}.pdf")

        # Verify valid PDF with standard header and trailer
        data = pdf_path.read_bytes()
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertTrue(b"%%EOF" in data)

        if PdfReader is not None:
            reader = PdfReader(BytesIO(data))
            self.assertEqual(len(reader.pages), 2)

    def test_multi_sheet_batch_collation(self):
        calls = [0]

        def mock_status(*args, **kwargs):
            calls[0] += 1
            if calls[0] in (1, 2):
                # 1 = initial detection of sheet 1, 2 = detection of sheet 2
                return ScannerStatus(state="Idle", adf_state="ScannerAdfLoaded")
            return ScannerStatus(state="Idle", adf_state="ScannerAdfEmpty")

        self.scanner.get_status.side_effect = mock_status
        self.scanner.start_scan_job.return_value = "http://192.168.0.25:8080/eSCL/ScanJobs/batch"

        def mock_fetch_pages(job_url, folder, start_page=1, **kwargs):
            p1 = make_dummy_jpeg(folder / f"page-{start_page}.jpg", color=(255, 0, 0))
            p2 = make_dummy_jpeg(folder / f"page-{start_page + 1}.jpg", color=(0, 255, 0))
            return [p1, p2]

        self.scanner.fetch_job_pages.side_effect = mock_fetch_pages

        scan_id = capture_if_ready_escl(
            self.scanner,
            self.spool,
            "a4",
            on_scan=self.on_scan,
            batch_timeout=1.0,
        )
        self.assertIsNotNone(scan_id)

        delivered = list(self.consume.glob("*.pdf"))
        self.assertEqual(len(delivered), 1)
        data = delivered[0].read_bytes()
        self.assertTrue(data.startswith(b"%PDF-"))

        if PdfReader is not None:
            reader = PdfReader(BytesIO(data))
            self.assertEqual(len(reader.pages), 4)

    def test_lifecycle_status_callbacks(self):
        self.scanner.get_status.return_value = ScannerStatus(state="Idle", adf_state="ScannerAdfLoaded")
        self.scanner.start_scan_job.return_value = "http://192.168.0.25:8080/eSCL/ScanJobs/lifecycle"

        def mock_fetch_pages(job_url, folder, start_page=1, **kwargs):
            p1 = make_dummy_jpeg(folder / f"page-{start_page}.jpg", color=(255, 0, 0))
            p2 = make_dummy_jpeg(folder / f"page-{start_page + 1}.jpg", color=(0, 255, 0))
            return [p1, p2]

        self.scanner.fetch_job_pages.side_effect = mock_fetch_pages

        status_events = []
        def record_status(state, **kwargs):
            status_events.append((state, kwargs))

        scan_id = capture_if_ready_escl(
            self.scanner,
            self.spool,
            "a4",
            on_status=record_status,
            batch_timeout=0,
        )
        self.assertIsNotNone(scan_id)

        states_called = [e[0] for e in status_events]
        self.assertIn("scanning", states_called)
        self.assertIn("assembling_pdf", states_called)
        self.assertIn("filing_document", states_called)
        self.assertIn("filed", states_called)

        # Check filed payload
        filed_event = [e for e in status_events if e[0] == "filed"][0]
        self.assertEqual(filed_event[1]["sheet"], 1)
        self.assertEqual(filed_event[1]["pages"], 2)
        self.assertTrue(filed_event[1]["destination"].endswith(".pdf"))

    def test_retry_states(self):
        self.assertEqual(retry_state_escl(ScannerOfflineError("down")), ("scanner_offline", 15))
        self.assertEqual(retry_state_escl(ScannerBusyError("busy")), ("scanner_busy", 30))
        self.assertEqual(retry_state_escl(PaperJammedError("jam")), ("paper_blocked", 20))
        self.assertEqual(retry_state_escl(RuntimeError("other")), ("retry_after_error", 10))


if __name__ == "__main__":
    unittest.main()
