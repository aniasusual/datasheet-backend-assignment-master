# Architecture: Data Ingestion & Extraction Pipeline

## Overview

This system extracts structured technical fields from industrial pump process datasheets (PDFs). It takes a PDF in and produces a normalized, queryable set of fields with citations, confidence scores, and equipment entity linkage.

The pipeline has three stages:
1. **Ingestion** — deterministic PDF processing (no LLM)
2. **Extraction** — per-page vision LLM calls with structured output
3. **Post-processing** — cross-page dedup, footnote resolution, entity creation

---

## Input Documents

We handle 4 pump process datasheets in 2 distinct formats:

### English Tabular (P718, P818)
- 3-4 pages each
- Page 1: revision modification log + hold list (boilerplate — no technical data)
- Page 2: main data page — spreadsheet-style grid with numbered rows, color-coded cells (green = vendor, blue = client, red = warnings)
- Page 3-4: general notes, footnoted remarks, off-spec operating conditions table
- Units: imperial (GPM, psig, °F, hp, ft)
- Row labels map to specific pump parameters (Product Handled, Pump, Driver)

### French Bilingual Form (P300228, P600173)
- 2-3 pages each
- No boilerplate pages — all pages contain data
- Structured form with French primary labels and English subtitles
- Units: metric (m³/h, kg/cm², °C, kW)
- Sections: Operating Conditions, Construction & Materials, Motor, Remarks
- Some pages are image-only (P600173) — pdfplumber extracts 0 chars

### Key Challenges
- **Spatial layout encodes meaning**: a value's meaning comes from its row label + column header, not just the text
- **Color coding carries semantic info**: not accessible via text extraction
- **Footnote cross-references**: values like "(3)" or "(7)" reference notes on later pages
- **Bilingual fields**: same field has French and English names
- **Image-only pages**: some PDFs have text embedded as images, not as selectable text

---

## Stage 1: Ingestion

**File**: `app/services/document_processor.py`
**Input**: PDF file upload
**Output**: `Document` + `DocumentPage` records in PostgreSQL
**LLM calls**: Zero — purely deterministic

### Process

```
PDF Upload
  → Validate (magic bytes via `filetype` lib — don't trust extensions alone)
  → Save to ./uploads/{session_id}/
  → Create Document record (status: uploading)
  → For each page:
      → Render to PNG at 300 DPI (pdf2image / poppler)
      → Extract raw text (pdfplumber)
      → Extract layout-aware text (pdfplumber, preserves spatial positioning)
      → Extract tables as JSON (pdfplumber)
      → Classify page: content vs boilerplate
      → Determine extraction quality: full_text / partial_text / image_only
      → Save PNG to ./rendered_pages/{document_id}/page_N.png
      → Create DocumentPage record
  → Detect pump tag from filename/content (regex)
  → Detect format type: french_form vs english_tabular (keyword counting)
  → Update Document status to 'uploaded'
```

### Design Decisions

**Why pdf2image instead of pdfplumber's built-in renderer?**
pdfplumber renders via `wand`/ImageMagick which can be inconsistent. `pdf2image` wraps `poppler` (pdftoppm) which is the gold standard for PDF rendering — sharper output, more reliable, better font handling.

**Why 300 DPI?**
These datasheets have small text in dense grid cells. 200 DPI (the previous setting) loses detail in small fonts. 300 DPI gives 2550×3300px per page — large enough for the LLM vision model to read every value clearly. Higher DPI (400+) would increase file sizes and LLM token costs without meaningful quality gain.

**Why keep both text and images?**
Text extraction is fast and cheap but lossy — it doesn't capture spatial layout, color coding, or text-in-images. The rendered PNG captures everything but costs more LLM tokens. We provide both: the image is the primary extraction input, the text is supplementary context that helps the LLM when image quality is ambiguous.

**Why classify pages as content vs boilerplate?**
Pages like revision logs and hold lists contain no technical data. Skipping them saves one LLM call per boilerplate page (~30s and ~5K tokens). Classification is simple: if a page contains "REVISION MODIFICATION LOG" or "HOLD LIST" and little other text, it's boilerplate.

**Why extract pump tag during ingestion?**
The tag (e.g., "P-718") is used to provide context to the LLM during extraction. Having it early means the extraction prompt can say "this is pump P-718" rather than making the LLM figure it out. Tags are extracted from filenames via regex (`pds-P718.pdf` → `P-718`), with a fallback to content scanning.

**Why detect format type?**
Knowing whether a document is `french_form` or `english_tabular` helps the LLM apply the right extraction strategy. French forms need bilingual field mapping; English tabular forms have different layout conventions. We detect this by counting French keywords (PRESSION, DÉBIT, ASPIRATION, etc.) — ≥3 hits = French form.

**Why PDF-only (dropped DOCX/XLSX/image support)?**
The assignment specifies PDF datasheets. Supporting DOCX/XLSX added LibreOffice as a system dependency (fragile in containers) and complex fallback rendering code. Removing it eliminated ~200 lines of code and one system dependency.

**Why no OCR?**
The previous design used Tesseract OCR as a fallback when text extraction failed. We dropped it because the vision LLM reads images directly — it is a better OCR than Tesseract for structured forms. This eliminated the pytesseract dependency and its Tesseract system requirement.

### Data Model

**Document** (`app/models/document.py`)
```
id: UUID (PK)
session_id: UUID (FK → sessions)
filename: str — original filename
file_path: str — path on disk
pump_tag: str | null — e.g. "P-718", extracted from filename/content
format_type: str | null — "french_form" or "english_tabular"
status: enum — uploading → uploaded → extracting → extracted → failed
num_pages: int
```

**DocumentPage** (`app/models/document_page.py`)
```
id: UUID (PK)
document_id: UUID (FK → documents)
page_number: int (1-indexed)
raw_text: text — pdfplumber extracted text
layout_text: text | null — spatial-aware text extraction
tables_json: JSONB | null — parsed tables
image_path: str — relative path to rendered PNG
width: float — page width in points
height: float — page height in points
extraction_quality: enum — full_text | partial_text | image_only
page_type: str — "content" or "boilerplate"
```

---

## Stage 2: Extraction

**File**: `app/services/extraction.py`
**Input**: DocumentPage records with images + text
**Output**: `ExtractedField` records
**LLM calls**: One per content page

### Process

```
For each content page (skip boilerplate):
  → Load page image from disk
  → Resize to max 2048px on long side (LANCZOS downscale)
  → Base64-encode the resized image
  → Build prompt: system prompt + [image, text context, instruction]
  → Call LLM with tool_choice forcing "save_extracted_fields"
  → Parse tool call response
  → Create ExtractedField records in DB
  → Retry up to 3 times if response is empty or has no tool call
```

### LLM Call Design

**Model**: Configurable via `LLM_MODEL` env var (currently `gemini/gemini-2.5-flash` via litellm)

**Input to LLM**:
1. System prompt — field taxonomy, extraction rules, bilingual mappings, confidence guidelines
2. Page image — base64 PNG, resized to max 2048px
3. Raw text — supplementary context from pdfplumber
4. Layout text — spatial-aware text when available
5. Parsed tables — JSON from pdfplumber table extraction
6. Instruction — "Extract all fields from page N of document X (pump tag: Y)"

**Output from LLM**: A single tool call `save_extracted_fields` with an array of field objects.

**Why tool use instead of JSON mode?**
Tool use (function calling) gives us a validated schema — the LLM must return fields matching our exact type definitions. JSON mode can produce arbitrary structures. Tool use also works consistently across providers (OpenAI, Anthropic, Google) via litellm.

**Why force tool_choice?**
Without forced tool choice, the model sometimes returns a text explanation instead of calling the tool. Forcing `tool_choice={"type": "function", "function": {"name": "save_extracted_fields"}}` ensures we always get structured output. However, Gemini Flash sometimes returns empty responses even with forced tool choice — hence the retry logic.

### Image Resizing

**Why resize at all?**
The 300 DPI PNGs are 2550×3300 pixels (~8.4M pixels). This is a large payload that:
- Consumes more LLM input tokens (each image costs tokens proportional to its size)
- Increases latency
- Can trigger model instability (Gemini Flash intermittently returns empty responses on very large images)

**Why 2048px max?**
We tested three sizes:
- **No resize (2550×3300)**: Best accuracy when it works, but Gemini returns empty responses ~30% of the time
- **1600px**: Too aggressive — French forms with small text lost quality, page 1 of P300228 failed
- **2048px**: Sweet spot — all text remains readable, payload reduced ~40%, Gemini reliability improved

The original 300 DPI images are kept on disk for the HITL interface. Only the LLM sees the resized version.

### Retry Logic

Gemini Flash intermittently returns empty `choices` arrays or responses without tool calls. This is a known model behavior, not a code bug — the same page succeeds on retry.

Our strategy:
- Up to 3 attempts per page
- 2 second delay between retries
- Log each failed attempt
- If all 3 fail, return empty fields for that page (the page can be re-extracted later)

In testing, this recovers ~80% of initially-failed pages.

### System Prompt

The system prompt (`EXTRACTION_SYSTEM_PROMPT` in `extraction.py`) defines:

**Field taxonomy** — 9 sections:
1. `general_info` — pump tag, service, type, driver, project metadata
2. `product_handled` — fluid, temperature, viscosity, density, vapor pressure
3. `operating_conditions` — flowrates, pressures
4. `pump_performance` — head, NPSH, power, efficiency
5. `construction_materials` — impeller, casing, shaft materials
6. `mechanical_design` — seals, bearings, couplings, nozzles
7. `motor_data` — voltage, protection, frame
8. `weights_dimensions` — weight, base dimensions
9. `notes_remarks` — footnotes, warnings, off-spec conditions

**Extraction rules**:
- Preserve exact values (no rounding, no unit conversion)
- Separate units from values ("928 GPM" → raw_value: "928", unit: "GPM")
- Use English display names for bilingual fields
- Include footnote references in `note_refs`
- Extract multiple values as separate fields (normal vs design flowrate)
- Skip empty/blank fields
- Include citation text showing where the value was found

**Confidence scoring**:
- 0.9-1.0: Clearly readable, unambiguous
- 0.7-0.89: Partially obscured or slightly ambiguous
- 0.5-0.69: Difficult to read, contextual guess
- Below 0.5: Very uncertain

**Bilingual mapping**: 20+ French→English field name translations (DÉBIT→Flowrate, PRESSION→Pressure, etc.)

### Data Model

**ExtractedField** (`app/models/extracted_field.py`)
```
id: UUID (PK)
document_id: UUID (FK → documents)
entity_id: UUID | null (FK → equipment_entities, set during post-processing)
field_name: str — normalized snake_case, e.g. "normal_flowrate"
display_name: str — human-readable, e.g. "Normal Flowrate"
raw_value: str — exactly as in document
unit: str | null — separated from value, e.g. "GPM"
data_type: enum — numeric | text | boolean
section: str — one of the 9 categories
confidence: float — 0.0 to 1.0
status: enum — extracted | verified | corrected | rejected
citation_page: int — which page the value was found on
citation_text: str — text snippet showing field + value
citation_bbox: JSONB | null — bounding box coordinates (not yet populated)
```

---

## Stage 3: Post-Processing

**File**: `app/services/post_processing.py`
**Input**: All ExtractedField records for a document + page text
**Output**: EquipmentEntity + deduplicated/updated fields
**LLM calls**: One per document

### Process

```
After all pages of a document are extracted:
  → Load all ExtractedField records
  → Load all page text (for footnote context)
  → Send to LLM as JSON summary + raw text
  → LLM calls post_process_results tool with:
      → entity: {tag, type, name, metadata}
      → footnote_resolutions: [{note_number, note_text}]
      → duplicate_field_ids: [field IDs to remove]
      → field_updates: [{field_id, updated_value, reason}]
  → Create EquipmentEntity record
  → Link entity to document (M2M via entity_documents table)
  → Link all fields to entity (set entity_id)
  → Delete duplicate fields
  → Apply field value updates
  → Store resolved footnotes in entity metadata
```

### Design Decisions

**Why a separate post-processing step?**
Per-page extraction can't resolve cross-page relationships:
- Footnotes: "(3)" on page 2 references note text on page 4
- Duplicates: page headers repeat pump tag/service on every page
- Entity: pump tag, type, and service name appear across multiple pages

**Why one LLM call for all of this?**
These tasks are interdependent — dedup needs to see all fields, footnote resolution needs all text, entity creation needs the full picture. One call with full context is more reliable than multiple narrow calls.

**Why store footnotes in entity metadata?**
Footnotes are document-level context, not field-level. Storing them in the entity's `metadata_json` keeps them accessible without adding a separate table. A dedicated footnotes table would be premature for 4 documents.

---

## API Endpoints

**File**: `app/api/documents.py`, `app/api/sessions.py`, `app/api/fields.py`, `app/api/entities.py`

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/v1/sessions` | Create a new session |
| GET | `/api/v1/sessions` | List all sessions |
| GET | `/api/v1/sessions/{id}` | Session detail with counts |
| DELETE | `/api/v1/sessions/{id}` | Delete session + cascade |
| POST | `/api/v1/sessions/{id}/documents/upload` | Upload PDFs (triggers ingestion) |
| POST | `/api/v1/sessions/{id}/documents/{id}/extract` | Extract one document |
| POST | `/api/v1/sessions/{id}/documents/extract-all` | Extract all uploaded documents |
| GET | `/api/v1/sessions/{id}/documents` | List documents |
| GET | `/api/v1/sessions/{id}/documents/{id}` | Document detail + pages |
| GET | `/api/v1/sessions/{id}/documents/{id}/pages/{n}/image` | Serve page PNG |
| GET | `/api/v1/sessions/{id}/fields` | List fields with filters |
| GET | `/api/v1/sessions/{id}/fields/stats` | Extraction statistics |
| GET | `/api/v1/sessions/{id}/fields/{id}` | Field detail + corrections |
| GET | `/api/v1/sessions/{id}/entities` | List equipment entities |
| GET | `/api/v1/sessions/{id}/entities/{id}` | Entity detail + fields |

### Field Filtering

`GET /fields` supports:
- `document_id` — filter by document
- `section` — filter by category (e.g., `operating_conditions`)
- `status` — filter by status (extracted, verified, corrected, rejected)
- `min_confidence` — minimum confidence threshold
- `field_name` — substring search on field name
- `limit` / `offset` — pagination

---

## What Was Removed (and Why)

### Agent System (deleted: `app/agent/`, `app/tools/`, `app/prompts/`)
The previous design used a 3-tier agent hierarchy (orchestrator → extraction sub-agent → validation sub-agent) with an iterative loop of up to 30 LLM calls per request. This was overengineered for structured extraction:
- Non-deterministic cost (could use 5 or 50 LLM calls for the same document)
- Complex context management (compaction, budget guards, token tracking)
- Debugging difficulty (which of 30 iterations went wrong?)

Our replacement: 1 LLM call per page + 1 post-processing call. Predictable, debuggable, cheaper.

### Redis/Arq Job Queue (deleted: `app/worker.py`)
The previous design enqueued extraction as async jobs via Redis/Arq. This added Redis as an infrastructure dependency and required a separate worker process. For 4 documents at ~60s each, synchronous extraction in the API request is simpler and sufficient.

### Chat/Conversation System (deleted: `app/api/chat.py`, `app/models/message.py`)
The previous design had a conversational chat interface where users typed "extract all fields" and an agent responded. This is unnecessary — extraction is triggered by a POST endpoint, not a conversation.

### Cost Tracking (deleted: `app/models/cost_record.py`, `app/api/costs.py`)
With predictable LLM calls (1 per page), cost is easy to calculate from token counts in the logs. A separate cost tracking database table with per-call records was overhead.

### OCR/Tesseract (removed from pipeline)
The vision LLM reads images directly — it is a better OCR than Tesseract for structured forms with complex layouts.

### DOCX/XLSX Support (removed)
Not needed for this assignment (PDF only). Eliminated LibreOffice system dependency.

### Correction Patterns (deleted: `app/models/correction_pattern.py`)
Global learning across sessions from recurring corrections. Premature optimization for 4 documents.

### Entity Relationships (deleted: `app/models/entity_relationship.py`)
Modeling relationships between entities (pump-to-motor sibling links). Not needed when each document maps to one pump entity.

---

## Technology Stack

| Component | Technology | Why |
|-----------|-----------|-----|
| API | FastAPI | Async, fast, auto-docs |
| Database | PostgreSQL + AsyncPG | Async, JSONB support, production-ready |
| ORM | SQLAlchemy 2.0 (async) | Type-safe, mature |
| Migrations | Alembic | Standard for SQLAlchemy |
| PDF text extraction | pdfplumber | Best Python PDF table/text extractor |
| PDF rendering | pdf2image (poppler) | Gold standard for PDF→PNG |
| Image processing | Pillow | Resize before LLM, standard |
| LLM | litellm | Provider-agnostic (Gemini, OpenAI, Anthropic) |
| File validation | filetype | Magic byte detection |

---

## Test Results

Tested on all 4 datasheets with Gemini 2.5 Flash, 2048px image resize, 3 retries:

| Document | Format | Pages | Content Pages | Fields | Time |
|----------|--------|-------|---------------|--------|------|
| pds-P300228 | French form | 2 | 2 | 84 | ~65s |
| pds-P600173 | French form (image-only) | 2 | 2 | 65 | ~55s |
| pds-P718 | English tabular | 3 | 2 | 47-95* | ~65s |
| pds-P818 | English tabular | 3 | 2 | 79-98* | ~70s |

*Range reflects Gemini Flash's intermittent tool-call failures. When all pages succeed, field counts are 80-98. When a page fails after 3 retries, those fields are lost. This is a model reliability issue, not a pipeline issue — switching to Claude Sonnet or GPT-4o would eliminate it.

### LLM Cost Per Document
- ~7K input tokens per page (image + text + prompt)
- ~5K output tokens per page (tool call with fields)
- **Total per document: ~25-35K tokens (~$0.01-0.02 with Gemini Flash)**
- **Total for all 4 documents: ~$0.05-0.08**

---

## Known Limitations

1. **Gemini Flash reliability**: Returns empty responses ~20-30% of the time on complex pages with tool use. Retry logic mitigates but doesn't eliminate this. A more reliable model (Claude Sonnet, GPT-4o) would fix it.

2. **No bounding box citations**: `citation_bbox` is always null. Would need the LLM to return pixel coordinates, or a separate text-location matching step against the PDF layout.

3. **Duplicate fields across pages**: Page headers (pump tag, service) appear on every page and get extracted multiple times. Post-processing handles dedup, but it's LLM-dependent.

4. **No parallel extraction**: Pages are processed sequentially. Parallel LLM calls per page would cut time by 2-3x but risks rate limiting.

5. **Synchronous API**: Extraction blocks the HTTP request for ~60s per document. Fine for 4 documents, but would need async job queue for production scale.

6. **Local file storage**: Uploads and rendered pages are on local disk. Would need object storage (S3) for horizontal scaling.
