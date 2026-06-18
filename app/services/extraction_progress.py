"""In-memory extraction progress store.

Tracks per-session extraction state so the frontend can poll for updates.
Entries are ephemeral — they only live as long as the server process.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class ExtractionPhase(str, Enum):
    queued = "queued"
    extracting = "extracting"
    done = "done"
    failed = "failed"


@dataclass
class DocumentProgress:
    document_id: str
    filename: str
    total_pages: int = 0
    current_page: int = 0
    phase: ExtractionPhase = ExtractionPhase.queued
    fields_extracted: int = 0
    error: str | None = None


@dataclass
class SessionExtractionProgress:
    session_id: str
    total_documents: int = 0
    documents_completed: int = 0
    current_document_index: int = 0
    documents: dict[str, DocumentProgress] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    status: str = "running"  # running | completed | failed
    gap_report: str | None = None

    def to_dict(self) -> dict:
        elapsed = (self.finished_at or time.time()) - self.started_at
        return {
            "status": self.status,
            "total_documents": self.total_documents,
            "documents_completed": self.documents_completed,
            "elapsed_seconds": round(elapsed, 1),
            "documents": {
                doc_id: {
                    "document_id": dp.document_id,
                    "filename": dp.filename,
                    "total_pages": dp.total_pages,
                    "current_page": dp.current_page,
                    "phase": dp.phase.value,
                    "fields_extracted": dp.fields_extracted,
                    "error": dp.error,
                }
                for doc_id, dp in self.documents.items()
            },
        }


# Global store: session_id -> progress
_progress: dict[str, SessionExtractionProgress] = {}


def start_session(session_id: str, docs: list[tuple[str, str, int]]) -> SessionExtractionProgress:
    """Initialize progress for a session extraction.

    docs: list of (document_id, filename, num_pages)
    """
    progress = SessionExtractionProgress(
        session_id=session_id,
        total_documents=len(docs),
    )
    for doc_id, filename, num_pages in docs:
        progress.documents[doc_id] = DocumentProgress(
            document_id=doc_id,
            filename=filename,
            total_pages=num_pages,
        )
    _progress[session_id] = progress
    return progress


def update_document(session_id: str, document_id: str, *,
                    phase: ExtractionPhase | None = None,
                    current_page: int | None = None,
                    fields_extracted: int | None = None,
                    error: str | None = None) -> None:
    """Update progress for a specific document."""
    progress = _progress.get(session_id)
    if not progress:
        return
    doc = progress.documents.get(document_id)
    if not doc:
        return
    if phase is not None:
        doc.phase = phase
    if current_page is not None:
        doc.current_page = current_page
    if fields_extracted is not None:
        doc.fields_extracted = fields_extracted
    if error is not None:
        doc.error = error


def mark_document_done(session_id: str, document_id: str, fields_extracted: int) -> None:
    """Mark a document as completed."""
    progress = _progress.get(session_id)
    if not progress:
        return
    doc = progress.documents.get(document_id)
    if doc:
        doc.phase = ExtractionPhase.done
        doc.fields_extracted = fields_extracted
    progress.documents_completed += 1


def mark_document_failed(session_id: str, document_id: str, error: str) -> None:
    """Mark a document as failed."""
    progress = _progress.get(session_id)
    if not progress:
        return
    doc = progress.documents.get(document_id)
    if doc:
        doc.phase = ExtractionPhase.failed
        doc.error = error
    progress.documents_completed += 1


def finish_session(session_id: str, status: str = "completed", gap_report: str | None = None) -> None:
    """Mark session extraction as finished."""
    progress = _progress.get(session_id)
    if progress:
        progress.status = status
        progress.finished_at = time.time()
        progress.gap_report = gap_report


def get_progress(session_id: str) -> dict | None:
    """Get current progress for a session. Returns None if no extraction is running."""
    progress = _progress.get(session_id)
    if progress:
        return progress.to_dict()
    return None


def cleanup(session_id: str) -> None:
    """Remove progress entry after frontend has consumed it."""
    _progress.pop(session_id, None)
