import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Enum, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, FullTimestampMixin, pk_uuid

if TYPE_CHECKING:
    from app.models.chat_message import ChatMessage
    from app.models.document import Document
    from app.models.equipment_entity import EquipmentEntity


class SessionStatus(str, enum.Enum):
    active = "active"
    archived = "archived"


class Session(Base, FullTimestampMixin):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = pk_uuid()
    status: Mapped[SessionStatus] = mapped_column(Enum(SessionStatus), default=SessionStatus.active, nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Relationships
    documents: Mapped[list["Document"]] = relationship(
        "Document", back_populates="session", cascade="all, delete-orphan"
    )
    entities: Mapped[list["EquipmentEntity"]] = relationship(
        "EquipmentEntity", back_populates="session", cascade="all, delete-orphan"
    )
    chat_messages: Mapped[list["ChatMessage"]] = relationship(
        "ChatMessage", back_populates="session", cascade="all, delete-orphan",
        order_by="ChatMessage.sequence",
    )
