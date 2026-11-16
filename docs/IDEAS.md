# Architectural Ideas & Community Homelab Insights

This document captures architectural analysis, homelab community discussions (including [r/selfhosted](https://www.reddit.com/r/selfhosted/comments/1hy690i/brother_l2710dw_and_scanning_to_network_folder_is/)), and ideas inspired by open-source implementations:
- [`thebino/brother-to-paperless`](https://github.com/thebino/brother-to-paperless) (Home Assistant HACS integration & pure-Python port 54921 driver)
- [Calvin Walden's MFC-L2750DW to Paperless Workflow](https://calvinrw.com/brscan4-paperless-workflow) (Ghostscript ink coverage calibration, NFS inotify limits, & DNS firmware blocking)
- [`vanessa/brother-scan-to-paperless`](https://github.com/vanessa/brother-scan-to-paperless) (UDP push-button listeners & SNMP scan profiles)
- [`PhilippMundhenk/BrotherScannerDocker`](https://github.com/PhilippMundhenk/BrotherScannerDocker) (Blank page filtering & simplex interleaving)
- [`quadportnick/brother-scan-paperless`](https://github.com/quadportnick/brother-scan-paperless) (Staging directories & Paperless inotify race condition guards)

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

### 💡 Idea 1: Optional Direct Paperless-ngx Ingestion (`CONSUME_DIR` vs. REST API)
* **Concept:** Automatically export or hardlink completed, searchable PDFs straight into Paperless-ngx.
* **Implementation Plan:**
  - Introduce optional environment variables in `ds940/config.py`:
    ```bash
    # REST API mode (Recommended):
    export PAPERLESS_API_URL="http://192.168.0.50:8000/api"
    export PAPERLESS_API_TOKEN="xxxxxxxxxxxx"
    # Or local/NFS consume folder mode:
    export PAPERLESS_CONSUME_DIR="/Volumes/NAS/paperless/consume"
    ```
  - When `PAPERLESS_API_URL` is set, `ds940/spool.py` directly POSTs the document to `/api/documents/post_document/`.
  - **REST API vs. Network Share Note (from Calvin Walden):** Calvin highlighted that mounting remote NFS/SMB shares into Paperless breaks Linux `inotify` file-watching (forcing a CPU-polling fallback via `PAPERLESS_CONSUMER_POLLING=15`) and introduces UID/GID permission collisions between host VMs. Direct HTTP REST API ingestion completely eliminates network filesystem latency, permissions bugs, and inotify drops.

---

### 💡 Idea 2: Atomic Staging & Ingestion Guard
* **Origin:** Highlighted in `quadportnick/brother-scan-paperless` and Paperless-ngx troubleshooting threads.
* **The Problem:** Paperless-ngx monitors the consume folder using `inotify`. If a scanner service writes a multi-page PDF or OCR file directly into `consume/`, Paperless may pick up the file prematurely while it is still being assembled, resulting in corrupt or partial document ingestions.
* **Implementation Plan:**
  - Assemble PDFs, run OCR, and extract AI taxonomy in an isolated temporary staging folder (`~/Documents/brother-scans/.staging/`).
  - Perform an atomic POSIX `rename()` / `shutil.move()` into the final directory (or `PAPERLESS_CONSUME_DIR`) only after all write operations and hash checks are fully finalized.

---

### 💡 Idea 3: Automatic Blank Page Detection & Filtering
* **Origin:** Implemented in `PhilippMundhenk/BrotherScannerDocker` and empirically calibrated by Calvin Walden.
* **Concept:** When scanning duplex documents (or one-sided receipts on a duplex scanner), blank rear pages clutter the resulting PDF.
* **Empirical Calibration & Thresholds (from Calvin Walden):**
  - Calvin measured objective ink coverage across batches of mixed mail:
    - Pure blank pages: `0.00 – 0.03` ink coverage.
    - Pages with light markings, text, or signatures: `0.30+` ink coverage.
    - Setting a removal threshold of `0.10` proved to be a rock-solid safe floor, stripping 100% of blank backsides without ever discarding sparse receipts.
* **Implementation Plan:**
  - Add an optional threshold parameter (`REMOVE_BLANK_THRESHOLD=0.10`).
  - In `ds940/spool.py`, calculate page pixel variance or ink density with Pillow before final PDF compilation. Discard pages below threshold and log: `Discarded blank page 2 (density: 0.02 < 0.10)`.

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

---

### 💡 Idea 10: Home Assistant Integration & Smart Home Automation (HACS & MQTT)
* **Origin:** Inspired by [`thebino/brother-to-paperless`](https://github.com/thebino/brother-to-paperless).
* **Concept:** Expose the scanner directly to Home Assistant as a first-class smart home device without requiring complex external glue code.
* **Entities & Architecture:**
  - **Button Entity (`button.ds940_scan`):** Allows triggering a scan cycle directly from Home Assistant dashboards, smart displays, or physical Zigbee/Z-Wave wireless buttons (e.g., Aqara, IKEA, Lutron) placed next to the scanner.
  - **Status Sensor (`sensor.ds940_status`):** Reports scanner state: `idle`, `scanning`, `processing_ocr`, `offline`, or `error`, along with diagnostic attributes (`last_document`, `pages_scanned`, `paper_detected`).
  - **Event Bus Signals:** Broadcasts `brother_scan_completed` and `brother_scan_failed` events with metadata payloads (document path, vendor name, category, page count).
* **Automation Possibilities:**
  - Flash an ambient LED strip or smart bulb green upon successful OCR filing.
  - Play an audio chime or voice announcement on smart speakers ("New tax document filed from Austin Water").
  - Send rich push notifications with PDF QuickLook attachments directly to the Home Assistant Companion App.
* **Implementation Options:**
  - **Option A (Native HACS Custom Component):** Follow `thebino`'s blueprint to build a dedicated HACS integration communicating with our Mac Mini web API (`https://scans.alw.lol/api`).
  - **Option B (Home Assistant MQTT Discovery):** Publish MQTT discovery payloads from `ds940/web.py` to Home Assistant's MQTT broker—automatically provisioning the button and sensor entities in HA with zero custom components needed!

---

### 💡 Idea 11: Pure-Python TCP Port 54921 Driver (Universal Legacy Brother Compatibility)
* **Origin:** Reverse-engineered protocol documented in [`thebino/brother-to-paperless`](https://github.com/thebino/brother-to-paperless/blob/main/PROTOCOL.md).
* **The Problem:** Modern Brother scanners (like our DS-940DW) speak driverless eSCL/AirScan over HTTP port 8080. However, many older Brother laser MFC printers (MFC-L2700DW, MFC-J430W, MFC-7820N) do *not* support eSCL and only expose a proprietary TCP protocol on port `54921`.
* **The Opportunity:**
  - Historically, homelabs required Brother's closed-source `brscan4` Linux binaries or SANE to talk to port 54921.
  - `thebino` reverse-engineered the entire binary protocol in pure Python:
    1. Connect TCP `:54921` → receives `+OK 200\r\n`.
    2. Lease request: `\x1bI\nR={dpi},{dpi}\nM={mode}\n\x80`.
    3. Capability negotiation: `\x00\x1d\x00...` dimensions and ADF state.
    4. Scan trigger: `\x1bX\nR=...` binary chunk framing with `0x82` (end-of-page) and `0x80` (end-of-scan).
  - By adding an optional `ds940/protocols/legacy_tcp.py` backend alongside `ds940/escl.py`, this project could become the definitive **universal driverless scanner engine** for *all* Brother scanners across both modern eSCL and legacy proprietary TCP generations!

---

### 💡 Idea 12: Binary Protocol Debug & Diagnostic Capture Mode (`--debug`)
* **Origin:** [`thebino/brother-to-paperless`](https://github.com/thebino/brother-to-paperless).
* **Concept:** Provide a one-flag protocol dump mode (`--debug`) that saves raw hardware transactions:
  - `offer.bin`: The raw capability handshake buffer returned by the scanner.
  - `scan.bin`: The unparsed raw byte stream received from the scanner socket.
* **Benefit:** When supporting new scanner models or investigating malformed TIFF/JPEG chunks, users can export and share diagnostic dumps without exposing any personal document text or image renderings.

---

### 💡 Idea 13: Vendor Firmware Isolation & Update Blocking
* **Origin:** Documented by [Calvin Walden](https://calvinrw.com/brscan4-paperless-workflow).
* **The Problem:** Brother periodically pushes silent firmware updates over Wi-Fi. In the homelab community, firmware updates have historically broken local network scan functionality, introduced proprietary protocol handshakes, or disabled third-party toner/cartridge chips.
* **Recommendation:**
  - Add documentation / network setup instructions recommending users blackhole Brother's firmware servers via their local DNS forwarder (Pi-hole, AdGuard Home, OPNSense, or pfSense):
    ```
    update.brother.co.jp
    firmverup.brother.co.jp
    ```
  - This guarantees that once a scanner's local network communication is working, it remains frozen in a known working state permanently.

---

### 💡 Idea 14: Multi-Action Keypad Routing (`to File` vs `to OCR` vs `to Image`)
* **Origin:** Highlighted in Calvin Walden's closing notes on multi-function printer keypads.
* **Concept:** Brother devices with keypad buttons transmit distinct event codes depending on whether the user pressed "Scan to File", "Scan to OCR", "Scan to Image", or "Scan to E-mail".
* **Architecture:**
  - In our UDP listener and eSCL profile mapping, map each physical button to a dedicated destination or processing pipeline:
    - **Scan to File:** Standard document ingest (`300 DPI Duplex` → Paperless-ngx / `~/Documents/brother-scans`).
    - **Scan to OCR:** Instant priority filing with local LLM taxonomy and immediate Spotlight search indexing.
    - **Scan to Image / Photo:** High-res mode (`600 DPI Color` → saved to a dedicated `Photos & Artifacts/` folder with zero lossy compression).



