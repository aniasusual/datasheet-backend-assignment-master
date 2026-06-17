"""Field endpoints: list, detail, statistics, and HITL corrections."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.document import Document
from app.models.extracted_field import ExtractedField, FieldStatus
from app.models.field_correction import FieldCorrection
from app.models.session import Session

router = APIRouter(prefix="/sessions/{session_id}/fields", tags=["fields"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class FieldUpdateRequest(BaseModel):
    raw_value: str | None = None
    unit: str | None = None
    section: str | None = None
    status: FieldStatus | None = None
    reason: str | None = None


class BulkVerifyRequest(BaseModel):
    field_ids: list[uuid.UUID]


# ---------------------------------------------------------------------------
# List / detail / stats (read-only)
# ---------------------------------------------------------------------------

@router.get("")
async def list_fields(
    session_id: uuid.UUID,
    document_id: uuid.UUID | None = Query(None),
    section: str | None = Query(None),
    status: FieldStatus | None = Query(None),
    min_confidence: float | None = Query(None, ge=0.0, le=1.0),
    field_name: str | None = Query(None),
    page: int | None = Query(None, ge=1),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """List extracted fields with optional filters and pagination."""
    session = await db.get(Session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    stmt = (
        select(ExtractedField)
        .join(Document, ExtractedField.document_id == Document.id)
        .where(Document.session_id == session_id)
    )

    if document_id:
        stmt = stmt.where(ExtractedField.document_id == document_id)
    if section:
        stmt = stmt.where(ExtractedField.section == section)
    if status:
        stmt = stmt.where(ExtractedField.status == status)
    if min_confidence is not None:
        stmt = stmt.where(ExtractedField.confidence >= min_confidence)
    if field_name:
        stmt = stmt.where(ExtractedField.field_name.ilike(f"%{field_name}%"))
    if page is not None:
        stmt = stmt.where(ExtractedField.citation_page == page)

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.execute(count_stmt)).scalar() or 0

    stmt = stmt.order_by(ExtractedField.section, ExtractedField.field_name).offset(offset).limit(limit)
    result = await db.execute(stmt)
    fields = result.scalars().all()

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "fields": [_field_to_dict(f) for f in fields],
    }


@router.get("/stats")
async def field_stats(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Extraction statistics: totals by section, status, confidence tier, and per-document."""
    session = await db.get(Session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    base = (
        select(ExtractedField)
        .join(Document, ExtractedField.document_id == Document.id)
        .where(Document.session_id == session_id)
    )

    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0

    by_section_stmt = (
        select(ExtractedField.section, func.count().label("count"))
        .join(Document, ExtractedField.document_id == Document.id)
        .where(Document.session_id == session_id)
        .group_by(ExtractedField.section)
        .order_by(ExtractedField.section)
    )
    by_section = {row.section: row.count for row in (await db.execute(by_section_stmt)).all()}

    by_status_stmt = (
        select(ExtractedField.status, func.count().label("count"))
        .join(Document, ExtractedField.document_id == Document.id)
        .where(Document.session_id == session_id)
        .group_by(ExtractedField.status)
    )
    by_status = {row.status.value: row.count for row in (await db.execute(by_status_stmt)).all()}

    confidence_tier = case(
        (ExtractedField.confidence >= 0.8, "high"),
        (ExtractedField.confidence >= 0.5, "medium"),
        else_="low",
    )
    by_confidence_stmt = (
        select(confidence_tier.label("tier"), func.count().label("count"))
        .join(Document, ExtractedField.document_id == Document.id)
        .where(Document.session_id == session_id)
        .group_by(confidence_tier)
    )
    by_confidence = {row.tier: row.count for row in (await db.execute(by_confidence_stmt)).all()}

    per_doc_stmt = (
        select(
            Document.id,
            Document.filename,
            Document.pump_tag,
            func.count(ExtractedField.id).label("field_count"),
        )
        .join(ExtractedField, ExtractedField.document_id == Document.id)
        .where(Document.session_id == session_id)
        .group_by(Document.id, Document.filename, Document.pump_tag)
        .order_by(Document.filename)
    )
    per_document = [
        {
            "document_id": str(row.id),
            "filename": row.filename,
            "pump_tag": row.pump_tag,
            "field_count": row.field_count,
        }
        for row in (await db.execute(per_doc_stmt)).all()
    ]

    return {
        "total_fields": total,
        "by_section": by_section,
        "by_status": by_status,
        "by_confidence_tier": by_confidence,
        "per_document": per_document,
    }


@router.get("/{field_id}")
async def get_field(
    session_id: uuid.UUID,
    field_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Single field detail with correction history."""
    field = await _get_field_or_404(session_id, field_id, db)

    result = _field_to_dict(field)
    result["corrections"] = [
        {
            "id": str(c.id),
            "original_value": c.original_value,
            "corrected_value": c.corrected_value,
            "reason": c.reason,
            "corrected_by": c.corrected_by,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in field.corrections
    ]
    return result


# ---------------------------------------------------------------------------
# HITL: update / verify / reject fields
# ---------------------------------------------------------------------------

@router.patch("/{field_id}")
async def update_field(
    session_id: uuid.UUID,
    field_id: uuid.UUID,
    body: FieldUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    """Update a field's value, unit, section, or status. Creates an audit trail."""
    field = await _get_field_or_404(session_id, field_id, db)

    # Track what changed for the correction record
    original_value = field.raw_value
    changes_made = False

    if body.raw_value is not None and body.raw_value != field.raw_value:
        field.raw_value = body.raw_value
        changes_made = True

    if body.unit is not None and body.unit != field.unit:
        field.unit = body.unit if body.unit != "" else None
        changes_made = True

    if body.section is not None and body.section != field.section:
        field.section = body.section
        changes_made = True

    if body.status is not None:
        field.status = body.status

    # If value or unit changed, create a correction record
    if changes_made:
        if field.status == FieldStatus.extracted:
            field.status = FieldStatus.corrected

        correction = FieldCorrection(
            field_id=field.id,
            original_value=original_value,
            corrected_value=field.raw_value,
            reason=body.reason,
            corrected_by="user",
        )
        db.add(correction)

    await db.flush()
    await db.refresh(field)

    return _field_to_dict(field)


@router.post("/bulk-verify")
async def bulk_verify(
    session_id: uuid.UUID,
    body: BulkVerifyRequest,
    db: AsyncSession = Depends(get_db),
):
    """Mark multiple fields as verified at once."""
    session = await db.get(Session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    stmt = (
        select(ExtractedField)
        .join(Document, ExtractedField.document_id == Document.id)
        .where(
            Document.session_id == session_id,
            ExtractedField.id.in_(body.field_ids),
        )
    )
    result = await db.execute(stmt)
    fields = result.scalars().all()

    updated = 0
    for f in fields:
        if f.status == FieldStatus.extracted:
            f.status = FieldStatus.verified
            updated += 1

    await db.flush()

    return {"verified": updated, "total_requested": len(body.field_ids)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_field_or_404(
    session_id: uuid.UUID,
    field_id: uuid.UUID,
    db: AsyncSession,
) -> ExtractedField:
    stmt = (
        select(ExtractedField)
        .options(selectinload(ExtractedField.corrections))
        .join(Document, ExtractedField.document_id == Document.id)
        .where(Document.session_id == session_id, ExtractedField.id == field_id)
    )
    result = await db.execute(stmt)
    field = result.scalar_one_or_none()
    if field is None:
        raise HTTPException(status_code=404, detail="Field not found")
    return field


def _field_to_dict(f: ExtractedField) -> dict:
    return {
        "id": str(f.id),
        "document_id": str(f.document_id),
        "entity_id": str(f.entity_id) if f.entity_id else None,
        "field_name": f.field_name,
        "display_name": f.display_name,
        "raw_value": f.raw_value,
        "unit": f.unit,
        "data_type": f.data_type.value,
        "section": f.section,
        "confidence": f.confidence,
        "status": f.status.value,
        "citation_page": f.citation_page,
        "citation_bbox": f.citation_bbox,
        "citation_text": f.citation_text,
        "created_at": f.created_at.isoformat() if f.created_at else None,
        "updated_at": f.updated_at.isoformat() if f.updated_at else None,
    }
