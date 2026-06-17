"""Per-page vision extraction service.

Sends each content page (image + text) to an LLM via litellm and gets back
structured fields via tool use. One LLM call per page.
"""

import asyncio
import base64
import io
import json
import logging
import uuid
from pathlib import Path

import litellm
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.document import Document, DocumentStatus
from app.models.document_page import DocumentPage
from app.models.extracted_field import ExtractedField, FieldDataType, FieldStatus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Extraction tool schema (OpenAI-format for litellm)
# ---------------------------------------------------------------------------

SAVE_FIELDS_TOOL = {
    "type": "function",
    "function": {
        "name": "save_extracted_fields",
        "description": (
            "Save all fields extracted from this page. Call this exactly once per page "
            "with ALL fields found on the page. Each field must include a citation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "fields": {
                    "type": "array",
                    "description": "Array of extracted fields from this page",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field_name": {
                                "type": "string",
                                "description": "Normalized snake_case field identifier, e.g. 'normal_flowrate', 'suction_pressure'",
                            },
                            "display_name": {
                                "type": "string",
                                "description": "Human-readable field name as shown in the document, e.g. 'Normal Flowrate'",
                            },
                            "raw_value": {
                                "type": "string",
                                "description": "Value exactly as it appears in the document. Do not convert units or round numbers.",
                            },
                            "unit": {
                                "type": "string",
                                "description": "Unit of measurement separated from the value, e.g. 'GPM', 'psig', 'kg/cm²', '°F'. Empty string if unitless.",
                            },
                            "section": {
                                "type": "string",
                                "enum": [
                                    "general_info",
                                    "product_handled",
                                    "operating_conditions",
                                    "pump_performance",
                                    "construction_materials",
                                    "mechanical_design",
                                    "motor_data",
                                    "weights_dimensions",
                                    "notes_remarks",
                                ],
                                "description": "Category this field belongs to",
                            },
                            "data_type": {
                                "type": "string",
                                "enum": ["numeric", "text", "boolean"],
                                "description": "Data type of the value",
                            },
                            "confidence": {
                                "type": "number",
                                "description": "Confidence score: 0.9+ = clearly readable, 0.7-0.9 = partially obscured or ambiguous, <0.7 = uncertain",
                            },
                            "citation_text": {
                                "type": "string",
                                "description": "The exact text snippet from the document that contains this field and value",
                            },
                            "note_refs": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Footnote references found near this field, e.g. ['3', '7']. Empty array if none.",
                            },
                        },
                        "required": [
                            "field_name", "display_name", "raw_value", "section",
                            "data_type", "confidence", "citation_text",
                        ],
                    },
                },
            },
            "required": ["fields"],
        },
    },
}


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

EXTRACTION_SYSTEM_PROMPT = """You are a specialist in extracting structured data from industrial process datasheets (pump datasheets).

You will receive a page image from a process datasheet along with any extracted text. Your job is to identify and extract every technical field on the page.

## Field Sections

Categorize each field into one of these sections:

1. **general_info** — pump tag/item number, service description, pump type, number of units, driver type, project info, document metadata (date, revision, page)
2. **product_handled** — pumped fluid name, temperature (pumping temp, auto-ignition), viscosity, density, specific gravity, vapor pressure, corrosive/erosive properties, solids content
3. **operating_conditions** — flowrates (normal, minimum, design), discharge/suction pressures, differential pressure
4. **pump_performance** — differential head, NPSH (available/required), shaft power, electrical consumption, efficiency, speed/RPM
5. **construction_materials** — impeller material, casing material, shaft material, wear rings, sleeve material, gasket material
6. **mechanical_design** — seal type (mechanical seal info), bearing type, coupling, nozzle sizes/ratings, casing type (volute), joint type, mounting, bossages/tapped openings
7. **motor_data** — motor supplier, voltage/phases/frequency, protection rating (IP), explosion proof class, frame type, lubrication, current at max loading
8. **weights_dimensions** — pump weight, base/socle weight, dimensions
9. **notes_remarks** — general notes, footnoted remarks, warnings, special conditions, off-spec operating conditions

## Extraction Rules

1. **Preserve exact values** — copy numbers exactly as they appear. Do not round, convert, or calculate.
2. **Separate units** — put the unit in the `unit` field, not in `raw_value`. "928 GPM" → raw_value: "928", unit: "GPM"
3. **Handle bilingual content** — these datasheets may have French and English labels. Use the English name for `display_name` when both are present. Common French→English mappings:
   - DÉBIT/DEBIT → Flowrate
   - PRESSION → Pressure
   - ASPIRATION → Suction
   - REFOULEMENT → Discharge
   - HAUTEUR MANO → Differential Head
   - MASSE VOL → Density
   - VISCOSITE/VISCOSITÉ → Viscosity
   - TENSION DE VAPEUR → Vapor Pressure
   - ROUE → Impeller
   - CORPS → Inner Case
   - ARBRE → Shaft
   - GARNITURE MECANIQUE → Mechanical Seal
   - PALIER → Bearing
   - MOTEUR FOURNI PAR → Motor Supplied By
   - REMARQUES → Remarks
   - POMPE CENTRIFUGE → Centrifugal Pump
4. **Footnote references** — if a value has a parenthetical number like (3) or (7) nearby, include it in `note_refs`. These reference notes on other pages.
5. **Multiple values** — if a field has both normal and design values, extract them as separate fields (e.g., `normal_flowrate` and `design_flowrate`).
6. **Confidence scoring**:
   - 0.9-1.0: Value is clearly readable, unambiguous
   - 0.7-0.89: Partially obscured, small text, or slightly ambiguous
   - 0.5-0.69: Difficult to read, guessing based on context
   - Below 0.5: Very uncertain, include but flag
7. **Skip empty fields** — do not extract fields where the value cell is blank or contains only dashes.
8. **Citation** — `citation_text` must be the actual text snippet from the document showing the field label and value together.

## Important

- Extract ALL fields visible on the page, not just a subset.
- Call the `save_extracted_fields` tool exactly ONCE with all fields from this page.
- If the page has no extractable technical data (e.g., it's a cover page or blank form), call the tool with an empty fields array.
"""


# ---------------------------------------------------------------------------
# Core extraction function
# ---------------------------------------------------------------------------

MAX_IMAGE_LONG_SIDE = 2048  # Resize for LLM; originals stay on disk at 300 DPI


def _resize_image_for_llm(image_path: Path) -> str:
    """Load image, resize if needed, return base64 PNG string.

    Keeps aspect ratio. Only downscales — never upscales.
    """
    with Image.open(image_path) as img:
        w, h = img.size
        long_side = max(w, h)

        if long_side > MAX_IMAGE_LONG_SIDE:
            scale = MAX_IMAGE_LONG_SIDE / long_side
            new_w = int(w * scale)
            new_h = int(h * scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            logger.debug("Resized %s from %dx%d to %dx%d", image_path.name, w, h, new_w, new_h)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("utf-8")


def _build_page_content(page: DocumentPage, document: Document) -> list[dict]:
    """Build the message content for a single page extraction."""
    content = []

    # Add the page image (resized for LLM)
    image_path = settings.RENDERED_PAGES_DIR / page.image_path
    if image_path.exists():
        image_data = _resize_image_for_llm(image_path)
        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{image_data}",
            },
        })

    # Add text context if available
    text_parts = []
    if page.raw_text and page.raw_text.strip():
        text_parts.append(f"## Extracted Text (page {page.page_number})\n{page.raw_text}")
    if page.layout_text and page.layout_text.strip():
        text_parts.append(f"## Layout Text (preserves spatial positioning)\n{page.layout_text}")
    if page.tables_json:
        text_parts.append(f"## Parsed Tables\n{json.dumps(page.tables_json)}")

    instruction = (
        f"Extract all technical fields from page {page.page_number} of document '{document.filename}' "
        f"(pump tag: {document.pump_tag or 'unknown'}, format: {document.format_type or 'unknown'})."
    )

    if text_parts:
        instruction += "\n\n" + "\n\n".join(text_parts)
    else:
        instruction += f"\n\nPage {page.page_number} — no text could be extracted. Please use the image to extract fields."

    content.append({"type": "text", "text": instruction})

    return content


def _parse_data_type(dt: str) -> FieldDataType:
    mapping = {
        "numeric": FieldDataType.numeric,
        "text": FieldDataType.text,
        "boolean": FieldDataType.boolean,
    }
    return mapping.get(dt, FieldDataType.text)


async def extract_page_fields(
    page: DocumentPage,
    document: Document,
    db: AsyncSession,
    corrections_context: str = "",
) -> list[ExtractedField]:
    """Extract fields from a single page using LLM vision.

    Returns the list of created ExtractedField records.
    """
    page_content = _build_page_content(page, document)
    system_prompt = EXTRACTION_SYSTEM_PROMPT + corrections_context
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": page_content},
    ]

    # Retry logic: up to 3 attempts on empty/no-tool-call responses
    message = None
    usage = None
    max_attempts = 3
    for attempt in range(max_attempts):
        response = await litellm.acompletion(
            model=settings.LLM_MODEL,
            api_key=settings.LLM_API_KEY or settings.GEMINI_API_KEY,
            messages=messages,
            tools=[SAVE_FIELDS_TOOL],
            tool_choice={"type": "function", "function": {"name": "save_extracted_fields"}},
            max_tokens=8192,
        )
        usage = response.usage

        if response.choices and response.choices[0].message.tool_calls:
            message = response.choices[0].message
            break

        logger.warning(
            "Attempt %d/%d: empty/no-tool response for doc %s page %d, retrying...",
            attempt + 1, max_attempts, document.id, page.page_number,
        )
        if attempt < max_attempts - 1:
            await asyncio.sleep(2)

    created_fields: list[ExtractedField] = []

    if message is None:
        logger.error("All attempts failed for doc %s page %d", document.id, page.page_number)
        return created_fields

    if message.tool_calls:
        for tool_call in message.tool_calls:
            if tool_call.function.name == "save_extracted_fields":
                args = json.loads(tool_call.function.arguments)
                fields_data = args.get("fields", [])

                for field_data in fields_data:
                    unit = field_data.get("unit")
                    if unit == "":
                        unit = None

                    field = ExtractedField(
                        document_id=document.id,
                        field_name=field_data["field_name"],
                        display_name=field_data["display_name"],
                        raw_value=field_data["raw_value"],
                        unit=unit,
                        data_type=_parse_data_type(field_data.get("data_type", "text")),
                        section=field_data["section"],
                        confidence=field_data.get("confidence", 0.8),
                        status=FieldStatus.extracted,
                        citation_page=page.page_number,
                        citation_text=field_data.get("citation_text", ""),
                        citation_bbox=None,
                    )
                    db.add(field)
                    created_fields.append(field)

                logger.info(
                    "Extracted %d fields from doc %s page %d",
                    len(created_fields), document.id, page.page_number,
                )

    # Log token usage
    usage = response.usage
    logger.info(
        "LLM usage for doc %s page %d: input=%d, output=%d tokens",
        document.id, page.page_number,
        getattr(usage, 'prompt_tokens', 0),
        getattr(usage, 'completion_tokens', 0),
    )

    return created_fields


# ---------------------------------------------------------------------------
# Document-level extraction orchestrator
# ---------------------------------------------------------------------------

def _build_corrections_context(corrections: list[dict]) -> str:
    """Build a prompt section from past corrections."""
    if not corrections:
        return ""

    lines = ["\n\n## Past Corrections for This Document",
             "Apply these lessons to avoid the same mistakes:\n"]
    for c in corrections:
        line = f"- Field '{c['field_name']}': you extracted '{c['original_value']}'"
        if c['corrected_value'] != c['original_value']:
            line += f" but correct value is '{c['corrected_value']}'"
        if c.get('unit'):
            line += f" (unit: {c['unit']})"
        if c.get('reason'):
            line += f". Reason: {c['reason']}"
        if c.get('status') == 'rejected':
            line += " — this field was REJECTED (do not extract it again)"
        lines.append(line)

    return "\n".join(lines)


async def extract_document(
    document_id: uuid.UUID,
    db: AsyncSession,
    corrections_context: str = "",
) -> list[ExtractedField]:
    """Extract all fields from a document, page by page.

    Skips boilerplate pages. Updates document status.
    If corrections_context is provided, it's appended to the system prompt.
    Returns all created ExtractedField records.
    """
    document = await db.get(Document, document_id)
    if document is None:
        raise ValueError(f"Document {document_id} not found")

    if document.status not in (DocumentStatus.uploaded, DocumentStatus.failed, DocumentStatus.extracted):
        raise ValueError(f"Document {document_id} is in status '{document.status}', cannot extract")

    document.status = DocumentStatus.extracting
    await db.flush()

    try:
        stmt = (
            select(DocumentPage)
            .where(DocumentPage.document_id == document_id)
            .order_by(DocumentPage.page_number)
        )
        result = await db.execute(stmt)
        pages = result.scalars().all()

        all_fields: list[ExtractedField] = []

        for page in pages:
            if page.page_type == "boilerplate":
                logger.info("Skipping boilerplate page %d of doc %s", page.page_number, document_id)
                continue

            page_fields = await extract_page_fields(page, document, db, corrections_context)
            all_fields.extend(page_fields)

        document.status = DocumentStatus.extracted
        await db.flush()

        logger.info(
            "Extraction complete for doc %s: %d fields from %d content pages",
            document_id, len(all_fields), sum(1 for p in pages if p.page_type != "boilerplate"),
        )

        return all_fields

    except Exception:
        document.status = DocumentStatus.failed
        await db.flush()
        logger.exception("Extraction failed for document %s", document_id)
        raise
