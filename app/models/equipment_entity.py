import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, pk_uuid

if TYPE_CHECKING:
    from app.models.document import Document
    from app.models.extracted_field import ExtractedField
    from app.models.session import Session


class EquipmentEntity(Base, TimestampMixin):
    __tablename__ = "equipment_entities"
    __table_args__ = (Index("ix_entities_session_tag", "session_id", "tag"),)

    id: Mapped[uuid.UUID] = pk_uuid()
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=False)
    tag: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Relationships
    session: Mapped["Session"] = relationship("Session", back_populates="entities")
    fields: Mapped[list["ExtractedField"]] = relationship("ExtractedField", back_populates="entity")
    documents: Mapped[list["Document"]] = relationship(
        "Document", secondary="entity_documents", backref="entities"
    )
