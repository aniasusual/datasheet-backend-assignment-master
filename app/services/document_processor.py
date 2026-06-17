"""PDF document processor.

Handles the ingestion step: PDF → rendered page images + extracted text.
No LLM involved — purely deterministic processing.
"""

import logging
import re
import uuid
from pathlib import Path

import pdfplumber
from pdf2image import convert_from_path
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.document import Document, DocumentStatus
from app.models.document_page import DocumentPage, ExtractionQuality

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_TEXT_CHARS = 20

FRENCH_KEYWORDS = {
    "PRESSION", "DÉBIT", "DEBIT", "ASPIRATION", "REFOULEMENT",
    "MASSE VOL", "VISCOSITE", "VISCOSITÉ", "HAUTEUR MANO",
    "POMPE CENTRIFUGE", "CONDITIONS OPERATOIRES", "MATERIAUX",
    "GARNITURE", "PALIER", "MOTEUR FOURNI", "REMARQUES",
    "FONCTION", "ENTRAINE PAR",
}

TAG_PATTERNS = [
    re.compile(r"pds[_-]?([A-Z]?\d{3,6}[A-Z]?)", re.IGNORECASE),
    re.compile(r"\b(P[- ]?\d{3,6})\s*(?:\([A-Z/]+\))?", re.IGNORECASE),
]

# Pages with very little text and these keywords are boilerplate
BOILERPLATE_KEYWORDS = {"REVISION MODIFICATION LOG", "HOLD LIST"}


# ---------------------------------------------------------------------------
# File type detection (magic bytes)
# ---------------------------------------------------------------------------

def detect_file_type(file_path: Path) -> str:
    """Detect file type via magic bytes. Returns 'pdf' or 'unknown'."""
    try:
        import filetype
        kind = filetype.guess(str(file_path))
        if kind and kind.mime == "application/pdf":
            return "pdf"
        return "unknown"
    except ImportError:
        return "pdf" if file_path.suffix.lower() == ".pdf" else "unknown"


# ---------------------------------------------------------------------------
# Tag / format detection helpers
# ---------------------------------------------------------------------------

def extract_tag_from_filename(filename: str) -> str | None:
    stem = Path(filename).stem
    match = TAG_PATTERNS[0].search(stem)
    if match:
        raw = match.group(1).upper()
        if raw and raw[0].isalpha() and raw[1:].isdigit():
            return f"{raw[0]}-{raw[1:]}"
        return raw
    return None


def extract_tag_from_content(text: str) -> str | None:
    for pattern in TAG_PATTERNS[1:]:
        match = pattern.search(text)
        if match:
            raw = match.group(1).upper().replace(" ", "-")
            if len(raw) >= 2 and raw[0].isalpha() and raw[1].isdigit():
                raw = f"{raw[0]}-{raw[1:]}"
            return raw
    return None


def detect_format_type(all_text: str) -> str:
    upper = all_text.upper()
    french_hits = sum(1 for kw in FRENCH_KEYWORDS if kw in upper)
    return "french_form" if french_hits >= 3 else "english_tabular"


def _classify_page(raw_text: str) -> str:
    """Classify a page as 'content' or 'boilerplate'."""
    text = raw_text.strip()
    if len(text) < MIN_TEXT_CHARS:
        # No text extracted — likely image-only content page, not boilerplate
        return "content"
    upper = text.upper()
    # If the page contains boilerplate markers, strip them and check what's left
    has_boilerplate = any(kw in upper for kw in BOILERPLATE_KEYWORDS)
    if has_boilerplate:
        remaining = upper
        for kw in BOILERPLATE_KEYWORDS:
            remaining = remaining.replace(kw, "")
        # Strip common table headers found on cover pages
        for header in ("REVISION", "SECTION", "DESCRIPTION", "HOLD", "COMMENT",
                       "PROCESS DATASHEET FOR PUMP", "UNCLASSIFIED", "PAGE", "DATE"):
            remaining = remaining.replace(header, "")
        # Remove punctuation, digits, whitespace
        remaining = re.sub(r"[^A-Z]", "", remaining)
        if len(remaining) < 50:
            return "boilerplate"
    return "content"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def process_document(
    file_path: str | Path,
    document_id: uuid.UUID,
    db: AsyncSession,
) -> Document:
    """Process a PDF: extract text per page, render pages to PNG at high DPI.

    This is deterministic CPU work — no LLM involved.
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    doc = await db.get(Document, document_id)
    if doc is None:
        raise ValueError(f"Document {document_id} not found in database")

    # Create output directory for rendered pages
    doc_render_dir = settings.RENDERED_PAGES_DIR / str(document_id)
    doc_render_dir.mkdir(parents=True, exist_ok=True)

    pump_tag: str | None = extract_tag_from_filename(doc.filename)

    try:
        all_text_parts: list[str] = []

        # Render all pages to PNG at high DPI using pdf2image (poppler)
        images = convert_from_path(str(file_path), dpi=settings.RENDER_DPI)

        with pdfplumber.open(str(file_path)) as pdf:
            num_pages = len(pdf.pages)

            for page_num, (page, page_image) in enumerate(zip(pdf.pages, images), start=1):
                # Extract raw text
                raw_text = page.extract_text() or ""

                # Extract layout-aware text
                layout_text: str | None = None
                try:
                    layout_text = page.extract_text(layout=True) or None
                except Exception:
                    logger.warning("Layout text extraction failed for doc %s page %d", document_id, page_num)

                # Extract tables
                tables = page.extract_tables() or []
                tables_json = tables if tables else None

                # Save rendered image from pdf2image
                image_filename = f"page_{page_num}.png"
                image_path = doc_render_dir / image_filename
                page_image.save(str(image_path), format="PNG")

                # Dimensions (in points from pdfplumber)
                width = float(page.width)
                height = float(page.height)

                # Determine extraction quality
                if len(raw_text.strip()) >= MIN_TEXT_CHARS:
                    quality = ExtractionQuality.full_text
                elif raw_text.strip():
                    quality = ExtractionQuality.partial_text
                else:
                    quality = ExtractionQuality.image_only

                # Classify page type
                page_type = _classify_page(raw_text)

                relative_image_path = str(Path(str(document_id)) / image_filename)

                doc_page = DocumentPage(
                    document_id=document_id,
                    page_number=page_num,
                    raw_text=raw_text,
                    layout_text=layout_text,
                    tables_json=tables_json,
                    image_path=relative_image_path,
                    width=width,
                    height=height,
                    extraction_quality=quality,
                    page_type=page_type,
                )
                db.add(doc_page)
                all_text_parts.append(raw_text)

        # Detect format type and extract tag from content
        combined_text = "\n".join(all_text_parts)
        format_type = detect_format_type(combined_text) if combined_text.strip() else "unknown"

        if pump_tag is None and combined_text:
            pump_tag = extract_tag_from_content(combined_text)

        # Update document record
        doc.num_pages = num_pages
        doc.pump_tag = pump_tag
        doc.format_type = format_type
        doc.status = DocumentStatus.uploaded

        await db.flush()

        logger.info(
            "Processed document %s: %d pages, tag=%s, format=%s",
            document_id, num_pages, pump_tag, format_type,
        )
        return doc

    except Exception:
        doc.status = DocumentStatus.failed
        await db.flush()
        logger.exception("Failed to process document %s", document_id)
        raise
