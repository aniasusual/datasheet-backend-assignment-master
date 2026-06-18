"""Query and Agent endpoints."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.chat_message import ChatMessage, ChatRole
from app.models.session import Session

router = APIRouter(prefix="/sessions/{session_id}", tags=["query"])


class QueryRequest(BaseModel):
    question: str


class AgentRequest(BaseModel):
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


@router.get("/chat")
async def get_chat_history(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Load the full agent chat history for a session."""
    session = await db.get(Session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    stmt = (
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.sequence)
    )
    result = await db.execute(stmt)
    messages = result.scalars().all()

    return {
        "messages": [
            {
                "id": str(m.id),
                "role": m.role.value,
                "content": m.content,
                "tool_actions": m.tool_actions,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in messages
        ],
    }


@router.delete("/chat")
async def clear_chat_history(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Clear agent chat history for a session."""
    session = await db.get(Session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    stmt = select(ChatMessage).where(ChatMessage.session_id == session_id)
    result = await db.execute(stmt)
    for msg in result.scalars().all():
        await db.delete(msg)

    return {"status": "cleared"}


@router.post("/agent")
async def agent_chat(
    session_id: uuid.UUID,
    body: AgentRequest,
    db: AsyncSession = Depends(get_db),
):
    """Conversational agent with tool use.

    Loads conversation history from DB, appends the new user message,
    runs the agent, persists both user + assistant messages, returns response.
    """
    session = await db.get(Session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    # Load existing history from DB
    stmt = (
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.sequence)
    )
    result = await db.execute(stmt)
    db_messages = result.scalars().all()

    history = [{"role": m.role.value, "content": m.content} for m in db_messages]

    # Get next sequence number
    next_seq = (db_messages[-1].sequence + 1) if db_messages else 0

    # Save user message to DB
    user_msg = ChatMessage(
        session_id=session_id,
        role=ChatRole.user,
        content=body.message,
        sequence=next_seq,
    )
    db.add(user_msg)
    await db.flush()

    # Run agent
    from app.services.agent import run_agent

    agent_result = await run_agent(session_id, history, body.message, db)

    # Save assistant message to DB
    assistant_msg = ChatMessage(
        session_id=session_id,
        role=ChatRole.assistant,
        content=agent_result["response"],
        tool_actions=agent_result["tool_actions"] if agent_result["tool_actions"] else None,
        sequence=next_seq + 1,
    )
    db.add(assistant_msg)

    return {
        "response": agent_result["response"],
        "tool_actions": agent_result["tool_actions"],
        "message_id": str(assistant_msg.id),
    }
