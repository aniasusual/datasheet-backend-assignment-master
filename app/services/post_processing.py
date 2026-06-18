"""Post-processing — DEPRECATED.

Entity creation is now handled inside extraction.py in a single LLM call.
This module is kept as a stub for backward compatibility.
"""

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def post_process_document(
    document_id: uuid.UUID,
    session_id: uuid.UUID,
    db: AsyncSession,
):
    """No-op stub. Entity creation now happens in extract_document()."""
    logger.debug("post_process_document called for %s — no-op (deprecated)", document_id)
    return None
