#!/usr/bin/env python3
import json
import os
from pathlib import Path
import shutil
import sys
import time

repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from ds940.finder_tags import set_finder_metadata
from ds940.taxonomy import TaxonomyLedger

_icloud_scans = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "brother-scans"
SCANS_DIR = _icloud_scans if _icloud_scans.exists() else Path.home() / "Documents" / "brother-scans"

FILES_TO_SORT = [
    {
        "source": "brother-20261117-184650-3996dde1.pdf",
        "category": "Banking & Finance",
        "suggested_filename": "2026-03-11_Wells-Fargo_Returned-Item-Notice.pdf",
        "sender": "Wells Fargo",
        "document_type": "Returned Item Notice",
    },
    {
        "source": "brother-20261117-184827-56c48e85.pdf",
        "category": "Medical & Health",
        "suggested_filename": "2025-07-03_BCBSTX_Claim-Refund-Notice.pdf",
        "sender": "BlueCross BlueShield of Texas",
        "document_type": "Claim Refund Notice",
    },
    {
        "source": "brother-20261117-184929-ea69f92d.pdf",
        "category": "Legal & Citations",
        "suggested_filename": "2026-01-27_UT-Austin-CCS_Parking-Citation-Notice.pdf",
        "sender": "Citation Collection Services",
        "document_type": "Parking Citation Notice",
    },
    {
        "source": "brother-20261117-185000-54c06cc5.pdf",
        "category": "Legal & Citations",
        "suggested_filename": "2025-11-18_UT-Austin-CCS_Parking-Citation-Notice.pdf",
        "sender": "Citation Collection Services",
        "document_type": "Parking Citation Notice",
    },
    {
        "source": "brother-20261117-185047-a2b2cacb.pdf",
        "category": "Gaming & Hobbies",
        "suggested_filename": "2026-11-17_DnD_Naum-Warlock-Character-Sheet.pdf",
        "sender": "Dungeons & Dragons",
        "document_type": "Character Sheet",
    },
    {
        "source": "brother-20261117-185448-96055dc5.pdf",
        "category": "Legal & Citations",
        "suggested_filename": "2025-12-18_Austin-Municipal-Court_Parking-Citation-Warning.pdf",
        "sender": "Austin Municipal Court",
        "document_type": "Parking Citation Warning",
    },
    {
        "source": "brother-20261117-185610-bfb0473c.pdf",
        "category": "Banking & Finance",
        "suggested_filename": "2026-11-02_Texas-Comptroller_Unclaimed-Property-Warrant.pdf",
        "sender": "Texas Comptroller",
        "document_type": "Unclaimed Property Warrant",
    },
    {
        "source": "brother-20261117-185730-138f3683.pdf",
        "category": "Auto & Vehicles",
        "suggested_filename": "2025-12-04_GEICO_Auto-Insurance-Policy.pdf",
        "sender": "GEICO",
        "document_type": "Auto Liability Insurance Policy",
    },
    {
        "source": "brother-20261117-191033-be9bc78d.pdf",
        "category": "Legal & Citations",
        "suggested_filename": "2025-09-30_Austin-Municipal-Court_Parking-Citation-Warning.pdf",
        "sender": "Austin Municipal Court",
        "document_type": "Parking Citation Warning",
    },
    {
        "source": "2025-06-13_TxDMV_Certificate-of-Title_2014-VW-Jetta.pdf",
        "category": "Auto & Vehicles",
        "suggested_filename": "2025-06-13_TxDMV_Certificate-of-Title_2014-VW-Jetta.pdf",
        "sender": "Texas DMV",
        "document_type": "Certificate of Title",
    },
    {
        "source": "2026-11-17_Charles-Schwab_Class-Action-Settlement.pdf",
        "category": "Banking & Finance",
        "suggested_filename": "2026-11-17_Charles-Schwab_Class-Action-Settlement.pdf",
        "sender": "Charles Schwab",
        "document_type": "Class Action Settlement Notice",
    },
]

def main():
    moved_count = 0
    results = []

    log_path = SCANS_DIR / "SORT_LOG.md"

    for item in FILES_TO_SORT:
        src = SCANS_DIR / item["source"]
        if not src.exists():
            print(f"Skipping {item['source']}: file not found")
            continue

        cat_dir = SCANS_DIR / item["category"]
        cat_dir.mkdir(parents=True, exist_ok=True)

        target = cat_dir / item["suggested_filename"]
        if target.exists() and target != src:
            stem = target.stem
            ext = target.suffix
            target = cat_dir / f"{stem}_{int(time.time()) % 10000:04d}{ext}"

        print(f"Moving: {src.name} -> {item['category']}/{target.name}")
        shutil.move(src, target)
        moved_count += 1

        # Set macOS Finder tags and Spotlight keywords
        try:
            set_finder_metadata(
                target,
                category=item["category"],
                tags=[t for t in [item["category"], item["sender"], "Brother DS-940DW"] if t],
                title=item["suggested_filename"],
                keywords=[k for k in [item["category"], item["sender"], "scanned", "brother-ds940dw"] if k],
            )
        except Exception as e:
            print(f"  Warning: failed to set finder metadata: {e}")

        # Append to SORT_LOG.md
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with log_path.open("a", encoding="utf-8") as f:
            entry = f"- **{ts}**: Moved `{item['source']}` → `{item['category']}/{target.name}` (Sender: *{item['sender']}*, Type: *{item['document_type']}*)\n"
            f.write(entry)

        results.append({
            "source": item["source"],
            "target": f"{item['category']}/{target.name}",
            "category": item["category"],
            "sender": item["sender"],
            "document_type": item["document_type"],
        })

    # Sync and clean taxonomy
    ledger = TaxonomyLedger(SCANS_DIR)
    ledger.sync_with_filesystem()

    # Clean legacy heuristic mappings
    data = ledger.to_dict()
    clean_categories = {}
    for cat_name, cat_obj in data.get("categories", {}).items():
        if cat_obj.get("count", 0) > 0:
            clean_categories[cat_name] = cat_obj
    data["categories"] = clean_categories

    clean_mappings = {
        "geico": "Auto & Vehicles",
        "txdmv": "Auto & Vehicles",
        "texas dmv": "Auto & Vehicles",
        "wells fargo": "Banking & Finance",
        "charles schwab": "Banking & Finance",
        "texas comptroller": "Banking & Finance",
        "chase": "Banking & Finance",
        "bluecross blueshield of texas": "Medical & Health",
        "labcorp": "Medical & Health",
        "citation collection services": "Legal & Citations",
        "austin municipal court": "Legal & Citations",
        "linebarger goggan blair & sampson": "Legal & Citations",
        "dungeons & dragons": "Gaming & Hobbies",
    }
    data["vendor_mappings"] = clean_mappings
    data["updated_at"] = time.time()

    tax_file = SCANS_DIR / ".taxonomy.json"
    with tax_file.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    print(f"\nSuccessfully moved and tagged {moved_count} scans.")
    print(json.dumps(results, indent=2))

if __name__ == "__main__":
    main()
