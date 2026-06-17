import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.document import Document
from app.models.extracted_field import ExtractedField
from app.models.session import Session, SessionStatus
from app.schemas.sessions import (
    SessionCreateRequest,
    SessionDetailResponse,
    SessionListResponse,
    SessionResponse,
)

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", response_model=SessionResponse, status_code=201)
async def create_session(
    body: SessionCreateRequest | None = None,
    db: AsyncSession = Depends(get_db),
):
    session = Session(
        status=SessionStatus.active,
        title=body.title if body else None,
    )
    db.add(session)
    await db.flush()
    await db.refresh(session)
    return session


@router.get("", response_model=list[SessionListResponse])
async def list_sessions(db: AsyncSession = Depends(get_db)):
    stmt = (
        select(
            Session.id,
            Session.status,
            Session.title,
            Session.created_at,
            func.count(Document.id).label("document_count"),
        )
        .outerjoin(Document, Document.session_id == Session.id)
        .group_by(Session.id)
        .order_by(Session.created_at.desc())
    )
    result = await db.execute(stmt)
    rows = result.all()
    return [
        SessionListResponse(
            id=row.id,
            status=row.status,
            title=row.title,
            created_at=row.created_at,
            document_count=row.document_count,
        )
        for row in rows
    ]


@router.delete("/{session_id}", status_code=204)
async def delete_session(session_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    session = await db.get(Session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    await db.delete(session)
    await db.flush()


@router.get("/{session_id}", response_model=SessionDetailResponse)
async def get_session(session_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    session = await db.get(Session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    doc_count_stmt = select(func.count(Document.id)).where(Document.session_id == session_id)
    field_count_stmt = (
        select(func.count(ExtractedField.id))
        .join(Document, ExtractedField.document_id == Document.id)
        .where(Document.session_id == session_id)
    )

    doc_count = (await db.execute(doc_count_stmt)).scalar() or 0
    field_count = (await db.execute(field_count_stmt)).scalar() or 0

    return SessionDetailResponse(
        id=session.id,
        status=session.status,
        title=session.title,
        created_at=session.created_at,
        updated_at=session.updated_at,
        document_count=doc_count,
        field_count=field_count,
    )
