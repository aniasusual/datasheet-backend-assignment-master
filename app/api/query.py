"""Query endpoint: answer natural language questions over extracted fields."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.session import Session

router = APIRouter(prefix="/sessions/{session_id}", tags=["query"])


class QueryRequest(BaseModel):
    question: str


@router.post("/query")
async def query_extracted_data(
    session_id: uuid.UUID,
    body: QueryRequest,
    db: AsyncSession = Depends(get_db),
):
    """Answer a natural language question using extracted fields from all documents in the session."""
    session = await db.get(Session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    from app.services.query import query_fields

    result = await query_fields(session_id, body.question, db)
    return result
