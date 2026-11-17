"""Apple Vision OCR wrapper for extracting text from scanned PDFs."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import shutil
import subprocess

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class OCRPage:
    page: int
    text: str


@dataclass(frozen=True)
class OCRResult:
    page_count: int
    pages: list[OCRPage]
    full_text: str

    @classmethod
    def empty(cls) -> OCRResult:
        return cls(page_count=0, pages=[], full_text="")

    def to_dict(self) -> dict:
        return {
            "page_count": self.page_count,
            "pages": [{"page": p.page, "text": p.text} for p in self.pages],
            "full_text": self.full_text,
        }


def find_ocr_binary() -> Path | None:
    """Locate the compiled ds940-ocr binary."""
    # 1. Check bin/ in repository root
    repo_root = Path(__file__).resolve().parent.parent
    candidate = repo_root / "bin" / "ds940-ocr"
    if candidate.is_file() and os_executable(candidate):
        return candidate
    # 2. Check system PATH
    which_bin = shutil.which("ds940-ocr")
    if which_bin:
        path = Path(which_bin)
        if path.is_file() and os_executable(path):
            return path
    return None


def os_executable(path: Path) -> bool:
    import os
    return os.access(path, os.X_OK)


def run_ocr(pdf_path: Path, timeout: float = 30.0) -> OCRResult:
    """Execute on-device Apple Vision OCR on a PDF and return the recognized text."""
    binary = find_ocr_binary()
    if not binary:
        LOG.warning("ds940-ocr binary not found at bin/ds940-ocr; skipping OCR.")
        return OCRResult.empty()

    if not pdf_path.exists():
        LOG.error("PDF file does not exist for OCR: %s", pdf_path)
        return OCRResult.empty()

    try:
        proc = subprocess.run(
            [str(binary), str(pdf_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            LOG.error("OCR failed with code %d: %s", proc.returncode, proc.stderr.strip())
            return OCRResult.empty()

        data = json.loads(proc.stdout)
        pages = [OCRPage(page=p["page"], text=p["text"]) for p in data.get("pages", [])]
        return OCRResult(
            page_count=data.get("pageCount", data.get("page_count", len(pages))),
            pages=pages,
            full_text=data.get("fullText", data.get("full_text", "")),
        )
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as err:
        LOG.exception("Error running OCR on %s: %s", pdf_path, err)
        return OCRResult.empty()
