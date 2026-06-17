import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, pk_uuid

if TYPE_CHECKING:
    from app.models.extracted_field import ExtractedField


class FieldCorrection(Base, TimestampMixin):
    __tablename__ = "field_corrections"

    id: Mapped[uuid.UUID] = pk_uuid()
    field_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("extracted_fields.id"), nullable=False)
    original_value: Mapped[str] = mapped_column(Text, nullable=False)
    corrected_value: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    corrected_by: Mapped[str] = mapped_column(String(100), default="user", nullable=False)

    # Relationships
    field: Mapped["ExtractedField"] = relationship("ExtractedField", back_populates="corrections")
