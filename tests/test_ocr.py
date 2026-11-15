from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from ds940.ocr import OCRPage, OCRResult, find_ocr_binary, run_ocr


class OCRTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmppath = Path(self.tmpdir.name)

    def test_empty_result(self):
        res = OCRResult.empty()
        self.assertEqual(res.page_count, 0)
        self.assertEqual(res.pages, [])
        self.assertEqual(res.full_text, "")
        self.assertEqual(res.to_dict(), {"page_count": 0, "pages": [], "full_text": ""})

    def test_missing_file_returns_empty(self):
        non_existent = self.tmppath / "does_not_exist.pdf"
        res = run_ocr(non_existent)
        self.assertEqual(res.page_count, 0)

    @patch("ds940.ocr.subprocess.run")
    @patch("ds940.ocr.find_ocr_binary")
    def test_mocked_successful_ocr(self, mock_find, mock_run):
        mock_find.return_value = Path("/usr/local/bin/ds940-ocr")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps({
            "pageCount": 2,
            "pages": [
                {"page": 1, "text": "Spectrum Internet\nAmount: $79.99"},
                {"page": 2, "text": "Terms and conditions"}
            ],
            "fullText": "Spectrum Internet\nAmount: $79.99\n\nTerms and conditions"
        })
        mock_run.return_value = mock_proc

        fake_pdf = self.tmppath / "sample.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4 sample")

        res = run_ocr(fake_pdf)
        self.assertEqual(res.page_count, 2)
        self.assertEqual(len(res.pages), 2)
        self.assertEqual(res.pages[0].page, 1)
        self.assertIn("Spectrum", res.pages[0].text)
        self.assertIn("Terms", res.pages[1].text)
        self.assertIn("Spectrum Internet", res.full_text)

    def test_real_binary_if_present(self):
        binary = find_ocr_binary()
        if not binary:
            self.skipTest("ds940-ocr binary not compiled yet")

        # Create a small valid test PDF with Pillow
        img = Image.new("RGB", (200, 200), color=(255, 255, 255))
        test_pdf = self.tmppath / "blank.pdf"
        img.save(test_pdf, "PDF")

        res = run_ocr(test_pdf)
        self.assertEqual(res.page_count, 1)
        self.assertEqual(len(res.pages), 1)


if __name__ == "__main__":
    unittest.main()
