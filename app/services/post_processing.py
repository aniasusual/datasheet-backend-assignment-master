"""Post-processing service: footnotes, dedup, entity creation.

One LLM call per document after all pages are extracted.
Resolves footnote references, deduplicates fields, creates equipment entities.
"""

import json
import logging
import uuid

import litellm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.document import Document
from app.models.document_page import DocumentPage
from app.models.entity_document import entity_documents
from app.models.equipment_entity import EquipmentEntity
from app.models.extracted_field import ExtractedField

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Post-processing tool schema (OpenAI format for litellm)
# ---------------------------------------------------------------------------

POST_PROCESS_TOOL = {
    "type": "function",
    "function": {
        "name": "post_process_results",
        "description": (
            "Submit the post-processing results: resolved footnotes, duplicate field IDs to remove, "
            "and equipment entity info extracted from the document header."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "entity": {
                    "type": "object",
                    "description": "Equipment entity extracted from the document header",
                    "properties": {
                        "tag": {
                            "type": "string",
                            "description": "Equipment tag, e.g. 'P-718(A/B)', 'P-300228'",
                        },
                        "entity_type": {
                            "type": "string",
                            "description": "Type of equipment, e.g. 'pump', 'motor'",
                        },
                        "name": {
                            "type": "string",
                            "description": "Service/descriptive name, e.g. 'DIESEL PRODUCT PUMPS'",
                        },
                        "metadata": {
                            "type": "object",
                            "description": "Additional metadata: project, area, unit, revision, date, etc.",
                        },
                    },
                    "required": ["tag", "entity_type", "name"],
                },
                "footnote_resolutions": {
                    "type": "array",
                    "description": "Resolved footnote references. Each maps a note number to the actual note text.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "note_number": {
                                "type": "string",
                                "description": "The footnote number, e.g. '3', '7'",
                            },
                            "note_text": {
                                "type": "string",
                                "description": "The full text of the footnote",
                            },
                        },
                        "required": ["note_number", "note_text"],
                    },
                },
                "duplicate_field_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "IDs of fields that are duplicates and should be removed. "
                        "Keep the one with higher confidence or from the more detailed page."
                    ),
                },
                "field_updates": {
                    "type": "array",
                    "description": "Fields that need their values or confidence updated based on cross-page context.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field_id": {"type": "string"},
                            "updated_raw_value": {"type": "string"},
                            "updated_confidence": {"type": "number"},
                            "reason": {"type": "string"},
                        },
                        "required": ["field_id", "reason"],
                    },
                },
            },
            "required": ["entity", "footnote_resolutions", "duplicate_field_ids"],
        },
    },
}

POST_PROCESS_SYSTEM_PROMPT = """You are a post-processing specialist for industrial process datasheet extractions.

You have been given all extracted fields from a document along with the notes/remarks pages. Your job is to:

1. **Resolve footnotes**: Match footnote references (e.g., "(3)", "(7)") found in fields to the actual note text from the notes pages. Create a mapping of note_number → full note text.

2. **Identify duplicates**: Fields that appear on multiple pages (e.g., repeated headers) should be deduplicated. Mark the duplicate's ID for removal — keep the version with higher confidence or more detail.

3. **Extract equipment entity**: From the document header/metadata, identify:
   - Equipment tag (e.g., "P-718(A/B)", "P-300228")
   - Equipment type (usually "pump" for these datasheets)
   - Service/descriptive name (e.g., "DIESEL PRODUCT PUMPS")
   - Metadata (project number, area, unit, revision, date)

4. **Flag corrections**: If cross-page context reveals a value is inconsistent or likely wrong, include it in field_updates with the reason.

Call the `post_process_results` tool exactly once with your findings.
"""


async def post_process_document(
    document_id: uuid.UUID,
    session_id: uuid.UUID,
    db: AsyncSession,
) -> EquipmentEntity | None:
    """Post-process extracted fields for a document.

    Resolves footnotes, removes duplicates, creates equipment entity.
    Returns the created EquipmentEntity or None.
    """
    document = await db.get(Document, document_id)
    if document is None:
        raise ValueError(f"Document {document_id} not found")

    # Load all extracted fields
    fields_stmt = (
        select(ExtractedField)
        .where(ExtractedField.document_id == document_id)
        .order_by(ExtractedField.section, ExtractedField.field_name)
    )
    fields_result = await db.execute(fields_stmt)
    fields = fields_result.scalars().all()

    if not fields:
        logger.warning("No fields to post-process for document %s", document_id)
        return None

    # Load all page text (especially notes pages)
    pages_stmt = (
        select(DocumentPage)
        .where(DocumentPage.document_id == document_id)
        .order_by(DocumentPage.page_number)
    )
    pages_result = await db.execute(pages_stmt)
    pages = pages_result.scalars().all()

    # Build context for the LLM
    fields_summary = []
    for f in fields:
        fields_summary.append({
            "id": str(f.id),
            "field_name": f.field_name,
            "display_name": f.display_name,
            "raw_value": f.raw_value,
            "unit": f.unit,
            "section": f.section,
            "confidence": f.confidence,
            "citation_page": f.citation_page,
            "citation_text": f.citation_text,
        })

    pages_text = []
    for p in pages:
        pages_text.append(f"--- Page {p.page_number} ({p.page_type}) ---\n{p.raw_text}")

    user_content = (
        f"Document: {document.filename} (tag: {document.pump_tag}, format: {document.format_type})\n\n"
        f"## Extracted Fields ({len(fields)} total)\n\n"
        f"{json.dumps(fields_summary, indent=2)}\n\n"
        f"## Full Page Text\n\n"
        + "\n\n".join(pages_text)
    )

    # Call LLM
    response = await litellm.acompletion(
        model=settings.LLM_MODEL,
        api_key=settings.LLM_API_KEY or settings.GEMINI_API_KEY,
        messages=[
            {"role": "system", "content": POST_PROCESS_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        tools=[POST_PROCESS_TOOL],
        max_tokens=4096,
        num_retries=2,
    )

    # Parse response
    entity: EquipmentEntity | None = None

    if not response.choices:
        logger.warning("Empty response from LLM for post-processing doc %s", document_id)
        return None

    message = response.choices[0].message

    if message.tool_calls:
        for tool_call in message.tool_calls:
            if tool_call.function.name != "post_process_results":
                continue

            data = json.loads(tool_call.function.arguments)

            # 1. Create equipment entity
            entity_data = data.get("entity", {})
            if entity_data.get("tag"):
                entity = EquipmentEntity(
                    session_id=session_id,
                    tag=entity_data["tag"],
                    entity_type=entity_data.get("entity_type", "pump"),
                    name=entity_data.get("name", ""),
                    metadata_json=entity_data.get("metadata"),
                )
                db.add(entity)
                await db.flush()

                # Link entity to document
                await db.execute(
                    entity_documents.insert().values(
                        entity_id=entity.id,
                        document_id=document_id,
                    )
                )

                # Link all fields to entity
                for f in fields:
                    f.entity_id = entity.id

                logger.info("Created entity '%s' for document %s", entity.tag, document_id)

            # 2. Remove duplicates
            dup_ids = data.get("duplicate_field_ids", [])
            if dup_ids:
                for f in fields:
                    if str(f.id) in dup_ids:
                        await db.delete(f)
                logger.info("Removed %d duplicate fields from document %s", len(dup_ids), document_id)

            # 3. Apply field updates
            field_updates = data.get("field_updates", [])
            field_map = {str(f.id): f for f in fields}
            for update in field_updates:
                fid = update.get("field_id")
                if fid in field_map:
                    f = field_map[fid]
                    if update.get("updated_raw_value"):
                        f.raw_value = update["updated_raw_value"]
                    if update.get("updated_confidence"):
                        f.confidence = update["updated_confidence"]
                    logger.info("Updated field %s: %s", fid, update.get("reason", ""))

            # 4. Store footnote resolutions in entity metadata
            footnotes = data.get("footnote_resolutions", [])
            if footnotes and entity:
                meta = entity.metadata_json or {}
                meta["footnotes"] = {fn["note_number"]: fn["note_text"] for fn in footnotes}
                entity.metadata_json = meta

    await db.flush()

    # Log usage
    usage = response.usage
    logger.info(
        "Post-processing LLM usage for doc %s: input=%d, output=%d tokens",
        document_id,
        getattr(usage, 'prompt_tokens', 0),
        getattr(usage, 'completion_tokens', 0),
    )

    return entity
