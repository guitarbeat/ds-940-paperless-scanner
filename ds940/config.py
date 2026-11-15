"""Receiver settings and scanner network configuration."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address
import os
from pathlib import Path

DPI = 300


@dataclass(frozen=True)
class Settings:
    scanner_ip: str
    work: Path
    output: Path
    paper_size: str
    gemini_api_key: str = ""
    enable_ai_filing: bool = False
    batch_timeout: float = 8.0
    web_port: int = 9400
    web_bind_ip: str = "0.0.0.0"

    @classmethod
    def from_environment(cls):
        address = os.environ.get("SCANNER_IP", "").strip()
        if not address:
            address = os.environ.get("SCANNER_HOST", "").strip()
        if not address:
            raise ValueError("Scanner requires a LAN IPv4 address or hostname (e.g. SCANNER_IP=192.168.0.25)")

        # Allow either IPv4 address or hostname (e.g. BR5CF370DC7026.local)
        try:
            scanner_ip = str(IPv4Address(address))
        except ValueError:
            scanner_ip = address

        paper_size = os.environ.get("SCAN_PAPER_SIZE", "a4").strip().lower()
        if paper_size not in {"a4", "auto", "legal"}:
            raise ValueError("SCAN_PAPER_SIZE must be a4, auto, or legal")

        default_work = Path("./work")
        default_output = (
            Path.home() / "Scans"
            if (Path.home() / "Scans").exists()
            else Path.home() / "Documents" / "Scans"
        )

        gemini_api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        enable_ai_filing = os.environ.get("ENABLE_AI_FILING", "0").strip().lower() in ("1", "true", "yes")
        batch_timeout = float(os.environ.get("BATCH_TIMEOUT", "8.0").strip())
        web_port = int(os.environ.get("WEB_PORT", "9400").strip())
        web_bind_ip = os.environ.get("WEB_BIND_IP", "0.0.0.0").strip()

        return cls(
            scanner_ip=scanner_ip,
            work=Path(os.environ.get("WORK_DIR", str(default_work))),
            output=Path(os.environ.get("OUTPUT_DIR", str(default_output))),
            paper_size=paper_size,
            gemini_api_key=gemini_api_key,
            enable_ai_filing=enable_ai_filing,
            batch_timeout=batch_timeout,
            web_port=web_port,
            web_bind_ip=web_bind_ip,
        )

    def configure_driver(self):
        """Optional driver config for legacy SANE backends."""
        try:
            home = self.work / "home"
            home.mkdir(parents=True, exist_ok=True)
            os.environ["HOME"] = str(home)
            cfg = self.work / "brsanenetdevice.cfg"
            cfg.write_text(
                f'DEVICE=DS940 , "DS-940DW" , 0x4f9:0x46a , IP-ADDRESS={self.scanner_ip} , FLAG=\n',
                encoding="ascii",
            )
        except OSError:
            pass
