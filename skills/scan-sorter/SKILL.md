---
name: scan-sorter
description: Multi-agent mail & document triage skill for Antigravity, Codex, Claude Code, Kiro, Cursor, and Grok. Inspects, OCRs, intelligently renames, and files incoming scans from Brother DS-940DW into adaptive, emergent category folders in ~/Documents/Scans using .taxonomy.json. Use when the user asks to "sort my scans", "organize my mail", "triage new scans", "clean up my scans folder", or "process scans".
---

# Scan Sorter & Mail Organizer (Multi-Agent Skill)

Use this skill when Aaron asks to triage, sort, rename, or file scanned mail and documents.

This skill is designed for a **multi-agent workflow** across Aaron's tools:
- **Antigravity / Gemini**
- **Codex / OpenAI**
- **Claude Code**
- **Cursor**
- **Kiro / KiroCrew**
- **Grok**

The Brother DS-940DW captures sheets into `~/Documents/Scans` (synced via iCloud Drive across Mac Mini and MacBook Air) with on-device Apple Vision OCR text layers. Agents provide the cognitive, conversational filing layer.

---

## Target Paths

- **Primary Tracked Folder (synced via iCloud):** `~/Documents/Scans`
- **Living Taxonomy Ledger:** `~/Documents/Scans/.taxonomy.json`
- **Audit & Undo Log:** `~/Documents/Scans/SORT_LOG.md`
- **Remote Host:** Mac Mini at `100.96.177.69` via Tailscale / SSH

---

## Adaptive, Emergent Taxonomy (`.taxonomy.json`)

**Do NOT force documents into rigid, pre-canned buckets.** Categories emerge naturally as Aaron scans documents:

1. **Read Existing Taxonomy**: Inspect `~/Documents/Scans/.taxonomy.json` to see current categories, descriptions, and learned vendor mappings (`Austin Energy ➔ Utilities`, `State Farm ➔ Auto Insurance`).
2. **Match or Evolve**:
   - If a document clearly fits an existing category (e.g. `Utilities`), assign it there and record the vendor.
   - If a document introduces a new domain (e.g. `Auto`, `Pets`, `Housing`, `Medical`, `Crafts`, `Receipts`, `Taxes`), propose a concise, natural category (1–2 words, Title Case).
   - Never use junk-drawer categories like `Personal & Other` or `Misc`.
3. **Learn User Preferences**: If Aaron asks to rename or move a document, update `vendor_mappings` in `.taxonomy.json`.

---

## Standard Naming Convention

All sorted PDFs follow the clean, searchable naming pattern:
```
YYYY-MM-DD_[Sender]_[Document-Type].pdf
```
Examples:
- `2026-11-04_Austin-Energy_Electric-Bill.pdf`
- `2026-10-15_IRS_Tax-Notice.pdf`
- `2026-09-01_St-Davids-Hospital_Medical-Summary.pdf`
- `2024-11-15_Quilt-Camp_Flyer.pdf`

---

## Agent Workflow

### 1. Inventory & OCR Unsorted Scans
Find all unorganized `.pdf` files sitting directly in the root of `~/Documents/Scans`.
Read each document's text (already embedded via Apple Vision OCR, or run native OCR via `bin/ds940-ocr`).

Alternatively, run the helper script in dry-run preview mode:
```bash
python3 ~/Projects/ds-940-paperless-scanner/scripts/sort_scans.py --dir ~/Documents/Scans
```

### 2. Conversational Proposal
Display proposed moves to Aaron in a clear table:
```markdown
| Source File | Detected Date | Sender | Category | New Filename |
|---|---|---|---|---|
| scan-20261115-001.pdf | 2026-11-04 | Austin Energy | Utilities | 2026-11-04_Austin-Energy_Electric-Bill.pdf |
```

If any document is ambiguous (e.g., medical bill vs insurance explanation of benefits, tax notice vs financial statement), ask Aaron for clarification before moving.

### 3. Execute Moves & Update Ledger
Move the files into `~/Documents/Scans/<Category>/` and update:
- `~/Documents/Scans/.taxonomy.json` (categories and vendor mappings)
- `~/Documents/Scans/SORT_LOG.md` (date, source file, target relative path, sender, document type)

### 4. Cross-System Integrations (Optional / Contextual)
- **Actual Budget**: If a document is a receipt or bill, offer to log the transaction or check matching cleared transactions in Actual Budget.
- **Calendar / Reminders**: If a notice contains a firm payment due date or court deadline, offer to note the deadline.
