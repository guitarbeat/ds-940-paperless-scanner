"""Durable scan staging and publication to the watched directory."""

from __future__ import annotations

from contextlib import ExitStack
from filecmp import cmp
import json
import logging
import os
from pathlib import Path
import shutil
import time
from uuid import uuid4

from PIL import Image

from .ai import classify_document
from .config import DPI
from .ocr import run_ocr

LOG = logging.getLogger(__name__)


def atomic_json(path, value, *, durable=False):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        if durable:
            stream.flush()
            os.fsync(stream.fileno())
    temporary.replace(path)


def build_pdf(images, target):
    with ExitStack() as stack:
        pages = [stack.enter_context(Image.open(path)) for path in images]
        temporary = target.with_suffix(".pdf.part")
        with temporary.open("xb") as stream:
            pages[0].save(
                stream, format="PDF", resolution=DPI, save_all=True, append_images=pages[1:]
            )
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)


class ScanSpool:
    def __init__(
        self,
        work,
        output,
        gemini_api_key: str = "",
        enable_ai_filing: bool = False,
    ):
        self.work = Path(work)
        self.output = Path(output)
        self.gemini_api_key = gemini_api_key
        self.enable_ai_filing = enable_ai_filing
        if not self.output.is_dir() or not os.access(self.output, os.W_OK | os.X_OK):
            raise ValueError("The output directory must be mounted and writable")
        self.jobs = self.work / "spool"
        self.jobs.mkdir(exist_ok=True)

    def new_scan(self):
        name = time.strftime("brother-%Y%m%d-%H%M%S-", time.gmtime()) + uuid4().hex[:8]
        folder = self.jobs / name
        folder.mkdir()
        return folder

    def process_ocr_and_ai(self, folder: Path, source: Path) -> tuple[Path, dict]:
        """Perform OCR and AI classification, returning destination path and metadata."""
        ocr_result = run_ocr(source)
        (folder / "ocr.txt").write_text(ocr_result.full_text, encoding="utf-8")

        ai_result = classify_document(ocr_result.full_text, api_key=self.gemini_api_key)

        category_dir = self.output / ai_result.category
        category_dir.mkdir(parents=True, exist_ok=True)

        target = category_dir / ai_result.suggested_filename
        if target.exists() and not cmp(source, target, shallow=False):
            stem = target.stem
            suffix = target.suffix
            target = category_dir / f"{stem}_{uuid4().hex[:4]}{suffix}"

        meta = {
            "ocr": ocr_result.to_dict(),
            "ai": ai_result.to_dict(),
            "category": ai_result.category,
            "filename": target.name,
        }
        return target, meta

    def publish(self, folder):
        manifest_path = folder / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["phase"] != "ready":
            return False

        source = folder / "document.pdf"
        with source.open("rb") as document:
            if document.read(5) != b"%PDF-":
                raise ValueError("Invalid staged PDF")

        if self.enable_ai_filing:
            try:
                target, ai_meta = self.process_ocr_and_ai(folder, source)
                manifest.update(ai_meta)
            except Exception as err:
                LOG.warning("AI filing failed (%s); using default naming: %s", type(err).__name__, err)
                target = self.output / f"{folder.name}.pdf"
        else:
            target = self.output / f"{folder.name}.pdf"

        temporary = target.parent / f".{target.name}.part"
        if target.exists():
            if not cmp(source, target, shallow=False):
                raise FileExistsError(f"Output already contains a different {target.name}")
        else:
            with source.open("rb") as document, temporary.open("wb") as stream:
                shutil.copyfileobj(document, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)

        destination_rel = str(target.relative_to(self.output))
        manifest.update(phase="forwarded", destination=destination_rel)
        atomic_json(manifest_path, manifest, durable=True)
        atomic_json(self.work / "last-scan.json", manifest, durable=True)
        LOG.info("Delivered %s (%d pages)", destination_rel, manifest["pages"])
        return True

    def recover(self):
        for manifest in sorted(self.jobs.glob("brother-*/manifest.json")):
            if manifest.is_symlink() or manifest.parent.is_symlink():
                continue
            try:
                self.publish(manifest.parent)
            except (OSError, ValueError, KeyError, TypeError):
                LOG.exception("Could not publish %s. The spool still holds the scan.", manifest.parent.name)
