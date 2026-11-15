"""Driverless eSCL (Apple AirScan) client for Brother DS-940DW."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

LOG = logging.getLogger(__name__)

PORT = 8080
NS = {
    "scan": "http://schemas.hp.com/imaging/escl/2011/05/03",
    "pwg": "http://www.pwg.org/schemas/2010/12/sm",
}


class EsclError(RuntimeError):
    """Base exception for eSCL communication failures."""


class ScannerOfflineError(EsclError):
    """Scanner is powered off or unreachable."""


class ScannerBusyError(EsclError):
    """Scanner is currently processing another scan job."""


class PaperJammedError(EsclError):
    """Paper is jammed in the feeder."""


@dataclass(frozen=True)
class ScannerStatus:
    state: str
    adf_state: str

    @property
    def is_paper_loaded(self) -> bool:
        return self.adf_state == "ScannerAdfLoaded"

    @property
    def is_paper_empty(self) -> bool:
        return self.adf_state == "ScannerAdfEmpty"

    @property
    def is_jammed(self) -> bool:
        return self.adf_state == "ScannerAdfJammed"

    @property
    def is_busy(self) -> bool:
        return self.state.lower() in ("processing", "busy", "testing")

    @property
    def is_idle(self) -> bool:
        return self.state.lower() == "idle"


class EsclScanner:
    """Manages driverless eSCL communication with a Brother DS-940DW."""

    def __init__(self, host: str, port: int = PORT):
        self.host = host
        self.port = port
        self.base_url = f"http://{host}:{port}/eSCL"

    def get_status(self, timeout: float = 3.0) -> ScannerStatus:
        """Query the scanner's operational and feeder status."""
        url = f"{self.base_url}/ScannerStatus"
        req = urllib.request.Request(url, headers={"User-Agent": "ds940-receiver/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    raise EsclError(f"ScannerStatus returned HTTP {resp.status}")
                payload = resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            raise ScannerOfflineError(f"Cannot reach scanner at {url}: {err}") from err

        try:
            root = ET.fromstring(payload)
            state_elem = root.find("pwg:State", NS)
            adf_elem = root.find("scan:AdfState", NS)

            state = state_elem.text if state_elem is not None and state_elem.text else "Unknown"
            adf_state = adf_elem.text if adf_elem is not None and adf_elem.text else "Unknown"
            return ScannerStatus(state=state, adf_state=adf_state)
        except ET.ParseError as err:
            raise EsclError(f"Invalid XML from ScannerStatus: {err}") from err

    def start_scan_job(
        self,
        duplex: bool = True,
        dpi: int = 300,
        color_mode: str = "RGB24",
        timeout: float = 10.0,
    ) -> str:
        """Create a new scan job on the scanner and return the job URL."""
        url = f"{self.base_url}/ScanJobs"
        xml_payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScanSettings xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03" xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
    <pwg:Version>2.63</pwg:Version>
    <scan:Intent>Document</scan:Intent>
    <scan:InputSource>Feeder</scan:InputSource>
    <scan:Duplex>{str(duplex).lower()}</scan:Duplex>
    <scan:ColorMode>{color_mode}</scan:ColorMode>
    <scan:XResolution>{dpi}</scan:XResolution>
    <scan:YResolution>{dpi}</scan:YResolution>
    <scan:DocumentFormat>image/jpeg</scan:DocumentFormat>
</scan:ScanSettings>"""

        req = urllib.request.Request(
            url,
            data=xml_payload.encode("utf-8"),
            headers={
                "Content-Type": "text/xml",
                "User-Agent": "ds940-receiver/1.0",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status not in (200, 201):
                    raise EsclError(f"ScanJobs returned HTTP {resp.status}")
                location = resp.headers.get("Location")
                if not location:
                    raise EsclError("Scanner did not return a Location header for the new job")
                if location.startswith("http"):
                    return location
                return f"http://{self.host}:{self.port}{location}"
        except urllib.error.HTTPError as err:
            if err.code in (409, 503):
                raise ScannerBusyError(f"Scanner busy (HTTP {err.code})") from err
            raise EsclError(f"ScanJob creation failed: HTTP {err.code}") from err
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            raise ScannerOfflineError(f"Failed to connect to scanner for ScanJob: {err}") from err

    def fetch_job_pages(
        self,
        job_url: str,
        output_folder: Path,
        start_page: int = 1,
        timeout: float = 60.0,
    ) -> list[Path]:
        """Download all scanned page images for a job into the given folder."""
        pages: list[Path] = []
        page_num = start_page
        output_folder.mkdir(parents=True, exist_ok=True)

        while True:
            doc_url = f"{job_url}/NextDocument"
            req = urllib.request.Request(doc_url, headers={"User-Agent": "ds940-receiver/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    if resp.status == 200:
                        data = resp.read()
                        if not data:
                            break
                        page_path = output_folder / f"page-{page_num}.jpg"
                        page_path.write_bytes(data)
                        pages.append(page_path)
                        page_num += 1
                    else:
                        break
            except urllib.error.HTTPError as err:
                # HTTP 404 (or 410) marks end of document in eSCL
                if err.code in (404, 410):
                    break
                raise EsclError(f"Error fetching page {page_num}: HTTP {err.code}") from err
            except (urllib.error.URLError, TimeoutError, OSError) as err:
                raise EsclError(f"Connection lost while downloading page {page_num}: {err}") from err

        # Clean up job on the scanner
        try:
            del_req = urllib.request.Request(job_url, method="DELETE")
            with urllib.request.urlopen(del_req, timeout=5.0):
                pass
        except Exception:
            pass

        return pages
