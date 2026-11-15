from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from ds940.ai import (
    classify_document,
    classify_with_gemini,
    classify_with_heuristic,
    extract_date_heuristic,
    sanitize_filename_part,
)


class AITests(unittest.TestCase):
    def test_sanitize_filename(self):
        self.assertEqual(sanitize_filename_part("Austin Energy / City"), "Austin-Energy-City")
        self.assertEqual(sanitize_filename_part("  Steph   Gran  "), "Steph-Gran")
        self.assertEqual(sanitize_filename_part("!!!???"), "Unknown")

    def test_extract_date_heuristic(self):
        self.assertEqual(extract_date_heuristic("Statement date 2026-11-04"), "2026-11-04")
        self.assertEqual(extract_date_heuristic("Due date: 11/04/2026 for service"), "2026-11-04")
        self.assertEqual(extract_date_heuristic("Dated November 4, 2026 in Austin"), "2026-11-04")
        self.assertTrue(extract_date_heuristic("Quilt camp 2024").startswith("2024-"))

    def test_heuristic_utility_bill(self):
        text = "Austin Energy\nAccount Number: 123456\nDue date: 2026-11-04\nTotal amount due: $142.50"
        result = classify_with_heuristic(text)
        self.assertEqual(result.category, "Bills & Utilities")
        self.assertEqual(result.date, "2026-11-04")
        self.assertIn("Bill", result.document_type)
        self.assertTrue(result.suggested_filename.startswith("2026-11-04_"))

    def test_heuristic_irs_notice(self):
        text = "Department of the Treasury\nInternal Revenue Service (IRS)\nNotice Date: 2026-10-15\nForm 1040"
        result = classify_with_heuristic(text)
        self.assertEqual(result.category, "Taxes & Financial")
        self.assertEqual(result.date, "2026-10-15")
        self.assertEqual(result.document_type, "Tax-Notice")

    def test_heuristic_medical_record(self):
        text = "St. David's Medical Center\nPatient Clinic Summary\nDoctor: Dr. Smith\nDate: 2026-09-01"
        result = classify_with_heuristic(text)
        self.assertEqual(result.category, "Medical & Health")
        self.assertEqual(result.document_type, "Medical-Record")

    def test_heuristic_insurance(self):
        text = "GEICO Auto Insurance\nPolicy Number: G-88990\nCoverage period effective 2026-08-01\nPremium"
        result = classify_with_heuristic(text)
        self.assertEqual(result.category, "Insurance")
        self.assertEqual(result.document_type, "Insurance-Document")

    def test_heuristic_quilt_camp(self):
        text = "quilt\ncamp\nHosted by Steph Gran1110\n2024"
        result = classify_with_heuristic(text)
        self.assertEqual(result.category, "Personal & Other")
        self.assertIn("Quilt-Camp", result.sender)
        self.assertTrue(result.suggested_filename.endswith(".pdf"))

    @patch("urllib.request.urlopen")
    def test_gemini_classification_mocked(self, mock_urlopen):
        mock_resp = MagicMock()
        gemini_json = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": json.dumps({
                            "sender": "Austin-Energy",
                            "date": "2026-11-04",
                            "document_type": "Electric-Bill",
                            "category": "Bills & Utilities",
                            "suggested_filename": "2026-11-04_Austin-Energy_Electric-Bill.pdf",
                            "summary": "Monthly electric utility bill for $142.50.",
                            "confidence": 0.95
                        })
                    }]
                }
            }]
        }
        mock_resp.read.return_value = json.dumps(gemini_json).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        result = classify_with_gemini("sample text", api_key="fake-key")
        self.assertEqual(result.sender, "Austin-Energy")
        self.assertEqual(result.category, "Bills & Utilities")
        self.assertEqual(result.suggested_filename, "2026-11-04_Austin-Energy_Electric-Bill.pdf")

    @patch("urllib.request.urlopen", side_effect=OSError("Network down"))
    def test_gemini_fallback_on_network_error(self, mock_urlopen):
        text = "Austin Energy\nTotal amount due: $142.50\nDate: 2026-11-04"
        result = classify_with_gemini(text, api_key="fake-key")
        # Should cleanly fallback to heuristic without raising exception
        self.assertEqual(result.category, "Bills & Utilities")
        self.assertEqual(result.date, "2026-11-04")

    def test_empty_document(self):
        result = classify_document("")
        self.assertEqual(result.category, "Personal & Other")
        self.assertIn("Scan", result.suggested_filename)


if __name__ == "__main__":
    unittest.main()
