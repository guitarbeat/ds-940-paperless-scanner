from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from ds940.taxonomy import CategoryMeta, TaxonomyLedger


class TaxonomyTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)

    def test_ledger_initialization_and_persistence(self):
        ledger = TaxonomyLedger(self.root)
        self.assertEqual(len(ledger.categories), 0)

        # Register category
        cat = ledger.register_category("Utilities", description="Power & water bills", example="Austin Energy")
        self.assertEqual(cat.name, "Utilities")
        self.assertIn("Austin Energy", cat.examples)

        # Re-load from disk
        ledger2 = TaxonomyLedger(self.root)
        self.assertIn("Utilities", ledger2.categories)
        self.assertEqual(ledger2.categories["Utilities"].description, "Power & water bills")

    def test_vendor_learning(self):
        ledger = TaxonomyLedger(self.root)
        ledger.learn_mapping("Austin Energy", "Utilities")

        # Direct and case-insensitive lookup
        self.assertEqual(ledger.lookup_vendor("Austin Energy"), "Utilities")
        self.assertEqual(ledger.lookup_vendor("austin energy!"), "Utilities")
        self.assertIsNone(ledger.lookup_vendor("Random Corp"))

    def test_sync_with_filesystem(self):
        ledger = TaxonomyLedger(self.root)
        
        # Create physical subfolders with dummy PDFs
        util_dir = self.root / "Utilities"
        util_dir.mkdir()
        (util_dir / "doc1.pdf").write_bytes(b"%PDF-1.4")
        (util_dir / "doc2.pdf").write_bytes(b"%PDF-1.4")

        craft_dir = self.root / "Crafts & Hobbies"
        craft_dir.mkdir()
        (craft_dir / "pattern.pdf").write_bytes(b"%PDF-1.4")

        counts = ledger.sync_with_filesystem()
        self.assertEqual(counts["Utilities"], 2)
        self.assertEqual(counts["Crafts & Hobbies"], 1)

        active = ledger.get_active_categories()
        cat_names = [c["name"] for c in active]
        self.assertIn("Utilities", cat_names)
        self.assertIn("Crafts & Hobbies", cat_names)

    def test_clean_category_name(self):
        ledger = TaxonomyLedger(self.root)
        self.assertEqual(ledger.clean_category_name("bills_&_utilities"), "Bills & Utilities")
        self.assertEqual(ledger.clean_category_name("auto-maintenance"), "Auto-Maintenance")
        self.assertEqual(ledger.clean_category_name("personal"), "Personal")


if __name__ == "__main__":
    unittest.main()
