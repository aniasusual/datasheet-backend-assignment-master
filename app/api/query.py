"""Query and Agent endpoints."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.session import Session

router = APIRouter(prefix="/sessions/{session_id}", tags=["query"])


class QueryRequest(BaseModel):
    question: str


class AgentRequest(BaseModel):
    messages: list[dict] = []
    message: str


@router.post("/query")
async def query_extracted_data(
    session_id: uuid.UUID,
    body: QueryRequest,
    db: AsyncSession = Depends(get_db),
):
    """Stateless single-shot query over extracted fields."""
    session = await db.get(Session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    from app.services.query import query_fields

    result = await query_fields(session_id, body.question, db)
    return result


@router.post("/agent")
async def agent_chat(
    session_id: uuid.UUID,
    body: AgentRequest,
    db: AsyncSession = Depends(get_db),
):
    """Conversational agent with tool use for querying and editing fields.

    Frontend sends conversation history + new message.
    Agent can search, update, verify, and reject fields.
    Returns response + updated messages + list of tool actions taken.
    """
    session = await db.get(Session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    from app.services.agent import run_agent

    result = await run_agent(session_id, body.messages, body.message, db)
    return result
