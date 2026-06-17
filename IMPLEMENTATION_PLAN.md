# Implementation Plan: Datasheet Extraction System

Phased build plan derived from `ARCHITECTURE.md`, `AGENT_DESIGN.md`, and `HLD.md`.
Each phase produces a working, testable increment.

---

## Phase 1: Project Skeleton & Database Foundation

### 1.1 Project Setup

- [x] Initialize Python project with `pyproject.toml` (dependencies: fastapi, uvicorn, sqlalchemy, asyncpg, pdfplumber, Pillow, litellm, arq, redis, python-dotenv, alembic, pydantic)
- [x] Create directory structure:
  ```
  app/
    __init__.py
    main.py              # FastAPI app factory
    config.py            # Settings via pydantic-settings (env vars)
    database.py          # Async SQLAlchemy engine, session factory
  app/models/            # SQLAlchemy ORM models
  app/schemas/           # Pydantic request/response schemas
  app/api/               # FastAPI routers
  app/agent/             # Agent engine
  app/tools/             # Agent tool implementations
  app/services/          # Business logic (PDF processor, etc.)
  app/prompts/           # System prompt templates
  alembic/               # DB migrations
  tests/
  uploads/               # Uploaded PDFs (gitignored)
  rendered_pages/        # Rendered PNGs (gitignored)
  ```
- [x] Create `.env.example` with all required env vars (DATABASE_URL, REDIS_URL, LLM_MODEL, LLM_API_KEY)
- [x] Create `.gitignore` (uploads/, rendered_pages/, .env, **pycache**, *.pyc)
- [x] Set up `alembic.ini` and `alembic/env.py` for async migrations

### 1.2 Configuration

- [x] `app/config.py` — Pydantic Settings class:
  - `DATABASE_URL` (PostgreSQL async URL)
  - `REDIS_URL`
  - `LLM_MODEL` (e.g. `gemini/gemini-2.0-flash` or `claude-sonnet-4-20250514`)
  - `LLM_API_KEY`
  - `UPLOAD_DIR` (default `./uploads`)
  - `RENDERED_PAGES_DIR` (default `./rendered_pages`)
  - Budget defaults: `MAX_ITERATIONS_PER_REQUEST=30`, `MAX_TOKENS_PER_REQUEST=50000`, `MAX_TOKENS_PER_SESSION=500000`, `MAX_COST_PER_SESSION=5.0`, `MAX_ITERATIONS_PER_SUBAGENT=20`, `MAX_TOKENS_PER_SUBAGENT=30000`, `TOOL_TIMEOUT_SECONDS=30`, `MAX_CONTEXT_TOKENS=16000`

### 1.3 Database Models (`app/models/`)

- [x] `session.py` — `Session` model:
  - `id` (UUID, PK)
  - `created_at` (DateTime, server_default=now)
  - `updated_at` (DateTime, onupdate=now)
  - `status` (Enum: active, archived)
  - `head_ptr` (Integer, default=0) — sequence number where active context starts
  - `compact_summary` (Text, nullable) — summarized text of compacted messages
- [x] `message.py` — `Message` model:
  - `id` (UUID, PK)
  - `session_id` (FK → Session)
  - `seq_number` (Integer, indexed) — ordering within session
  - `role` (Enum: user, assistant, tool, system)
  - `content` (Text, nullable)
  - `tool_calls` (JSONB, nullable) — list of tool call objects
  - `tool_call_id` (String, nullable) — for tool result messages
  - `tool_result` (JSONB, nullable)
  - `token_count` (Integer, nullable)
  - `is_compacted` (Boolean, default=False)
  - `created_at` (DateTime)
  - Composite index on `(session_id, seq_number)`
- [x] `document.py` — `Document` model:
  - `id` (UUID, PK)
  - `session_id` (FK → Session)
  - `filename` (String)
  - `file_path` (String) — path on disk
  - `pump_tag` (String, nullable) — extracted from filename/content
  - `format_type` (String, nullable) — e.g. "french_form", "english_tabular"
  - `status` (Enum: uploading, uploaded, extracting, extracted, failed)
  - `num_pages` (Integer)
  - `created_at` (DateTime)
- [x] `document_page.py` — `DocumentPage` model:
  - `id` (UUID, PK)
  - `document_id` (FK → Document)
  - `page_number` (Integer)
  - `raw_text` (Text) — full extracted text
  - `layout_text` (Text, nullable) — layout-aware text with spatial info
  - `tables_json` (JSONB, nullable) — parsed tables from pdfplumber
  - `image_path` (String) — path to rendered PNG
  - `width` (Float)
  - `height` (Float)
- [x] `extracted_field.py` — `ExtractedField` model:
  - `id` (UUID, PK)
  - `document_id` (FK → Document)
  - `entity_id` (FK → EquipmentEntity, nullable)
  - `field_name` (String) — snake_case normalized name
  - `display_name` (String) — human-readable name
  - `raw_value` (String) — exactly as it appears in the document
  - `unit` (String, nullable)
  - `data_type` (Enum: numeric, text, boolean, reference)
  - `section` (String) — e.g. "operating_conditions", "construction_materials"
  - `confidence` (Float) — 0.0 to 1.0
  - `status` (Enum: extracted, verified, corrected, rejected)
  - `citation_page` (Integer)
  - `citation_bbox` (JSONB, nullable) — {x0, y0, x1, y1}
  - `citation_text` (String) — exact source text snippet
  - `created_at` (DateTime)
  - `updated_at` (DateTime)
  - Index on `(document_id, section)`
  - Index on `(document_id, field_name)`
- [x] `field_correction.py` — `FieldCorrection` model:
  - `id` (UUID, PK)
  - `field_id` (FK → ExtractedField)
  - `original_value` (String)
  - `corrected_value` (String)
  - `reason` (Text, nullable)
  - `corrected_by` (String, default="user")
  - `created_at` (DateTime)
- [x] `equipment_entity.py` — `EquipmentEntity` model:
  - `id` (UUID, PK)
  - `session_id` (FK → Session)
  - `tag` (String) — e.g. "P-718"
  - `entity_type` (String) — e.g. "centrifugal_pump"
  - `name` (String) — e.g. "Diesel Product Pump"
  - `metadata_json` (JSONB, nullable)
  - `created_at` (DateTime)
  - Index on `(session_id, tag)`
- [x] `entity_document.py` — Association table `entity_documents`:
  - `entity_id` (FK → EquipmentEntity)
  - `document_id` (FK → Document)
- [x] `entity_relationship.py` — `EntityRelationship` model:
  - `id` (UUID, PK)
  - `entity_a_id` (FK → EquipmentEntity)
  - `entity_b_id` (FK → EquipmentEntity)
  - `relationship_type` (Enum: sibling, parent, references)
  - `metadata_json` (JSONB, nullable)
  - `created_at` (DateTime)
- [x] `correction_pattern.py` — `CorrectionPattern` model (global, cross-session):
  - `id` (UUID, PK)
  - `description` (String)
  - `guidance_text` (Text) — injected into system prompts
  - `frequency` (Integer) — how many times this pattern was observed
  - `is_active` (Boolean, default=True)
  - `created_at` (DateTime)
  - `updated_at` (DateTime)
- [x] `cost_record.py` — `CostRecord` model:
  - `id` (UUID, PK)
  - `session_id` (FK → Session)
  - `document_id` (FK → Document, nullable)
  - `operation` (String) — e.g. "extraction", "validation", "query", "compaction"
  - `model` (String) — model name used
  - `input_tokens` (Integer)
  - `output_tokens` (Integer)
  - `cost_usd` (Float)
  - `duration_sec` (Float)
  - `created_at` (DateTime)

### 1.4 Database Setup

- [x] `app/database.py` — async engine, async sessionmaker, `get_db()` dependency
- [x] `app/main.py` — FastAPI app factory with CORS, lifespan, health endpoint
- [x] `docker-compose.yml` — PostgreSQL 16 + Redis 7
- [x] Create initial Alembic migration from models (manual migration with full schema)
- [x] Run migration, verify all 11 tables created correctly in PostgreSQL
- [x] Write smoke tests: 13 tests covering all models, relationships, JSONB fields, compaction simulation, corrections (all passing)

---

## Phase 2: PDF Processor

### 2.1 PDF Processing Service (`app/services/pdf_processor.py`)

- [x] `process_document(file_path, document_id, session) -> Document`:
  - Open PDF with pdfplumber
  - Detect number of pages
  - For each page:
    - [x] Extract raw text via `page.extract_text()`
    - [x] Extract layout-aware text via `page.extract_text(layout=True)` (preserves spatial positioning)
    - [x] Detect and parse tables via `page.extract_tables()` → store as JSON
    - [x] Render page to PNG at 200 DPI via Pillow (`page.to_image(resolution=200).save(...)`)
    - [x] Capture page dimensions (width, height)
    - [x] Create `DocumentPage` record in DB
  - [x] Detect format type heuristic (French-form vs English-tabular) — check for French keywords like "PRESSION", "DÉBIT", or layout patterns
  - [x] Extract equipment tag from filename (regex: `pds-(.+)\.pdf` → tag) and from content (look for tag patterns like P-XXX, P-XXXXXX)
  - [x] Update `Document` record: set `num_pages`, `pump_tag`, `format_type`, `status=uploaded`
  - [x] Return updated Document

### 2.2 Upload Endpoint (`app/api/documents.py`)

- [x] `POST /sessions/{session_id}/documents/upload` — accept multiple PDF files (multipart)
  - Validate session exists
  - Save each file to `UPLOAD_DIR/{session_id}/{filename}`
  - Create `Document` record with `status=uploading`
  - Call `process_document()` for each file
  - Return list of created documents with metadata
- [x] `GET /sessions/{session_id}/documents` — list all documents in session
- [x] `GET /sessions/{session_id}/documents/{doc_id}` — document detail with page count, status, tag
- [x] `GET /sessions/{session_id}/documents/{doc_id}/pages/{page_num}/image` — serve rendered PNG (FileResponse)

### 2.3 Test PDF Processing

- [x] Process all 4 provided PDFs (P718, P818, P300228, P600173)
- [x] Verify text extraction quality — spot check extracted text against actual PDF content
- [x] Verify table extraction — check that pdfplumber finds tables where they exist
- [x] Verify PNG rendering — images are legible at 200 DPI
- [x] Verify equipment tag extraction from filenames

---

## Phase 3: Agent Engine Core

### 3.1 LLM Client (`app/agent/llm_client.py`)

- [x] Async wrapper around LiteLLM `acompletion()`:
  - Accept messages list, tools list, model override
  - Return parsed response (content, tool_calls, usage)
  - Track token usage (input_tokens, output_tokens) from response
  - Calculate cost using LiteLLM's `completion_cost()` or manual rates
  - Create `CostRecord` in DB for every call
  - Handle retries: LiteLLM built-in retry with exponential backoff (3 retries)
  - Timeout: configurable per call

### 3.2 Tool Registry (`app/agent/tool_registry.py`)

- [x] `ToolRegistry` class:
  - Register tools by name with their callable and JSON schema
  - `get_tools_for_llm()` → returns tool definitions in OpenAI function-calling format
  - `execute_tool(name, arguments)` → dispatches to the right function, enforces timeout
  - Tool execution timeout via `asyncio.wait_for()`
  - Error wrapping: if tool raises, return error message string (not exception) so LLM can see it

### 3.3 Context Manager (`app/agent/context_manager.py`)

- [x] `build_context(session_id, system_prompt) -> list[messages]`:
  - Load session's `compact_summary` and `head_ptr`
  - Build message list:
    1. System message (system_prompt + global correction patterns from DB)
    2. If compact_summary exists: system message with summary prefixed "Summary of earlier conversation:"
    3. All messages from DB where `seq_number >= head_ptr` and `is_compacted=False`, ordered by seq_number
  - Convert DB messages to LLM message format (role, content, tool_calls, tool_call_id)
  - Count total tokens (estimate via tiktoken or simple char/4 heuristic)
  - Return built context
- [x] `should_compact(context_tokens, max_tokens) -> bool` — true if context exceeds threshold (e.g. 75% of max)
- [x] `compact(session_id)`:
  - Load oldest batch of active messages (e.g. oldest 40% of active zone)
  - Summarize via cheap LLM call (separate from main agent call):
    - Prompt: "Summarize this conversation chunk. Preserve: documents and their status, extracted field counts, corrections made and reasons, user instructions, naming conventions established. Discard: raw page content, verbose tool outputs, intermediate reasoning."
  - Merge new summary with existing `compact_summary`
  - Mark summarized messages as `is_compacted=True` in DB
  - Advance session `head_ptr` to new boundary
  - Create `CostRecord` for the compaction LLM call

### 3.4 Cost Tracker (`app/agent/cost_tracker.py`)

- [x] `CostTracker` class (per agent run):
  - Track cumulative: iterations, input_tokens, output_tokens, cost_usd
  - `check_budget(session_id)` → load session totals from CostRecord table, compare against config limits
  - `get_warning_message()` → returns system message if approaching limits (>80% of any limit)
  - `is_budget_exceeded()` → true if any hard limit hit
  - `record_llm_call(input_tokens, output_tokens, cost, duration, operation, session_id, document_id)`

### 3.5 Agent Runner (`app/agent/runner.py`)

- [x] `AgentRunner` class:
  - `__init__(session_id, system_prompt, tools: ToolRegistry, max_iterations, token_budget)`
  - `run(user_message: str) -> str`:
    1. Save user message to DB (increment seq_number)
    2. Enter while loop (iteration counter):
      - Build context via ContextManager
      - Check if compaction needed → compact if so
      - Inject budget warning if approaching limits
      - Call LLM via `llm_client`
      - Save assistant message to DB
      - If response has NO tool_calls → return content text, exit loop
      - If response HAS tool_calls:
        - For each tool call: execute via ToolRegistry, save tool result message to DB
        - Continue loop
      - If iteration limit hit → save system message "Budget exceeded, wrapping up", break
      - If token/cost budget exceeded → same
    3. Return final assistant text content
- [x] Handle edge cases:
  - [x] LLM returns empty content with no tool calls → retry once, then return error message
  - [x] Tool execution raises unhandled exception → wrap as error message in tool result
  - [x] LLM calls a tool that doesn't exist → return error "Unknown tool: {name}" as tool result

### 3.6 Smoke Test Agent

- [x] Create a minimal system prompt ("You are a test agent. You have one tool: echo.")
- [x] Register one dummy tool `echo(text) -> text`
- [x] Run agent with "Say hello using the echo tool" → verify loop executes, tool called, response returned
- [x] Verify all messages persisted to DB correctly (user, assistant with tool_call, tool result, final assistant)

---

## Phase 4: Agent Tools — Document & Extraction

### 4.1 Document Tools (`app/tools/document_tools.py`)

- [x] `get_session_documents(session_id) -> list[dict]`:
  - Query all Documents for session
  - Return: id, filename, pump_tag, format_type, status, num_pages
- [x] `get_document_info(document_id) -> dict`:
  - Return full document metadata including page count, status, format_type
- [x] `get_page_content(document_id, page_number) -> dict`:
  - Return: raw_text, layout_text, tables_json, image (base64-encoded PNG for LLM vision), width, height
  - Image encoding: read PNG file, base64 encode, return as data URI for LLM
- [x] `get_document_text(document_id) -> dict`:
  - Return all pages' raw_text concatenated (cheap scan, no images)
  - Include page breaks with page numbers

### 4.2 Extraction Tools (`app/tools/extraction_tools.py`)

- [x] `save_extracted_field(document_id, field_name, display_name, raw_value, unit, data_type, section, confidence, citation_page, citation_bbox, citation_text) -> dict`:
  - Validate all required fields present
  - Normalize field_name to snake_case
  - Create `ExtractedField` record with `status=extracted`
  - Return created field with id
- [x] `update_extracted_field(field_id, corrected_value, reason) -> dict`:
  - Load existing field
  - Create `FieldCorrection` record (preserve original_value)
  - Update field: `raw_value=corrected_value`, `status=corrected`
  - Return updated field
- [x] `delete_extracted_field(field_id) -> dict`:
  - Soft delete or hard delete the field
  - Return confirmation
- [x] `get_extraction_progress(document_id) -> dict`:
  - Count fields grouped by section
  - Count fields grouped by confidence tier (high/medium/low)
  - Count fields grouped by status
  - Return summary
- [x] `mark_extraction_complete(document_id) -> dict`:
  - Update Document status → `extracted`
  - Return confirmation

### 4.3 Register Tools with JSON Schema

- [x] For each tool function, define the OpenAI-format JSON schema (name, description, parameters with types and required fields)
- [x] Register all tools in a factory function `create_document_tools(session_id)` and `create_extraction_tools()`
- [x] Verify schemas are valid — test by calling `ToolRegistry.get_tools_for_llm()`

---

## Phase 5: Agent Tools — Entity, Query & Sub-Agent

### 5.1 Entity Tools (`app/tools/entity_tools.py`)

- [x] `create_equipment_entity(session_id, tag, entity_type, name, metadata) -> dict`:
  - Create `EquipmentEntity` record
  - Return created entity with id
- [x] `link_entity_to_document(entity_id, document_id) -> dict`:
  - Create entry in `entity_documents` association table
  - Return confirmation
- [x] `link_entity_to_fields(entity_id, field_ids: list) -> dict`:
  - Update `entity_id` on each `ExtractedField`
  - Return count of linked fields
- [x] `create_entity_relationship(entity_a_id, entity_b_id, relationship_type, metadata) -> dict`:
  - Create `EntityRelationship` record
  - Return created relationship
- [x] `search_entities(session_id, tag, entity_type, name) -> list[dict]`:
  - Filter entities by any combination of parameters
  - Return matching entities with basic info
- [x] `get_entity_detail(entity_id) -> dict`:
  - Load entity with all linked documents, all linked fields, all relationships
  - Return full detail

### 5.2 Query Tools (`app/tools/query_tools.py`)

- [x] `search_fields(session_id, field_name, value, section, document_id, status, min_confidence) -> list[dict]`:
  - Filter across extracted fields with any combination of parameters
  - Return matching fields with document info and citations
- [x] `get_correction_history(document_id, field_id) -> list[dict]`:
  - Query FieldCorrection records, optionally filtered by document or specific field
  - Return chronological list of corrections with original/corrected/reason
- [x] `get_global_correction_patterns() -> list[dict]`:
  - Query active CorrectionPattern records
  - Return list of patterns with description and guidance

### 5.3 Sub-Agent Tools (`app/tools/subagent_tools.py`)

- [x] `spawn_extraction_agent(session_id, document_id) -> str`:
  - Build extraction-specific system prompt (from `app/prompts/extraction_prompt.py`)
  - Include field taxonomy in prompt
  - Load correction history for this document → inject into prompt
  - Load correction history from sibling documents in session → inject into prompt
  - Create a new `AgentRunner` with:
    - Subset of tools: document tools (read-only), extraction tools, entity tools
    - Lower budget limits (MAX_ITERATIONS_PER_SUBAGENT, MAX_TOKENS_PER_SUBAGENT)
    - Its own fresh context (no shared message history)
  - Run the sub-agent with initial message: "Extract all structured fields from document {doc_id} ({filename}, {num_pages} pages). Work page by page."
  - Return the sub-agent's final text response (summary)
- [x] `spawn_validation_agent(session_id) -> str`:
  - Build validation-specific system prompt (from `app/prompts/validation_prompt.py`)
  - Create a new `AgentRunner` with:
    - Subset of tools: query tools, extraction tools (update only), entity tools
    - Own budget limits
  - Run with initial message: "Validate all extractions in session {session_id}. Check naming consistency, unit consistency, coverage gaps, and entity relationships."
  - Return validation report text

### 5.4 Register All Tool Schemas

- [x] Define JSON schemas for all entity, query, and sub-agent tools
- [x] Create factory: `create_orchestrator_tools(session_id)` — returns all tools (document + extraction + entity + query + sub-agent)
- [x] Create factory: `create_extraction_subagent_tools(session_id, document_id)` — returns subset (document + extraction + entity)
- [x] Create factory: `create_validation_subagent_tools(session_id)` — returns subset (query + extraction-update + entity)

---

## Phase 6: System Prompts

### 6.1 Orchestrator Prompt (`app/prompts/orchestrator_prompt.py`)

- [x] Define the orchestrator system prompt covering:
  - Agent identity: specializes in industrial process plant datasheets
  - Capabilities: extract fields, answer questions with citations, build knowledge graph, learn from corrections
  - Guardrails:
    - Citations mandatory (page, bbox, source text, confidence)
    - Never hallucinate — "not found" is valid
    - Ambiguous → report low confidence + explain
    - "I don't know" better than wrong answer
  - Workflow guidance:
    - For extraction requests: use `get_session_documents`, process each doc via `spawn_extraction_agent`, then `spawn_validation_agent`
    - For questions: use `search_fields` and `search_entities`, cite sources
    - For corrections: use `update_extracted_field`, record reason, check siblings
  - Global correction patterns placeholder (injected dynamically)
- [x] Function `build_orchestrator_prompt(correction_patterns: list[str]) -> str`

### 6.2 Extraction Sub-Agent Prompt (`app/prompts/extraction_prompt.py`)

- [x] Define extraction system prompt covering:
  - Focused role: extract all structured fields from one document
  - Methodology:
    - Work page by page sequentially
    - For each page: fetch content (image + text + tables), identify fields, save each one
    - Extract EVERY field that has a value — do not skip
    - Separate values from units (e.g. "335 m³/h" → value="335", unit="m³/h")
    - Normalize field names to snake_case using the taxonomy
    - For bilingual documents, use English field names
    - Notes and remarks are fields too
  - Confidence rules:
    - High (0.8-1.0): clearly printed, unambiguous
    - Medium (0.5-0.8): readable but could be misread or inferred
    - Low (0.0-0.5): guessing or partially obscured
    - Flag low confidence to user
  - After all pages: review via `get_extraction_progress`, check for gaps
  - Create equipment entity and link to document and fields
  - Mark document complete when done
  - Correction history placeholder (injected dynamically)
- [x] Field taxonomy definition — categorized list of common field names:
  - `general_info`: equipment_tag, equipment_name, equipment_type, manufacturer, model, serial_number, service_description, project_number, revision_number, revision_date
  - `operating_conditions`: flow_nominal, flow_rated, flow_maximum, flow_minimum, temperature_pumping, temperature_maximum, temperature_minimum, density, viscosity, vapor_pressure, specific_gravity
  - `pressure_conditions`: pressure_suction, pressure_discharge, pressure_differential, npsh_available, npsh_required
  - `performance`: head_rated, head_maximum, efficiency, power_absorbed, power_rated, speed_rated, speed_maximum, specific_speed
  - `construction_materials`: casing_material, impeller_material, shaft_material, seal_type, seal_material, bearing_type, gasket_material
  - `mechanical_design`: design_pressure, design_temperature, hydrostatic_test_pressure, casing_type, impeller_type, impeller_diameter, stages, rotation_direction
  - `motor_data`: motor_type, motor_power, motor_voltage, motor_frequency, motor_speed, motor_efficiency, motor_frame, motor_enclosure, motor_insulation_class
  - `weights_dimensions`: dry_weight, wet_weight, overall_length, overall_width, overall_height
  - `notes_remarks`: note (free text fields from notes/remarks sections)
- [x] Function `build_extraction_prompt(document_info: dict, correction_history: list[dict]) -> str`

### 6.3 Validation Sub-Agent Prompt (`app/prompts/validation_prompt.py`)

- [x] Define validation system prompt covering:
  - Role: cross-check all extractions across all documents in session
  - Checks to perform:
    - Field naming consistency: same physical quantity should have same field_name across docs
    - Unit consistency: flag if same field has different units across docs (might be valid, but flag)
    - Coverage gaps: compare field counts across similar documents, flag significant differences
    - Entity relationships: identify sibling equipment, parent-child, cross-references in notes
    - Confidence distribution: flag documents with unusually many low-confidence fields
  - Actions:
    - Auto-fix simple naming inconsistencies via `update_extracted_field`
    - Create entity relationships via `create_entity_relationship`
    - Report warnings for ambiguous issues (don't auto-fix)
  - Return structured validation report
- [x] Function `build_validation_prompt(session_summary: dict) -> str`

---

## Phase 7: Chat API & Async Job Execution

### 7.1 Session Endpoints (`app/api/sessions.py`)

- [x] `POST /sessions` — create new session, return session_id
- [x] `GET /sessions` — list all sessions with status, created_at, document count
- [x] `GET /sessions/{session_id}` — session detail: status, document count, field count, cost total, message count (basic version exists but missing field_count, cost_total, message_count)

### 7.2 Chat Endpoint (`app/api/chat.py`)

- [x] `POST /sessions/{session_id}/chat` — send message to agent:
  - Request body: `{ "message": "string" }`
  - Determine sync vs async:
    - Heuristic: if message looks like extraction request (contains "extract", "process", etc.) → async
    - Simple queries → sync
    - Or: always async, poll via SSE/websocket (simpler to implement uniformly)
  - **Sync path**: run `AgentRunner.run()` inline, return response text
  - **Async path**: enqueue Arq job, return `{ "job_id": "...", "status": "queued" }`
  - Response: `{ "response": "string", "job_id": "optional" }`
- [x] `GET /sessions/{session_id}/chat/status/{job_id}` — poll job status (if using async):
  - Return: status (queued, running, completed, failed), result text if completed

### 7.3 Arq Worker Setup (`app/worker.py`)

- [x] Configure Arq worker:
  - Redis connection from config
  - Register `run_agent_job(ctx, session_id, user_message)` function
  - Job function:
    - Build orchestrator tools
    - Build system prompt (with global correction patterns)
    - Create `AgentRunner`
    - Run agent, catch exceptions
    - On completion: publish result to Redis pub-sub channel `session:{session_id}:events`
    - On failure: save error message to conversation, publish error event
  - Concurrency: 1 worker per process (agent is async but LLM calls are sequential per session)
  - Retry settings: 3 retries with exponential backoff for transient failures

### 7.4 Real-Time Updates via SSE (`app/api/events.py`)

- [x] `GET /sessions/{session_id}/events` — Server-Sent Events stream:
  - Subscribe to Redis pub-sub channel `session:{session_id}:events`
  - Stream events: `agent_thinking`, `tool_call`, `tool_result`, `agent_response`, `extraction_progress`, `error`
  - Frontend uses this to show real-time progress during extraction
  - Events include: event type, timestamp, optional data (tool name, progress counts, response text)

### 7.5 Test Chat Flow End-to-End

- [x] Start server + worker
- [x] Create session, upload a PDF
- [x] Send "What documents are in this session?" → verify agent uses `get_session_documents` tool and responds
- [x] Verify messages persisted to DB in correct order
- [x] Verify cost record created for LLM call

---

## Phase 8: Read-Only Data Endpoints

### 8.1 Field Endpoints (`app/api/fields.py`)

- [x] `GET /sessions/{session_id}/fields` — list all extracted fields in session
  - Query params: document_id, section, status, min_confidence, field_name
  - Return: list of fields with document info, citations
  - Support pagination (offset + limit)
- [x] `GET /sessions/{session_id}/fields/{field_id}` — single field detail with correction history
- [x] `GET /sessions/{session_id}/fields/stats` — extraction statistics:
  - Total fields, by section, by status, by confidence tier
  - Per-document breakdown

### 8.2 Entity Endpoints (`app/api/entities.py`)

- [x] `GET /sessions/{session_id}/entities` — list all equipment entities in session
  - Return: tag, type, name, linked document count, linked field count
- [x] `GET /sessions/{session_id}/entities/{entity_id}` — entity detail:
  - All linked documents, all linked fields, all relationships with other entities

### 8.3 Message History Endpoint (`app/api/messages.py`)

- [x] `GET /sessions/{session_id}/messages` — conversation history
  - Query params: limit, offset (for pagination)
  - Return: messages ordered by seq_number, with role, content, tool_calls (simplified)
  - Exclude raw tool results for compacted messages (they're summarized)

### 8.4 Cost Endpoints (`app/api/costs.py`)

- [x] `GET /sessions/{session_id}/costs` — cost breakdown:
  - Total session cost
  - Per-operation breakdown (extraction, validation, query, compaction)
  - Per-document breakdown
  - Per-model breakdown
  - Token usage totals

### 8.5 Correction Endpoints (`app/api/corrections.py`)

- [x] `GET /sessions/{session_id}/corrections` — all corrections in session
  - Include: field info, original value, corrected value, reason, timestamp
- [x] `GET /corrections/patterns` — global correction patterns (cross-session)

---

## Phase 9: Full Extraction Pipeline Integration

### 9.1 End-to-End Extraction Test

- [x] Create session
- [x] Upload all 4 PDFs (P718, P818, P300228, P600173)
- [x] Send chat message: "Extract all fields from these datasheets"
- [x] Verify orchestrator:
  - [x] Calls `get_session_documents` → sees 4 docs
  - [x] Spawns extraction sub-agent for each document sequentially
  - [x] Each sub-agent works page by page, saves fields to DB
  - [x] Each sub-agent creates equipment entity
  - [x] Each sub-agent marks document complete
  - [x] After all docs, spawns validation sub-agent
  - [x] Validation sub-agent checks consistency, creates relationships
  - [x] Orchestrator composes final summary response

### 9.2 Validate Extraction Quality

- [x] Check extracted fields against manual reading of each PDF:
  - [x] P718: verify key fields (flow, pressure, materials, motor data)
  - [x] P818: verify key fields, check consistency with P718 (sibling pumps)
  - [x] P300228: verify fields, check format handling
  - [x] P600173: verify fields
- [x] Check citations: every field has page number and source text that matches the PDF
- [x] Check entities: each document has an equipment entity with correct tag and type
- [x] Check relationships: sibling pumps identified (P718 ↔ P818)

### 9.3 Validate Context Management

- [x] After full extraction (50+ messages), verify:
  - [x] Context window stays within token limit
  - [x] Compaction fires when needed
  - [x] Compact summary preserves key information
  - [x] Agent can still answer questions about earlier extractions using its tools

### 9.4 Validate Budget Guards

- [x] Verify cost records exist for every LLM call
- [x] Verify sub-agents respect their own budget limits
- [x] Test budget warning: set artificially low limits, verify agent receives warning and wraps up

---

## Phase 10: HITL Correction Flow

### 10.1 Correction Through Chat

- [x] Test correction flow: "The impeller material for P-718 is wrong, it should be SS 316"
  - [x] Agent searches for the field
  - [x] Agent calls `update_extracted_field` with corrected value
  - [x] FieldCorrection record created in DB
  - [x] Field status updated to `corrected`
  - [x] Agent confirms the change
  - [x] Agent checks for sibling entities and offers to re-check them

### 10.2 Re-Extraction with Correction Awareness

- [x] After making corrections, send: "Re-extract P-818"
  - [x] Sub-agent receives correction history in its prompt
  - [x] Sub-agent applies learned corrections during extraction
  - [x] Verify the previously-corrected field type is handled correctly

### 10.3 Global Correction Pattern Detection

- [x] `app/services/pattern_detector.py`:
  - [x] `detect_patterns()` — scan all FieldCorrection records across all sessions
  - [x] Group corrections by: field_name + original_value + corrected_value
  - [x] If same correction appears 3+ times across different sessions → create CorrectionPattern
  - [x] Store pattern with description and guidance_text
  - [x] This can run as a periodic background task or on-demand
- [x] Verify patterns are injected into agent system prompts for new sessions

---

## Phase 11: Frontend (HITL Interface)

### 11.1 Project Setup

- [ ] Create React app (Vite + React + TypeScript)
- [ ] Install dependencies: axios or fetch wrapper, tailwindcss (or minimal CSS)
- [ ] Set up API client pointing to FastAPI backend
- [ ] Set up proxy in dev server for API calls

### 11.2 Session Management Page

- [ ] List existing sessions
- [ ] Create new session button
- [ ] Navigate to session detail

### 11.3 Chat Interface (`ChatPanel` component)

- [ ] Message input box + send button
- [ ] Message history display:
  - User messages (right-aligned)
  - Agent responses (left-aligned, markdown rendered)
  - Tool call indicators (collapsible, show tool name + brief result)
- [ ] Loading state while agent is processing
- [ ] SSE connection for real-time updates during async processing
- [ ] Auto-scroll to latest message

### 11.4 Document Panel (`DocumentPanel` component)

- [ ] File upload area (drag & drop or file picker, PDF only)
- [ ] List of uploaded documents with status badges (uploading, uploaded, extracting, extracted)
- [ ] Click document → open PDF viewer

### 11.5 PDF Viewer (`PdfViewer` component)

- [ ] Display rendered page images (from API)
- [ ] Page navigation (prev/next, page number input)
- [ ] Citation highlight overlays:
  - When a field is selected in the field review panel, highlight its bbox on the page
  - Draw colored rectangles on the page image at citation_bbox coordinates
  - Color by confidence: green=high, yellow=medium, red=low

### 11.6 Field Review Panel (`FieldReviewPanel` component)

- [ ] Display extracted fields for selected document
- [ ] Group by section (collapsible sections)
- [ ] Each field shows: display_name, value, unit, confidence badge, status badge
- [ ] Click field → highlight citation in PDF viewer
- [ ] Inline correction:
  - Click "Edit" on a field → input for corrected value + reason
  - Submit correction → sends chat message to agent: "Correct {field_name} for {equipment_tag} from {old_value} to {new_value}. Reason: {reason}"
  - Agent processes it through conversation (maintaining full context)
- [ ] Verify/Reject buttons:
  - "Verify" → sends "Verify {field_name} for {equipment_tag} is correct"
  - "Reject" → sends "Reject {field_name} for {equipment_tag}, reason: {reason}" (with reason input)
- [ ] Filter fields by: section, status, confidence level
- [ ] Sort fields by: section, confidence, status

### 11.7 Extraction Stats & Cost Display

- [ ] Show extraction progress: fields by section, coverage stats
- [ ] Show cost breakdown: per document, per operation, total session cost
- [ ] Show confidence distribution chart (simple bar chart)

### 11.8 Entity View (`EntityPanel` component)

- [ ] List equipment entities in session
- [ ] Show entity detail: tag, type, name, linked documents, linked fields count
- [ ] Show relationships: sibling equipment, parent-child, references
- [ ] Click entity → filter fields panel to show that entity's fields

---

## Phase 12: Polish & Production Hardening

### 12.1 Error Handling & Resilience

- [ ] API error responses: consistent JSON error format `{ "error": "message", "detail": "..." }`
- [ ] Global exception handler in FastAPI
- [ ] Agent runner: graceful handling of LLM API failures (retry, then inform user)
- [x] Tool execution: timeout enforcement, error message wrapping
- [ ] Frontend: error toasts for API failures, retry buttons where appropriate

### 12.2 CORS & API Configuration

- [x] Configure CORS middleware for frontend origin
- [x] API versioning prefix `/api/v1/`
- [x] Request validation with Pydantic (already via FastAPI, but verify edge cases)

### 12.3 Logging

- [ ] Structured logging (JSON format) with Python `logging`
- [x] Log every: LLM call (model, tokens, cost, duration), tool execution (name, duration, success/fail), agent iteration (loop count, budget status), compaction event, error
- [ ] Request-level logging middleware (request_id, session_id, duration)

### 12.4 Testing

- [x] Unit tests for:
  - [x] PDF processor (text extraction, table parsing, image rendering)
  - [x] Context manager (build context, compaction logic)
  - [x] Cost tracker (budget checks, warning thresholds)
  - [x] Tool registry (dispatch, timeout, error wrapping)
  - [x] Pattern detector (grouping logic, threshold)
- [ ] Integration tests for:
  - [x] Full agent loop with mock LLM (verify message persistence, tool dispatch)
  - [x] Chat endpoint → agent → tool → DB round trip
  - [ ] Upload → process → extract → correct → re-extract flow

### 12.5 Performance

- [ ] Database query optimization: verify indexes are used (explain analyze on key queries)
- [x] Async throughout: all DB calls async, all LLM calls async, file I/O async where possible
- [x] Page image caching: render once at upload, serve from disk thereafter
- [x] Connection pooling: SQLAlchemy async pool settings, Redis connection pool

### 12.6 Documentation

- [ ] API documentation: FastAPI auto-generates OpenAPI/Swagger — verify all endpoints documented with descriptions and examples
- [ ] Write-up document:
  - [ ] Architecture overview with diagrams
  - [ ] Trade-offs explained (sequential vs parallel, native loop vs framework, etc.)
  - [ ] Evaluation metrics: extraction accuracy, cost per document, coverage
  - [ ] Cost model with actual numbers from test runs
  - [ ] Future improvements section

### 12.7 Demo Recording

- [ ] Record walkthrough:
  - [ ] Create session, upload PDFs
  - [ ] Trigger extraction, show real-time progress
  - [ ] Show extracted fields in review panel
  - [ ] Click field → see citation highlighted in PDF
  - [ ] Make a correction through the chat and through the UI
  - [ ] Ask a question: "What's the impeller material for P-718?"
  - [ ] Show entity relationships
  - [ ] Show cost breakdown
  - [ ] Show re-extraction with correction awareness

---

## Phase Summary


| Phase | What You Get                                                      | Depends On       |
| ----- | ----------------------------------------------------------------- | ---------------- |
| 1     | Database + project skeleton                                       | Nothing          |
| 2     | PDF processing + upload API                                       | Phase 1          |
| 3     | Working agent loop (LLM + tools + context)                        | Phase 1          |
| 4     | Agent can read docs and save fields                               | Phase 2, 3       |
| 5     | Agent can build knowledge graph, query, spawn sub-agents          | Phase 4          |
| 6     | Well-crafted prompts driving agent behavior                       | Phase 5          |
| 7     | Chat API + async execution + real-time events                     | Phase 3, 6       |
| 8     | Frontend can display all data                                     | Phase 7          |
| 9     | Full extraction pipeline working end-to-end                       | Phase 4, 5, 6, 7 |
| 10    | Corrections flow through conversation, improve future extractions | Phase 9          |
| 11    | Complete HITL frontend                                            | Phase 8, 10      |
| 12    | Production-grade polish                                           | Phase 11         |


**Phases 1-3 can overlap** (DB schema and agent engine are independent until tools need both).
**Phases 4-5 are sequential** (entity/query tools build on extraction tools).
**Phase 6 can start alongside Phase 4** (prompts don't depend on tool implementation).
**Phase 11 can start after Phase 8** (frontend reads data while backend features are being completed).