from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from ds940.finder_tags import (
    FINDER_COLOR_BLUE,
    FINDER_COLOR_GRAY,
    FINDER_COLOR_GREEN,
    FINDER_COLOR_ORANGE,
    FINDER_COLOR_PURPLE,
    FINDER_COLOR_RED,
    FINDER_COLOR_YELLOW,
    get_finder_color,
    get_finder_metadata,
    set_finder_metadata,
    tag_all_scans,
)


class FinderTagsTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmppath = Path(self.tmpdir.name)

    def test_get_finder_color_mapping(self):
        self.assertEqual(get_finder_color("Legal & Citations"), FINDER_COLOR_PURPLE)
        self.assertEqual(get_finder_color("Court Notice"), FINDER_COLOR_PURPLE)
        self.assertEqual(get_finder_color("Medical & Health"), FINDER_COLOR_BLUE)
        self.assertEqual(get_finder_color("Labcorp Diagnostics"), FINDER_COLOR_BLUE)
        self.assertEqual(get_finder_color("Banking & Finance"), FINDER_COLOR_GREEN)
        self.assertEqual(get_finder_color("Austin Energy Bill"), FINDER_COLOR_GREEN)
        self.assertEqual(get_finder_color("Annual Tax Return"), FINDER_COLOR_RED)
        self.assertEqual(get_finder_color("Auto & Vehicles"), FINDER_COLOR_ORANGE)
        self.assertEqual(get_finder_color("TxDMV Registration"), FINDER_COLOR_ORANGE)
        self.assertEqual(get_finder_color("Utilities"), FINDER_COLOR_YELLOW)
        self.assertEqual(get_finder_color("Inbox"), FINDER_COLOR_GRAY)
        self.assertEqual(get_finder_color(""), FINDER_COLOR_GRAY)

    @unittest.skipUnless(sys.platform == "darwin", "macOS extended attributes only")
    def test_set_and_get_finder_metadata(self):
        sample_pdf = self.tmppath / "sample.pdf"
        sample_pdf.write_bytes(b"%PDF-1.4 sample content")

        success = set_finder_metadata(
            sample_pdf,
            category="Legal & Citations",
            tags=["Legal & Citations", "Austin Municipal Court"],
            title="2026-10-02 Austin Municipal Court Notice",
            keywords=["Legal & Citations", "Austin Municipal Court", "scanned"],
        )
        self.assertTrue(success)

        meta = get_finder_metadata(sample_pdf)
        self.assertIn("Legal & Citations", meta["tags"])
        self.assertIn("Austin Municipal Court", meta["tags"])
        self.assertEqual(meta["color"], FINDER_COLOR_PURPLE)
        self.assertIn("scanned", meta["keywords"])
        self.assertEqual(meta["title"], "2026-10-02 Austin Municipal Court Notice")

    def test_set_metadata_nonexistent_file_returns_false(self):
        missing = self.tmppath / "does_not_exist.pdf"
        self.assertFalse(set_finder_metadata(missing, category="Bills"))
        self.assertEqual(get_finder_metadata(missing), {"tags": [], "color": 0, "keywords": [], "title": ""})

    def test_non_darwin_platform_safely_no_ops(self):
        sample_pdf = self.tmppath / "sample.pdf"
        sample_pdf.write_bytes(b"%PDF-1.4 test")

        with patch("sys.platform", "linux"):
            self.assertFalse(set_finder_metadata(sample_pdf, category="Bills"))
            self.assertEqual(get_finder_metadata(sample_pdf), {"tags": [], "color": 0, "keywords": [], "title": ""})

    @unittest.skipUnless(sys.platform == "darwin", "macOS extended attributes only")
    def test_tag_all_scans_recursive(self):
        bills_dir = self.tmppath / "Bills"
        bills_dir.mkdir()
        b_pdf = bills_dir / "2026-10-02_Austin-Energy_Bill.pdf"
        b_pdf.write_bytes(b"%PDF-1.4 bill")

        legal_dir = self.tmppath / "Legal & Citations"
        legal_dir.mkdir()
        l_pdf = legal_dir / "2026-09-29_Austin-Municipal-Court_Notice.pdf"
        l_pdf.write_bytes(b"%PDF-1.4 legal")

        root_pdf = self.tmppath / "brother-20261116.pdf"
        root_pdf.write_bytes(b"%PDF-1.4 root")

        results = tag_all_scans(self.tmppath)
        self.assertEqual(len(results), 3)

        by_name = {r["filename"]: r for r in results}
        self.assertEqual(by_name["2026-10-02_Austin-Energy_Bill.pdf"]["color"], FINDER_COLOR_GREEN)
        self.assertEqual(by_name["2026-09-29_Austin-Municipal-Court_Notice.pdf"]["color"], FINDER_COLOR_PURPLE)
        self.assertEqual(by_name["brother-20261116.pdf"]["color"], FINDER_COLOR_GRAY)


if __name__ == "__main__":
    unittest.main()
