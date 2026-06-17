"""Read-only entity endpoints: list and detail."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.equipment_entity import EquipmentEntity
from app.models.extracted_field import ExtractedField
from app.models.session import Session

router = APIRouter(prefix="/sessions/{session_id}/entities", tags=["entities"])


@router.get("")
async def list_entities(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """List all equipment entities in session with linked counts."""
    session = await db.get(Session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    stmt = (
        select(
            EquipmentEntity,
            func.count(ExtractedField.id.distinct()).label("field_count"),
        )
        .outerjoin(ExtractedField, ExtractedField.entity_id == EquipmentEntity.id)
        .where(EquipmentEntity.session_id == session_id)
        .group_by(EquipmentEntity.id)
        .order_by(EquipmentEntity.tag)
    )
    result = await db.execute(stmt)
    rows = result.all()

    entities = []
    for entity, field_count in rows:
        await db.refresh(entity, ["documents"])
        entities.append({
            "id": str(entity.id),
            "tag": entity.tag,
            "entity_type": entity.entity_type,
            "name": entity.name,
            "metadata_json": entity.metadata_json,
            "document_count": len(entity.documents),
            "field_count": field_count,
            "created_at": entity.created_at.isoformat() if entity.created_at else None,
        })

    return {"entities": entities}


@router.get("/{entity_id}")
async def get_entity(
    session_id: uuid.UUID,
    entity_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Entity detail: linked documents and fields."""
    stmt = (
        select(EquipmentEntity)
        .options(
            selectinload(EquipmentEntity.documents),
            selectinload(EquipmentEntity.fields),
        )
        .where(EquipmentEntity.session_id == session_id, EquipmentEntity.id == entity_id)
    )
    result = await db.execute(stmt)
    entity = result.scalar_one_or_none()

    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")

    return {
        "id": str(entity.id),
        "tag": entity.tag,
        "entity_type": entity.entity_type,
        "name": entity.name,
        "metadata_json": entity.metadata_json,
        "created_at": entity.created_at.isoformat() if entity.created_at else None,
        "documents": [
            {
                "id": str(d.id),
                "filename": d.filename,
                "pump_tag": d.pump_tag,
                "status": d.status.value,
            }
            for d in entity.documents
        ],
        "fields": [
            {
                "id": str(f.id),
                "field_name": f.field_name,
                "display_name": f.display_name,
                "raw_value": f.raw_value,
                "unit": f.unit,
                "section": f.section,
                "confidence": f.confidence,
                "status": f.status.value,
            }
            for f in entity.fields
        ],
    }
