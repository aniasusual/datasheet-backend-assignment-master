"""PDF document processor.

Handles the ingestion step: PDF → text extraction per page.
No rendering, no LLM — purely deterministic processing.
The frontend renders PDFs natively; the LLM receives raw PDF pages.

Classification (format type, page type, pump tag) is done by the
extraction pipeline's field discovery pass — not hardcoded here.
"""

import logging
import uuid
from pathlib import Path

import pdfplumber
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document, DocumentStatus
from app.models.document_page import DocumentPage, ExtractionQuality

logger = logging.getLogger(__name__)

MIN_TEXT_CHARS = 20


# ---------------------------------------------------------------------------
# File type detection (magic bytes — the only validation we do here)
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
# Main entry point
# ---------------------------------------------------------------------------

async def process_document(
    file_path: str | Path,
    document_id: uuid.UUID,
    db: AsyncSession,
) -> Document:
    """Process a PDF: extract text, tables, and dimensions per page.

    This is deterministic CPU work — no LLM, no hardcoded classification.
    Format type, pump tag, and page classification are handled downstream
    by the extraction pipeline (which uses the LLM).
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    doc = await db.get(Document, document_id)
    if doc is None:
        raise ValueError(f"Document {document_id} not found in database")

    try:
        all_text_parts: list[str] = []

        with pdfplumber.open(str(file_path)) as pdf:
            num_pages = len(pdf.pages)

            for page_num, page in enumerate(pdf.pages, start=1):
                raw_text = page.extract_text() or ""

                layout_text: str | None = None
                try:
                    layout_text = page.extract_text(layout=True) or None
                except Exception:
                    logger.warning("Layout text extraction failed for doc %s page %d", document_id, page_num)

                tables = page.extract_tables() or []
                tables_json = tables if tables else None

                width = float(page.width)
                height = float(page.height)

                # Text quality — how much text pdfplumber could extract
                if len(raw_text.strip()) >= MIN_TEXT_CHARS:
                    quality = ExtractionQuality.full_text
                elif raw_text.strip():
                    quality = ExtractionQuality.partial_text
                else:
                    quality = ExtractionQuality.image_only

                doc_page = DocumentPage(
                    document_id=document_id,
                    page_number=page_num,
                    raw_text=raw_text,
                    layout_text=layout_text,
                    tables_json=tables_json,
                    width=width,
                    height=height,
                    extraction_quality=quality,
                )
                db.add(doc_page)
                all_text_parts.append(raw_text)

        doc.num_pages = num_pages
        doc.status = DocumentStatus.uploaded

        await db.flush()

        logger.info("Processed document %s: %d pages", document_id, num_pages)
        return doc

    except Exception:
        doc.status = DocumentStatus.failed
        await db.flush()
        logger.exception("Failed to process document %s", document_id)
        raise
