#!/usr/bin/env python3
"""CLI utility to inventory, OCR, classify, and sort scanned documents into category folders."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import time

# Ensure ds940 package is importable
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from ds940.ai import DEFAULT_CATEGORY, classify_document
from ds940.ocr import run_ocr
from ds940.taxonomy import TaxonomyLedger


def find_unsorted_scans(scans_dir: Path) -> list[Path]:
    """Find all unorganized PDFs in the top-level scans directory."""
    if not scans_dir.exists():
        return []
    # Collect files matching brother-*.pdf or any PDF sitting directly in the root
    unsorted = []
    for item in sorted(scans_dir.iterdir()):
        if item.is_file() and item.suffix.lower() == ".pdf" and not item.name.startswith("."):
            unsorted.append(item)
    return unsorted


def inspect_and_classify(
    pdf_path: Path,
    api_key: str | None = None,
    ledger: TaxonomyLedger | None = None,
) -> dict:
    """Run OCR and classify a single PDF with adaptive taxonomy support."""
    ocr_result = run_ocr(pdf_path)
    classification = classify_document(ocr_result.full_text, api_key=api_key, ledger=ledger)
    return {
        "source": str(pdf_path),
        "source_name": pdf_path.name,
        "page_count": ocr_result.page_count,
        "text_sample": ocr_result.full_text[:300].replace("\n", " "),
        "date": classification.date,
        "sender": classification.sender,
        "document_type": classification.document_type,
        "category": classification.category,
        "suggested_filename": classification.suggested_filename,
        "confidence": classification.confidence,
    }


def sort_scans(
    scans_dir: Path,
    dry_run: bool = True,
    api_key: str | None = None,
) -> list[dict]:
    """Inspect all unsorted scans and return plan or execute moves."""
    unsorted = find_unsorted_scans(scans_dir)
    results = []
    ledger = TaxonomyLedger(scans_dir)

    for pdf in unsorted:
        item = inspect_and_classify(pdf, api_key=api_key, ledger=ledger)
        target_dir = scans_dir / item["category"]
        target_path = target_dir / item["suggested_filename"]

        if target_path.exists() and target_path != pdf:
            # Handle collision
            stem = target_path.stem
            suffix = target_path.suffix
            target_path = target_dir / f"{stem}_{int(time.time()) % 10000:04d}{suffix}"

        item["target_path"] = str(target_path)
        item["target_relative"] = f"{item['category']}/{target_path.name}"

        if not dry_run:
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(pdf, target_path)
            item["status"] = "moved"
            # Update log
            log_path = scans_dir / "SORT_LOG.md"
            with log_path.open("a", encoding="utf-8") as log_file:
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                log_file.write(
                    f"- **{ts}**: Moved `{pdf.name}` → `{item['target_relative']}` (Sender: *{item['sender']}*, Type: *{item['document_type']}*)\n"
                )
        else:
            item["status"] = "preview"

        results.append(item)

    if not dry_run and results:
        ledger.sync_with_filesystem()

    return results


def main():
    parser = argparse.ArgumentParser(description="Triage and sort scanned documents into category folders.")
    parser.add_argument(
        "--dir",
        default=str(Path.home() / "Documents" / "Scans"),
        help="Path to scans directory (default: ~/Documents/Scans)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute the file moves (default is dry-run preview)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )
    args = parser.parse_args()

    scans_path = Path(args.dir).expanduser().resolve()
    api_key = os.environ.get("GEMINI_API_KEY", "").strip() or None

    results = sort_scans(scans_path, dry_run=not args.apply, api_key=api_key)

    if args.json:
        print(json.dumps(results, indent=2))
        return

    if not results:
        print(f"No unsorted scans found in {scans_path}")
        return

    mode = "EXECUTED MOVES" if args.apply else "DRY-RUN PREVIEW"
    print(f"\n📁 Scan Sorting: {mode} ({len(results)} file(s))\n")
    print(f"{'Source File':<36} {'Category':<22} {'New Filename':<40}")
    print("-" * 100)
    for r in results:
        print(f"{r['source_name']:<36} {r['category']:<22} {r['suggested_filename']:<40}")
    print("-" * 100)

    if not args.apply:
        print("\n💡 Run with --apply to perform these file moves, or invoke via the scan-sorter skill.\n")


if __name__ == "__main__":
    main()
