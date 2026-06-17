import uuid
from datetime import datetime

from pydantic import BaseModel

from app.models.document import DocumentStatus


class DocumentResponse(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID
    filename: str
    file_path: str
    pump_tag: str | None
    format_type: str | None
    status: DocumentStatus
    num_pages: int
    created_at: datetime

    model_config = {"from_attributes": True}


class DocumentDetailResponse(DocumentResponse):
    pages: list["DocumentPageResponse"]


class DocumentPageResponse(BaseModel):
    id: uuid.UUID
    page_number: int
    raw_text: str
    layout_text: str | None
    tables_json: list | None
    width: float
    height: float
    extraction_quality: str

    model_config = {"from_attributes": True}


class DocumentUploadResponse(BaseModel):
    documents: list[DocumentResponse]
    message: str
