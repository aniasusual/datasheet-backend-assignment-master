"""Three-pass extraction pipeline.

Pass 1: FIELD DISCOVERY — identify all field labels on the page (including empty)
Pass 2: GUIDED EXTRACTION — extract values for each discovered field
Pass 3: VERIFICATION — cross-check extracted values against raw text
"""

import asyncio
import base64
import json
import logging
import uuid
from pathlib import Path

import fitz  # PyMuPDF
import litellm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.document import Document, DocumentStatus
from app.models.document_page import DocumentPage
from app.models.extracted_field import ExtractedField, FieldDataType, FieldStatus

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _extract_pdf_page(pdf_path: str, page_number: int) -> str:
    """Extract a single page from a PDF as base64 PDF bytes.

    page_number is 1-indexed (matching our DocumentPage convention).
    """
    doc = fitz.open(pdf_path)
    single = fitz.open()
    single.insert_pdf(doc, from_page=page_number - 1, to_page=page_number - 1)
    pdf_bytes = single.tobytes()
    single.close()
    doc.close()
    return base64.b64encode(pdf_bytes).decode("utf-8")


def _get_page_pdf_content(page: DocumentPage, document: Document) -> list[dict]:
    """Return PDF content block for a page. Sends the actual PDF page, not a rendered image."""
    try:
        pdf_b64 = _extract_pdf_page(document.file_path, page.page_number)
        return [{
            "type": "image_url",
            "image_url": {"url": f"data:application/pdf;base64,{pdf_b64}"},
        }]
    except Exception:
        logger.warning("Failed to extract PDF page %d from %s", page.page_number, document.file_path)
        return []


def _get_page_text_context(page: DocumentPage) -> str:
    """Return text context for a page."""
    parts = []
    if page.raw_text and page.raw_text.strip():
        parts.append(f"## Raw Text\n{page.raw_text}")
    if page.layout_text and page.layout_text.strip():
        parts.append(f"## Layout Text\n{page.layout_text}")
    if page.tables_json:
        parts.append(f"## Tables\n{json.dumps(page.tables_json)}")
    return "\n\n".join(parts) if parts else ""


async def _llm_call(messages: list[dict], tools: list[dict] | None = None,
                    tool_choice: dict | None = None, max_tokens: int = 8192) -> dict | None:
    """Make an LLM call with retry logic. Returns the message or None."""
    for attempt in range(MAX_ATTEMPTS):
        response = await litellm.acompletion(
            model=settings.LLM_MODEL,
            api_key=settings.LLM_API_KEY or settings.GEMINI_API_KEY,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=max_tokens,
        )

        if response.choices:
            msg = response.choices[0].message
            # If we need a tool call, check we got one
            if tool_choice and not msg.tool_calls:
                logger.warning("Attempt %d/%d: no tool call, retrying", attempt + 1, MAX_ATTEMPTS)
                if attempt < MAX_ATTEMPTS - 1:
                    await asyncio.sleep(2)
                continue
            usage = response.usage
            logger.info("LLM call: input=%d, output=%d tokens",
                        getattr(usage, 'prompt_tokens', 0),
                        getattr(usage, 'completion_tokens', 0))
            return msg

        logger.warning("Attempt %d/%d: empty response, retrying", attempt + 1, MAX_ATTEMPTS)
        if attempt < MAX_ATTEMPTS - 1:
            await asyncio.sleep(2)

    return None


def _parse_tool_args(message) -> dict:
    """Extract the first tool call's arguments from a message."""
    if message and message.tool_calls:
        for tc in message.tool_calls:
            try:
                return json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                pass
    return {}


# ===========================================================================
# PASS 1: FIELD DISCOVERY
# ===========================================================================

DISCOVERY_SYSTEM_PROMPT = """You are a document structure analyst for industrial/engineering datasheets.

Your job is to:
1. Classify what kind of page this is
2. Identify EVERY field label on the page — including fields where the value cell is empty

## Field Discovery
For each field on the page, report:
- label: the exact text of the field label as it appears
- has_value: whether the value cell has actual data (true) or is empty/blank/dash (false)
- section: categorize into one of the sections below
- location_hint: brief description of where on the page

## Document Metadata
If visible on this page, also report:
- equipment_tag: the equipment identifier (e.g., "P-718(A/B)", "HX-101", "C-450")
- equipment_type: what kind of equipment (e.g., "pump", "heat_exchanger", "compressor")
- service_name: the service description (e.g., "DIESEL PRODUCT PUMPS")
- language: primary language of the document ("english", "french", "bilingual", etc.)

## Sections
- general_info, product_handled, operating_conditions, pump_performance
- construction_materials, mechanical_design, motor_data, weights_dimensions, notes_remarks

## Rules
- Include EVERY field label, even if the value is empty
- For bilingual documents, use the English label when both languages are present
- Include notes/remarks section fields
- Do NOT include decorative text or repeated page headers
"""

DISCOVERY_TOOL = {
    "type": "function",
    "function": {
        "name": "report_page_analysis",
        "description": "Report page classification, document metadata, and all field labels found",
        "parameters": {
            "type": "object",
            "properties": {
                "equipment_tag": {
                    "type": "string",
                    "description": "Equipment identifier if visible (e.g., 'P-718(A/B)', 'HX-101'). Empty if not found.",
                },
                "equipment_type": {
                    "type": "string",
                    "description": "Type of equipment (e.g., 'pump', 'heat_exchanger', 'compressor'). Empty if not determinable.",
                },
                "service_name": {
                    "type": "string",
                    "description": "Service/description (e.g., 'DIESEL PRODUCT PUMPS'). Empty if not found.",
                },
                "language": {
                    "type": "string",
                    "enum": ["english", "french", "bilingual", "other"],
                    "description": "Primary language of the document content",
                },
                "fields": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "description": "Exact field label text"},
                            "has_value": {"type": "boolean", "description": "Whether the value cell has data"},
                            "section": {
                                "type": "string",
                                "enum": [
                                    "general_info", "product_handled", "operating_conditions",
                                    "pump_performance", "construction_materials", "mechanical_design",
                                    "motor_data", "weights_dimensions", "notes_remarks",
                                ],
                            },
                            "location_hint": {"type": "string", "description": "Where on the page"},
                        },
                        "required": ["label", "has_value", "section"],
                    },
                },
            },
            "required": ["fields"],
        },
    },
}


async def pass1_discover_fields(page: DocumentPage, document: Document) -> dict:
    """Pass 1: Discover all field labels on the page + classify page + extract metadata.

    Returns dict with: fields, equipment_tag, equipment_type, service_name, language
    """
    content = _get_page_pdf_content(page, document)
    text_ctx = _get_page_text_context(page)
    content.append({
        "type": "text",
        "text": (
            f"Analyze page {page.page_number} of '{document.filename}'.\n"
            f"1. Identify any equipment tag, type, and service name visible\n"
            f"2. List EVERY field label, including empty fields\n\n{text_ctx}"
        ),
    })

    msg = await _llm_call(
        messages=[
            {"role": "system", "content": DISCOVERY_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        tools=[DISCOVERY_TOOL],
        tool_choice={"type": "function", "function": {"name": "report_page_analysis"}},
        max_tokens=8192,
    )

    args = _parse_tool_args(msg)
    fields = args.get("fields", [])

    logger.info("Pass 1: page %d, %d fields, tag=%s",
                page.page_number, len(fields),
                args.get("equipment_tag", ""))

    return {
        "fields": fields,
        "equipment_tag": args.get("equipment_tag", ""),
        "equipment_type": args.get("equipment_type", ""),
        "service_name": args.get("service_name", ""),
        "language": args.get("language", ""),
    }


# ===========================================================================
# PASS 2: GUIDED EXTRACTION
# ===========================================================================

EXTRACTION_SYSTEM_PROMPT = """You are a precision data extractor for industrial process datasheets.

You will receive a page image and a LIST OF SPECIFIC FIELDS to extract. Extract the value for each field on the list.

## Rules
1. Preserve exact values — do not round, convert, or calculate
2. Separate units from values: "928 GPM" → value: "928", unit: "GPM"
3. For bilingual content, use English display names
4. Include footnote references in note_refs (e.g., "(3)" → note_refs: ["3"])
5. citation_text must be the exact text from the document showing label + value
6. For fields marked has_value: false (empty/blank/dash), still include them with raw_value set to "" (empty string). These represent fields that exist on the datasheet but have no value filled in — we need to track them.
7. Confidence: 0.9+ clearly readable, 0.7-0.9 partially obscured, <0.7 uncertain, 1.0 for empty fields (we are certain the field is empty)

## French→English mappings
DÉBIT→Flowrate, PRESSION→Pressure, ASPIRATION→Suction, REFOULEMENT→Discharge,
HAUTEUR MANO→Differential Head, MASSE VOL→Density, VISCOSITE→Viscosity,
TENSION DE VAPEUR→Vapor Pressure, ROUE→Impeller, CORPS→Inner Case,
ARBRE→Shaft, GARNITURE MECANIQUE→Mechanical Seal, PALIER→Bearing,
MOTEUR FOURNI PAR→Motor Supplied By, REMARQUES→Remarks
"""

EXTRACTION_TOOL = {
    "type": "function",
    "function": {
        "name": "save_extracted_fields",
        "description": "Save extracted field values. Call once with all fields.",
        "parameters": {
            "type": "object",
            "properties": {
                "fields": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field_name": {"type": "string", "description": "Normalized snake_case identifier"},
                            "display_name": {"type": "string", "description": "Human-readable name"},
                            "raw_value": {"type": "string", "description": "Exact value from document"},
                            "unit": {"type": "string", "description": "Unit of measurement, empty if none"},
                            "section": {
                                "type": "string",
                                "enum": [
                                    "general_info", "product_handled", "operating_conditions",
                                    "pump_performance", "construction_materials", "mechanical_design",
                                    "motor_data", "weights_dimensions", "notes_remarks",
                                ],
                            },
                            "data_type": {"type": "string", "enum": ["numeric", "text", "boolean"]},
                            "confidence": {"type": "number"},
                            "citation_text": {"type": "string"},
                            "note_refs": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["field_name", "display_name", "raw_value", "section", "data_type", "confidence", "citation_text"],
                    },
                },
            },
            "required": ["fields"],
        },
    },
}


async def pass2_extract_values(
    page: DocumentPage,
    document: Document,
    discovered_fields: list[dict],
    corrections_context: str = "",
) -> list[dict]:
    """Pass 2: Extract values for the discovered fields."""
    # Build the field list for the prompt — include ALL discovered fields, even empty ones
    if not discovered_fields:
        logger.info("Pass 2: no fields discovered on doc %s page %d", document.id, page.page_number)
        return []

    field_list = "\n".join(
        f"- {f['label']} (section: {f.get('section', 'unknown')}, location: {f.get('location_hint', 'unknown')}, has_value: {f.get('has_value', True)})"
        for f in discovered_fields
    )

    content = _get_page_pdf_content(page, document)
    text_ctx = _get_page_text_context(page)
    content.append({
        "type": "text",
        "text": (
            f"Extract values for these specific fields from page {page.page_number} "
            f"of '{document.filename}' (pump: {document.pump_tag or 'unknown'}):\n\n"
            f"{field_list}\n\n{text_ctx}"
        ),
    })

    system = EXTRACTION_SYSTEM_PROMPT + corrections_context

    msg = await _llm_call(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
        tools=[EXTRACTION_TOOL],
        tool_choice={"type": "function", "function": {"name": "save_extracted_fields"}},
        max_tokens=8192,
    )

    args = _parse_tool_args(msg)
    fields = args.get("fields", [])
    logger.info("Pass 2: extracted %d field values on doc %s page %d", len(fields), document.id, page.page_number)
    return fields


# ===========================================================================
# PASS 3: VERIFICATION
# ===========================================================================

VERIFICATION_SYSTEM_PROMPT = """You are a quality assurance specialist for extracted datasheet fields.

You will receive extracted fields and the raw text from the page. Your job is to verify each field's value against the text.

For each field, check:
1. Does the raw_value match what's in the text?
2. Is the unit correct and properly separated?
3. Is the field_name/display_name accurate for this value?
4. Is the section categorization correct?

Report issues only — fields that pass verification don't need to be listed.
"""

VERIFICATION_TOOL = {
    "type": "function",
    "function": {
        "name": "report_verification",
        "description": "Report verification results",
        "parameters": {
            "type": "object",
            "properties": {
                "issues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field_name": {"type": "string"},
                            "issue_type": {
                                "type": "string",
                                "enum": ["wrong_value", "wrong_unit", "wrong_section", "hallucinated", "misread"],
                            },
                            "current_value": {"type": "string"},
                            "correct_value": {"type": "string"},
                            "current_unit": {"type": "string"},
                            "correct_unit": {"type": "string"},
                            "explanation": {"type": "string"},
                        },
                        "required": ["field_name", "issue_type", "explanation"],
                    },
                },
                "verified_count": {"type": "integer", "description": "Number of fields that passed verification"},
            },
            "required": ["issues", "verified_count"],
        },
    },
}


async def pass3_verify(
    page: DocumentPage,
    extracted_fields: list[dict],
) -> list[dict]:
    """Pass 3: Verify extracted values against raw text. Text-only, no image (cheap)."""
    if not extracted_fields or not page.raw_text.strip():
        return []

    fields_json = json.dumps(extracted_fields, indent=2)

    msg = await _llm_call(
        messages=[
            {"role": "system", "content": VERIFICATION_SYSTEM_PROMPT},
            {"role": "user", "content": (
                f"## Extracted Fields\n{fields_json}\n\n"
                f"## Raw Text from Page {page.page_number}\n{page.raw_text}\n\n"
                f"## Layout Text\n{page.layout_text or '(not available)'}\n\n"
                "Verify each field against the text. Report any mismatches."
            )},
        ],
        tools=[VERIFICATION_TOOL],
        tool_choice={"type": "function", "function": {"name": "report_verification"}},
        max_tokens=4096,
    )

    args = _parse_tool_args(msg)
    issues = args.get("issues", [])
    verified = args.get("verified_count", 0)
    logger.info("Pass 3: %d issues found, %d verified on doc page %d", len(issues), verified, page.page_number)
    return issues


# ===========================================================================
# Apply verification fixes
# ===========================================================================

def _apply_verification(extracted: list[dict], issues: list[dict]) -> list[dict]:
    """Apply verification fixes to extracted fields."""
    issue_map = {}
    for issue in issues:
        name = issue.get("field_name", "")
        issue_map[name] = issue

    result = []
    for field in extracted:
        fname = field.get("field_name", "")
        issue = issue_map.get(fname)

        if issue:
            itype = issue.get("issue_type", "")

            if itype == "hallucinated":
                logger.info("Removing hallucinated field: %s", fname)
                continue  # drop it

            if itype in ("wrong_value", "misread") and issue.get("correct_value"):
                field["raw_value"] = issue["correct_value"]
                field["confidence"] = min(field.get("confidence", 0.8), 0.75)
                logger.info("Corrected value for %s: %s → %s", fname, issue.get("current_value"), issue["correct_value"])

            if itype == "wrong_unit" and issue.get("correct_unit"):
                field["unit"] = issue["correct_unit"]
                logger.info("Corrected unit for %s: %s → %s", fname, issue.get("current_unit"), issue["correct_unit"])

            if itype == "wrong_section":
                # Lower confidence but keep it
                field["confidence"] = min(field.get("confidence", 0.8), 0.7)

        result.append(field)

    return result


# ===========================================================================
# Parse helpers
# ===========================================================================

def _parse_data_type(dt: str) -> FieldDataType:
    return {"numeric": FieldDataType.numeric, "text": FieldDataType.text,
            "boolean": FieldDataType.boolean}.get(dt, FieldDataType.text)


# ===========================================================================
# Page-level orchestrator (all 3 passes)
# ===========================================================================

async def extract_page(
    page: DocumentPage,
    document: Document,
    db: AsyncSession,
    corrections_context: str = "",
    on_phase: object = None,
) -> tuple[list[ExtractedField], dict]:
    """Run all 3 passes on a page. Returns (created fields, discovery result).

    Discovery result contains: fields, equipment_tag, equipment_type, etc.
    If Pass 1 discovers no fields with values, Pass 2 naturally returns empty.
    on_phase: optional callable(phase_str) to report progress.
    """
    _report = on_phase if callable(on_phase) else (lambda p: None)

    # Pass 1: Discover fields + extract metadata
    _report("discovery")
    discovery = await pass1_discover_fields(page, document)

    # Pass 2: Extract values (using discovered fields)
    _report("extracting_values")
    raw_extracted = await pass2_extract_values(page, document, discovery["fields"], corrections_context)

    # Pass 3: Verify (only if we have text to check against)
    if raw_extracted and page.raw_text and page.raw_text.strip():
        _report("verifying")
        issues = await pass3_verify(page, raw_extracted)
        final_fields = _apply_verification(raw_extracted, issues)
    else:
        final_fields = raw_extracted

    # Save to DB — truncate strings to fit column limits
    created: list[ExtractedField] = []
    for field_data in final_fields:
        unit = field_data.get("unit")
        if unit == "":
            unit = None
        if unit and len(unit) > 50:
            unit = unit[:50]

        field = ExtractedField(
            document_id=document.id,
            field_name=field_data.get("field_name", "unknown")[:255],
            display_name=field_data.get("display_name", "Unknown")[:255],
            raw_value=field_data.get("raw_value", ""),
            unit=unit,
            data_type=_parse_data_type(field_data.get("data_type", "text")),
            section=field_data.get("section", "general_info")[:100],
            confidence=field_data.get("confidence", 0.8),
            status=FieldStatus.extracted,
            citation_page=page.page_number,
            citation_text=field_data.get("citation_text", ""),
            citation_bbox=None,
        )
        db.add(field)
        created.append(field)

    logger.info(
        "Page %d complete: %d discovered, %d extracted, %d saved",
        page.page_number, len(discovery["fields"]), len(raw_extracted), len(created),
    )

    return created, discovery


# ===========================================================================
# Document-level orchestrator
# ===========================================================================

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


async def extract_document(
    document_id: uuid.UUID,
    db: AsyncSession,
    corrections_context: str = "",
    on_progress: object = None,
) -> list[ExtractedField]:
    """Extract all fields from a document using the three-pass pipeline.

    Returns all created ExtractedField records.
    Also stores discovered field labels in document metadata for gap analysis.
    on_progress: optional callable(page_number, phase_str) for progress reporting.
    """
    _report = on_progress if callable(on_progress) else (lambda p, ph: None)

    document = await db.get(Document, document_id)
    if document is None:
        raise ValueError(f"Document {document_id} not found")

    if document.status not in (DocumentStatus.uploaded, DocumentStatus.failed, DocumentStatus.extracted):
        raise ValueError(f"Document {document_id} status '{document.status}' — cannot extract")

    document.status = DocumentStatus.extracting
    await db.flush()

    try:
        stmt = (
            select(DocumentPage)
            .where(DocumentPage.document_id == document_id)
            .order_by(DocumentPage.page_number)
        )
        pages = (await db.execute(stmt)).scalars().all()

        all_fields: list[ExtractedField] = []
        all_discoveries: list[dict] = []

        for page in pages:
            page_phase_cb = lambda phase, _pn=page.page_number: _report(_pn, phase)
            created, discovery = await extract_page(page, document, db, corrections_context, on_phase=page_phase_cb)
            all_fields.extend(created)
            all_discoveries.append(discovery)

            # Update document metadata from first content page that has it
            if not document.pump_tag and discovery.get("equipment_tag"):
                document.pump_tag = discovery["equipment_tag"][:100]
            if not document.format_type and discovery.get("language"):
                lang = discovery["language"]
                if lang in ("french", "bilingual"):
                    document.format_type = "french_form"
                elif lang == "english":
                    document.format_type = "english_tabular"
                else:
                    document.format_type = lang

        document.status = DocumentStatus.extracted
        await db.flush()

        logger.info(
            "Extraction complete for doc %s: %d fields from %d pages, tag=%s",
            document_id, len(all_fields), len(pages), document.pump_tag,
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
