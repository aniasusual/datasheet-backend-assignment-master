from sqlalchemy import Column, ForeignKey, Table
from sqlalchemy.dialects.postgresql import UUID

from app.models.base import Base

entity_documents = Table(
    "entity_documents",
    Base.metadata,
    Column("entity_id", UUID(as_uuid=True), ForeignKey("equipment_entities.id"), primary_key=True),
    Column("document_id", UUID(as_uuid=True), ForeignKey("documents.id"), primary_key=True),
)
