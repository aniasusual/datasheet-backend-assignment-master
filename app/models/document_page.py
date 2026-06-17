import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Enum, Float, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, pk_uuid

if TYPE_CHECKING:
    from app.models.document import Document


class ExtractionQuality(str, enum.Enum):
    """How the text was obtained for this page."""
    full_text = "full_text"
    partial_text = "partial_text"
    image_only = "image_only"


class DocumentPage(Base):
    __tablename__ = "document_pages"

    id: Mapped[uuid.UUID] = pk_uuid()
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    layout_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    tables_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    width: Mapped[float] = mapped_column(Float, nullable=False)
    height: Mapped[float] = mapped_column(Float, nullable=False)
    extraction_quality: Mapped[ExtractionQuality] = mapped_column(
        Enum(ExtractionQuality), default=ExtractionQuality.full_text, nullable=False,
        server_default="full_text",
    )

    # Relationships
    document: Mapped["Document"] = relationship("Document", back_populates="pages")
