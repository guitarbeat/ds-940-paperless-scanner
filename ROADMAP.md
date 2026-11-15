# 🗺️ Roadmap: DS-940DW Driverless Scanner Receiver

## Phase 1: Core Driverless eSCL Receiver (In Progress)
- [x] Hardware verification: Scanner connects via Wi-Fi and responds to eSCL AirScan protocol at `:8080`.
- [x] Hardware verification: Feeder sensor status detection (`ScannerAdfEmpty` vs `ScannerAdfLoaded`).
- [x] Hardware verification: Duplex 300 DPI scan trigger and retrieval over Wi-Fi without proprietary drivers.
- [ ] Implement `ds940/escl.py` module to replace closed-source x86 `brscan5`/SANE driver.
- [ ] Connect `escl.py` to `spool.py` for atomic PDF assembly via Pillow.
- [ ] End-to-end hands-free scan-to-folder live test on Mac Mini.
- [ ] Configure `launchd` background service for 24/7 always-on operation on macOS (Apple Silicon).

---

## Phase 2: Configuration & Monitoring GUI (Pinned 📌)
Add user-facing controls and visibility so the service isn't a black box:

### A. Web Dashboard (`http://mac-mini.local:9400`)
- Accessible from both **MacBook Air** and **Mac Mini** (and mobile devices).
- **Settings & Controls**:
  - **Save Destination**: Interactive folder picker / path editor (e.g. `/Users/aaron/Documents/Scans`, Desktop, or external storage).
  - **Feeder Polling Frequency**: Configurable interval slider (e.g. 2s, 3s, 5s) with adaptive sleep backoff.
  - **RAM & Resource Guard**: Live memory and CPU consumption monitor with configurable memory ceiling and auto-recovery (e.g. gracefully recycle worker if memory exceeds 200MB).
  - **Scan Profiles**: Quick toggles for DPI (150 / 300 / 600), Color vs Grayscale, and Simplex / Duplex.
  - **Multi-Page Collation Timeout**: Optional multi-sheet buffer (e.g. combine sheets fed within 5 seconds into a single document).
- **Status & History**:
  - Live scanner state indicator (`Waiting for paper`, `Scanning`, `Offline`, `Jammed`).
  - Recent scans list with 1-click preview and download.
  - Real-time service log viewer.

### B. macOS Menu Bar Utility (Status Item)
- Discrete scanner icon in the macOS status bar on the Mac Mini.
- Status dropdown showing current connectivity.
- Quick shortcuts: "Open Scans Folder", "Pause Scanning", "Preferences...".

---

## Phase 3: Intelligent Post-Processing & Paperless Integration
- Native Apple Vision framework OCR (instant, on-device text layer generation).
- Smart AI filename extractor (`YYYY-MM-DD - [Sender] - [Doc Type].pdf`).
- Integration with Paperless-ngx consume folder and Atlas (`personal-cloud-ops`).
