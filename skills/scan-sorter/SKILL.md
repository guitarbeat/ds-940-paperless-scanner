---
name: scan-sorter
description: Triage, inspect, OCR, rename, and organize incoming scanned documents and mail from Brother DS-940DW into categorized folders in ~/Documents/Scans (Bills & Utilities, Taxes & Financial, Medical & Health, Insurance, Receipts, Personal & Other). Use when the user asks to "sort my scans", "organize my mail", "triage new scans", "clean up my scans folder", or "process scans".
---

# Scan Sorter & Mail Organizer

Use this skill to inspect, OCR, intelligently rename, and organize incoming PDF scans from the Brother DS-940DW scanner into clean, permanent category folders.

## Target Paths

- **Primary Tracked Folder (synced via iCloud):** `~/Documents/Scans`
- **Remote Host:** Mac Mini at `100.96.177.69` via Tailscale / SSH

## Category Structure

Unsorted scans sit in the root of the scans directory. Sorted scans are placed into these standardized category subfolders:

| Category Folder | Typical Documents |
|---|---|
| `Bills & Utilities/` | Electric, water, gas, internet (Spectrum), cell phone (AT&T, Verizon), trash, recurring bills |
| `Taxes & Financial/` | IRS notices, W-2, 1099, Form 1040, bank statements (Chase, Wells Fargo), brokerage/investments |
| `Medical & Health/` | Clinic summaries, hospital bills, doctor letters, prescriptions, lab results, dental/vision |
| `Insurance/` | Auto, home, health, life insurance policies, renewal notices, claim forms, coverage summaries |
| `Receipts/` | Store receipts, purchase invoices, equipment/hardware receipts |
| `Personal & Other/` | Event flyers, invitations, personal letters, non-bill correspondence |

## Standard Naming Convention

All sorted PDFs follow the clean naming pattern:
```
YYYY-MM-DD_[Sender]_[Document-Type].pdf
```
Examples:
- `2026-11-04_Austin-Energy_Electric-Bill.pdf`
- `2026-10-15_IRS_Tax-Notice.pdf`
- `2026-09-01_St-Davids-Hospital_Medical-Summary.pdf`
- `2024-11-15_Quilt-Camp_Flyer.pdf`

---

## Workflow

### 1. Inventory & Preview Unsorted Scans
Run the helper tool in dry-run preview mode:

```bash
# On Mac Mini or MacBook Air locally:
python3 ~/Projects/ds-940-paperless-scanner/scripts/sort_scans.py --dir ~/Documents/Scans

# Or remotely from MacBook Air:
ssh aaron@100.96.177.69 "python3 ~/Projects/ds-940-paperless-scanner/scripts/sort_scans.py --dir ~/Documents/Scans"
```

The tool uses native Apple Vision OCR (`bin/ds940-ocr`) to read the text in milliseconds and determines the date, sender, document type, and category.

### 2. Present Proposal to User
Display the proposed moves in a clean markdown table:
```markdown
| Source File | Detected Date | Sender | Category | New Filename |
|---|---|---|---|---|
| live_test_scan.pdf | 2024-11-15 | Quilt-Camp | Personal & Other | 2024-11-15_Quilt-Camp_Flyer.pdf |
```

If the user wants any adjustments (e.g. custom category or different title), modify the plan accordingly.

### 3. Apply the File Moves
Execute the moves and create the category subfolders:

```bash
# On Mac Mini or MacBook Air locally:
python3 ~/Projects/ds-940-paperless-scanner/scripts/sort_scans.py --dir ~/Documents/Scans --apply

# Or remotely from MacBook Air:
ssh aaron@100.96.177.69 "python3 ~/Projects/ds-940-paperless-scanner/scripts/sort_scans.py --dir ~/Documents/Scans --apply"
```

### 4. Verify & Confirm
- Verify that the moved files exist in their target category subfolder.
- Note that an entry is automatically appended to `~/Documents/Scans/SORT_LOG.md` for tracking and easy undo.
- Report completion to the user with the final destination paths.
