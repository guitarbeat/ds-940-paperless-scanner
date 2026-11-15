# 🗺️ Roadmap: DS-940DW Driverless Scanner Receiver & AI Mail Sorter

## Phase 1: Core Driverless eSCL Receiver (Completed ✅)
- [x] Hardware verification: Scanner connects via Wi-Fi and responds to eSCL AirScan protocol at `:8080`.
- [x] Hardware verification: Feeder sensor status detection (`ScannerAdfEmpty` vs `ScannerAdfLoaded`).
- [x] Hardware verification: Duplex 300 DPI scan trigger and retrieval over Wi-Fi without proprietary drivers.
- [x] Implement `ds940/escl.py` module to replace closed-source x86 `brscan5`/SANE driver.
- [x] Connect `escl.py` to `spool.py` for atomic PDF assembly via Pillow.
- [x] End-to-end hands-free scan-to-folder live test on Mac Mini.
- [x] Configure `launchd` background service for 24/7 always-on operation on macOS (Apple Silicon).
- [x] Comprehensive unit tests passing with zero errors.

---

## Phase 2: Configuration & Monitoring GUI (Pinned 📌)
Add user-facing controls and visibility so the service isn't a black box:

### A. Web Dashboard (`http://mac-mini.local:9400`)
- Accessible from both **MacBook Air** and **Mac Mini** (and mobile devices).
- **Settings & Controls**:
  - **Save Destination**: Interactive folder picker / path editor (e.g. `~/Documents/Scans`, Desktop, or external storage).
  - **Feeder Polling Frequency**: Configurable interval slider (e.g. 2s, 3s, 5s) with adaptive sleep backoff.
  - **RAM & Resource Guard**: Live memory and CPU consumption monitor with configurable memory ceiling and auto-recovery (e.g. gracefully recycle worker if memory exceeds 200MB).
  - **Scan Profiles**: Quick toggles for DPI (150 / 300 / 600), Color vs Grayscale, and Simplex / Duplex.
  - [x] Multi-Page Collation Timeout: Multi-sheet buffer (8-second window) automatically merges sequential sheets into a single unified multi-page PDF.
- **Status & History**:
  - Live scanner state indicator (`Waiting for paper`, `Scanning`, `Offline`, `Jammed`).
  - Recent scans list with 1-click preview and download.
  - Real-time service log viewer.

### B. macOS Menu Bar Utility (Status Item)
- Discrete scanner icon in the macOS status bar on the Mac Mini.
- Status dropdown showing current connectivity.
- Quick shortcuts: "Open Scans Folder", "Pause Scanning", "Preferences...".

---

## Phase 3: Native Apple Vision OCR & AI Mail Assistant (Option 1)
Build a lightweight, zero-bloat mail filing assistant that runs entirely on macOS:

1. **Native Apple Vision OCR:**
   - On-device text recognition using Apple's Vision framework (`VNRecognizeTextRequest`).
   - Hardware-accelerated by the Apple Neural Engine (ANE) in milliseconds.
   - Embeds a hidden searchable text layer directly into the scanned PDF.
   - Enables instant full-text search via macOS Spotlight (`Cmd + Space`).

2. **AI Document Titling & Metadata Extraction:**
   - Lightweight hook triggered upon document completion.
   - Prompts an LLM (Gemini API or local model) with the extracted text to classify:
     - `sender`: Organization or business name (e.g., "Austin Water", "IRS", "Geico").
     - `date`: Document or billing date (format `YYYY-MM-DD`).
     - `document_type`: "Bill", "Tax Notice", "Invoice", "Insurance Policy", "Receipt".
     - `category`: "Utilities", "Taxes", "Insurance", "Medical", "Banking".
   - Automatically renames file to `YYYY-MM-DD_[Sender]_[Document_Type].pdf`.
   - Files into `~/Documents/Scans/<Category>/` and assigns matching macOS Finder tags.

3. **Optional Paperless-ngx Ingestion:**
   - Optional webhook/REST API forwarder to push copies to a self-hosted Paperless-ngx instance if desired.
