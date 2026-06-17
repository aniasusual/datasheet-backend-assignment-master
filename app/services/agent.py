"""Conversational agent for querying and editing extracted fields.

The agent gets ALL extracted data injected into its system prompt so it can
reason freely over the full dataset — no hardcoded query patterns.

Tools are only for write operations: correct, verify, reject fields.
Max 5 rounds. No DB message storage — frontend owns the conversation history.
"""

import json
import logging
import uuid

import litellm
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models.document import Document
from app.models.document_page import DocumentPage
from app.models.extracted_field import ExtractedField, FieldStatus
from app.models.field_correction import FieldCorrection

logger = logging.getLogger(__name__)

MAX_ROUNDS = 5


# ---------------------------------------------------------------------------
# Build the full data context for the system prompt
# ---------------------------------------------------------------------------

async def _build_data_context(session_id: uuid.UUID, db: AsyncSession) -> str:
    """Load all extracted data and format it as context for the LLM."""

    # Load all documents
    docs_stmt = (
        select(Document)
        .where(Document.session_id == session_id)
        .order_by(Document.filename)
    )
    docs = (await db.execute(docs_stmt)).scalars().all()

    if not docs:
        return "\n## Extracted Data\nNo documents have been processed yet.\n"

    # Load all fields grouped by document
    parts = ["\n## Extracted Data\n"]

    for doc in docs:
        fields_stmt = (
            select(ExtractedField)
            .where(ExtractedField.document_id == doc.id)
            .order_by(ExtractedField.section, ExtractedField.field_name)
        )
        fields = (await db.execute(fields_stmt)).scalars().all()

        tag = doc.pump_tag or "unknown"
        parts.append(f"### Document: {doc.filename} (Pump: {tag}, Format: {doc.format_type or 'unknown'}, Pages: {doc.num_pages})")
        parts.append(f"Document ID: {doc.id}")

        if not fields:
            parts.append("No fields extracted.\n")
            continue

        # Group by section
        sections: dict[str, list[ExtractedField]] = {}
        for f in fields:
            sections.setdefault(f.section, []).append(f)

        for section, section_fields in sections.items():
            parts.append(f"\n**{section}:**")
            for f in section_fields:
                unit = f" {f.unit}" if f.unit else ""
                value = f.raw_value if f.raw_value else "(empty)"
                conf = f"{f.confidence:.0%}"
                status = f.status.value
                parts.append(
                    f"- {f.display_name}: {value}{unit} "
                    f"[confidence: {conf}, status: {status}, page: {f.citation_page}, "
                    f"id: {f.id}]"
                )
                if f.citation_text:
                    parts.append(f"  citation: \"{f.citation_text}\"")

        parts.append("")  # blank line between docs

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

AGENT_SYSTEM_PROMPT = """You are an expert technical data assistant for industrial pump process datasheets.

You have access to ALL extracted data from the datasheets below. Use this data to answer any question — you can cross-reference fields, compare values across pumps, reason about engineering properties, and provide technical analysis.

## Your capabilities:
1. **Answer any question** about the extracted data — materials, pressures, flows, dimensions, compatibility, comparisons, etc.
2. **Technical reasoning** — infer properties like corrosion risk from materials + fluid data, efficiency from motor/pump specs, etc.
3. **Cross-document analysis** — compare values across different pumps/datasheets
4. **Correct fields** — use update_field tool when the user points out errors
5. **Verify fields** — use verify_fields tool when the user confirms data is correct
6. **Reject fields** — use reject_fields tool to mark junk/irrelevant fields
7. **Read raw page text** — use get_page_text when you need to see the original document content that may not have been extracted as fields

## Guidelines:
- Always cite specific values with their source document, page number, and confidence level
- When making engineering inferences (e.g., corrosion risk), explain your reasoning based on the specific materials and fluids involved
- When correcting a field, always include a reason
- Be concise and precise — these are engineering values, accuracy matters
- If data is missing or confidence is low, say so explicitly
- For comparisons, present data in a structured format (tables or bullet points)
- Empty fields (value = "(empty)") mean the field exists on the datasheet but has no value filled in
- Each field has an `id` — use this when calling tools to modify fields
"""


# ---------------------------------------------------------------------------
# Tools — write operations + raw page text access
# ---------------------------------------------------------------------------

AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "update_field",
            "description": "Update a field's value and/or unit. Creates an audit trail. Use when the user points out an error in the extracted data.",
            "parameters": {
                "type": "object",
                "properties": {
                    "field_id": {
                        "type": "string",
                        "description": "The UUID of the field to update (from the data context)",
                    },
                    "raw_value": {
                        "type": "string",
                        "description": "The corrected value",
                    },
                    "unit": {
                        "type": "string",
                        "description": "The corrected unit (empty string to clear)",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why this correction was needed",
                    },
                },
                "required": ["field_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_fields",
            "description": "Mark one or more fields as verified (confirmed correct by the user).",
            "parameters": {
                "type": "object",
                "properties": {
                    "field_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "UUIDs of fields to verify",
                    },
                },
                "required": ["field_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reject_fields",
            "description": "Mark one or more fields as rejected (junk, irrelevant, or duplicate).",
            "parameters": {
                "type": "object",
                "properties": {
                    "field_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "UUIDs of fields to reject",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why these fields are being rejected",
                    },
                },
                "required": ["field_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_page_text",
            "description": "Get the raw text, layout text, and tables for a specific page of a document. Use when you need to see original document content that wasn't captured in extracted fields.",
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {
                        "type": "string",
                        "description": "Document UUID",
                    },
                    "page_number": {
                        "type": "integer",
                        "description": "Page number (1-indexed)",
                    },
                },
                "required": ["document_id", "page_number"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

async def _exec_update_field(
    session_id: uuid.UUID, args: dict, db: AsyncSession
) -> str:
    field_id = args.get("field_id")
    if not field_id:
        return json.dumps({"error": "field_id is required"})

    stmt = (
        select(ExtractedField)
        .join(Document, ExtractedField.document_id == Document.id)
        .where(Document.session_id == session_id, ExtractedField.id == uuid.UUID(field_id))
    )
    result = await db.execute(stmt)
    field = result.scalar_one_or_none()

    if not field:
        return json.dumps({"error": f"Field {field_id} not found"})

    original_value = field.raw_value
    changed = False

    if "raw_value" in args and args["raw_value"] != field.raw_value:
        field.raw_value = args["raw_value"]
        changed = True

    if "unit" in args:
        new_unit = args["unit"] if args["unit"] != "" else None
        if new_unit != field.unit:
            field.unit = new_unit
            changed = True

    if changed:
        field.status = FieldStatus.corrected
        correction = FieldCorrection(
            field_id=field.id,
            original_value=original_value,
            corrected_value=field.raw_value,
            reason=args.get("reason", "Agent correction"),
            corrected_by="agent",
        )
        db.add(correction)
        await db.flush()

    return json.dumps({
        "success": True,
        "field_id": str(field.id),
        "field_name": field.display_name,
        "old_value": original_value,
        "new_value": field.raw_value,
        "unit": field.unit,
        "status": field.status.value,
    })


async def _exec_verify_fields(
    session_id: uuid.UUID, args: dict, db: AsyncSession
) -> str:
    field_ids = args.get("field_ids", [])
    if not field_ids:
        return json.dumps({"error": "field_ids is required"})

    uuids = [uuid.UUID(fid) for fid in field_ids]
    stmt = (
        select(ExtractedField)
        .join(Document, ExtractedField.document_id == Document.id)
        .where(Document.session_id == session_id, ExtractedField.id.in_(uuids))
    )
    result = await db.execute(stmt)
    fields = result.scalars().all()

    verified = 0
    for f in fields:
        if f.status in (FieldStatus.extracted, FieldStatus.corrected):
            f.status = FieldStatus.verified
            verified += 1

    await db.flush()
    return json.dumps({"verified": verified, "total_requested": len(field_ids)})


async def _exec_reject_fields(
    session_id: uuid.UUID, args: dict, db: AsyncSession
) -> str:
    field_ids = args.get("field_ids", [])
    if not field_ids:
        return json.dumps({"error": "field_ids is required"})

    uuids = [uuid.UUID(fid) for fid in field_ids]
    stmt = (
        select(ExtractedField)
        .join(Document, ExtractedField.document_id == Document.id)
        .where(Document.session_id == session_id, ExtractedField.id.in_(uuids))
    )
    result = await db.execute(stmt)
    fields = result.scalars().all()

    rejected = 0
    for f in fields:
        f.status = FieldStatus.rejected
        rejected += 1

    await db.flush()
    return json.dumps({"rejected": rejected, "reason": args.get("reason", "")})


async def _exec_get_page_text(
    session_id: uuid.UUID, args: dict, db: AsyncSession
) -> str:
    doc_id = args.get("document_id")
    page_num = args.get("page_number")
    if not doc_id or not page_num:
        return json.dumps({"error": "document_id and page_number are required"})

    doc = await db.get(Document, uuid.UUID(doc_id))
    if not doc or doc.session_id != session_id:
        return json.dumps({"error": "Document not found in this session"})

    stmt = select(DocumentPage).where(
        DocumentPage.document_id == uuid.UUID(doc_id),
        DocumentPage.page_number == page_num,
    )
    page = (await db.execute(stmt)).scalar_one_or_none()

    if not page:
        return json.dumps({"error": f"Page {page_num} not found"})

    return json.dumps({
        "document": doc.filename,
        "pump_tag": doc.pump_tag,
        "page_number": page.page_number,
        "raw_text": page.raw_text[:8000] if page.raw_text else "",
        "layout_text": page.layout_text[:8000] if page.layout_text else None,
        "has_tables": bool(page.tables_json),
        "tables": page.tables_json if page.tables_json else None,
    })


TOOL_EXECUTORS = {
    "update_field": _exec_update_field,
    "verify_fields": _exec_verify_fields,
    "reject_fields": _exec_reject_fields,
    "get_page_text": _exec_get_page_text,
}


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

async def run_agent(
    session_id: uuid.UUID,
    messages: list[dict],
    new_message: str,
    db: AsyncSession,
) -> dict:
    """Run the agent loop.

    Loads all extracted data into context, then runs a tool-use loop for
    write operations. The LLM can answer questions directly from context
    without needing search tools.
    """
    # Build full data context
    data_context = await _build_data_context(session_id, db)
    system_prompt = AGENT_SYSTEM_PROMPT + data_context

    # Build conversation
    conversation = [{"role": "system", "content": system_prompt}]
    for msg in messages:
        conversation.append({"role": msg["role"], "content": msg["content"]})
    conversation.append({"role": "user", "content": new_message})

    tool_actions: list[dict] = []

    for round_num in range(MAX_ROUNDS):
        response = await litellm.acompletion(
            model=settings.LLM_MODEL,
            api_key=settings.LLM_API_KEY or settings.GEMINI_API_KEY,
            messages=conversation,
            tools=AGENT_TOOLS,
            max_tokens=4096,
        )

        if not response.choices:
            logger.warning("Empty response from LLM in agent round %d", round_num)
            break

        message = response.choices[0].message

        # No tool calls — we have the final text response
        if not message.tool_calls:
            final_text = message.content or "I couldn't generate a response."

            updated_messages = list(messages)
            updated_messages.append({"role": "user", "content": new_message})
            updated_messages.append({"role": "assistant", "content": final_text})

            return {
                "response": final_text,
                "messages": updated_messages,
                "tool_actions": tool_actions,
            }

        # Execute tool calls
        conversation.append({
            "role": "assistant",
            "content": message.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in message.tool_calls
            ],
        })

        for tc in message.tool_calls:
            func_name = tc.function.name
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}

            executor = TOOL_EXECUTORS.get(func_name)
            if executor:
                result_str = await executor(session_id, args, db)
                tool_actions.append({
                    "tool": func_name,
                    "args": args,
                    "result": json.loads(result_str),
                })
            else:
                result_str = json.dumps({"error": f"Unknown tool: {func_name}"})

            conversation.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result_str,
            })

        logger.info("Agent round %d: executed %d tool calls", round_num, len(message.tool_calls))

    # Exhausted rounds
    updated_messages = list(messages)
    updated_messages.append({"role": "user", "content": new_message})
    updated_messages.append({"role": "assistant", "content": "I ran out of processing steps. Please try a simpler request."})

    return {
        "response": "I ran out of processing steps. Please try a simpler request.",
        "messages": updated_messages,
        "tool_actions": tool_actions,
    }
