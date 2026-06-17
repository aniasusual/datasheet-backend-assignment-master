import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.extracted_field import ExtractedField, FieldStatus
from app.models.field_correction import FieldCorrection

from app.config import settings
from app.database import get_db
from app.models.document import Document, DocumentStatus
from app.models.document_page import DocumentPage
from app.models.session import Session
from app.schemas.documents import (
    DocumentDetailResponse,
    DocumentPageResponse,
    DocumentResponse,
    DocumentUploadResponse,
)
from app.services.document_processor import process_document, detect_file_type

router = APIRouter(prefix="/sessions/{session_id}/documents", tags=["documents"])


def _validate_file(filename: str | None) -> str:
    """Validate uploaded file has an accepted extension."""
    if not filename:
        raise HTTPException(status_code=400, detail="File has no name")
    ext = Path(filename).suffix.lower()
    if ext not in settings.ACCEPTED_EXTENSIONS:
        accepted = ", ".join(sorted(settings.ACCEPTED_EXTENSIONS))
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {ext}. Accepted: {accepted}",
        )
    return filename


@router.post("/upload", response_model=DocumentUploadResponse, status_code=201)
async def upload_documents(
    session_id: uuid.UUID,
    files: list[UploadFile],
    db: AsyncSession = Depends(get_db),
):
    session = await db.get(Session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    for f in files:
        _validate_file(f.filename)

    session_upload_dir = settings.UPLOAD_DIR / str(session_id)
    session_upload_dir.mkdir(parents=True, exist_ok=True)

    created_docs: list[Document] = []

    for f in files:
        file_path = session_upload_dir / f.filename
        content = await f.read()
        file_path.write_bytes(content)

        detected = detect_file_type(file_path)
        if detected == "unknown":
            file_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=400,
                detail=f"File '{f.filename}' has an unrecognized format",
            )

        doc = Document(
            session_id=session_id,
            filename=f.filename,
            file_path=str(file_path),
            status=DocumentStatus.uploading,
        )
        db.add(doc)
        await db.flush()

        doc = await process_document(file_path, doc.id, db)
        created_docs.append(doc)

    return DocumentUploadResponse(
        documents=[DocumentResponse.model_validate(doc) for doc in created_docs],
        message=f"Successfully processed {len(created_docs)} document(s)",
    )


@router.post("/{document_id}/extract")
async def extract_document_fields(
    session_id: uuid.UUID,
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Trigger field extraction for a document. Runs synchronously."""
    doc = await db.get(Document, document_id)
    if doc is None or doc.session_id != session_id:
        raise HTTPException(status_code=404, detail="Document not found")

    if doc.status not in (DocumentStatus.uploaded, DocumentStatus.failed):
        raise HTTPException(
            status_code=400,
            detail=f"Document is in status '{doc.status}'. Must be 'uploaded' to extract.",
        )

    from app.services.extraction import extract_document
    from app.services.post_processing import post_process_document

    fields = await extract_document(document_id, db)
    entity = await post_process_document(document_id, session_id, db)

    return {
        "status": "completed",
        "document_id": str(document_id),
        "fields_extracted": len(fields),
        "entity": {
            "id": str(entity.id),
            "tag": entity.tag,
            "name": entity.name,
        } if entity else None,
    }


@router.post("/{document_id}/re-extract")
async def re_extract_document(
    session_id: uuid.UUID,
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Re-extract a document with HITL corrections injected into the prompt.

    1. Loads all past corrections for this document
    2. Deletes existing extracted fields (corrections are preserved)
    3. Re-runs extraction with corrections as context
    4. Re-runs post-processing
    """
    doc = await db.get(Document, document_id)
    if doc is None or doc.session_id != session_id:
        raise HTTPException(status_code=404, detail="Document not found")

    if doc.status != DocumentStatus.extracted:
        raise HTTPException(
            status_code=400,
            detail=f"Document must be in 'extracted' status to re-extract. Current: '{doc.status}'",
        )

    from app.services.extraction import extract_document, _build_corrections_context

    # 1. Gather corrections before deleting fields
    corrections_data = []
    fields_stmt = (
        select(ExtractedField)
        .options(selectinload(ExtractedField.corrections))
        .where(ExtractedField.document_id == document_id)
    )
    fields_result = await db.execute(fields_stmt)
    old_fields = fields_result.scalars().all()

    for f in old_fields:
        for c in f.corrections:
            corrections_data.append({
                "field_name": f.field_name,
                "original_value": c.original_value,
                "corrected_value": c.corrected_value,
                "unit": f.unit,
                "reason": c.reason,
                "status": f.status.value,
            })
        # Also capture rejected fields (even without corrections)
        if f.status == FieldStatus.rejected and not f.corrections:
            corrections_data.append({
                "field_name": f.field_name,
                "original_value": f.raw_value,
                "corrected_value": f.raw_value,
                "unit": f.unit,
                "reason": "Field was rejected by reviewer",
                "status": "rejected",
            })

    corrections_context = _build_corrections_context(corrections_data)

    # 2. Delete old fields (cascade deletes corrections too)
    for f in old_fields:
        await db.delete(f)
    await db.flush()

    # 3. Reset document status so extract_document accepts it
    doc.status = DocumentStatus.uploaded
    await db.flush()

    # 4. Re-extract with corrections context
    from app.services.post_processing import post_process_document

    fields = await extract_document(document_id, db, corrections_context=corrections_context)
    entity = await post_process_document(document_id, session_id, db)

    return {
        "status": "completed",
        "document_id": str(document_id),
        "fields_extracted": len(fields),
        "corrections_applied": len(corrections_data),
        "entity": {
            "id": str(entity.id),
            "tag": entity.tag,
            "name": entity.name,
        } if entity else None,
    }


@router.post("/extract-all")
async def extract_all_documents(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Trigger extraction for all uploaded documents in a session."""
    session = await db.get(Session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    stmt = (
        select(Document)
        .where(
            Document.session_id == session_id,
            Document.status.in_([DocumentStatus.uploaded, DocumentStatus.failed]),
        )
        .order_by(Document.created_at)
    )
    result = await db.execute(stmt)
    docs = result.scalars().all()

    if not docs:
        raise HTTPException(status_code=400, detail="No documents ready for extraction")

    from app.services.extraction import extract_document
    from app.services.post_processing import post_process_document

    results = []
    for doc in docs:
        fields = await extract_document(doc.id, db)
        entity = await post_process_document(doc.id, session_id, db)
        results.append({
            "document_id": str(doc.id),
            "filename": doc.filename,
            "fields_extracted": len(fields),
            "entity": {
                "id": str(entity.id),
                "tag": entity.tag,
                "name": entity.name,
            } if entity else None,
        })

    return {
        "status": "completed",
        "documents_processed": len(results),
        "results": results,
    }


@router.get("", response_model=list[DocumentResponse])
async def list_documents(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    session = await db.get(Session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    stmt = (
        select(Document)
        .where(Document.session_id == session_id)
        .order_by(Document.created_at)
    )
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/{document_id}", response_model=DocumentDetailResponse)
async def get_document(
    session_id: uuid.UUID,
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(Document)
        .options(selectinload(Document.pages))
        .where(Document.id == document_id, Document.session_id == session_id)
    )
    result = await db.execute(stmt)
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@router.get("/{document_id}/pages/{page_num}/image")
async def get_page_image(
    session_id: uuid.UUID,
    document_id: uuid.UUID,
    page_num: int,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(DocumentPage).where(
        DocumentPage.document_id == document_id,
        DocumentPage.page_number == page_num,
    )
    result = await db.execute(stmt)
    page = result.scalar_one_or_none()
    if page is None:
        raise HTTPException(status_code=404, detail="Page not found")

    full_path = settings.RENDERED_PAGES_DIR / page.image_path
    if not full_path.exists():
        raise HTTPException(status_code=404, detail="Page image file not found")

    return FileResponse(str(full_path), media_type="image/png")
