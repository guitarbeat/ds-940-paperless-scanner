"""Adaptive, emergent taxonomy ledger and category learning engine."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import logging
from pathlib import Path
import re
import time

LOG = logging.getLogger(__name__)

LEDGER_FILENAME = ".taxonomy.json"


@dataclass
class CategoryMeta:
    name: str
    description: str = ""
    examples: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> CategoryMeta:
        return cls(
            name=data.get("name", "Inbox"),
            description=data.get("description", ""),
            examples=list(data.get("examples", [])),
            created_at=data.get("created_at", time.time()),
            count=int(data.get("count", 0)),
        )


class TaxonomyLedger:
    """Manages the evolving taxonomy ledger and vendor routing rules."""

    def __init__(self, root_dir: Path):
        self.root_dir = Path(root_dir).resolve()
        self.ledger_path = self.root_dir / LEDGER_FILENAME
        self.categories: dict[str, CategoryMeta] = {}
        self.vendor_mappings: dict[str, str] = {}
        self.version: int = 1
        self.load()

    def load(self) -> None:
        """Load the ledger from disk or initialize fresh if missing."""
        if self.ledger_path.exists():
            try:
                raw = json.loads(self.ledger_path.read_text(encoding="utf-8"))
                self.version = raw.get("version", 1)
                self.vendor_mappings = raw.get("vendor_mappings", {})
                cats_data = raw.get("categories", {})
                self.categories = {
                    name: CategoryMeta.from_dict(cdata)
                    for name, cdata in cats_data.items()
                }
            except Exception as err:
                LOG.warning("Failed to parse taxonomy ledger at %s: %s", self.ledger_path, err)
                self._init_defaults()
        else:
            self._init_defaults()

    def _init_defaults(self) -> None:
        """Initialize empty taxonomy. Categories will emerge organically."""
        self.categories = {}
        self.vendor_mappings = {}

    def save(self) -> None:
        """Persist taxonomy state to .taxonomy.json."""
        try:
            self.root_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": self.version,
                "updated_at": time.time(),
                "categories": {name: cat.to_dict() for name, cat in self.categories.items()},
                "vendor_mappings": self.vendor_mappings,
            }
            # Write atomically via temp file
            tmp = self.root_dir / f"{LEDGER_FILENAME}.tmp"
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.ledger_path)
        except OSError as err:
            LOG.error("Failed to save taxonomy ledger: %s", err)

    def normalize_vendor(self, vendor: str) -> str:
        """Normalize vendor strings for consistent lookup."""
        clean = vendor.replace("-", " ").replace("_", " ")
        clean = re.sub(r"[^\w\s]", "", clean.lower())
        return re.sub(r"\s+", " ", clean).strip()

    def clean_category_name(self, raw_name: str) -> str:
        """Sanitize category name into clean Title Case representation."""
        clean = raw_name.replace("_", " ")
        clean = re.sub(r"[^\w\s&-]", "", clean).strip()
        clean = re.sub(r"\s+", " ", clean)
        # Convert to Title Case words
        words = []
        for w in clean.split(" "):
            if "-" in w:
                w = "-".join(part.capitalize() for part in w.split("-"))
            elif w.lower() not in {"and", "&", "of", "for", "the"}:
                w = w.capitalize()
            else:
                w = w.lower()
            words.append(w)
        result = " ".join(words)
        if result.startswith("and "):
            result = result[4:]
        return result or "Inbox"

    def register_category(self, name: str, description: str = "", example: str = "") -> CategoryMeta:
        """Register or retrieve an emergent category."""
        clean_name = self.clean_category_name(name)
        if clean_name not in self.categories:
            examples = [example.strip()] if example.strip() else []
            self.categories[clean_name] = CategoryMeta(
                name=clean_name,
                description=description.strip(),
                examples=examples,
                created_at=time.time(),
                count=0,
            )
            LOG.info("Emergent category registered: '%s' (%s)", clean_name, description)
            self.save()
        else:
            cat = self.categories[clean_name]
            if description and not cat.description:
                cat.description = description.strip()
            if example and example not in cat.examples:
                cat.examples.append(example.strip())
            self.save()
        return self.categories[clean_name]

    def lookup_vendor(self, vendor: str) -> str | None:
        """Look up known category mapping for a vendor."""
        norm = self.normalize_vendor(vendor)
        return self.vendor_mappings.get(norm)

    def learn_mapping(self, vendor: str, category: str) -> None:
        """Learn and record that a vendor belongs to a specific category."""
        clean_cat = self.clean_category_name(category)
        norm_v = self.normalize_vendor(vendor)
        if norm_v and clean_cat:
            self.vendor_mappings[norm_v] = clean_cat
            if clean_cat in self.categories:
                if vendor not in self.categories[clean_cat].examples:
                    self.categories[clean_cat].examples.append(vendor)
            else:
                self.register_category(clean_cat, example=vendor)
            self.save()
            LOG.info("Learned routing rule: '%s' -> '%s'", vendor, clean_cat)

    def sync_with_filesystem(self) -> dict[str, int]:
        """Scan filesystem subfolders, register any newly discovered user folders, and update counts."""
        if not self.root_dir.exists():
            return {}

        counts: dict[str, int] = {}
        # Find all physical subdirectories
        for entry in self.root_dir.iterdir():
            if entry.is_dir() and not entry.name.startswith(".") and entry.name != "work":
                pdf_count = len([f for f in entry.iterdir() if f.is_file() and f.suffix.lower() == ".pdf" and not f.name.startswith(".")])
                cat_name = self.clean_category_name(entry.name)
                counts[cat_name] = pdf_count
                if cat_name not in self.categories:
                    self.categories[cat_name] = CategoryMeta(
                        name=cat_name,
                        description="Discovered folder",
                        count=pdf_count,
                    )
                else:
                    self.categories[cat_name].count = pdf_count

        # Sync existing categories that may have 0 files
        for name, meta in self.categories.items():
            if name not in counts:
                meta.count = 0

        self.save()
        return counts

    def get_active_categories(self) -> list[dict]:
        """Return list of categories that currently have files or defined rules."""
        self.sync_with_filesystem()
        active = []
        for name in sorted(self.categories.keys()):
            meta = self.categories[name]
            active.append({
                "name": meta.name,
                "description": meta.description,
                "count": meta.count,
                "examples": meta.examples[:4],
            })
        return active
