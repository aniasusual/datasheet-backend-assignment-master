"""Single-call extraction pipeline.

Send the entire PDF to Gemini, get JSON back with all fields + entity metadata.
One LLM call per document. No tool_choice, no verification pass, no pdfplumber text.
"""

import asyncio
import base64
import json
import logging
import re
import uuid

import litellm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.document import Document, DocumentStatus
from app.models.entity_document import entity_documents
from app.models.equipment_entity import EquipmentEntity
from app.models.extracted_field import ExtractedField, FieldDataType, FieldStatus

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 4
_llm_semaphore = asyncio.Semaphore(3)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _encode_full_pdf(file_path: str) -> str:
    """Read entire PDF file and encode as base64."""
    with open(file_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _parse_json_response(text: str) -> dict:
    """Parse JSON from LLM response with fallbacks for markdown wrapping.

    Tries:
    1. Direct json.loads
    2. Strip markdown fences (closed or unclosed)
    3. Find first { to last }
    4. Repair truncated JSON (close open braces/brackets)
    """
    text = text.strip()

    # Try 1: direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try 2: strip markdown fences (closed)
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Try 2b: strip unclosed markdown fence (truncated response)
    match = re.search(r"```(?:json)?\s*([\s\S]*)", text)
    if match:
        inner = match.group(1).strip().rstrip("`")
        try:
            return json.loads(inner)
        except json.JSONDecodeError:
            # Try repairing truncated JSON from the fence content
            repaired = _repair_truncated_json(inner)
            if repaired is not None:
                return repaired

    # Try 3: find first { to last }
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last != -1 and last > first:
        try:
            return json.loads(text[first:last + 1])
        except json.JSONDecodeError:
            pass

    # Try 4: repair truncated JSON from raw text
    first = text.find("{")
    if first != -1:
        repaired = _repair_truncated_json(text[first:])
        if repaired is not None:
            return repaired

    raise ValueError(f"Could not parse JSON from LLM response: {text[:500]}")


def _repair_truncated_json(text: str) -> dict | None:
    """Attempt to repair truncated JSON by closing open structures.

    Works by removing the last incomplete element and closing all open braces/brackets.
    """
    # Remove any trailing incomplete string/value (after last comma or opening bracket)
    # Find the last complete element boundary
    for trim in range(min(200, len(text)), 0, -1):
        candidate = text[:len(text) - trim]
        # Remove trailing comma, whitespace
        candidate = candidate.rstrip().rstrip(",").rstrip()

        # Count open/close braces and brackets
        open_braces = candidate.count("{") - candidate.count("}")
        open_brackets = candidate.count("[") - candidate.count("]")

        if open_braces >= 0 and open_brackets >= 0:
            # Close all open structures
            candidate += "]" * open_brackets + "}" * open_braces
            try:
                result = json.loads(candidate)
                if isinstance(result, dict) and "fields" in result:
                    logger.warning("Repaired truncated JSON — %d fields recovered", len(result.get("fields", [])))
                    return result
            except json.JSONDecodeError:
                continue

    return None


def _parse_data_type(dt: str) -> FieldDataType:
    return {"numeric": FieldDataType.numeric, "text": FieldDataType.text,
            "boolean": FieldDataType.boolean}.get(dt, FieldDataType.text)


# ---------------------------------------------------------------------------
# LLM call with retries
# ---------------------------------------------------------------------------

async def _llm_call(messages: list[dict], max_tokens: int = 16384) -> str:
    """Make an LLM call, return the response text. Retries on failure.

    Raises ValueError if all attempts fail.
    """
    last_error = None

    for attempt in range(MAX_ATTEMPTS):
        try:
            async with _llm_semaphore:
                response = await litellm.acompletion(
                    model=settings.LLM_MODEL,
                    api_key=settings.LLM_API_KEY or settings.GEMINI_API_KEY,
                    messages=messages,
                    max_tokens=max_tokens,
                )
        except Exception as exc:
            last_error = exc
            exc_str = str(exc).lower()
            if any(kw in exc_str for kw in ("rate", "limit", "quota", "429", "500", "503", "resource")):
                wait = (2 ** attempt) * 3
                logger.warning("Attempt %d/%d: rate limit/server error, waiting %ds: %s",
                               attempt + 1, MAX_ATTEMPTS, wait, exc)
                await asyncio.sleep(wait)
                continue
            else:
                logger.warning("Attempt %d/%d: LLM error: %s", attempt + 1, MAX_ATTEMPTS, exc)
                await asyncio.sleep(2)
                continue

        if response.choices and response.choices[0].message.content:
            content = response.choices[0].message.content
            usage = response.usage
            logger.info("LLM call: input=%d, output=%d tokens",
                        getattr(usage, 'prompt_tokens', 0),
                        getattr(usage, 'completion_tokens', 0))
            return content

        logger.warning("Attempt %d/%d: empty response, retrying", attempt + 1, MAX_ATTEMPTS)
        last_error = ValueError("Empty LLM response")
        await asyncio.sleep(2)

    raise ValueError(f"LLM call failed after {MAX_ATTEMPTS} attempts. Last error: {last_error}")


# ---------------------------------------------------------------------------
# Extraction prompt
# ---------------------------------------------------------------------------

EXTRACTION_SYSTEM_PROMPT = """You are a precision data extractor for industrial/engineering process datasheets.

You will receive a PDF document. Extract ALL fields from ALL pages and return them as JSON.

## Output Format
You MUST respond with ONLY a valid JSON object. No markdown fences, no explanation, no text before or after.

The JSON must have this structure:
{
  "equipment_tag": "P-718(A/B)",
  "equipment_type": "pump",
  "service_name": "DIESEL PRODUCT PUMPS",
  "language": "english",
  "entity_metadata": {"project": "...", "revision": "...", "date": "..."},
  "fields": [
    {
      "field_name": "suction_pressure_normal",
      "display_name": "Suction Pressure (Normal)",
      "raw_value": "3.5",
      "unit": "kg/cm²g",
      "section": "Operating Conditions",
      "data_type": "numeric",
      "confidence": 0.95,
      "citation_text": "Suction Pressure Normal: 3.5 kg/cm²g",
      "citation_page": 1
    }
  ]
}

## Rules
1. Preserve exact values — do not round, convert, or calculate
2. Separate units from values: "928 GPM" → value: "928", unit: "GPM"
3. Preserve the ORIGINAL language of the document — if a field label is in French, keep it in French. Do NOT translate anything. The display_name, field_name, and all text must match the language used in the document exactly.
4. citation_text must be the exact text from the document showing label + value
5. For empty fields (blank/dash/no value), include them with raw_value set to "" (empty string)
6. Confidence: 0.9+ clearly readable, 0.7-0.9 partially obscured, <0.7 uncertain, 1.0 for confirmed empty fields
7. Include EVERY field label on every page, even if the value is empty
8. Do NOT include decorative text or repeated page headers
9. citation_page is 1-indexed
10. field_name must be normalized snake_case (transliterate accented characters, e.g. "débit" → "debit")
11. For the "section" field, use the actual section heading from the document as it appears. Do not use hardcoded categories — derive the section name from the document's own structure and headings.
"""


# ---------------------------------------------------------------------------
# Corrections context (for re-extraction with HITL feedback)
# ---------------------------------------------------------------------------

def _build_corrections_context(corrections: list[dict]) -> str:
    if not corrections:
        return ""
    lines = ["\n\n## Past Corrections — apply these lessons:\n"]
    for c in corrections:
        line = f"- Field '{c['field_name']}': extracted '{c['original_value']}'"
        if c['corrected_value'] != c['original_value']:
            line += f" → correct: '{c['corrected_value']}'"
        if c.get('unit'):
            line += f" (unit: {c['unit']})"
        if c.get('reason'):
            line += f". {c['reason']}"
        if c.get('status') == 'rejected':
            line += " — REJECTED, do not extract again"
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main extraction function — 1 LLM call per document
# ---------------------------------------------------------------------------

async def extract_document(
    document_id: uuid.UUID,
    db: AsyncSession,
    session_id: uuid.UUID | None = None,
    corrections_context: str = "",
) -> list[ExtractedField]:
    """Extract all fields from a document in a single LLM call.

    Sends the raw PDF to Gemini, gets JSON back, creates ExtractedField
    and EquipmentEntity records.
    """
    document = await db.get(Document, document_id)
    if document is None:
        raise ValueError(f"Document {document_id} not found")

    if document.status not in (DocumentStatus.uploaded, DocumentStatus.failed, DocumentStatus.extracted):
        raise ValueError(f"Document {document_id} status '{document.status}' — cannot extract")

    document.status = DocumentStatus.extracting
    await db.flush()

    try:
        # Build the message: PDF + simple instruction
        pdf_b64 = _encode_full_pdf(document.file_path)

        system = EXTRACTION_SYSTEM_PROMPT + corrections_context

        content: list[dict] = [
            {
                "type": "image_url",
                "image_url": {"url": f"data:application/pdf;base64,{pdf_b64}"},
            },
            {
                "type": "text",
                "text": f"Extract ALL fields from this {document.num_pages}-page document: '{document.filename}'. Respond with ONLY JSON.",
            },
        ]

        # Single LLM call
        response_text = await _llm_call(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            max_tokens=65536,
        )

        # Parse JSON response
        data = _parse_json_response(response_text)
        raw_fields = data.get("fields", [])

        # Update document metadata
        if data.get("equipment_tag"):
            document.pump_tag = data["equipment_tag"][:100]
        lang = data.get("language", "")
        if lang:
            if lang in ("french", "bilingual"):
                document.format_type = "french_form"
            elif lang == "english":
                document.format_type = "english_tabular"
            else:
                document.format_type = lang

        # Create EquipmentEntity if we have a tag and session_id
        entity = None
        if session_id and data.get("equipment_tag"):
            entity = EquipmentEntity(
                session_id=session_id,
                tag=data["equipment_tag"][:100],
                entity_type=data.get("equipment_type", "pump")[:100],
                name=data.get("service_name", "")[:255],
                metadata_json=data.get("entity_metadata"),
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

        # Create ExtractedField records
        all_fields: list[ExtractedField] = []
        for field_data in raw_fields:
            unit = field_data.get("unit")
            if unit == "":
                unit = None
            if unit and len(unit) > 50:
                unit = unit[:50]

            field = ExtractedField(
                document_id=document.id,
                entity_id=entity.id if entity else None,
                field_name=field_data.get("field_name", "unknown")[:255],
                display_name=field_data.get("display_name", "Unknown")[:255],
                raw_value=field_data.get("raw_value", ""),
                unit=unit,
                data_type=_parse_data_type(field_data.get("data_type", "text")),
                section=field_data.get("section", "general_info")[:100],
                confidence=field_data.get("confidence", 0.8),
                status=FieldStatus.extracted,
                citation_page=field_data.get("citation_page", 1),
                citation_text=field_data.get("citation_text", ""),
                citation_bbox=None,
            )
            db.add(field)
            all_fields.append(field)

        document.status = DocumentStatus.extracted
        await db.flush()

        logger.info(
            "Extraction complete for doc %s: %d fields, tag=%s",
            document_id, len(all_fields), document.pump_tag,
        )

        return all_fields

    except Exception:
        logger.exception("Extraction failed for document %s", document_id)
        try:
            await db.rollback()
            document = await db.get(Document, document_id)
            if document:
                document.status = DocumentStatus.failed
                await db.flush()
        except Exception:
            logger.exception("Failed to update document status after error")
        raise
