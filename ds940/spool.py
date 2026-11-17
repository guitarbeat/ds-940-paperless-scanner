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


def calculate_ink_coverage(
    image_path: Path | str,
    margin_ratio: float = 0.03,
    ink_threshold: int = 210,
) -> float:
    """Calculate the ratio of ink pixels on the page (0.0 to 1.0).

    - Inset margin crop (default 3% on all sides) eliminates roller shadows and ADF feed edges.
    - Grayscale luminosity threshold < 210 filters out paper bleed-through shadows from opposite side.
    """
    with Image.open(image_path) as img:
        gray = img.convert("L")
        w, h = gray.size
        crop_x = int(w * margin_ratio)
        crop_y = int(h * margin_ratio)
        if crop_x > 0 and crop_y > 0 and (w - 2 * crop_x) > 0 and (h - 2 * crop_y) > 0:
            gray = gray.crop((crop_x, crop_y, w - crop_x, h - crop_y))

        hist = gray.histogram()
        total_pixels = gray.width * gray.height
        if total_pixels <= 0:
            return 0.0

        ink_pixels = sum(hist[:ink_threshold])
        return ink_pixels / total_pixels


def is_blank_page(
    image_path: Path | str,
    threshold: float = 0.05,
    margin_ratio: float = 0.03,
    ink_threshold: int = 210,
) -> bool:
    """Check if a single page image is blank."""
    if threshold <= 0.0:
        return False
    cov = calculate_ink_coverage(image_path, margin_ratio=margin_ratio, ink_threshold=ink_threshold)
    return cov < threshold


def filter_blank_pages(
    image_paths: list[Path],
    threshold: float = 0.05,
    margin_ratio: float = 0.03,
    ink_threshold: int = 210,
) -> tuple[list[Path], list[Path]]:
    """Filter out blank pages from a multi-page scan batch.

    Returns (kept_pages, discarded_blank_pages).

    Guarantees:
    - Retain Page 1 Invariant: If all pages are blank, Page 1 is always kept
      to avoid zero-page spool crashes and invalid empty PDFs.
    - Zero False Negatives: If threshold <= 0.0, filtering is disabled.
    """
    if not image_paths:
        return [], []
    if threshold <= 0.0:
        return list(image_paths), []

    kept: list[Path] = []
    discarded: list[Path] = []

    for idx, path in enumerate(image_paths):
        try:
            cov = calculate_ink_coverage(path, margin_ratio=margin_ratio, ink_threshold=ink_threshold)
            if cov < threshold:
                discarded.append(path)
                LOG.info(
                    "Page %d (%s) classified as blank (ink coverage: %.4f < %.4f)",
                    idx + 1,
                    path.name,
                    cov,
                    threshold,
                )
            else:
                kept.append(path)
        except Exception as err:
            LOG.warning("Failed to evaluate blankness for %s (%s); keeping page", path.name, err)
            kept.append(path)

    # Retain Page 1 Invariant: If everything evaluated as blank, keep Page 1
    if not kept and image_paths:
        kept.append(image_paths[0])
        discarded.remove(image_paths[0])
        LOG.info("All pages evaluated as blank; retaining Page 1 as safety invariant")

    return kept, discarded


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
        remove_blank_threshold: float = 0.05,
    ):
        self.work = Path(work)
        self.output = Path(output)
        self.gemini_api_key = gemini_api_key
        self.enable_ai_filing = enable_ai_filing
        self.remove_blank_threshold = remove_blank_threshold
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
                ai_meta = {}
        else:
            target = self.output / f"{folder.name}.pdf"
            ai_meta = {}

        # Atomic staging guard: write to hidden .staging_xxx.tmp on target filesystem, fsync, chmod, and atomic replace
        staging_path = target.parent / f".staging_{uuid4().hex[:8]}.tmp"

        if target.exists():
            if not cmp(source, target, shallow=False):
                raise FileExistsError(f"Output already contains a different {target.name}")
        else:
            with source.open("rb") as document, staging_path.open("wb") as stream:
                shutil.copyfileobj(document, stream)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.chmod(staging_path, 0o644)
            except OSError:
                pass
            staging_path.replace(target)

        # Apply native macOS Finder color tags and Spotlight metadata
        try:
            from .finder_tags import set_finder_metadata
            doc_category = ai_meta.get("category") or (target.parent.name if target.parent != self.output else "Inbox")
            doc_sender = ai_meta.get("ai", {}).get("sender") or ""
            doc_title = ai_meta.get("ai", {}).get("suggested_filename", target.name)
            set_finder_metadata(
                target,
                category=doc_category,
                tags=[t for t in [doc_category, doc_sender, "Brother DS-940DW"] if t],
                title=doc_title,
                keywords=[k for k in [doc_category, doc_sender, "scanned", "brother-ds940dw"] if k],
            )
        except Exception as tag_err:
            LOG.warning("Could not apply Finder tags to %s: %s", target.name, tag_err)

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
