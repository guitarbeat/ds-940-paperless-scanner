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
Features and enhancements cherry-picked from [Ruegen/scanbro](https://github.com/Ruegen/scanbro) for DS-940DW capabilities, with concrete implementation blueprints:

### 1. Long Receipt Scanning Mode (`long_receipt`)
- **Hardware Mechanics**: Standard duplex hardware caps at 14" (4200 units @ 300ths of an inch). In 1-sided simplex mode, the DS-940DW supports up to **31.5 inches (~80 cm / 9450 units)**.
- **eSCL Tag**: Setting `<scan:AutoCrop>true</scan:AutoCrop>` instructs the Brother hardware sensor to detect when the tail of the receipt passes the feed roller, automatically cutting off the scan early so it doesn't feed empty air.
- **Implementation in `ds940/escl.py`**:
```python
def build_scan_settings(
    duplex: bool = True,
    dpi: int = 300,
    color_mode: str = "RGB24",
    long_receipt: bool = False,
) -> str:
    """Build eSCL XML payload supporting 31.5" auto-cropped long receipts."""
    height = 9450 if long_receipt else 4200
    duplex_val = "false" if long_receipt else ("true" if duplex else "false")

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScanSettings xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03" xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
    <pwg:Version>2.0</pwg:Version>
    <scan:Intent>Document</scan:Intent>
    <pwg:ScanRegions>
        <pwg:ScanRegion>
            <pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>
            <pwg:XOffset>0</pwg:XOffset>
            <pwg:YOffset>0</pwg:YOffset>
            <pwg:Width>2550</pwg:Width>
            <pwg:Height>{height}</pwg:Height>
        </pwg:ScanRegion>
    </pwg:ScanRegions>
    <pwg:InputSource>Feeder</pwg:InputSource>
    <scan:Duplex>{duplex_val}</scan:Duplex>
    <scan:AutoCrop>true</scan:AutoCrop>
    <pwg:ContentType>TextAndPhoto</pwg:ContentType>
    <scan:ColorMode>{color_mode}</scan:ColorMode>
    <scan:XResolution>{dpi}</scan:XResolution>
    <scan:YResolution>{dpi}</scan:YResolution>
    <scan:DocumentFormat>image/jpeg</scan:DocumentFormat>
</scan:ScanSettings>"""
```

### 2. Intelligent Content-Box Auto-Cropping (`ds940/crop.py`)
- **The Problem**: A 2.5"-wide store receipt fed into an 8.5" scanner bed results in 5.5 inches of wasted blank whitespace on the right side.
- **The Solution**: Detect content boundary box on a downscaled luma thumbnail. Preserve left and top margins (`ORIGIN_ALIGN_FRAC = 0.08`) because the DS-940DW is top-fed and left-aligned, and trim off the blank right-hand bed.
- **Implementation in `ds940/crop.py`**:
```python
from PIL import Image
import numpy as np

CONTENT_LUMA = 236      # Pixels darker than this count as printed ink
DETECT_MAX = 480        # Fast thumbnail size for speckle-free detection
PAD_INCH = 0.12         # Padding margin in inches
ORIGIN_ALIGN_FRAC = 0.08 # Anchor left/top if content starts within 8% of edge

def trim_to_page(img: Image.Image, dpi: int = 300) -> Image.Image:
    w, h = img.size
    scale = max(1.0, max(w, h) / float(DETECT_MAX))
    thumb = img.resize((max(1, int(w / scale)), max(1, int(h / scale))), Image.Resampling.BILINEAR).convert("L")
    mask = np.array(thumb) < CONTENT_LUMA

    row_hits = np.sum(mask, axis=1) >= max(2, int(thumb.width * 0.004))
    col_hits = np.sum(mask, axis=0) >= max(2, int(thumb.height * 0.004))
    if not np.any(row_hits) or not np.any(col_hits):
        return img

    pad = max(8, int(round(dpi * PAD_INCH)))
    x0 = int(np.argmax(col_hits) * scale) - pad
    y0 = int(np.argmax(row_hits) * scale) - pad
    x1 = int((len(col_hits) - 1 - np.argmax(col_hits[::-1])) * scale) + pad
    y1 = int((len(row_hits) - 1 - np.argmax(row_hits[::-1])) * scale) + pad

    # Feeder is top-fed and left-aligned: preserve header and left margin
    if x0 < w * ORIGIN_ALIGN_FRAC: x0 = 0
    if y0 < h * ORIGIN_ALIGN_FRAC: y0 = 0

    return img.crop((max(0, x0), max(0, y0), min(w, x1), min(h, y1)))
```

### 3. Zero-Config mDNS Network Discovery (`ds940/discovery.py`)
- **The Problem**: Hardcoded IP addresses or `.local` mDNS names fail if the router assigns a new DHCP lease.
- **The Solution**: Background Apple Bonjour browse for `_uscan._tcp.local.` to discover Brother scanners dynamically.
- **Implementation in `ds940/discovery.py`**:
```python
import subprocess
import re

def discover_brother_scanner(timeout: float = 2.5) -> dict | None:
    """Discover Brother eSCL scanner on LAN via native macOS dns-sd Bonjour."""
    try:
        proc = subprocess.run(
            ["dns-sd", "-B", "_uscan._tcp", "local."],
            capture_output=True,
            text=True,
            timeout=timeout
        )
        for line in proc.stdout.splitlines():
            if "Brother" in line and "_uscan._tcp" in line:
                m = re.search(r"Brother DS-\w+\s+\[([0-9A-Fa-f]+)\]", line)
                if m:
                    mac = m.group(1)
                    return {"name": m.group(0), "host": f"BR{mac}.local"}
    except Exception:
        pass
    return None
```

### 4. Mailbox-Safe Size-Bounded PDF Export (< 5 MB) (`ds940/compress.py`)
- **The Problem**: 300 DPI duplex scans often exceed 10–25 MB, causing email attachments to bounce.
- **The Solution**: Recursive binary search on image resolution and JPEG quality (targeting 78% down to 38%) guaranteeing the file remains under 5 MB.
- **Implementation in `ds940/compress.py`**:
```python
import io
from PIL import Image

EMAIL_MAX_BYTES = 5 * 1024 * 1024  # 5 MB

def compress_images_to_pdf(images: list[Image.Image], max_bytes: int = EMAIL_MAX_BYTES) -> bytes:
    """Iteratively downscale and recompress pages until the PDF is under 5 MB."""
    buf = io.BytesIO()
    images[0].save(buf, format="PDF", save_all=True, append_images=images[1:], quality=88)
    pdf_bytes = buf.getvalue()
    if len(pdf_bytes) <= max_bytes:
        return pdf_bytes

    scale = min(0.95, max(0.15, (max_bytes / float(len(pdf_bytes))) ** 0.5 * 0.92))
    quality = 78
    for _ in range(8):
        scaled = [
            img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.Resampling.BILINEAR)
            for img in images
        ]
        buf = io.BytesIO()
        scaled[0].save(buf, format="PDF", save_all=True, append_images=scaled[1:], quality=quality)
        out = buf.getvalue()
        if len(out) <= max_bytes:
            return out
        overshoot = len(out) / float(max_bytes)
        scale = max(0.15, scale * ((1.0 / overshoot) ** 0.5) * 0.9)
        quality = max(38, quality - 8)
    return out
```

### 5. Direct AirPrint / macOS `lpr` Printing (`ds940/print_helper.py`)
- **Feature**: 1-click "Print" button in Quick Look or keyboard shortcut (`R`) that sends the current scan directly to the default printer without opening a viewer.
- **Implementation**:
```python
import subprocess
from pathlib import Path

def print_pdf(pdf_path: Path, printer: str | None = None) -> bool:
    """Send scanned PDF directly to local or AirPrint printer using macOS lpr."""
    cmd = ["lpr"]
    if printer:
        cmd.extend(["-P", printer])
    cmd.append(str(pdf_path))
    res = subprocess.run(cmd, capture_output=True, text=True)
    return res.returncode == 0
```

### 6. Multi-Preset Scanner Quality Keyboard Shortcuts
- Quick 1-key presets matching Scanbro keyboard mapping:
  - `1`: Fast Web (150 DPI, Grayscale)
  - `2`: Email Friendly (200 DPI, Color)
  - `3`: Standard Document (300 DPI, Color - Default)
  - `4`: High-Res Photo (400 DPI, Color)
  - `5`: Fine Archival (600 DPI, Color)
  - `L`: Long Receipt Mode Toggle (1-Sided, 31.5" auto-cropped)
  - `R`: Direct Print (`lpr`)
  - `E`: Export for Email (< 5 MB)
