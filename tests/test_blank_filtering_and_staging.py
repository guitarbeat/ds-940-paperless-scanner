from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock

from PIL import Image, ImageDraw
try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

from ds940.escl import EsclScanner, ScannerStatus
from ds940.spool import (
    ScanSpool,
    calculate_ink_coverage,
    filter_blank_pages,
    is_blank_page,
)
from ds940.worker import capture_if_ready_escl


def make_white_image(path: Path, size=(300, 300)) -> Path:
    img = Image.new("RGB", size, color=(255, 255, 255))
    img.save(path, "JPEG", dpi=(300, 300))
    return path


def make_text_image(path: Path, size=(300, 300)) -> Path:
    img = Image.new("RGB", size, color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    # Draw dark content (rectangles / text blocks) in central region
    draw.rectangle([50, 50, 250, 150], fill=(20, 20, 20))
    draw.rectangle([50, 180, 250, 240], fill=(30, 30, 30))
    img.save(path, "JPEG", dpi=(300, 300))
    return path


def make_roller_shadow_image(path: Path, size=(300, 300)) -> Path:
    """Simulate a pure blank sheet with a 4px black scanner roller edge shadow."""
    img = Image.new("RGB", size, color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    # 4px border (well within 3% of 300px = 9px)
    draw.rectangle([0, 0, 300, 4], fill=(0, 0, 0))
    draw.rectangle([0, 296, 300, 300], fill=(0, 0, 0))
    img.save(path, "JPEG", dpi=(300, 300))
    return path


def make_bleed_through_image(path: Path, size=(300, 300)) -> Path:
    """Simulate thin paper with faint gray bleed-through shadows from opposite side (gray ~230)."""
    img = Image.new("RGB", size, color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    # Faint bleed-through shadow (luminosity 230 > 210 cutoff)
    draw.rectangle([40, 40, 260, 260], fill=(230, 230, 230))
    img.save(path, "JPEG", dpi=(300, 300))
    return path


class BlankFilteringAndStagingTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.output = self.root / "output"
        self.output.mkdir()

    def test_calculate_ink_coverage_pure_white(self):
        p = make_white_image(self.work / "white.jpg")
        cov = calculate_ink_coverage(p)
        self.assertEqual(cov, 0.0)

    def test_calculate_ink_coverage_roller_shadow_ignored(self):
        p = make_roller_shadow_image(self.work / "roller.jpg")
        cov = calculate_ink_coverage(p, margin_ratio=0.03)
        # Margin crop should remove the 4px border, yielding 0.0 ink coverage
        self.assertEqual(cov, 0.0)

    def test_calculate_ink_coverage_bleed_through_ignored(self):
        p = make_bleed_through_image(self.work / "bleed.jpg")
        cov = calculate_ink_coverage(p, ink_threshold=210)
        # Luminance 230 is above 210 cutoff, so not counted as ink
        self.assertEqual(cov, 0.0)

    def test_calculate_ink_coverage_real_content(self):
        p = make_text_image(self.work / "text.jpg")
        cov = calculate_ink_coverage(p)
        self.assertGreater(cov, 0.20)

    def test_is_blank_page(self):
        white = make_white_image(self.work / "w.jpg")
        text = make_text_image(self.work / "t.jpg")
        self.assertTrue(is_blank_page(white, threshold=0.05))
        self.assertFalse(is_blank_page(text, threshold=0.05))
        # Threshold 0.0 disables blank detection
        self.assertFalse(is_blank_page(white, threshold=0.0))

    def test_filter_blank_pages_prunes_blanks(self):
        p1 = make_text_image(self.work / "p1.jpg")
        p2 = make_white_image(self.work / "p2.jpg")
        kept, discarded = filter_blank_pages([p1, p2], threshold=0.05)
        self.assertEqual(kept, [p1])
        self.assertEqual(discarded, [p2])

    def test_filter_blank_pages_all_blank_retains_page_1(self):
        # Retain Page 1 Invariant
        p1 = make_white_image(self.work / "blank1.jpg")
        p2 = make_white_image(self.work / "blank2.jpg")
        kept, discarded = filter_blank_pages([p1, p2], threshold=0.05)
        self.assertEqual(kept, [p1])
        self.assertEqual(discarded, [p2])

    def test_worker_escl_prunes_blank_backside_in_duplex_job(self):
        spool = ScanSpool(self.work, self.output, remove_blank_threshold=0.05)
        scanner = MagicMock(spec=EsclScanner)
        scanner.get_status.return_value = ScannerStatus(state="Idle", adf_state="ScannerAdfLoaded")
        scanner.start_scan_job.return_value = "http://192.168.0.25:8080/eSCL/ScanJobs/duplex"

        def mock_fetch(job_url, folder, start_page=1, **kwargs):
            # Page 1: front with text; Page 2: blank backside
            f1 = make_text_image(folder / f"page-{start_page}.jpg")
            f2 = make_white_image(folder / f"page-{start_page + 1}.jpg")
            return [f1, f2]

        scanner.fetch_job_pages.side_effect = mock_fetch

        scan_id = capture_if_ready_escl(scanner, spool, "a4")
        self.assertIsNotNone(scan_id)

        delivered = list(self.output.glob("*.pdf"))
        self.assertEqual(len(delivered), 1)

        # Verify PDF has exactly 1 page
        if PdfReader is not None:
            reader = PdfReader(BytesIO(delivered[0].read_bytes()))
            self.assertEqual(len(reader.pages), 1)

        # Verify manifest recorded filtered blank pages
        manifest_path = spool.jobs / scan_id / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["pages"], 1)
        self.assertEqual(manifest["raw_pages"], 2)
        self.assertEqual(manifest["discarded_blank_pages"], 1)

    def test_worker_escl_all_blank_retains_page_1_without_crashing(self):
        spool = ScanSpool(self.work, self.output, remove_blank_threshold=0.05)
        scanner = MagicMock(spec=EsclScanner)
        scanner.get_status.return_value = ScannerStatus(state="Idle", adf_state="ScannerAdfLoaded")
        scanner.start_scan_job.return_value = "http://192.168.0.25:8080/eSCL/ScanJobs/all_blank"

        def mock_fetch(job_url, folder, start_page=1, **kwargs):
            f1 = make_white_image(folder / f"page-{start_page}.jpg")
            f2 = make_white_image(folder / f"page-{start_page + 1}.jpg")
            return [f1, f2]

        scanner.fetch_job_pages.side_effect = mock_fetch

        scan_id = capture_if_ready_escl(scanner, spool, "a4")
        self.assertIsNotNone(scan_id)

        delivered = list(self.output.glob("*.pdf"))
        self.assertEqual(len(delivered), 1)

        if PdfReader is not None:
            reader = PdfReader(BytesIO(delivered[0].read_bytes()))
            self.assertEqual(len(reader.pages), 1)

    def test_atomic_staging_guard_publishes_cleanly(self):
        spool = ScanSpool(self.work, self.output)
        folder = spool.new_scan()
        p1 = make_text_image(folder / "page-1.jpg")
        p2 = make_text_image(folder / "page-2.jpg")

        from ds940.spool import build_pdf
        build_pdf([p1, p2], folder / "document.pdf")

        manifest_path = folder / "manifest.json"
        manifest_path.write_text(json.dumps({"phase": "ready", "pages": 2}), encoding="utf-8")

        success = spool.publish(folder)
        self.assertTrue(success)

        delivered = list(self.output.glob("*.pdf"))
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0].name, f"{folder.name}.pdf")

        # Verify no .part or .staging files remain in output
        leftovers = [p.name for p in self.output.iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
