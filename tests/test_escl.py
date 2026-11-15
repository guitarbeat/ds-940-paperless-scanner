from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from ds940.escl import (
    EsclError,
    EsclScanner,
    PaperJammedError,
    ScannerBusyError,
    ScannerOfflineError,
    ScannerStatus,
)


SAMPLE_STATUS_EMPTY = b"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerStatus xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03" xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
    <pwg:Version>2.63</pwg:Version>
    <pwg:State>Idle</pwg:State>
    <scan:AdfState>ScannerAdfEmpty</scan:AdfState>
</scan:ScannerStatus>"""

SAMPLE_STATUS_LOADED = b"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerStatus xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03" xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
    <pwg:Version>2.63</pwg:Version>
    <pwg:State>Idle</pwg:State>
    <scan:AdfState>ScannerAdfLoaded</scan:AdfState>
</scan:ScannerStatus>"""

SAMPLE_STATUS_JAMMED = b"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerStatus xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03" xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
    <pwg:Version>2.63</pwg:Version>
    <pwg:State>Stopped</pwg:State>
    <scan:AdfState>ScannerAdfJammed</scan:AdfState>
</scan:ScannerStatus>"""


class FakeResponse:
    def __init__(self, data: bytes, status: int = 200, headers: dict | None = None):
        self.data = data
        self.status = status
        self.headers = headers or {}

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class EsclScannerTests(unittest.TestCase):
    def setUp(self):
        self.scanner = EsclScanner("192.168.0.25", port=8080)

    @patch("urllib.request.urlopen")
    def test_status_empty_feeder(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(SAMPLE_STATUS_EMPTY)
        status = self.scanner.get_status()
        self.assertTrue(status.is_paper_empty)
        self.assertFalse(status.is_paper_loaded)
        self.assertTrue(status.is_idle)
        self.assertFalse(status.is_jammed)

    @patch("urllib.request.urlopen")
    def test_status_loaded_feeder(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(SAMPLE_STATUS_LOADED)
        status = self.scanner.get_status()
        self.assertTrue(status.is_paper_loaded)
        self.assertFalse(status.is_paper_empty)
        self.assertTrue(status.is_idle)

    @patch("urllib.request.urlopen")
    def test_status_jammed_feeder(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(SAMPLE_STATUS_JAMMED)
        status = self.scanner.get_status()
        self.assertTrue(status.is_jammed)
        self.assertFalse(status.is_idle)

    @patch("urllib.request.urlopen")
    def test_status_offline_raises(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        with self.assertRaises(ScannerOfflineError):
            self.scanner.get_status()

    @patch("urllib.request.urlopen")
    def test_start_scan_job_returns_location(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(b"", status=201, headers={"Location": "/eSCL/ScanJobs/1"})
        job_url = self.scanner.start_scan_job(duplex=True, dpi=300)
        self.assertEqual(job_url, "http://192.168.0.25:8080/eSCL/ScanJobs/1")

    @patch("urllib.request.urlopen")
    def test_start_scan_job_busy_raises(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "http://192.168.0.25:8080/eSCL/ScanJobs", 409, "Conflict", {}, io.BytesIO()
        )
        with self.assertRaises(ScannerBusyError):
            self.scanner.start_scan_job()

    @patch("urllib.request.urlopen")
    def test_fetch_job_pages_duplex(self, mock_urlopen):
        page1 = b"fake-jpeg-data-front"
        page2 = b"fake-jpeg-data-back"

        # Sequential calls: page1 (200), page2 (200), EOF (404), DELETE (200)
        mock_urlopen.side_effect = [
            FakeResponse(page1, status=200),
            FakeResponse(page2, status=200),
            urllib.error.HTTPError("url", 404, "Not Found", {}, io.BytesIO()),
            FakeResponse(b"", status=200),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir)
            pages = self.scanner.fetch_job_pages("http://192.168.0.25:8080/eSCL/ScanJobs/1", folder)
            self.assertEqual(len(pages), 2)
            self.assertEqual(pages[0].read_bytes(), page1)
            self.assertEqual(pages[1].read_bytes(), page2)


if __name__ == "__main__":
    unittest.main()
