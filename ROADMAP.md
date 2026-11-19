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
- [x] Document integrity: Atomic staging guard preventing partial-file consumption (`.staging_xxx.tmp` + `os.replace`).
- [x] Document integrity: Automatic blank page detection and suppression with Page 1 safety invariant (`REMOVE_BLANK_THRESHOLD=0.05`).
- [x] macOS integration: Native Finder color tags and Spotlight metadata attributes (`ds940/finder_tags.py`).

---

## Phase 2: Configuration & Monitoring GUI (Pinned 📌)
Add user-facing controls and visibility so the service isn't a black box:

### A. Web Dashboard (`https://scans.alw.lol` / `:9400`)
- Accessible from both **MacBook Air** and **Mac Mini** (and mobile devices).
- **Settings & Controls**:
  - **Save Destination**: Interactive folder picker / path editor (e.g. `~/Documents/brother-scans`, Desktop, or external storage).
  - **Feeder Polling Frequency**: Configurable interval slider (e.g. 2s, 3s, 5s) with adaptive sleep backoff.
  - **RAM & Resource Guard**: Live memory and CPU consumption monitor with configurable memory ceiling and auto-recovery.
  - **Scan Profiles & Presets**: 1-click toggles for DPI (150 / 300 / 600), Color vs Grayscale, and Duplex vs Simplex (Fast Document, Archival Text, High-Res Photo).
  - **Persistent Settings Drawer**: File-backed `~/.config/ds940/scanner.conf` edited directly from the web drawer without modifying system launchd plists.
  - [x] Multi-Page Collation Timeout: Multi-sheet buffer (8-second window) automatically merges sequential sheets into a single unified multi-page PDF.
- **Status & History**:
  - [x] Live scanner state indicator (`Waiting for paper`, `Scanning`, `Offline`, `Jammed`).
  - [x] Recent scans list with 1-click preview and download.
  - [x] Real-time service log viewer, hardware probe, and diagnostics inspector (press `D` or click toolbar icon).

### B. Audible State Chimes (`afplay`)
- Native macOS CoreAudio chime notifications via background `afplay`:
  - Sheet feed started (discrete tock).
  - Sheet collation window open (waiting for next page).
  - PDF assembly and categorization completed (subtle chime).
  - Paper jam or feeder error (basso alert).

### C. macOS Menu Bar Utility (Status Item)
- Discrete scanner icon in the macOS status bar on the Mac Mini.
- Status dropdown showing current connectivity.
- Quick shortcuts: "Open Scans Folder", "Pause Scanning", "Preferences...".

---

## Phase 3: Native Apple Vision OCR & AI Mail Assistant
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
   - Files into `~/Documents/brother-scans/<Category>/` and assigns matching macOS Finder tags.

3. **Duplicate Scan Suppression:**
   - Perceptual difference hash (dHash) and OCR text-layer comparison on ingested documents.
   - Automatically suppresses duplicate file creation and flags duplicate mail.

4. **Direct Paperless-ngx REST Ingestion:**
   - Direct HTTP POST API forwarder to ingest documents into a self-hosted Paperless-ngx instance via token auth, bypassing network file sharing permissions and inotify drops.

---

## Phase 4: Advanced Hardware Presets & Scanbro Insights (Planned 💡)
Features and enhancements cherry-picked from [Ruegen/scanbro](https://github.com/Ruegen/scanbro) for DS-940DW capabilities:

1. **Long Receipt Scanning Mode (`long_receipt`):**
   - Extend hardware scanning limits beyond the standard 14" duplex ceiling up to **31.5 inches (~80 cm / 9450 units @ 300ths of an inch)**.
   - Enforce 1-sided simplex capture (`<scan:Duplex>false</scan:Duplex>`) tailored for grocery, pharmacy, and retail receipts.
   - Inject `<scan:AutoCrop>true</scan:AutoCrop>` to trigger Brother's hardware tail sensor, stopping feed rollers immediately when paper finishes rather than scanning empty air.
   - UI preset toggle in the dashboard toolbar and keyboard shortcut (`L`).

2. **Intelligent Content-Box Auto-Cropping (`crop.py`):**
   - Feeder-aware whitespace trimmer for receipts, checks, and business cards (which otherwise leave ~5.5 inches of blank scanner bed on an 8.5" swath).
   - Fast 480 px downscaled luma detector (`CONTENT_LUMA = 236`) with customizable padding (`0.12"`).
   - Origin-anchored left/top margins (`ORIGIN_ALIGN_FRAC = 0.08`) preserving headers and logos on the DS-940DW's top-fed, left-aligned tray while pruning trailing blank paper.

3. **Zero-Config mDNS Network Discovery (`_uscan._tcp.local.`):**
   - Automatic background browse for Brother eSCL scanners using Apple Bonjour / mDNS (`_uscan._tcp.local.`).
   - Dynamically resolves scanner IP address and adapts if the router assigns a new DHCP lease, removing the requirement for static IP assignment.

4. **Mailbox-Safe Compressed PDF Export (< 5 MB):**
   - Smart size-bounded PDF compression utility for the Quick Look inspector (`Export for Email`).
   - Iteratively adjusts scale and JPEG quality (targeting 78%–38%) to guarantee the output remains strictly under 5 MB for email and messaging attachments.

5. **Wired USB Scanning Fallback (SANE/libusb `04f9:0466`):**
   - USB backend communication for Brother vendor ID `04f9` / product `0466`.
   - Allows seamless zero-network scanning over the USB-C cable when the scanner's Wi-Fi switch is turned off.
