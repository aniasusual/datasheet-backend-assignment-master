import uuid
from datetime import datetime

from pydantic import BaseModel

from app.models.session import SessionStatus


class SessionCreateRequest(BaseModel):
    title: str | None = None


class SessionResponse(BaseModel):
    id: uuid.UUID
    status: SessionStatus
    title: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SessionDetailResponse(BaseModel):
    id: uuid.UUID
    status: SessionStatus
    title: str | None
    created_at: datetime
    updated_at: datetime
    document_count: int
    field_count: int

    model_config = {"from_attributes": True}


class SessionListResponse(BaseModel):
    id: uuid.UUID
    status: SessionStatus
    title: str | None
    created_at: datetime
    document_count: int

    model_config = {"from_attributes": True}
