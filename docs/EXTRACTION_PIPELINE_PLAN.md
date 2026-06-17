# Extraction Pipeline Redesign Plan

## Overview

Replace the current over-engineered agentic pipeline with a simple, deterministic, vision-first extraction system. The goal: PDF in, structured fields with citations out.

## Input Documents

4 pump process datasheets in 2 formats:

| Format | Documents | Pages | Characteristics |
|--------|-----------|-------|-----------------|
| English tabular | P718, P818 | 3-4 each | Spreadsheet grid, color-coded, numbered rows |
| French bilingual form | P300228, P600173 | 2-3 each | French/English labels, kg/cm² units, structured form |

## Architecture

```
PDF Upload
    │
    ▼
┌─────────────────────────────┐
│  STEP 1: INGESTION          │
│  (deterministic, no LLM)    │
│                             │
│  - Validate file (magic bytes)
│  - Render pages → PNG @300dpi
│  - Extract raw text (pdfplumber)
│  - Classify page type        │
│  - Store DocumentPage records │
└─────────────┬───────────────┘
              │
              ▼
┌─────────────────────────────┐
│  STEP 2: EXTRACTION         │
│  (1 LLM call per page)      │
│                             │
│  - Send page image + text   │
│  - Structured output (tool use)
│  - Returns field array      │
│  - Save ExtractedField rows │
└─────────────┬───────────────┘
              │
              ▼
┌─────────────────────────────┐
│  STEP 3: POST-PROCESSING    │
│  (1 LLM call per document)  │
│                             │
│  - Resolve footnote refs    │
│  - Deduplicate fields       │
│  - Create EquipmentEntity   │
│  - Link fields to entity    │
└─────────────────────────────┘
```

## Step 1: Ingestion (No LLM)

**Input:** PDF file upload
**Output:** DocumentPage records with images + text

### Process:
1. Validate uploaded file (extension + magic bytes via `filetype` lib)
2. Save PDF to `./uploads/{session_id}/`
3. Create `Document` record (status: `uploading` → `uploaded`)
4. For each page:
   - Render to PNG at **300 DPI** using `pdf2image` (poppler)
   - Extract raw text via `pdfplumber`
   - Extract layout-aware text via `pdfplumber` (preserves spatial info)
   - Extract tables via `pdfplumber`
   - Classify page: `content` | `boilerplate` (revision logs, hold lists with <30 chars)
   - Save PNG to `./rendered_pages/{document_id}/page_{N}.png`
   - Create `DocumentPage` record

### What changes from current code:
- Bump render DPI from 200 → 300 (small text in these forms)
- Use `pdf2image` instead of pdfplumber's built-in renderer (better quality)
- Add page classification (skip boilerplate pages in extraction)
- Drop DOCX/XLSX/image handling (input is PDF only per assignment)
- Drop OCR/Tesseract (vision model reads images directly)
- Drop LibreOffice dependency

## Step 2: Per-Page Extraction (1 LLM call per page)

**Input:** Page image (base64) + raw text + extraction schema
**Output:** Array of ExtractedField objects

### LLM Call Design:
- **Model:** Claude Sonnet 4 (vision + tool use)
- **Input:** System prompt + page image + raw text as supplementary context
- **Output mode:** Tool use — LLM calls a `save_fields` tool with structured JSON
- **One call per content page** — skip boilerplate pages

### System Prompt Contains:
- Field taxonomy (sections + known field names)
- Extraction rules (preserve exact values, separate units, cite everything)
- Confidence guidelines
- French/English field name mappings
- Few-shot examples from each format

### Output Schema Per Field:
```json
{
  "field_name": "normal_flowrate",
  "display_name": "Normal Flowrate",
  "raw_value": "928",
  "unit": "GPM",
  "section": "operating_conditions",
  "data_type": "numeric",
  "confidence": 0.95,
  "citation_text": "Normal flowrate (11) GPM 928",
  "citation_page": 3,
  "note_refs": ["11"]
}
```

### Sections:
1. `general_info` — pump tag, service, type, number, driver
2. `product_handled` — fluid, temperature, viscosity, density, gravity, vapor pressure
3. `operating_conditions` — flowrates (normal, min, design), pressures
4. `pump_performance` — head, NPSH, power, efficiency
5. `construction_materials` — impeller, casing, shaft materials
6. `mechanical_design` — seal type, bearing, coupling, nozzles
7. `motor_data` — voltage, protection, frame, speed
8. `weights_dimensions` — weight, base dimensions
9. `notes_remarks` — general notes, footnotes, warnings

## Step 3: Post-Processing (1 LLM call per document)

**Input:** All extracted fields + notes page text
**Output:** Cleaned, deduplicated fields + equipment entity

### Process:
1. Collect all fields from all pages of one document
2. Collect all note/footnote text from notes pages
3. Send to LLM with instructions to:
   - **Resolve footnotes:** Match `(3)`, `(7)` references to actual note text
   - **Deduplicate:** Remove duplicate fields from repeated page headers
   - **Validate:** Flag contradictions or suspicious values
   - **Entity extraction:** Identify pump tag, type, service name
4. Create `EquipmentEntity` record
5. Link all fields to entity
6. Update document status to `extracted`

## Database Models (Kept From Current)

- `Session` — groups documents
- `Document` — file metadata + status lifecycle
- `DocumentPage` — per-page text, image, classification
- `ExtractedField` — individual data points with citations
- `EquipmentEntity` — pump/motor entities
- `FieldCorrection` — audit trail for HITL corrections

## Models Dropped:
- `Message` — no conversational agent
- `CostRecord` — simplified cost tracking (log-based)
- `CorrectionPattern` — over-engineered for 4 docs
- `EntityRelationship` — not needed for this scope

## API Endpoints

### Keep:
- `POST /sessions` — create session
- `POST /sessions/{id}/documents/upload` — upload PDF
- `GET /sessions/{id}/documents` — list documents
- `GET /sessions/{id}/documents/{id}` — document detail
- `GET /sessions/{id}/documents/{id}/pages/{n}/image` — serve page PNG
- `GET /sessions/{id}/fields` — list extracted fields (with filters)
- `GET /sessions/{id}/entities` — list equipment entities

### Add:
- `POST /sessions/{id}/documents/{id}/extract` — trigger extraction for a document
- `GET /sessions/{id}/documents/{id}/extract/status` — extraction progress

### Drop:
- Chat endpoint (no conversational agent)
- WebSocket events
- Cost endpoints
- Correction patterns endpoint

## Cost Estimate

Per document (3-4 pages):
- Step 2: 3-4 LLM calls × ~$0.05 = ~$0.15-0.20
- Step 3: 1 LLM call × ~$0.05 = ~$0.05
- **Total: ~$0.20-0.25 per document, ~$1.00 for all 4**

## Implementation Order

1. **Ingestion pipeline** — PDF upload, page rendering, text extraction, DB storage
2. **Extraction pipeline** — LLM integration, per-page extraction, structured output
3. **Post-processing** — footnote resolution, dedup, entity creation
4. **API endpoints** — upload, extract trigger, field retrieval
5. **HITL corrections** — field editing, audit trail (phase 2)

## What Gets Deleted

- `app/agent/` — entire agent system (runner, context manager, cost tracker)
- `app/tools/` — agent tool definitions
- `app/prompts/` — agent prompts (replaced by extraction prompts)
- `app/api/chat.py` — conversational chat endpoint
- `app/api/events.py` — WebSocket events
- `app/api/costs.py` — cost tracking endpoints
- `app/services/document_processor.py` — replaced with new ingestion service
- Redis/Arq worker dependencies
