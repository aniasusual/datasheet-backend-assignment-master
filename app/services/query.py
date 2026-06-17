"""Query service: answer natural language questions using extracted fields.

Single LLM call — loads all fields for the session, sends them with the
question, gets back an answer with field citations.
"""

import json
import logging
import uuid

import litellm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.document import Document
from app.models.extracted_field import ExtractedField

logger = logging.getLogger(__name__)

QUERY_SYSTEM_PROMPT = """You are a technical data assistant for industrial pump process datasheets.

You have access to structured fields extracted from pump datasheets. Answer the user's question using ONLY the data provided. Do not guess or use outside knowledge.

## Response Format

Answer concisely. After your answer, list the fields you used as citations in this format:

**Sources:**
- [field_id] field_name: value unit — from document_filename (page N)

If the data does not contain information to answer the question, say so clearly.
"""

QUERY_TOOL = {
    "type": "function",
    "function": {
        "name": "answer_query",
        "description": "Provide the answer to the user's question with cited fields.",
        "parameters": {
            "type": "object",
            "properties": {
                "answer": {
                    "type": "string",
                    "description": "The answer to the user's question in clear, concise language.",
                },
                "cited_field_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "IDs of the extracted fields used to answer the question.",
                },
                "confidence": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                    "description": "How confident the answer is based on available data.",
                },
            },
            "required": ["answer", "cited_field_ids", "confidence"],
        },
    },
}


async def query_fields(
    session_id: uuid.UUID,
    question: str,
    db: AsyncSession,
) -> dict:
    """Answer a natural language question using extracted fields.

    Returns dict with answer, cited fields, and confidence.
    """
    # Load all fields for the session with document info
    stmt = (
        select(ExtractedField, Document.filename, Document.pump_tag)
        .join(Document, ExtractedField.document_id == Document.id)
        .where(Document.session_id == session_id)
        .order_by(Document.filename, ExtractedField.section, ExtractedField.field_name)
    )
    result = await db.execute(stmt)
    rows = result.all()

    if not rows:
        return {
            "answer": "No extracted data available. Please upload and extract documents first.",
            "cited_fields": [],
            "confidence": "low",
        }

    # Build field context for LLM
    field_map = {}
    fields_text_parts = []
    current_doc = None

    for field, filename, pump_tag in rows:
        if filename != current_doc:
            current_doc = filename
            fields_text_parts.append(f"\n## {filename} (Pump: {pump_tag or 'unknown'})")

        field_map[str(field.id)] = {
            "id": str(field.id),
            "document_id": str(field.document_id),
            "field_name": field.field_name,
            "display_name": field.display_name,
            "raw_value": field.raw_value,
            "unit": field.unit,
            "section": field.section,
            "confidence": field.confidence,
            "citation_page": field.citation_page,
            "citation_text": field.citation_text,
            "filename": filename,
            "pump_tag": pump_tag,
        }

        unit_str = f" {field.unit}" if field.unit else ""
        fields_text_parts.append(
            f"- [{field.id}] {field.display_name}: {field.raw_value}{unit_str} "
            f"(section: {field.section}, page: {field.citation_page}, confidence: {field.confidence})"
        )

    fields_context = "\n".join(fields_text_parts)

    user_content = (
        f"## Available Extracted Data\n{fields_context}\n\n"
        f"## Question\n{question}"
    )

    # Call LLM
    response = await litellm.acompletion(
        model=settings.LLM_MODEL,
        api_key=settings.LLM_API_KEY or settings.GEMINI_API_KEY,
        messages=[
            {"role": "system", "content": QUERY_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        tools=[QUERY_TOOL],
        max_tokens=2048,
    )

    # Parse response
    answer = "Could not generate an answer."
    cited_field_ids = []
    confidence = "low"

    if response.choices:
        message = response.choices[0].message

        if message.tool_calls:
            for tc in message.tool_calls:
                if tc.function.name == "answer_query":
                    args = json.loads(tc.function.arguments)
                    answer = args.get("answer", answer)
                    cited_field_ids = args.get("cited_field_ids", [])
                    confidence = args.get("confidence", "low")
        elif message.content:
            # Fallback if model responds with text instead of tool call
            answer = message.content

    # Resolve cited fields
    cited_fields = [field_map[fid] for fid in cited_field_ids if fid in field_map]

    logger.info(
        "Query answered: %d fields cited, confidence=%s, tokens=%d/%d",
        len(cited_fields), confidence,
        getattr(response.usage, 'prompt_tokens', 0),
        getattr(response.usage, 'completion_tokens', 0),
    )

    return {
        "answer": answer,
        "cited_fields": cited_fields,
        "confidence": confidence,
    }
