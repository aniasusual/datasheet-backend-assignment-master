"""Phase 2: PDF Processor and Document API tests."""

import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.main import app
from app.models.document import Document, DocumentStatus
from app.models.document_page import DocumentPage
from app.models.session import Session, SessionStatus
from app.services.pdf_processor import (
    detect_format_type,
    extract_tag_from_content,
    extract_tag_from_filename,
    process_document,
)

# Sample PDFs in project root
PROJECT_ROOT = Path(__file__).parent.parent
SAMPLE_PDFS = {
    "P718": PROJECT_ROOT / "pds-P718.pdf",
    "P818": PROJECT_ROOT / "pds-P818.pdf",
    "P300228": PROJECT_ROOT / "pds-P300228.pdf",
    "P600173": PROJECT_ROOT / "pds-P600173.pdf",
}


# ──────────────────────────────────────────────
# Unit tests: tag extraction and format detection
# ──────────────────────────────────────────────


class TestTagExtraction:
    def test_tag_from_filename_p718(self):
        assert extract_tag_from_filename("pds-P718.pdf") == "P-718"

    def test_tag_from_filename_p818(self):
        assert extract_tag_from_filename("pds-P818.pdf") == "P-818"

    def test_tag_from_filename_p300228(self):
        assert extract_tag_from_filename("pds-P300228.pdf") == "P-300228"

    def test_tag_from_filename_p600173(self):
        assert extract_tag_from_filename("pds-P600173.pdf") == "P-600173"

    def test_tag_from_filename_no_match(self):
        assert extract_tag_from_filename("random_file.pdf") is None

    def test_tag_from_content(self):
        text = "Item No.: P718 (A/B)\nService: DIESEL PRODUCT PUMPS"
        tag = extract_tag_from_content(text)
        assert tag is not None
        assert "718" in tag

    def test_tag_from_content_with_hyphen(self):
        text = "Equipment tag P-300228 is referenced"
        tag = extract_tag_from_content(text)
        assert tag == "P-300228"


class TestFormatDetection:
    def test_french_form_detected(self):
        text = """
        CONDITIONS OPERATOIRES
        DEBIT Nominal 3.35
        PRESSION ASPIRATION 0.7
        MASSE VOL A PT 941
        VISCOSITE A PT 0.3 cP
        MATERIAUX POMPE
        GARNITURE MECANIQUE
        REMARQUES
        """
        assert detect_format_type(text) == "french_form"

    def test_english_tabular_detected(self):
        text = """
        PROCESS DATA SHEET
        Normal flowrate 928 GPM
        Discharge pressure 174.5 psig
        Suction pressure -2.5 psig
        Design temperature 500 F
        Material of construction CS / CS
        """
        assert detect_format_type(text) == "english_tabular"


# ──────────────────────────────────────────────
# Integration tests: PDF processing
# ──────────────────────────────────────────────


@pytest_asyncio.fixture
async def test_session(db: AsyncSession) -> Session:
    """Create a test session for document processing."""
    s = Session(status=SessionStatus.active, title="Phase 2 Test")
    db.add(s)
    await db.flush()
    await db.refresh(s)
    return s


async def _create_and_process_doc(
    db: AsyncSession, session_id: uuid.UUID, pdf_key: str
) -> Document:
    """Helper: create a document record and process the PDF."""
    pdf_path = SAMPLE_PDFS[pdf_key]
    doc = Document(
        session_id=session_id,
        filename=pdf_path.name,
        file_path=str(pdf_path),
        status=DocumentStatus.uploading,
    )
    db.add(doc)
    await db.flush()
    return await process_document(pdf_path, doc.id, db)


@pytest.mark.parametrize(
    "pdf_key,expected_tag,expected_format,expected_pages",
    [
        ("P718", "P-718", "english_tabular", 3),
        ("P818", "P-818", "english_tabular", 3),
        ("P300228", "P-300228", "french_form", 2),
        # P600173 is a scanned/image-only PDF — pdfplumber extracts no text,
        # so format detection falls back to english_tabular
        ("P600173", "P-600173", "english_tabular", 2),
    ],
)
async def test_process_document(
    db: AsyncSession,
    test_session: Session,
    pdf_key: str,
    expected_tag: str,
    expected_format: str,
    expected_pages: int,
):
    doc = await _create_and_process_doc(db, test_session.id, pdf_key)

    # Status should be uploaded after processing
    assert doc.status == DocumentStatus.uploaded

    # Tag and format detection
    assert doc.pump_tag == expected_tag
    assert doc.format_type == expected_format

    # Page count
    assert doc.num_pages == expected_pages

    # Verify DocumentPage records were created
    stmt = (
        select(DocumentPage)
        .where(DocumentPage.document_id == doc.id)
        .order_by(DocumentPage.page_number)
    )
    result = await db.execute(stmt)
    pages = result.scalars().all()

    assert len(pages) == expected_pages

    for i, page in enumerate(pages, start=1):
        assert page.page_number == i
        assert page.width > 0
        assert page.height > 0
        assert page.image_path  # PNG path set

        # Verify PNG was rendered
        full_path = settings.RENDERED_PAGES_DIR / page.image_path
        assert full_path.exists(), f"PNG not found: {full_path}"
        assert full_path.stat().st_size > 0

    # First page should have some text extracted (except scanned/image-only PDFs)
    if pdf_key != "P600173":
        assert len(pages[0].raw_text) > 50


async def test_text_extraction_quality_p718(
    db: AsyncSession, test_session: Session
):
    """Spot-check that key text content is extracted from P718."""
    doc = await _create_and_process_doc(db, test_session.id, "P718")

    stmt = (
        select(DocumentPage)
        .where(DocumentPage.document_id == doc.id)
        .order_by(DocumentPage.page_number)
    )
    result = await db.execute(stmt)
    pages = result.scalars().all()

    # Combine all page text
    all_text = " ".join(p.raw_text for p in pages)

    # Check for key content that should be present in P718
    assert "DIESEL PRODUCT" in all_text.upper()
    assert "PROCESS DATA SHEET" in all_text.upper() or "PDS" in all_text.upper()


async def test_text_extraction_quality_p300228(
    db: AsyncSession, test_session: Session
):
    """Spot-check that key French-form content is extracted from P300228."""
    doc = await _create_and_process_doc(db, test_session.id, "P300228")

    stmt = (
        select(DocumentPage)
        .where(DocumentPage.document_id == doc.id)
        .order_by(DocumentPage.page_number)
    )
    result = await db.execute(stmt)
    pages = result.scalars().all()

    all_text = " ".join(p.raw_text for p in pages)
    upper = all_text.upper()

    # French-form datasheets should contain these terms
    assert "POMPE CENTRIFUGE" in upper or "CENTRIFUGAL" in upper


async def test_table_extraction(db: AsyncSession, test_session: Session):
    """Verify pdfplumber detects tables in at least some pages."""
    doc = await _create_and_process_doc(db, test_session.id, "P718")

    stmt = select(DocumentPage).where(DocumentPage.document_id == doc.id)
    result = await db.execute(stmt)
    pages = result.scalars().all()

    # At least one page should have tables detected
    has_tables = any(p.tables_json and len(p.tables_json) > 0 for p in pages)
    assert has_tables, "No tables detected in P718 — expected at least one"


# ──────────────────────────────────────────────
# API endpoint tests
# ──────────────────────────────────────────────


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_create_session_api(client: AsyncClient):
    resp = await client.post("/api/v1/sessions", json={"title": "API Test"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "active"
    assert data["title"] == "API Test"
    assert "id" in data


async def test_list_sessions_api(client: AsyncClient):
    # Create a session first
    await client.post("/api/v1/sessions", json={"title": "List Test"})
    resp = await client.get("/api/v1/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1


async def test_upload_and_list_documents_api(client: AsyncClient):
    # Create session
    resp = await client.post("/api/v1/sessions", json={"title": "Upload Test"})
    session_id = resp.json()["id"]

    # Upload a PDF
    pdf_path = SAMPLE_PDFS["P718"]
    with open(pdf_path, "rb") as f:
        resp = await client.post(
            f"/api/v1/sessions/{session_id}/documents/upload",
            files=[("files", ("pds-P718.pdf", f, "application/pdf"))],
        )
    assert resp.status_code == 201
    data = resp.json()
    assert len(data["documents"]) == 1
    doc = data["documents"][0]
    assert doc["status"] == "uploaded"
    assert doc["pump_tag"] == "P-718"
    assert doc["num_pages"] == 3

    # List documents
    resp = await client.get(f"/api/v1/sessions/{session_id}/documents")
    assert resp.status_code == 200
    docs = resp.json()
    assert len(docs) == 1


async def test_get_document_detail_api(client: AsyncClient):
    # Create session + upload
    resp = await client.post("/api/v1/sessions")
    session_id = resp.json()["id"]

    pdf_path = SAMPLE_PDFS["P300228"]
    with open(pdf_path, "rb") as f:
        resp = await client.post(
            f"/api/v1/sessions/{session_id}/documents/upload",
            files=[("files", ("pds-P300228.pdf", f, "application/pdf"))],
        )
    doc_id = resp.json()["documents"][0]["id"]

    # Get detail with pages
    resp = await client.get(f"/api/v1/sessions/{session_id}/documents/{doc_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["format_type"] == "french_form"
    assert len(data["pages"]) == data["num_pages"]
    assert data["pages"][0]["page_number"] == 1
    assert len(data["pages"][0]["raw_text"]) > 0


async def test_get_page_image_api(client: AsyncClient):
    # Create session + upload
    resp = await client.post("/api/v1/sessions")
    session_id = resp.json()["id"]

    pdf_path = SAMPLE_PDFS["P718"]
    with open(pdf_path, "rb") as f:
        resp = await client.post(
            f"/api/v1/sessions/{session_id}/documents/upload",
            files=[("files", ("pds-P718.pdf", f, "application/pdf"))],
        )
    doc_id = resp.json()["documents"][0]["id"]

    # Fetch page 1 image
    resp = await client.get(
        f"/api/v1/sessions/{session_id}/documents/{doc_id}/pages/1/image"
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert len(resp.content) > 1000  # PNG should be non-trivial


async def test_upload_non_pdf_rejected(client: AsyncClient):
    resp = await client.post("/api/v1/sessions")
    session_id = resp.json()["id"]

    resp = await client.post(
        f"/api/v1/sessions/{session_id}/documents/upload",
        files=[("files", ("test.txt", b"not a pdf", "text/plain"))],
    )
    assert resp.status_code == 400


async def test_upload_multiple_pdfs(client: AsyncClient):
    resp = await client.post("/api/v1/sessions")
    session_id = resp.json()["id"]

    files = []
    for key in ["P718", "P818"]:
        pdf_path = SAMPLE_PDFS[key]
        files.append(("files", (pdf_path.name, open(pdf_path, "rb"), "application/pdf")))

    try:
        resp = await client.post(
            f"/api/v1/sessions/{session_id}/documents/upload",
            files=files,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert len(data["documents"]) == 2
        tags = {d["pump_tag"] for d in data["documents"]}
        assert "P-718" in tags
        assert "P-818" in tags
    finally:
        for _, (_, fobj, _) in files:
            fobj.close()
