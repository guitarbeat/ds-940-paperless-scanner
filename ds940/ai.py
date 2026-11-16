"""Document classification, metadata extraction, and adaptive taxonomy learning."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re
import time
from typing import TYPE_CHECKING
import urllib.error
import urllib.request

if TYPE_CHECKING:
    from .taxonomy import TaxonomyLedger

LOG = logging.getLogger(__name__)

DEFAULT_CATEGORY = "Inbox"


@dataclass(frozen=True)
class DocumentClassification:
    sender: str
    date: str
    document_type: str
    category: str
    suggested_filename: str
    summary: str
    confidence: float

    def to_dict(self) -> dict:
        return {
            "sender": self.sender,
            "date": self.date,
            "document_type": self.document_type,
            "category": self.category,
            "suggested_filename": self.suggested_filename,
            "summary": self.summary,
            "confidence": self.confidence,
        }


def sanitize_filename_part(part: str, max_length: int = 40) -> str:
    """Normalize a string to safe characters for filenames (A-Z, 0-9, dash, underscore)."""
    cleaned = re.sub(r"[^\w\s-]", "", part).strip()
    cleaned = re.sub(r"[-\s]+", "-", cleaned)
    if not cleaned:
        cleaned = "Unknown"
    return cleaned[:max_length]


def extract_date_heuristic(text: str) -> str:
    """Extract a date in YYYY-MM-DD format using common patterns, or return today's UTC date."""
    # Pattern 1: YYYY-MM-DD or YYYY/MM/DD
    match = re.search(r"\b(20\d\d)[-/.](0[1-9]|1[0-2])[-/.](0[1-9]|[12]\d|3[01])\b", text)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"

    # Pattern 2: MM/DD/YYYY or MM-DD-YYYY
    match = re.search(r"\b(0[1-9]|1[0-2])[-/.](0[1-9]|[12]\d|3[01])[-/.](20\d\d)\b", text)
    if match:
        return f"{match.group(3)}-{match.group(1)}-{match.group(2)}"

    # Pattern 3: Month DD, YYYY
    months = {
        "jan": "01", "feb": "02", "mar": "03", "apr": "04",
        "may": "05", "jun": "06", "jul": "07", "aug": "08",
        "sep": "09", "oct": "10", "nov": "11", "dec": "12",
    }
    month_regex = r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+([0-2]?[0-9]|3[01]),?\s+(20\d\d)\b"
    match = re.search(month_regex, text, re.IGNORECASE)
    if match:
        mon_str = match.group(1)[:3].lower()
        mon = months.get(mon_str, "01")
        day = f"{int(match.group(2)):02d}"
        year = match.group(3)
        return f"{year}-{mon}-{day}"

    # Pattern 4: Year only (e.g. 2024, 2025, 2026)
    match = re.search(r"\b(20[2-3]\d)\b", text)
    if match:
        year = match.group(1)
        today = time.strftime("%Y-%m-%d", time.gmtime())
        return f"{year}-{today[5:7]}-{today[8:10]}"

    return time.strftime("%Y-%m-%d", time.gmtime())


def classify_with_heuristic(text: str, ledger: TaxonomyLedger | None = None) -> DocumentClassification:
    """Offline heuristic classifier based on learned vendor mappings, keywords, and document headers."""
    date = extract_date_heuristic(text)
    lower = text.lower()

    # Extract sender from top lines
    lines = [line.strip() for line in text.splitlines() if len(line.strip()) > 1]
    sender = "Document"
    if lines:
        if "camp" in lower or "quilt" in lower:
            sender = "Quilt-Camp"
        elif "spectrum" in lower:
            sender = "Spectrum"
        elif "austin energy" in lower:
            sender = "Austin-Energy"
        elif "chase" in lower:
            sender = "Chase"
        elif "geico" in lower:
            sender = "Geico"
        else:
            sender = sanitize_filename_part(lines[0], max_length=25)

    sender = sanitize_filename_part(sender)
    doc_type = "Document"
    category = None

    # 1. Check ledger for learned vendor mapping first
    if ledger:
        matched_cat = ledger.lookup_vendor(sender)
        if matched_cat:
            category = matched_cat

    # 2. Heuristic domain patterns if not mapped
    if not category:
        if any(k in lower for k in ["irs", "internal revenue", "tax return", "form 1040", "w-2", "1099"]):
            category = "Taxes"
            doc_type = "Tax-Notice"
        elif any(k in lower for k in ["checking", "savings", "deposit", "credit card", "bank", "chase", "wells fargo", "vanguard", "fidelity", "schwab"]):
            category = "Banking"
            doc_type = "Bank-Statement"
        elif any(k in lower for k in ["electric", "water", "energy", "utility", "gas", "spectrum", "internet", "at&t", "verizon", "t-mobile", "due date", "amount due", "bill"]):
            category = "Utilities"
            doc_type = "Bill"
        elif any(k in lower for k in ["clinic", "hospital", "patient", "doctor", "physician", "prescription", "pharmacy", "dental", "vision", "medical", "labcorp", "quest"]):
            category = "Medical"
            doc_type = "Medical-Record"
        elif any(k in lower for k in ["policy", "claim", "coverage", "deductible", "premium", "geico", "progressive", "state farm", "allstate", "insurance"]):
            category = "Insurance"
            doc_type = "Policy"
        elif any(k in lower for k in ["receipt", "cashier", "subtotal", "register", "store", "merchant"]):
            category = "Receipts"
            doc_type = "Receipt"
        elif any(k in lower for k in ["camp", "event", "flyer", "invitation", "host", "craft", "quilt"]):
            category = "Crafts & Hobbies"
            doc_type = "Flyer"
        else:
            category = DEFAULT_CATEGORY

    # Register in ledger if present
    if ledger and category:
        ledger.register_category(category, example=sender)
        if sender and sender != "Document":
            ledger.learn_mapping(sender, category)

    doc_type = sanitize_filename_part(doc_type)
    filename = f"{date}_{sender}_{doc_type}.pdf"

    return DocumentClassification(
        sender=sender,
        date=date,
        document_type=doc_type,
        category=category,
        suggested_filename=filename,
        summary=text[:200].replace("\n", " "),
        confidence=0.7,
    )


def classify_with_gemini(
    text: str,
    api_key: str,
    ledger: TaxonomyLedger | None = None,
    timeout: float = 10.0,
) -> DocumentClassification:
    """Call Gemini 2.5 Flash API with adaptive taxonomy guidance."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"

    # Build list of existing categories
    existing_desc = []
    if ledger and ledger.categories:
        for name, meta in ledger.categories.items():
            ex_str = f" (e.g. {', '.join(meta.examples[:3])})" if meta.examples else ""
            desc = meta.description or "General documents"
            existing_desc.append(f"- \"{name}\": {desc}{ex_str}")
        categories_block = "\n".join(existing_desc)
    else:
        categories_block = "(None yet - you are establishing the first categories for this user's life)"

    prompt = f"""You are an intelligent, adaptive personal document organizer. Analyze the following OCR text extracted from a scanned document or piece of mail:

TEXT:
\"\"\"
{text[:4000]}
\"\"\"

CURRENT KNOWN CATEGORIES:
{categories_block}

TAXONOMY EVOLUTION RULES:
1. If this document fits clearly into one of the current known categories above, select that category.
2. If this document represents a distinctly new topic, domain, or area (e.g. Utilities, Housing, Taxes, Medical, Auto, Pets, Insurance, Crafts, Receipts, Education, Legal), PROPOSE A NEW, CONCISE CATEGORY name (1-3 words, Title Case).
3. Keep category names clean, natural, and reusable (e.g. "Utilities", "Medical", "Auto & Maintenance", "Housing", "Taxes", "Receipts").
4. NEVER use junk-drawer categories like "Personal & Other", "Misc", or "Unknown".

Output valid JSON only with this structure:
{{
  "sender": "<Short organization, company, or sender name, e.g. Austin-Energy, IRS, Chase, Geico>",
  "date": "<YYYY-MM-DD date found in document, or best estimate>",
  "document_type": "<e.g. Electric-Bill, Tax-Return, Bank-Statement, Medical-Summary, Receipt, Flyer>",
  "category": "<Exact existing category name OR your concise new proposed category>",
  "category_description": "<1 sentence explanation of what belongs in this category>",
  "suggested_filename": "<YYYY-MM-DD_Sender_DocType.pdf>",
  "summary": "<1-2 sentence description>",
  "confidence": <float between 0.0 and 1.0>
}}"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.1,
        },
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            raw_json_str = body["candidates"][0]["content"]["parts"][0]["text"]
            result = json.loads(raw_json_str)

            raw_cat = result.get("category", DEFAULT_CATEGORY).strip()
            cat_desc = result.get("category_description", "")
            sender = sanitize_filename_part(result.get("sender", "Unknown"))
            date = result.get("date", time.strftime("%Y-%m-%d"))
            doc_type = sanitize_filename_part(result.get("document_type", "Document"))
            filename = result.get("suggested_filename", f"{date}_{sender}_{doc_type}.pdf")
            if not filename.endswith(".pdf"):
                filename += ".pdf"

            # Register/evolve category in ledger
            category = raw_cat
            if ledger:
                meta = ledger.register_category(raw_cat, description=cat_desc, example=sender)
                category = meta.name
                if sender and sender != "Unknown":
                    ledger.learn_mapping(sender, category)

            return DocumentClassification(
                sender=sender,
                date=date,
                document_type=doc_type,
                category=category,
                suggested_filename=filename,
                summary=result.get("summary", ""),
                confidence=float(result.get("confidence", 0.9)),
            )
    except Exception as err:
        LOG.warning("Gemini classification failed (%s), falling back to heuristic: %s", type(err).__name__, err)
        return classify_with_heuristic(text, ledger=ledger)


def classify_document(
    text: str,
    api_key: str | None = None,
    ledger: TaxonomyLedger | None = None,
) -> DocumentClassification:
    """Classify a document using Gemini if key is provided, otherwise heuristic."""
    if not text.strip():
        date = time.strftime("%Y-%m-%d", time.gmtime())
        return DocumentClassification(
            sender="Scan",
            date=date,
            document_type="Document",
            category=DEFAULT_CATEGORY,
            suggested_filename=f"{date}_Scan.pdf",
            summary="Empty scan",
            confidence=0.1,
        )

    if api_key and api_key.strip():
        return classify_with_gemini(text, api_key.strip(), ledger=ledger)
    return classify_with_heuristic(text, ledger=ledger)
