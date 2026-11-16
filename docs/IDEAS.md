# Architectural Ideas & Community Homelab Insights

This document captures architectural analysis, homelab community discussions (including [r/selfhosted](https://www.reddit.com/r/selfhosted/comments/1hy690i/brother_l2710dw_and_scanning_to_network_folder_is/)), and ideas inspired by open-source implementations ([`vanessa/brother-scan-to-paperless`](https://github.com/vanessa/brother-scan-to-paperless), [`PhilippMundhenk/BrotherScannerDocker`](https://github.com/PhilippMundhenk/BrotherScannerDocker), [`quadportnick/brother-scan-paperless`](https://github.com/quadportnick/brother-scan-paperless)), evaluating how these concepts can enhance `ds-940-paperless-scanner`.

---

## 1. Context: The Self-Hosted Scanner Dilemma

In the self-hosted and homelab community (e.g. `r/selfhosted`), a ubiquitous complaint is that consumer and entry-level Brother printers and scanners (such as the MFC-L2710DW, DCP series, and portable DS line) **lack native, firmware-level Scan-to-SMBv2/v3 or Network Share capabilities**.

To get documents automatically into **Paperless-ngx** without manually using USB thumbdrives or proprietary desktop utilities, users typically resort to:
1. **Always-On Windows VM:** Running a heavy Windows virtual machine 24/7 just to keep Brother's bloated *iPrint&Scan* or *ControlCenter* app listening.
2. **Complex SANE/`brscan` Docker Containers:** Running x86_64 containers that wrap Brother's proprietary, closed-source `brscan4`/`brscan5` Linux binaries and `brscan-skey`. These frequently suffer from memory leaks, break on Apple Silicon (ARM64), and drop connections due to SMB protocol mismatches.

### How `ds-940-paperless-scanner` Solves This Natively
* **100% Driverless AirScan/eSCL:** Uses standard HTTP eSCL over local Wi-Fi/Ethernet (`:8080`). No proprietary binaries, no SANE dependencies, and native compatibility across Apple Silicon macOS, Linux, and Docker.
* **Hands-Free Optical Sensor Polling:** Detects paper directly via hardware feeder sensors (`<scan:AdfState>`) without needing any screen buttons pressed.
* **On-Device Apple Neural Engine OCR:** Generates a searchable text layer in milliseconds via native Apple Vision framework (`VNRecognizeTextRequest`) instead of requiring heavy server-side OCR pipelines.

---

## 2. Key Concepts & Features to Adopt

### 💡 Idea 1: Optional Direct Paperless-ngx Ingestion (`CONSUME_DIR`)
* **Concept:** Automatically export or hardlink completed, searchable PDFs straight into Paperless-ngx.
* **Implementation Plan:**
  - Introduce an optional environment variable in `ds940/config.py`:
    ```bash
    export PAPERLESS_CONSUME_DIR="/Volumes/NAS/paperless/consume"
    # or REST API mode:
    export PAPERLESS_API_URL="http://192.168.0.50:8000/api"
    export PAPERLESS_API_TOKEN="xxxxxxxxxxxx"
    ```
  - When `PAPERLESS_CONSUME_DIR` is set, `ds940/spool.py` copies or hardlinks the finished PDF into the consume folder right after OCR generation.
  - Allows seamless coexistence: keep the local clean `~/Documents/brother-scans` library while simultaneously feeding self-hosted Paperless-ngx instances.

---

### 💡 Idea 2: Atomic Staging & Ingestion Guard
* **Origin:** Highlighted in `quadportnick/brother-scan-paperless` and Paperless-ngx troubleshooting threads.
* **The Problem:** Paperless-ngx monitors the consume folder using `inotify`. If a scanner service writes a multi-page PDF or OCR file directly into `consume/`, Paperless may pick up the file prematurely while it is still being assembled, resulting in corrupt or partial document ingestions.
* **Implementation Plan:**
  - Assemble PDFs, run OCR, and extract AI taxonomy in an isolated temporary staging folder (`~/Documents/brother-scans/.staging/`).
  - Perform an atomic POSIX `rename()` / `shutil.move()` into the final directory (or `PAPERLESS_CONSUME_DIR`) only after all write operations and hash checks are fully finalized.

---

### 💡 Idea 3: Automatic Blank Page Detection & Filtering
* **Origin:** Implemented in `PhilippMundhenk/BrotherScannerDocker` via `REMOVE_BLANK_THRESHOLD`.
* **Concept:** When scanning duplex documents (or one-sided receipts on a duplex scanner), blank rear pages clutter the resulting PDF.
* **Implementation Plan:**
  - Add an optional threshold parameter (e.g. `REMOVE_BLANK_THRESHOLD=0.3%`).
  - In `ds940/spool.py`, inspect each scanned image with Pillow before compilation: compute standard deviation / edge variance or white pixel percentage.
  - If a page has virtually zero ink/pixels, automatically discard it from the final multi-page PDF, logging: `Discarded blank page 2 (variance: 0.08 < 0.3)`.

---

### 💡 Idea 4: Push Notifications & Webhooks (Telegram / Discord / Pushover / n8n)
* **Concept:** Notify the user on their phone or homelab dashboard when a document has been scanned and filed.
* **Payload Details:**
  - Document Title (e.g., `2026-11-16_Labcorp_Invoice.pdf`)
  - Detected Category & Vendor (e.g., `Medical & Health` / `Labcorp`)
  - Page Count & Execution Time
  - Direct HTTPS link to the document preview in the web UI (`https://scans.alw.lol/`)
* **Implementation:**
  - Support generic outbound webhooks (`WEBHOOK_URL="https://discord.com/api/webhooks/..."` or `TELEGRAM_TOKEN` + `TELEGRAM_CHATID`).
  - Fire an async HTTP POST upon successful triage.

---

### 💡 Idea 5: Dual-Mode Triggering via UDP 54925 Button Listener
* **Concept:** Listen for physical hardware button-press events in addition to optical sensor polling.
* **Mechanism:**
  - Brother devices broadcast UDP packets to port `54925` containing `TYPE=BR;BUTTON=SCAN` when the physical scan button is pressed.
  - By running a lightweight background UDP socket listener on `0.0.0.0:54925`, our receiver can trigger an immediate scan cycle the microsecond the button is pressed—eliminating the standard 3-second eSCL polling sleep.
  - Supported by periodic SNMP profile registration on OID `.1.3.6.1.4.1.2435.2.3.9.2.11.1.1.0`.

---

### 💡 Idea 6: Simplex-to-Duplex Manual Interleaving
* **Origin:** Noted in `PhilippMundhenk/BrotherScannerDocker` for manual batch feeds.
* **Concept:** For documents where automatic duplex is disabled, or for scanners with single-sided ADFs:
  - User scans the stack of front pages (`[1, 3, 5]`).
  - User flips the stack and scans the back pages (`[6, 4, 2]`).
  - The software detects the "rear page" batch within a configurable window (e.g. `REAR_PAGE_WAIT_SECONDS=60`), reverses the second set, and interleaves them seamlessly into `[1, 2, 3, 4, 5, 6]`.

---

### 💡 Idea 7: Persistent File-Based Config (`scanner.conf`) with Web Settings
* **Concept:** Avoid editing shell files, environment variables, or launchd plists for tuning scanner settings.
* **Mechanism:**
  - Support `~/.config/ds940/scanner.conf` with simple `KEY=VALUE` pairs.
  - Precedence: Environment Variables > `scanner.conf` > Code Defaults.
  - Expose a clean "Settings" tab in the web dashboard at `scans.alw.lol` allowing users to tune polling rates, destinations, and thresholds directly from the browser.

---

### 💡 Idea 8: One-Step Turn-Key `install.sh` Wizard
* **Concept:** Provide a single-command setup script for new users cloning the repository.
* **Features:**
  - **Platform Detection:** Detects macOS vs Linux automatically.
    - macOS: Sets up and loads `~/Library/LaunchAgents/com.ds940receiver.plist`.
    - Linux / Proxmox: Generates and enables `/etc/systemd/system/ds940receiver.service`.
  - **LAN Scanner Discovery:** Probes mDNS (`_uscan._tcp`) to auto-detect the scanner's local IP address without manual router checks.
  - **Permission Check:** Verifies read/write access to destination folders and tests eSCL HTTP connectivity.

---

### 💡 Idea 9: Scan Profiles & Presets
* **Concept:** Allow rapid switching of resolution, color mode, and page bounds.
* **Preset Definitions:**
  - **Fast Document (Default):** 300 DPI Duplex, Color/Grayscale auto, A4/Letter.
  - **Archival Text / Receipt:** 300 DPI Black & White (thresholded), high compression.
  - **High-Res Graphic:** 600 DPI Color, maximum JPEG quality.
* **Dashboard Integration:** Quick toggle chips in the web interface at `scans.alw.lol` to switch profiles with one click.

