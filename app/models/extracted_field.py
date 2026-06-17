import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Enum, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, FullTimestampMixin, pk_uuid

if TYPE_CHECKING:
    from app.models.document import Document
    from app.models.equipment_entity import EquipmentEntity
    from app.models.field_correction import FieldCorrection


class FieldDataType(str, enum.Enum):
    numeric = "numeric"
    text = "text"
    boolean = "boolean"
    reference = "reference"


class FieldStatus(str, enum.Enum):
    extracted = "extracted"
    verified = "verified"
    corrected = "corrected"
    rejected = "rejected"


class ExtractedField(Base, FullTimestampMixin):
    __tablename__ = "extracted_fields"
    __table_args__ = (
        Index("ix_fields_doc_section", "document_id", "section"),
        Index("ix_fields_doc_name", "document_id", "field_name"),
    )

    id: Mapped[uuid.UUID] = pk_uuid()
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("equipment_entities.id"), nullable=True
    )
    field_name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    raw_value: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    data_type: Mapped[FieldDataType] = mapped_column(Enum(FieldDataType), default=FieldDataType.text, nullable=False)
    section: Mapped[str] = mapped_column(String(100), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[FieldStatus] = mapped_column(Enum(FieldStatus), default=FieldStatus.extracted, nullable=False)

    # Citation
    citation_page: Mapped[int] = mapped_column(Integer, nullable=False)
    citation_bbox: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    citation_text: Mapped[str] = mapped_column(Text, nullable=False)

    # Relationships
    document: Mapped["Document"] = relationship("Document", back_populates="fields")
    entity: Mapped["EquipmentEntity | None"] = relationship("EquipmentEntity", back_populates="fields")
    corrections: Mapped[list["FieldCorrection"]] = relationship(
        "FieldCorrection", back_populates="field", cascade="all, delete-orphan"
    )
