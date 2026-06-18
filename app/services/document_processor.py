"""PDF document processor.

Handles ingestion: save file, count pages, record dimensions.
No text extraction — the LLM reads the PDF natively.
"""

import logging
import uuid
from pathlib import Path

import fitz  # PyMuPDF
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document, DocumentStatus
from app.models.document_page import DocumentPage, ExtractionQuality

logger = logging.getLogger(__name__)


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


async def process_document(
    file_path: str | Path,
    document_id: uuid.UUID,
    db: AsyncSession,
) -> Document:
    """Process a PDF: count pages and record dimensions.

    No text extraction — Gemini reads the PDF natively.
    DocumentPage records are created for backward compatibility.
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    doc = await db.get(Document, document_id)
    if doc is None:
        raise ValueError(f"Document {document_id} not found in database")

    try:
        pdf = fitz.open(str(file_path))
        num_pages = pdf.page_count

        for page_num in range(num_pages):
            page = pdf[page_num]
            rect = page.rect

            doc_page = DocumentPage(
                document_id=document_id,
                page_number=page_num + 1,
                raw_text="",
                layout_text=None,
                tables_json=None,
                width=float(rect.width),
                height=float(rect.height),
                extraction_quality=ExtractionQuality.image_only,
            )
            db.add(doc_page)

        pdf.close()

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
