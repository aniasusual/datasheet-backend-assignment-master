# Architecture: Datasheet Extraction System

## What This System Does

Takes industrial process datasheets (PDFs) — pumps, compressors, heat exchangers, any equipment type — and extracts every technical field into a structured, queryable database. Includes human-in-the-loop review, a conversational agent, and a feedback loop that improves extraction over time. No hardcoded field lists, no hardcoded document formats.

---

## System Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│                         FRONTEND (React)                             │
│                                                                      │
│   ┌──────────┐  ┌──────────────────┐  ┌────────────────────────┐    │
│   │ Document  │  │   PDF Viewer     │  │   Field Review Panel   │    │
│   │ Sidebar   │  │ (native browser) │  │   (verify/edit/reject) │    │
│   │           │  ├──────────────────┤  └────────────────────────┘    │
│   │ Upload    │  │   Agent Chat     │                                │
│   │ Extract   │  │ (query + HITL)   │                                │
│   └──────────┘  └──────────────────┘                                │
├──────────────────────────────────────────────────────────────────────┤
│                        BACKEND (FastAPI)                             │
│                                                                      │
│   ┌───────────┐  ┌──────────────┐  ┌────────────┐  ┌────────────┐  │
│   │ Ingestion │  │  Extraction  │  │   Agent    │  │    Gap     │  │
│   │ Service   │  │  (3-pass)    │  │  Service   │  │  Analysis  │  │
│   └───────────┘  └──────────────┘  └────────────┘  └────────────┘  │
├──────────────────────────────────────────────────────────────────────┤
│                      DATABASE (PostgreSQL)                            │
│                                                                      │
│   Sessions → Documents → DocumentPages                               │
│                  ↓                                                    │
│           ExtractedFields → FieldCorrections                         │
│                  ↓                                                    │
│          EquipmentEntities                                           │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Stage 1: Ingestion

**File:** `app/services/document_processor.py`
**Trigger:** `POST /sessions/{id}/documents/upload`
**LLM calls:** Zero

When a user uploads a PDF:

1. **Validate** — check magic bytes via `filetype` library (don't trust extensions alone)
2. **Save** — write PDF to `./uploads/{session_id}/filename.pdf`
3. **Create Document record** — status: `uploading`
4. **Process each page** with `pdfplumber`:
   - Extract raw text (plain text dump)
   - Extract layout text (preserves spatial positioning of text on the page)
   - Extract tables as JSON (rows × columns)
   - Measure page dimensions (width × height in PDF points)
   - Rate text quality: `full_text` / `partial_text` / `image_only`
5. **Update Document** — status: `uploaded`, set `num_pages`

### What ingestion does NOT do

- **No rendering to images.** The PDF file sits on disk untouched. The frontend renders it natively via the browser's built-in PDF viewer. The LLM receives raw PDF pages via PyMuPDF.
- **No OCR.** The vision LLM reads the PDF directly.
- **No classification.** No hardcoded keywords, no regex tag detection, no language detection. All classification is deferred to the extraction pipeline where the LLM can see the actual content.
- **No hardcoding of any kind.** The processor is format-agnostic — it works identically for pump datasheets, compressor specs, heat exchanger data, or any other PDF.

### Data Stored

**Document:**
- `id`, `session_id`, `filename`, `file_path` (to PDF on disk)
- `pump_tag` (set later by extraction, initially null)
- `format_type` (set later by extraction, initially null)
- `status` (uploading → uploaded → extracting → extracted → failed)
- `num_pages`

**DocumentPage** (one per page):
- `page_number`, `raw_text`, `layout_text`, `tables_json`
- `width`, `height` (in PDF points)
- `extraction_quality` (full_text / partial_text / image_only)

---

## Stage 2: Extraction (Three-Pass Pipeline)

**File:** `app/services/extraction.py`
**Trigger:** `POST /sessions/{id}/documents/{id}/extract` or `POST /sessions/{id}/documents/extract-all`
**LLM calls:** ~3 per page

Three LLM calls per page. Every page gets processed — there's no hardcoded "boilerplate" skipping. Pages with no fields simply produce zero extracted values naturally.

### Pass 1: Field Discovery + Document Metadata

**Question to LLM:** *"Here's the PDF page. What field labels exist? What's the equipment tag, type, and language?"*

```
Input:  Original PDF page (via PyMuPDF — native PDF, not a rendered image)
      + raw text from pdfplumber as supplementary context

Output: {
  equipment_tag: "P-718(A/B)",       // detected from page content
  equipment_type: "pump",             // could be compressor, heat_exchanger, etc.
  service_name: "DIESEL PRODUCT PUMPS",
  language: "english",                // or french, bilingual, other
  fields: [
    {label: "Normal Flowrate", has_value: true,  section: "operating_conditions"},
    {label: "Minimum Flowrate", has_value: true,  section: "operating_conditions"},
    {label: "Speed Range",      has_value: false, section: "pump_performance"},
    ...
  ]
}
```

This identifies the **document's own schema** — every field that exists on the page, including empty ones. No hardcoded field list, no hardcoded language detection, no regex tag patterns. The LLM reads the actual document content.

The `equipment_tag`, `equipment_type`, and `language` from the first page that has them are used to populate the Document record's `pump_tag` and `format_type` fields — replacing the old hardcoded regex + French keyword approach.

Fields with `has_value: false` are tracked — these are labels where the value cell is empty (e.g., vendor hasn't filled their part). This is different from "the LLM missed it."

### Pass 2: Guided Extraction

**Question to LLM:** *"Here's the PDF page. Extract values for these specific fields: [list from Pass 1]."*

```
Input:  PDF page
      + field list from Pass 1
      + raw text context
      + past corrections (if re-extracting)

Output: [
  {field_name: "normal_flowrate", display_name: "Normal Flowrate",
   raw_value: "928", unit: "GPM", confidence: 0.95,
   section: "operating_conditions", data_type: "numeric",
   citation_text: "Normal flowrate (11) GPM 928", note_refs: ["11"]},
  ...
]
```

This is **much more accurate** than the old single-pass approach because:
- The LLM knows exactly what to look for
- It doesn't have to simultaneously figure out layout AND extract values
- It can focus on precision — reading the right cell for the right label

### Pass 3: Verification

**Question to LLM:** *"Here are the extracted fields and the raw text. Does anything look wrong?"*

```
Input:  Extracted fields JSON + raw text + layout text
        (TEXT ONLY — no PDF page, so this call is cheap)

Output: {
  issues: [
    {field_name: "serial_no", issue_type: "misread",
     current_value: "0028", correct_value: "0025",
     explanation: "Text clearly shows 0025"},
    {field_name: "phantom_field", issue_type: "hallucinated",
     explanation: "This value doesn't appear in the text"},
  ],
  verified_count: 43
}
```

What happens with issues:
- **hallucinated** → field is dropped entirely
- **wrong_value / misread** → value corrected, confidence lowered
- **wrong_unit** → unit corrected
- **wrong_section** → confidence lowered, kept

### Why send PDF pages instead of rendered images?

The LLM (Gemini, Claude) accepts PDF input natively. Sending the original PDF page gives:
- **Full fidelity** — vector graphics, real fonts, no rendering artifacts
- **4x smaller payload** — ~120KB PDF vs ~420KB PNG per page
- **No dependencies** — no pdf2image, no poppler, no Pillow

PyMuPDF extracts individual pages as PDF bytes.

### Per-field data stored

**ExtractedField:**
- `field_name` (snake_case, e.g., "normal_flowrate")
- `display_name` (human-readable, e.g., "Normal Flowrate")
- `raw_value` (exactly as in document, e.g., "928")
- `unit` (separated from value, e.g., "GPM")
- `data_type` (numeric / text / boolean)
- `section` (one of 9 categories — see below)
- `confidence` (0.0-1.0, adjusted by verification pass)
- `status` (extracted / verified / corrected / rejected)
- `citation_page` (which page)
- `citation_text` (exact text snippet showing field + value)

### Field Sections

| Section | Examples |
|---------|----------|
| `general_info` | Equipment tag, service, type, driver, project metadata |
| `product_handled` | Fluid, temperature, viscosity, density, vapor pressure |
| `operating_conditions` | Flowrates, pressures (suction, discharge, differential) |
| `pump_performance` | Head, NPSH, shaft power, efficiency, speed |
| `construction_materials` | Impeller, casing, shaft materials |
| `mechanical_design` | Seals, bearings, couplings, nozzles |
| `motor_data` | Voltage, protection, frame, speed |
| `weights_dimensions` | Weight, base dimensions |
| `notes_remarks` | Footnotes, warnings, special conditions |

These sections are prompting guidelines — the LLM categorizes fields into whichever section fits. They're not enforced as a strict schema.

---

## Stage 3: Post-Processing

**File:** `app/services/post_processing.py`
**LLM calls:** 1 per document (after all pages extracted)

One LLM call per document handles cross-page concerns:

1. **Footnote resolution** — matches "(3)", "(7)" references to actual note text from notes pages
2. **Deduplication** — removes duplicate fields from repeated page headers
3. **Entity creation** — extracts pump tag, type, service name → creates `EquipmentEntity` record
4. **Field linking** — links all fields to the entity
5. **Cross-page validation** — flags contradictions between pages

### Equipment Entity

**EquipmentEntity:**
- `tag` (e.g., "P-718(A/B)")
- `entity_type` (e.g., "pump")
- `name` (e.g., "DIESEL PRODUCT PUMPS")
- `metadata_json` (project, area, unit, revision, footnotes)
- Linked to documents (many-to-many) and fields

---

## Stage 4: Gap Analysis

**File:** `app/services/gap_analysis.py`
**Trigger:** Automatically after extraction, or via agent tool

### Cross-Document Comparison (no hardcoding)

Instead of a hardcoded list of expected fields, the system compares what fields exist across all documents in the session:

- If 3 out of 4 documents have `suction_pressure` but one doesn't → that's a gap
- A field is "common" if it appears in >50% of documents
- Works for any document type — pumps, compressors, heat exchangers, whatever

### Gap Report Contains

1. **Failed pages** — pages with 0 extracted fields (LLM returned empty)
2. **Missing fields** — common fields present in other documents but absent from this one
3. **Low-confidence fields** — fields with confidence <70% that need human review

The report is formatted as readable markdown and injected into the agent chat automatically after extraction.

---

## HITL Feedback Loop

**Files:** `app/api/fields.py` (PATCH endpoint), `app/services/extraction.py` (_build_corrections_context)

### How corrections work

1. User reviews a field in the UI (or via agent)
2. User edits value/unit → `PATCH /fields/{id}`
3. System creates a `FieldCorrection` audit record:
   ```
   original_value: "2.7"
   corrected_value: "2.7"
   unit change: null → "kW"
   reason: "unit was missing from extraction"
   corrected_by: "user" (or "agent")
   ```
4. Field status changes to `corrected`

### How corrections improve re-extraction

When a user clicks "Re-extract":

1. System gathers all corrections + rejected fields for the document
2. Formats them as prompt context:
   ```
   Past Corrections — apply these lessons:
   - Field 'bhp_rated': extracted '2.7' → correct: '2.7' (unit: kW). unit was missing
   - Field 'corrosion_erosion': '(5)' — REJECTED, do not extract again
   ```
3. Deletes old extracted fields
4. Re-runs the three-pass extraction with corrections appended to the system prompt
5. The LLM learns from past mistakes — few-shot learning from corrections

No fine-tuning. No conversation memory. Just corrections from the DB injected into a fresh prompt each time.

---

## Conversational Agent

**File:** `app/services/agent.py`
**Trigger:** `POST /sessions/{id}/agent`

A tool-use agent that can query, edit, verify, and reject fields through natural language.

### Architecture

```
Frontend sends: {messages: [...history], message: "new message"}
    │
    ▼
Build prompt: system + conversation history + new message
    │
    ▼
LLM responds → text response OR tool calls
    │
    ├── Text → return to user
    └── Tool calls → execute → feed results back to LLM → loop
        (max 5 rounds)
```

### 8 Tools

| Tool | Purpose |
|------|---------|
| `get_session_overview` | Lists all documents, pages, statuses, field counts |
| `get_page_text` | Returns raw/layout text + tables for a specific page |
| `search_fields` | Search fields by pump tag, name, section, status, confidence |
| `get_field` | Single field detail with correction history |
| `update_field` | Edit value/unit, creates audit trail |
| `verify_fields` | Bulk verify fields |
| `reject_fields` | Bulk reject fields |
| `get_extraction_gaps` | Cross-document gap analysis |

### Context Management

**Frontend owns the conversation history.** The message array lives in React state and is sent with each request. The server is stateless — no DB message storage, no context compaction. If the conversation gets too long, the frontend can truncate old messages.

### Example Interactions

```
User: "What's the impeller material for P-300228?"
Agent: [search_fields(pump_tag="P-300228", field_name="impeller")]
       → "CS (Carbon Steel), confidence 0.9, from page 1."

User: "That's wrong, it should be SS316"
Agent: [update_field(field_id="...", raw_value="SS316", reason="user correction")]
       → "Corrected: Impeller Material updated from CS to SS316."

User: "Verify all product_handled fields above 0.9 confidence"
Agent: [search_fields(section="product_handled", min_confidence=0.9)]
       [verify_fields(field_ids=["...", "..."])]
       → "Verified 5 fields: Liquid, Density, Viscosity, Vapor Pressure, Specific Gravity."

User: "How complete is the extraction?"
Agent: [get_extraction_gaps()]
       → "P718 page 3 failed extraction. P600173 is missing 3 fields found in other docs..."
```

---

## Frontend

**Stack:** React 19, TypeScript, Tailwind CSS, Vite
**Theme:** Dark mode (`#0f1117` background)

### Layout: Three Panels

```
┌──────────┬───────────────────────┬────────────────────┐
│ Document │                       │  Extracted Fields   │
│ Sidebar  │     Center Panel      │  (Review Mode)      │
│          │                       │                     │
│ [docs]   │  Review: PDF Viewer   │  [general_info ▼]   │
│ [upload] │  Agent:  Chat UI      │    Service: PUMP... │
│ [extract]│                       │    Tag: P-718       │
│          │  ← mode toggle →      │  [product ▼]        │
│          │  [Review] [Agent]     │    Liquid: Hydro... │
└──────────┴───────────────────────┴────────────────────┘
```

### Components

**DocumentSidebar** (`components/DocumentSidebar.tsx`)
- Document list with status badges (color-coded)
- Upload PDF button
- "Extract All" button (triggers extraction + auto-sends report to agent)

**PageViewer** (`components/PageViewer.tsx`)
- Renders PDF natively via `<iframe>` with `#page=N` fragment
- Page navigation (prev/next)
- Zoom controls (50-200%)
- Citation text display when a field is selected

**FieldPanel** (`components/FieldPanel.tsx`)
- Fields grouped by section (collapsible)
- Per-field: name, value, unit, confidence badge, status badge
- Action buttons: Verify, Edit, Reject
- Inline editor: value, unit, reason → saves correction
- Filters: section, status
- Clicking a field jumps the PDF viewer to the citation page

**AgentChat** (`components/AgentChat.tsx`)
- Chat interface with user/assistant message bubbles
- Tool action badges (shows what the agent did: "Searched fields", "Verified 5 fields")
- Example queries as quick-start buttons
- Injection mode: extraction report appears automatically after extraction
- Auto-refreshes field panel when agent modifies fields

### Pages

**SessionsPage** — landing page, lists sessions, create/delete
**SessionDetailPage** — three-panel layout, mode toggle (Review/Agent), state management for selected doc/page/field

---

## API Endpoints

### Sessions
| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/v1/sessions` | Create session |
| GET | `/api/v1/sessions` | List sessions |
| GET | `/api/v1/sessions/{id}` | Session detail |
| DELETE | `/api/v1/sessions/{id}` | Delete session (cascade) |

### Documents
| Method | Path | Purpose |
|--------|------|---------|
| POST | `.../documents/upload` | Upload PDFs (triggers ingestion) |
| POST | `.../documents/{id}/extract` | Extract one document |
| POST | `.../documents/extract-all` | Extract all uploaded docs |
| POST | `.../documents/{id}/re-extract` | Re-extract with corrections |
| GET | `.../documents/extraction-report` | Formatted gap analysis |
| GET | `.../documents` | List documents |
| GET | `.../documents/{id}` | Document detail + pages |
| GET | `.../documents/{id}/pdf` | Serve original PDF |

### Fields
| Method | Path | Purpose |
|--------|------|---------|
| GET | `.../fields` | List fields (filters: doc, section, status, confidence, page) |
| GET | `.../fields/stats` | Extraction statistics |
| GET | `.../fields/{id}` | Field detail + corrections |
| PATCH | `.../fields/{id}` | Update field (creates correction) |
| POST | `.../fields/bulk-verify` | Verify multiple fields |

### Entities
| Method | Path | Purpose |
|--------|------|---------|
| GET | `.../entities` | List equipment entities |
| GET | `.../entities/{id}` | Entity detail + linked fields |

### Query & Agent
| Method | Path | Purpose |
|--------|------|---------|
| POST | `.../query` | Stateless single-shot query |
| POST | `.../agent` | Conversational agent with tools |

---

## Database Schema

```
sessions
  ├── documents
  │     ├── document_pages
  │     └── extracted_fields
  │           └── field_corrections
  ├── equipment_entities
  │     └── entity_documents (M2M → documents)
```

All primary keys are UUIDs. Timestamps use `server_default=func.now()`.

---

## Technology Stack

| Component | Technology | Why |
|-----------|-----------|-----|
| API | FastAPI (async) | Fast, auto-docs, async-native |
| Database | PostgreSQL + AsyncPG | JSONB support, async, production-ready |
| ORM | SQLAlchemy 2.0 (async) | Type-safe, mature |
| Migrations | Alembic | Standard for SQLAlchemy |
| PDF text | pdfplumber | Best Python PDF table/text extractor |
| PDF pages | PyMuPDF (fitz) | Extract individual pages as PDF bytes |
| LLM | litellm | Provider-agnostic (Gemini, OpenAI, Claude) |
| File validation | filetype | Magic byte detection |
| Frontend | React 19 + TypeScript | Type-safe, modern |
| Styling | Tailwind CSS v4 | Utility-first, dark theme |
| Build | Vite | Fast dev server + build |
| Icons | lucide-react | Clean, consistent icons |

---

## Dependencies Eliminated (vs original codebase)

| Removed | Replaced by |
|---------|-------------|
| Redis + Arq (job queue) | Synchronous extraction |
| pdf2image + poppler | PyMuPDF for LLM, browser for frontend |
| Pillow | Not needed — no image processing |
| LiteLLM agent framework | Simple tool-use loop |
| python-docx, openpyxl | PDF-only input |
| pytesseract + Tesseract | Vision LLM reads PDFs directly |

---

## Design Principles

1. **No hardcoding.** No hardcoded field lists, language keywords, tag patterns, or page classifications. The LLM sees the document and figures it out. The system works for any equipment type, any language, any company format.

2. **The document is the schema.** Pass 1 discovers what fields exist on each page. The document defines its own structure — we don't impose one.

3. **Separate identification from extraction.** Pass 1 finds what's there. Pass 2 reads the values. Pass 3 verifies. Each pass is focused on one job, which is more accurate than asking the LLM to do everything at once.

4. **Send the real document, not a degraded copy.** The LLM receives native PDF pages, not rendered PNG images. The browser renders PDFs natively, not pre-rendered thumbnails. No unnecessary transformations.

5. **HITL corrections feed forward.** Corrections aren't just stored — they're injected into re-extraction prompts so the LLM learns from mistakes without fine-tuning.

6. **Cross-document gap analysis over hardcoded checklists.** If most documents have a field but one doesn't, that's a gap. No maintained field registry needed.

---

## Cost Per Document

With Gemini 2.5 Flash:
- **Pass 1** (discovery + metadata): ~5K input + ~3K output tokens per page
- **Pass 2** (guided extraction): ~5K input + ~5K output tokens per page
- **Pass 3** (text verification): ~3K input + ~1K output tokens per page (no PDF, cheap)
- **Post-processing**: ~10K input + ~3K output tokens per document
- **Total per page**: ~13K input + ~9K output ≈ $0.01-0.03
- **Total per document (3 pages)**: ~$0.05-0.15

---

## Known Limitations

1. **Gemini Flash reliability** — returns empty responses ~20% of the time. Retry logic (3 attempts) mitigates but doesn't eliminate. Claude Sonnet or GPT-4o would fix this.
2. **No parallel page extraction** — pages are processed sequentially. Parallel LLM calls would cut time by 2-3x.
3. **Synchronous API** — extraction blocks the HTTP request for ~60s per document. Would need async job queue for production scale.
4. **No bounding box citations** — `citation_bbox` is always null. Would need LLM to return coordinates or a text-location matching step.
5. **Single-session gap analysis** — cross-document comparison only works within a session. Cross-session learning would need a global field registry.
6. **Local file storage** — PDFs stored on local disk. Would need S3 for horizontal scaling.
