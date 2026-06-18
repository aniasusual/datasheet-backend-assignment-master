"""Chat message model — stores the full agent conversation trajectory per session."""

import enum
import uuid

from sqlalchemy import Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, pk_uuid


class ChatRole(str, enum.Enum):
    user = "user"
    assistant = "assistant"


class ChatMessage(Base, TimestampMixin):
    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = pk_uuid()
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[ChatRole] = mapped_column(Enum(ChatRole), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_actions: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    session = relationship("Session", back_populates="chat_messages")
