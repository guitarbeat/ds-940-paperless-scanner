# Architectural Ideas & Community Homelab Insights

This document catalogs architectural enhancements, community homelab patterns (including [r/selfhosted](https://www.reddit.com/r/selfhosted/comments/1hy690i/brother_l2710dw_and_scanning_to_network_folder_is/)), and lessons learned from open-source Brother implementations:
- [`thebino/brother-to-paperless`](https://github.com/thebino/brother-to-paperless) (Home Assistant HACS integration and pure-Python port 54921 binary driver)
- [Calvin Walden's MFC-L2750DW to Paperless Workflow](https://calvinrw.com/brscan4-paperless-workflow) (Ghostscript ink coverage calibration, NFS inotify limitations, and DNS firmware blackholing)
- [`vanessa/brother-scan-to-paperless`](https://github.com/vanessa/brother-scan-to-paperless) (UDP push-button listeners and SNMP profile registration)
- [`PhilippMundhenk/BrotherScannerDocker`](https://github.com/PhilippMundhenk/BrotherScannerDocker) (Blank page filtering and manual simplex interleaving)
- [`quadportnick/brother-scan-paperless`](https://github.com/quadportnick/brother-scan-paperless) (Staging directories and Paperless inotify race condition guards)

---

## 1. Quick Reference: Edits by Category & Status

| Category | Idea | Target Components | Complexity | Status | Primary Impact |
|:---|:---|:---|:---:|:---:|:---|
| **Pipeline & Processing** | [Atomic Staging & Ingestion Guard](#idea-1-atomic-staging--ingestion-guard) | `ds940/spool.py` | Low | **Completed** | Eliminates file corruption in Paperless `inotify` |
| **Pipeline & Processing** | [Blank Page Detection & Suppression](#idea-2-automatic-blank-page-detection--filtering) | `ds940/spool.py`, `ds940/worker.py` | Medium | **Completed** | Removes empty backsides on duplex mail |
| **Pipeline & Processing** | [Simplex-to-Duplex Manual Interleaving](#idea-3-simplex-to-duplex-manual-interleaving) | `ds940/spool.py` | Medium | Proposed | Assembles multi-page batches on single-sided ADFs |
| **Pipeline & Processing** | [Automatic Deskew & Affine Tilt Correction](#idea-16-automatic-deskew--affine-tilt-correction) | `ds940/spool.py` | Medium | Proposed | Straightens crooked roller feeds before OCR |
| **Pipeline & Processing** | [Auto-Crop & Small Item Trimming](#idea-17-auto-crop--small-item-canvas-trimming-cards--receipts) | `ds940/spool.py` | Medium | Proposed | Cuts excess white borders around ID cards and receipts |
| **Hardware & Protocol** | [Pure-Python TCP Port 54921 Driver](#idea-4-pure-python-tcp-port-54921-driver-legacy-brother-support) | `ds940/protocols/legacy_tcp.py` | High | Proposed | Adds legacy Brother laser MFC support |
| **Hardware & Protocol** | [Dual-Mode UDP Port 54925 Button Listener](#idea-5-dual-mode-triggering-via-udp-54925-button-listener) | `ds940/button_listener.py` | Medium | Proposed | Instant trigger when physical button is pressed |
| **Hardware & Protocol** | [Multi-Action Keypad Routing](#idea-6-multi-action-keypad-routing-file-vs-ocr-vs-image) | `ds940/supervisor.py` | Medium | Proposed | Routes scans based on scanner screen buttons |
| **Hardware & Protocol** | [Binary Protocol Debug Capture Mode](#idea-7-binary-protocol-debug--diagnostic-capture-mode---debug) | `ds940/cli.py`, `ds940/escl.py` | Low | Proposed | Diagnostic byte dumps without leaking document text |
| **OS & Desktop Integration**| [macOS Finder Extended Attributes & Tags](#idea-15-macos-finder-extended-attributes--native-color-tagging) | `ds940/finder_tags.py`, `ds940/spool.py` | Low | **Completed** | Native macOS Finder colors and Spotlight metadata |
| **OS & Desktop Integration**| [Audible Hardware State Chimes](#idea-20-audible-hardware-state-chimes-afplay--say) | `ds940/audio.py` | Low | Proposed | Headless audio cues for feed, collation, and filing |
| **Downstream Integrations**| [Direct Paperless-ngx REST API Ingestion](#idea-8-direct-paperless-ngx-rest-api-ingestion) | `ds940/export/paperless.py` | Medium | Proposed | Bypasses NFS permissions, latency, and inotify drops |
| **Downstream Integrations**| [Home Assistant MQTT Discovery & Sensors](#idea-9-home-assistant-integration--smart-home-automation-mqtt--hacs) | `ds940/mqtt.py` | Medium | Proposed | Exposes buttons and status sensors to Home Assistant |
| **Downstream Integrations**| [Push Notifications & Webhooks](#idea-10-push-notifications--webhooks-discord-telegram-pushover-n8n) | `ds940/notify.py` | Low | Proposed | Alerts mobile phones upon successful document filing |
| **Downstream Integrations**| [Barcode & QR Code Batch Splitting](#idea-18-barcode--qr-code-batch-splitting--tagging) | `ds940/barcode.py` | Medium | Proposed | Splits multi-document stacks via barcode separator sheets |
| **Downstream Integrations**| [Perceptual Deduplication](#idea-19-perceptual-hashing--duplicate-scan-suppression) | `ds940/dedup.py` | Low | Proposed | Flags duplicate scans using pHash and text layer checks |
| **Dashboard & Config** | [Scan Profiles & Preset Toggles](#idea-11-scan-profiles--quick-presets) | `ds940/config.py`, `ds940/web.py` | Low | Proposed | One-click switching between Document, Archival, Photo |
| **Dashboard & Config** | [Persistent `scanner.conf` & Web Settings](#idea-12-persistent-file-based-config-scannerconf-with-web-settings) | `ds940/config.py`, `ds940/web.py` | Medium | Proposed | Live configuration changes without touching system plists |
| **Dashboard & Config** | [Prometheus Metrics Endpoint (`/metrics`)](#idea-21-prometheus--opentelemetry-metrics-endpoint-metrics) | `ds940/web.py` | Low | Proposed | Grafana time-series metrics for throughput and latency |
| **DevOps & Infrastructure**| [Turn-Key `install.sh` Provisioning Wizard](#idea-13-one-step-turn-key-installsh-wizard) | `scripts/install.sh` | Medium | Proposed | Single-command launchd and systemd setup |
| **DevOps & Infrastructure**| [Vendor Firmware Blackholing Runbook](#idea-14-vendor-firmware-isolation--update-blocking) | `docs/DNS_SECURITY.md` | Low | Proposed | Protects local network scanners from breaking updates |

---

## 2. Pipeline & Document Processing Edits

### Idea 1: Atomic Staging & Ingestion Guard
* **Status:** **Completed** (Shipped in `ds940/spool.py`)
* **Type of Edit:** Backend Pipeline / Filesystem Guard
* **Target Components:** `ds940/spool.py`, `ds940/config.py`
* **Origin:** Highlighted in `quadportnick/brother-scan-paperless` and Paperless-ngx community forums.
* **The Problem:** Paperless-ngx watches ingest folders using Linux `inotify`. If a scanner daemon streams pages or writes multi-page PDFs directly into `consume/`, Paperless frequently consumes partial, incomplete files while they are still being written or OCR-processed.
* **Shipped Implementation:**
  1. All page ingestion, multi-page collation, OCR, and AI categorization execute inside the spool job directory (`work/spool/brother-xxx/`). The destination directory observes zero partial writes.
  2. During publication, `ScanSpool.publish` writes the completed PDF to a hidden temporary staging file directly on the destination filesystem:
     `target.parent / f".staging_{uuid4().hex[:8]}.tmp"`
  3. The byte stream is flushed and forced to disk with `os.fsync(stream.fileno())`.
  4. Standard POSIX permissions (`0o644`) are applied to the staged file.
  5. An atomic POSIX `replace()` swaps the staged file into its final destination path:
     `temporary.replace(target)`
* **Impact:** Eliminates file corruption, zero-byte PDF ingestion errors, and file watcher race conditions permanently. Verified by `tests/test_blank_filtering_and_staging.py::test_atomic_staging_guard_publishes_cleanly`.

---

### Idea 2: Automatic Blank Page Detection & Filtering
* **Status:** **Completed** (Shipped in `ds940/spool.py`, `ds940/config.py`, `ds940/worker.py`)
* **Type of Edit:** Image Processing & PDF Compilation
* **Target Components:** `ds940/spool.py`, `ds940/worker.py`, `ds940/config.py`
* **Origin:** Implemented in `PhilippMundhenk/BrotherScannerDocker` and empirically calibrated by Calvin Walden.
* **The Problem:** In duplex mode, scanning single-sided mail or receipts produces blank white backsides that double the resulting file size and clutter the searchable document archive.
* **Calvin Walden's Empirical Ink Coverage Calibration:**
  * True blank pages (with scanner dust or minor paper transparency bleed-through): `0.00 to 0.03` ink coverage.
  * Real document pages (sparse receipts, signature lines, short forms): `0.30+` ink coverage.
  * Setting a removal threshold of `0.05` ink density establishes a safe margin: it drops 100% of blank reverse pages while guaranteeing that faint receipts or signatures are never discarded (zero false negatives).
* **Shipped Implementation:**
  1. `calculate_ink_coverage(image_path, margin_ratio=0.03, ink_threshold=210)`:
     - Inset margin crop (outer 3% border ignored) strips roller edge shadows and ADF feeder border lines.
     - Grayscale luminosity threshold `< 210` ignores thin paper bleed-through shadows from opposite sides.
     - Ratio of `ink_pixels / total_cropped_pixels` computes ink density.
  2. `filter_blank_pages(image_paths, threshold=0.05)`:
     - Configurable via `REMOVE_BLANK_THRESHOLD` environment variable (default `0.05`, set `0.0` to disable).
     - **Retain Page 1 Invariant**: If all pages in a batch evaluate as blank, Page 1 is strictly preserved to prevent zero-page spool crashes and ensure valid PDF output.
  3. End-to-end worker integration in `capture_if_ready_escl`:
     - Discards blank pages before calling `build_pdf`.
     - Logs filtered pages and writes `raw_pages` and `discarded_blank_pages` to `manifest.json`.
* **Impact:** Cuts document storage in half for mixed one-sided/two-sided mail batches without risk of data loss. Verified by 10 focused unit tests in `tests/test_blank_filtering_and_staging.py`.

---

### Idea 3: Simplex-to-Duplex Manual Interleaving
* **Status:** Proposed
* **Type of Edit:** Multi-Page Collation Engine
* **Target Components:** `ds940/spool.py`, `ds940/supervisor.py`
* **Origin:** Documented in `PhilippMundhenk/BrotherScannerDocker` for manual batch feeds.
* **The Problem:** When duplex scanning is disabled (or when using single-sided portable scanners), scanning a double-sided multi-page stack requires manual PDF page rearranging in an external editor.
* **Technical Specification:**
  1. User feeds the front-page stack through the feeder: pages `[1, 3, 5]`.
  2. User flips the stack and feeds the reverse side: pages `[6, 4, 2]`.
  3. Supervisor detects the secondary feed within an active window (`REAR_PAGE_WAIT_SECONDS=60`).
  4. The engine reverses the second stack to `[2, 4, 6]` and interleaves:
     `[1, 2, 3, 4, 5, 6]`.
* **Impact:** Provides hardware duplex emulation for single-sided document feeders.

---

### Idea 16: Automatic Deskew & Affine Tilt Correction
* **Status:** Proposed
* **Type of Edit:** Pre-OCR Image Processing (`ds940/spool.py`)
* **The Problem:** Sheets feeding through portable slot rollers often tilt 1 to 4 degrees, degrading OCR character confidence and resulting in crooked page scans.
* **Technical Specification:**
  - Compute dominant text line angles via Hough transform or Radon projection.
  - Apply sub-pixel affine rotation with Pillow bicubic interpolation.
  - Corrects page orientation before text extraction and PDF assembly.
* **Impact:** Clean, publication-grade upright PDFs regardless of physical paper skew.

---

### Idea 17: Auto-Crop & Small Item Canvas Trimming (Cards & Receipts)
* **Status:** Proposed
* **Type of Edit:** Bounding Box Canvas Optimizer (`ds940/spool.py`)
* **The Problem:** Scanning business cards, driver licenses, or thermal register receipts outputs full A4 canvases with tiny items centered in empty white space.
* **Technical Specification:**
  - Identify non-white convex hull boundaries across the cropped page.
  - If content bounds are smaller than 60% of A4 dimensions, crop canvas to content dimensions + 5mm padding.
* **Impact:** Native aspect ratio documents for mobile wallets and receipt archives.

---

## 3. Hardware Driver & Protocol Edits

### Idea 4: Pure-Python TCP Port 54921 Driver (Legacy Brother Support)
* **Status:** Proposed
* **Type of Edit:** Hardware Protocol & Transport Layer
* **Target Components:** `ds940/protocols/legacy_tcp.py`, `ds940/driver.py`
* **Origin:** Reverse-engineered protocol documented by [`thebino/brother-to-paperless`](https://github.com/thebino/brother-to-paperless/blob/main/PROTOCOL.md).
* **The Problem:** Modern Brother scanners (such as our DS-940DW) support standard HTTP eSCL/AirScan on port 8080. However, millions of legacy Brother laser multifunction printers (MFC-L2700DW, MFC-J430W, MFC-7820N, DCP series) only speak Brother's proprietary binary protocol over TCP port 54921, previously requiring closed-source `brscan4` Linux binaries.
* **Technical Specification:**
  * Implement `thebino`'s reverse-engineered pure-Python socket driver:
    1. Connect via TCP to port `54921` -> awaits `+OK 200\r\n`.
    2. Lease and configuration request: `\x1bI\nR={dpi},{dpi}\nM={mode}\n\x80`.
    3. Dimension and ADF capability negotiation: `\x00\x1d\x00...`.
    4. Binary stream ingestion: `\x1bX\nR=...` framing with `0x82` (end-of-page) and `0x80` (end-of-scan) delimiters.
  * Register as an alternate backend in `ds940/driver.py`:
    ```python
    if protocol == "escl":
        from .escl import ESCLDriver as Driver
    elif protocol == "legacy_tcp":
        from .protocols.legacy_tcp import LegacyTCPDriver as Driver
    ```
* **Impact:** Transforms this project into a universal driverless scanner engine for both modern eSCL and legacy Brother hardware across macOS and Linux.

---

### Idea 5: Dual-Mode Triggering via UDP 54925 Button Listener
* **Status:** Proposed
* **Type of Edit:** Network Event Listener & Ingestion Driver
* **Target Components:** `ds940/button_listener.py`, `ds940/supervisor.py`
* **Origin:** Found in `vanessa/brother-scan-to-paperless`.
* **The Problem:** Polling the HTTP eSCL scanner endpoint introduces a 2 to 3 second delay before sheet feeding starts.
* **Technical Specification:**
  * When a physical scan button is pushed on a Brother device, it sends a UDP broadcast packet on port `54925` containing:
    `TYPE=BR;BUTTON=SCAN`
  * Run an async UDP socket listener on `0.0.0.0:54925`.
  * Trigger an immediate scan run upon packet arrival, bypassing the scheduled polling interval.
  * Keep the current optical sensor polling as a parallel fallback for completely touch-free feeding.
* **Impact:** Sub-millisecond scan initiation when using physical device buttons.

---

### Idea 6: Multi-Action Keypad Routing (`to File` vs `to OCR` vs `to Image`)
* **Status:** Proposed
* **Type of Edit:** Workflow Routing & Supervisor Logic
* **Target Components:** `ds940/supervisor.py`, `ds940/config.py`
* **Origin:** Highlighted by Calvin Walden on multifunction printer keypads.
* **The Problem:** Users often want different workflows for different document types (receipts vs archival contracts vs color photos) without having to change configuration files.
* **Technical Specification:**
  * Read the action sub-code transmitted in the Brother button payload:
    * `Scan to File`: Standard duplex document ingestion (`300 DPI Duplex` -> Paperless-ngx).
    * `Scan to OCR`: Priority ingestion with full Apple Vision OCR and automated LLM categorization.
    * `Scan to Image`: High-resolution photograph mode (`600 DPI Color` -> saved losslessly to `Photos/`).
* **Impact:** Enables tactile, hardware-level workflow selection straight from the scanner front panel.

---

### Idea 7: Binary Protocol Debug & Diagnostic Capture Mode (`--debug`)
* **Status:** Proposed
* **Type of Edit:** CLI & Troubleshooting Utilities
* **Target Components:** `ds940/cli.py`, `ds940/escl.py`
* **Origin:** Implemented in `thebino/brother-to-paperless`.
* **The Problem:** Troubleshooting scanner communication often requires Wireshark captures, which risk exposing private document text and credentials.
* **Technical Specification:**
  * Add a `--debug-capture` command-line flag.
  * When active, save raw network packets to isolated binary artifacts:
    * `offer.bin`: The exact XML/binary capability negotiation payload.
    * `scan.bin`: The raw byte stream from the scanner socket before parsing.
* **Impact:** Enables bug reporting and hardware diagnostics without leaking document images or personal information.

---

## 4. macOS & Desktop OS Integration

### Idea 15: macOS Finder Extended Attributes & Native Color Tagging
* **Status:** **Completed** (Shipped in `ds940/finder_tags.py`, `ds940/spool.py`, `scripts/sort_scans.py`)
* **Type of Edit:** OS Filesystem Publisher & Metadata Tagging
* **Target Components:** `ds940/finder_tags.py`, `ds940/spool.py`, `scripts/sort_scans.py`
* **The Problem:** macOS Finder and Spotlight only index raw text layers. They cannot see the AI-classified category, vendor, or document date in native Finder column views, colored bubble tags, or Finder Smart Folders.
* **Shipped Implementation:**
  1. `com.apple.metadata:_kMDItemUserTags`: Writes binary plist with category tag and color code:
     - `Legal & Citations`: Purple (Color 3)
     - `Medical & Health`: Blue (Color 4)
     - `Banking & Finance / Bills`: Green (Color 2)
     - `Tax`: Red (Color 6)
     - `Auto & Vehicles`: Orange (Color 7)
     - `Utilities`: Yellow (Color 5)
     - `Inbox`: Gray (Color 1)
  2. `com.apple.FinderInfo`: Sets byte 9 of the 32-byte struct to `(color & 0x07) * 2`, ensuring Finder immediately displays the colored circular dot in Finder list and column views.
  3. `com.apple.metadata:kMDItemKeywords`: Injects category, vendor, and `scanned` keywords for instant Spotlight indexing.
  4. `com.apple.metadata:kMDItemTitle`: Sets the human-readable document title on the file.
  5. `scripts/sort_scans.py --tag-all`: Utility command to batch-tag all historical documents in `~/Documents/brother-scans`.
* **Impact:** Native macOS Finder color dots, instant tag filtering in Finder sidebar, and Spotlight metadata search. Verified by 5 unit tests in `tests/test_finder_tags.py`.

---

### Idea 20: Audible Hardware State Chimes (`afplay` / `say`)
* **Status:** Proposed
* **Type of Edit:** Daemon Telemetry & Audio Accessibility (`ds940/audio.py`)
* **The Problem:** Headless scanners lack visual feedback across the room. Users cannot hear whether a collation countdown is active or if OCR filing completed.
* **Technical Specification:**
  - Fire subtle macOS CoreAudio alerts via background `afplay`:
    - *Tock*: Sheet feeding initiated.
    - *Subtle chime*: Sheet collation window active (insert next sheet).
    - *Glass ding*: Document OCR'd, categorized, and filed.
    - *Basso alert*: Paper jam or scanner offline.
* **Impact:** Eyes-free headless operation with zero dashboard checking required.

---

## 5. Downstream Export & Integration Edits

### Idea 8: Direct Paperless-ngx REST API Ingestion
* **Status:** Proposed
* **Type of Edit:** Downstream Exporter & API Connector
* **Target Components:** `ds940/export/paperless.py`, `ds940/config.py`
* **Origin:** Homelab community discussions on NFS/SMB inotify limitations.
* **The Problem:** Mounting shared network folders (NFS/SMB) into Paperless-ngx disables Linux kernel `inotify` events, forcing Paperless into slow, CPU-heavy polling loops (`PAPERLESS_CONSUMER_POLLING=15`). Network file mounts also suffer from UID/GID permission mismatches between containers and host operating systems.
* **Technical Specification:**
  * Introduce direct HTTP REST API integration via configuration:
    ```bash
    export PAPERLESS_API_URL="http://192.168.0.50:8000/api"
    export PAPERLESS_API_TOKEN="your_auth_token_here"
    ```
  * Once a document is OCR-processed and filed locally, post it directly to Paperless:
    ```python
    # ds940/export/paperless.py
    import requests

    def upload_to_paperless(pdf_path: Path, title: str, category: str, api_url: str, token: str):
        headers = {"Authorization": f"Token {token}"}
        with open(pdf_path, "rb") as f:
            files = {"document": (pdf_path.name, f, "application/pdf")}
            data = {"title": title}
            resp = requests.post(f"{api_url}/documents/post_document/", headers=headers, files=files, data=data)
            resp.raise_for_status()
    ```
* **Impact:** Instant ingestion into Paperless-ngx with zero network filesystem dependencies or permission issues.

---

### Idea 9: Home Assistant Integration & Smart Home Automation (MQTT / HACS)
* **Status:** Proposed
* **Type of Edit:** Smart Home Integration & Telemetry
* **Target Components:** `ds940/mqtt.py`, `ds940/web.py`
* **Origin:** Inspired by `thebino/brother-to-paperless`.
* **The Problem:** The scanner currently operates as an isolated appliance without visibility from home dashboards or smart displays.
* **Technical Specification:**
  * Implement Home Assistant MQTT Auto-Discovery:
    * **Button Entity (`button.ds940_scan`):** Trigger a scan cycle from Home Assistant dashboards, smart switches, or wireless buttons (Zigbee, Z-Wave).
    * **Sensor Entity (`sensor.ds940_status`):** Real-time hardware states: `idle`, `scanning`, `processing_ocr`, `offline`, or `error`.
    * **Diagnostic Attributes:** `last_document`, `pages_scanned`, `paper_detected`.
    * **Event Signals:** Broadcast `brother_scan_completed` events with document metadata payloads (vendor, category, page count).
* **Automation Use Cases:**
  * Flash an ambient desk lamp green when a scan finishes filing.
  * Announce over smart speakers: "Tax document filed from Travis County".
  * Send mobile alerts with document preview links via the Home Assistant Companion App.
* **Impact:** Turns the scanner into a native smart home participant.

---

### Idea 10: Push Notifications & Webhooks (Discord / Telegram / Pushover / n8n)
* **Status:** Proposed
* **Type of Edit:** Notification Service
* **Target Components:** `ds940/notify.py`, `ds940/config.py`
* **The Problem:** The user has no mobile awareness of whether a multi-page document was recognized, sorted, or flagged for review.
* **Technical Specification:**
  * Support webhook configuration:
    ```bash
    export WEBHOOK_URL="https://discord.com/api/webhooks/..."
    # or Telegram:
    export TELEGRAM_TOKEN="bot_token"
    export TELEGRAM_CHAT_ID="chat_id"
    ```
  * Send a formatted payload upon document filing:
    * Document Title: `2026-11-16_Labcorp_Invoice.pdf`
    * Category & Vendor: `Medical & Health / Labcorp`
    * Metrics: `2 pages, 142 KB, 1.4s OCR time`
    * Direct Link: `https://scans.alw.lol/`
* **Impact:** Real-time confirmation on phones and tablets as soon as paper feeds through the scanner.

---

### Idea 18: Barcode & QR Code Batch Splitting & Tagging
* **Status:** Proposed
* **Type of Edit:** Ingestion Parser (`ds940/barcode.py` / `ds940/spool.py`)
* **The Problem:** Scanning multi-document mail batches requires manual single-page feeds or manual PDF splitting later.
* **Technical Specification:**
  - Utilize macOS native `VNDetectBarcodesRequest` to detect Code-128, Code-39, QR, or Paperless `PATCH-T` separator sheets.
  - Automatically split document batches when separator barcodes are encountered.
  - Inject barcode payload numbers (account number, invoice tracking) into taxonomy metadata.
* **Impact:** Continuous batch feed scanning with automatic multi-document division.

---

### Idea 19: Perceptual Hashing & Duplicate Scan Suppression
* **Status:** Proposed
* **Type of Edit:** Document Ledger & Deduplication (`ds940/dedup.py`)
* **The Problem:** Re-scanning an already-filed document clutters archives with redundant duplicate files.
* **Technical Mechanism:**
  - Generate 64-bit difference hash (dHash) and SHA-256 OCR text hash on every delivered document.
  - Route matches >98% perceptual similarity to `Duplicates/` folder or issue notification.
* **Impact:** Prevents redundant storage and eliminates duplicate notifications.

---

## 6. Web Dashboard & Runtime Configuration Edits

### Idea 11: Scan Profiles & Quick Presets
* **Status:** Proposed
* **Type of Edit:** Configuration & Frontend UI
* **Target Components:** `ds940/config.py`, `ds940/web.py`
* **The Problem:** Adjusting DPI or switching between grayscale and color currently requires editing launch environment variables and restarting daemons.
* **Technical Specification:**
  * Define three operational presets in `ds940/config.py`:
    * **Fast Document (Default):** 300 DPI, Grayscale auto, Duplex, high compression.
    * **Archival Text / Receipt:** 300 DPI, Black and White thresholded, compact size.
    * **High-Res Photo:** 600 DPI, Full 24-bit Color, zero lossy compression.
  * Add profile switching chips to the header bar of `https://scans.alw.lol/`.
* **Impact:** Immediate, one-click adjustments for specialized document batches.

---

### Idea 12: Persistent File-Based Config (`scanner.conf`) with Web Settings
* **Status:** Proposed
* **Type of Edit:** Configuration Engine & Web Management API
* **Target Components:** `ds940/config.py`, `ds940/web.py`
* **The Problem:** Environment variables in macOS LaunchAgents require unloading and reloading plists via `launchctl` to take effect.
* **Technical Specification:**
  * Read settings from `~/.config/ds940/scanner.conf` using simple `KEY=VALUE` formatting.
  * Configuration priority:
    `Environment Variables > ~/.config/ds940/scanner.conf > Code Defaults`
  * Expose a settings drawer in the web interface allowing users to adjust polling intervals, destination paths, and thresholds directly from their browser.
* **Impact:** Simple user-friendly administration without terminal commands.

---

### Idea 21: Prometheus / OpenTelemetry Metrics Endpoint (`/metrics`)
* **Status:** Proposed
* **Type of Edit:** Web API & Observability (`ds940/web.py`)
* **The Problem:** Homelab monitoring stacks lack time-series metrics on scanner health, battery levels, daily scan volume, and OCR processing latency.
* **Technical Specification:**
  - Expose `/metrics` on port 9400 in standard Prometheus exposition format:
    `ds940_scans_total`, `ds940_pages_total`, `ds940_blank_pages_discarded_total`, `ds940_ocr_duration_seconds`.
* **Impact:** Pre-built Grafana panels for homelab infrastructure monitoring.

---

## 7. DevOps, Setup & Network Security Edits

### Idea 13: One-Step Turn-Key `install.sh` Wizard
* **Status:** Proposed
* **Type of Edit:** Provisioning Scripts & Packaging
* **Target Components:** `scripts/install.sh`
* **The Problem:** Setting up automatic startup daemons, virtual environments, and scanner network parameters requires several manual commands.
* **Technical Specification:**
  * Create an interactive POSIX setup script:
    1. **Operating System Detection:**
       * macOS: Generates and loads `~/Library/LaunchAgents/com.aaron.ds940receiver.plist`.
       * Linux / Proxmox: Creates and enables `/etc/systemd/system/ds940receiver.service`.
    2. **mDNS Hardware Discovery:** Probes local network for `_uscan._tcp` services to identify the Brother scanner IP address automatically.
    3. **Directory Verification:** Creates storage folders, verifies permissions, and executes an eSCL connectivity handshake.
* **Impact:** One-command onboarding for new homelab installations.

---

### Idea 14: Vendor Firmware Isolation & Update Blocking
* **Status:** Proposed
* **Type of Edit:** Network Hardening Documentation & Runbook
* **Target Components:** `docs/DNS_SECURITY.md`
* **Origin:** Documented by Calvin Walden on Brother MFC firmware behavior.
* **The Problem:** Brother periodically deploys automatic firmware updates over Wi-Fi. In the homelab community, firmware updates have broken driverless eSCL scanning, introduced proprietary handshakes, or disabled third-party supplies.
* **Technical Specification:**
  * Document instructions to block Brother update endpoints in local DNS resolvers (Pi-hole, AdGuard Home, pfSense, OPNSense):
    ```text
    update.brother.co.jp
    firmverup.brother.co.jp
    ```
  * Keep local subnet communication (HTTP 8080, TCP 54921, UDP 54925) fully open.
* **Impact:** Freezes scanner firmware permanently in a stable, verified working state.
