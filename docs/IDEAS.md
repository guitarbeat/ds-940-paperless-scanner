# Architectural Ideas & Community Homelab Insights

This document catalogs architectural enhancements, community homelab patterns (including [r/selfhosted](https://www.reddit.com/r/selfhosted/comments/1hy690i/brother_l2710dw_and_scanning_to_network_folder_is/)), and lessons learned from open-source Brother implementations:
- [`thebino/brother-to-paperless`](https://github.com/thebino/brother-to-paperless) (Home Assistant HACS integration and pure-Python port 54921 binary driver)
- [Calvin Walden's MFC-L2750DW to Paperless Workflow](https://calvinrw.com/brscan4-paperless-workflow) (Ghostscript ink coverage calibration, NFS inotify limitations, and DNS firmware blackholing)
- [`vanessa/brother-scan-to-paperless`](https://github.com/vanessa/brother-scan-to-paperless) (UDP push-button listeners and SNMP profile registration)
- [`PhilippMundhenk/BrotherScannerDocker`](https://github.com/PhilippMundhenk/BrotherScannerDocker) (Blank page filtering and manual simplex interleaving)
- [`quadportnick/brother-scan-paperless`](https://github.com/quadportnick/brother-scan-paperless) (Staging directories and Paperless inotify race condition guards)

---

## 1. Quick Reference: Edits by Category

| Category | Idea | Target Components | Complexity | Primary Impact |
|:---|:---|:---|:---:|:---|
| **Pipeline & Processing** | [Atomic Staging & Ingestion Guard](#idea-1-atomic-staging--ingestion-guard) | `ds940/spool.py` | Low | Eliminates file corruption in Paperless `inotify` |
| **Pipeline & Processing** | [Blank Page Detection & Suppression](#idea-2-automatic-blank-page-detection--filtering) | `ds940/spool.py` | Medium | Removes empty backsides on duplex mail |
| **Pipeline & Processing** | [Simplex-to-Duplex Manual Interleaving](#idea-3-simplex-to-duplex-manual-interleaving) | `ds940/spool.py` | Medium | Assembles multi-page batches on single-sided ADFs |
| **Hardware & Protocol** | [Pure-Python TCP Port 54921 Driver](#idea-4-pure-python-tcp-port-54921-driver-legacy-brother-support) | `ds940/protocols/legacy_tcp.py` | High | Adds legacy Brother laser MFC support |
| **Hardware & Protocol** | [Dual-Mode UDP Port 54925 Button Listener](#idea-5-dual-mode-triggering-via-udp-54925-button-listener) | `ds940/button_listener.py` | Medium | Instant trigger when physical button is pressed |
| **Hardware & Protocol** | [Multi-Action Keypad Routing](#idea-6-multi-action-keypad-routing-file-vs-ocr-vs-image) | `ds940/supervisor.py` | Medium | Routes scans based on scanner screen buttons |
| **Hardware & Protocol** | [Binary Protocol Debug Capture Mode](#idea-7-binary-protocol-debug--diagnostic-capture-mode---debug) | `ds940/cli.py`, `ds940/escl.py` | Low | Diagnostic byte dumps without leaking document text |
| **Downstream Integrations**| [Direct Paperless-ngx REST API Ingestion](#idea-8-direct-paperless-ngx-rest-api-ingestion) | `ds940/export/paperless.py` | Medium | Bypasses NFS permissions, latency, and inotify drops |
| **Downstream Integrations**| [Home Assistant MQTT Discovery & Sensors](#idea-9-home-assistant-integration--smart-home-automation-mqtt--hacs) | `ds940/mqtt.py` | Medium | Exposes buttons and status sensors to Home Assistant |
| **Downstream Integrations**| [Push Notifications & Webhooks](#idea-10-push-notifications--webhooks-discord-telegram-pushover-n8n) | `ds940/notify.py` | Low | Alerts mobile phones upon successful document filing |
| **Dashboard & Config** | [Scan Profiles & Preset Toggles](#idea-11-scan-profiles--quick-presets) | `ds940/config.py`, `ds940/web.py` | Low | One-click switching between Document, Archival, Photo |
| **Dashboard & Config** | [Persistent `scanner.conf` & Web Settings](#idea-12-persistent-file-based-config-scannerconf-with-web-settings) | `ds940/config.py`, `ds940/web.py` | Medium | Live configuration changes without touching system plists |
| **DevOps & Infrastructure**| [Turn-Key `install.sh` Provisioning Wizard](#idea-13-one-step-turn-key-installsh-wizard) | `scripts/install.sh` | Medium | Single-command launchd and systemd setup |
| **DevOps & Infrastructure**| [Vendor Firmware Blackholing Runbook](#idea-14-vendor-firmware-isolation--update-blocking) | `docs/DNS_SECURITY.md` | Low | Protects local network scanners from breaking updates |

---

## 2. Pipeline & Document Processing Edits

### Idea 1: Atomic Staging & Ingestion Guard
* **Type of Edit:** Backend Pipeline / Filesystem Guard
* **Target Components:** `ds940/spool.py`, `ds940/config.py`
* **Origin:** Highlighted in `quadportnick/brother-scan-paperless` and Paperless-ngx community forums.
* **The Problem:** Paperless-ngx watches ingest folders using Linux `inotify`. If our daemon streams pages or writes multi-page PDFs directly into `consume/`, Paperless frequently consumes partial, incomplete files while they are still being assembled or OCR-processed.
* **Technical Specification:**
  1. Assemble all PDF pages, run native Vision OCR, and extract AI taxonomy inside an isolated hidden folder:
     `~/Documents/brother-scans/.staging/`
  2. Perform an atomic POSIX file move (`os.replace` / `shutil.move`) into the target archive or `PAPERLESS_CONSUME_DIR` only after the document is finalized, flushed, and verified.
* **Impact:** Eliminates file corruption, zero-byte PDF errors, and ingestion race conditions permanently.

---

### Idea 2: Automatic Blank Page Detection & Filtering
* **Type of Edit:** Image Processing & PDF Compilation
* **Target Components:** `ds940/spool.py`
* **Origin:** Implemented in `PhilippMundhenk/BrotherScannerDocker` and empirically calibrated by Calvin Walden.
* **The Problem:** In duplex mode, scanning single-sided mail or receipts produces blank white backsides that double the resulting file size and clutter the searchable document archive.
* **Calvin Walden's Empirical Ink Coverage Calibration:**
  * True blank pages (with scanner dust or minor paper transparency bleed-through): `0.00 to 0.03` ink coverage.
  * Real document pages (sparse receipts, signature lines, short forms): `0.30+` ink coverage.
  * Setting a removal threshold of `0.10` ink density establishes a safe margin: it drops 100% of blank reverse pages while guaranteeing that faint invoices are never pruned.
* **Technical Specification:**
  ```python
  # ds940/spool.py
  def is_blank_page(image: Image.Image, threshold: float = 0.10) -> bool:
      # Convert to grayscale and measure ink pixel ratio against pure white
      gray = image.convert("L")
      histogram = gray.histogram()
      total_pixels = image.width * image.height
      # Non-white pixels below 245 brightness
      ink_pixels = sum(histogram[:245])
      coverage = ink_pixels / total_pixels
      return coverage < threshold
  ```
  * Discard detected blank pages prior to OCR and log:
    `Discarded blank page 2 (ink coverage: 0.018 < 0.100)`
* **Impact:** Cuts document storage in half for mixed one-sided/two-sided mail batches without losing legitimate data.

---

### Idea 3: Simplex-to-Duplex Manual Interleaving
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

## 3. Hardware Driver & Protocol Edits

### Idea 4: Pure-Python TCP Port 54921 Driver (Legacy Brother Support)
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

## 4. Downstream Export & Integration Edits

### Idea 8: Direct Paperless-ngx REST API Ingestion
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

## 5. Web Dashboard & Runtime Configuration Edits

### Idea 11: Scan Profiles & Quick Presets
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

## 6. DevOps, Setup & Network Security Edits

### Idea 13: One-Step Turn-Key `install.sh` Wizard
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
