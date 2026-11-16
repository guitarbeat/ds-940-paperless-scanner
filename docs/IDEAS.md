# Architectural Ideas & Inspirations

This document captures architectural analysis and ideas inspired by [`vanessa/brother-scan-to-paperless`](https://github.com/vanessa/brother-scan-to-paperless), evaluating how its concepts can enhance `ds-940-paperless-scanner`.

---

## 1. Architectural Comparison

| Dimension | `brother-scan-to-paperless` (Vanessa) | `ds-940-paperless-scanner` (This Project) |
| :--- | :--- | :--- |
| **Primary Target** | Brother Multi-Function Laser Printers (`DCP-L2540DW`) with LCD control panels | Brother Portable Document Scanner (`DS-940DW`) sheet-fed mobile unit |
| **Driver Architecture** | **Proprietary:** Requires Brother closed-source `brscan4` Linux binaries (x86_64 only), SANE `scanimage`, and SNMP | **100% Driverless:** Pure Python eSCL (Apple AirScan) over local HTTP (`:8080`). Runs natively on Apple Silicon, Linux, and Docker |
| **Trigger Mechanism** | User selects "Scan to PC" on printer LCD screen, firing a UDP packet to port `54925` | **Hands-Free:** Feeder optical sensor detects incoming paper (`<scan:AdfState>`) and auto-feeds |
| **OCR & Text Layer** | Delegated downstream to Paperless-ngx | **On-Device Native:** Local Apple Vision Neural Engine text layer (`bin/ds940-ocr`) + emergent AI taxonomy |
| **Management UI** | CLI logs only | Responsive Web Dashboard (`:9400` / `scans.alw.lol`), live QuickLook preview, filters, and mobile access |

---

## 2. Key Concepts to Adopt

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

### 💡 Idea 2: Dual-Mode Triggering via UDP 54925 Listener

* **Concept:** Listen for hardware button-press events in addition to optical sensor polling.
* **Mechanism:**
  - Brother devices broadcast UDP packets to port `54925` containing `TYPE=BR;BUTTON=SCAN` when the physical scan button is pressed.
  - By running a lightweight background UDP socket listener on `0.0.0.0:54925`, our receiver can trigger an immediate scan cycle the microsecond the button is pressed—eliminating the standard 3-second eSCL polling sleep.
  - Supported by periodic SNMP profile registration on OID `.1.3.6.1.4.1.2435.2.3.9.2.11.1.1.0`.

---

### 💡 Idea 3: One-Step Turn-Key `install.sh` Wizard

* **Concept:** Provide a single-command setup script for new users cloning the repository.
* **Features:**
  - **Platform Detection:** Detects macOS vs Linux automatically.
    - macOS: Sets up and loads `~/Library/LaunchAgents/com.ds940receiver.plist`.
    - Linux / Proxmox: Generates and enables `/etc/systemd/system/ds940receiver.service`.
  - **LAN Scanner Discovery:** Probes mDNS (`_uscan._tcp`) to auto-detect the scanner's local IP address without manual router checks.
  - **Permission Check:** Verifies read/write access to destination folders and tests eSCL HTTP connectivity.

---

### 💡 Idea 4: Scan Profiles & Presets

* **Concept:** Allow rapid switching of resolution, color mode, and page bounds.
* **Preset Definitions:**
  - **Fast Document (Default):** 300 DPI Duplex, Color/Grayscale auto, A4/Letter.
  - **Archival Text / Receipt:** 300 DPI Black & White (thresholded), high compression.
  - **High-Res Graphic:** 600 DPI Color, maximum JPEG quality.
* **Dashboard Integration:** Quick toggle chips in the web interface at `scans.alw.lol` to switch profiles with one click.
