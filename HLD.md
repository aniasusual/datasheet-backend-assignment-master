# High-Level Design: Datasheet Extraction System

## Purpose

Complete system design for a conversational AI agent that extracts structured knowledge from industrial process datasheets. This document covers components, interactions, data flows, failure handling, and scalability.

---

## 1. System Components

```
┌─────────────────────────────────────────────────────────────────────┐
│                                                                     │
│   ┌──────────┐         ┌──────────────────────────────────────┐    │
│   │ Frontend  │  HTTP   │           API Server                 │    │
│   │ (React)   │────────►│           (FastAPI)                  │    │
│   │           │◄────────│                                      │    │
│   │ • Chat UI │  WS/SSE │  • Chat endpoint (→ agent)           │    │
│   │ • PDF     │         │  • Read-only data endpoints          │    │
│   │   viewer  │         │  • Upload endpoint (→ PDF processor) │    │
│   │ • Field   │         │                                      │    │
│   │   review  │         └──────────┬──────────┬────────────────┘    │
│   └──────────┘                     │          │                     │
│                                    │          │                     │
│                           ┌────────▼───┐  ┌───▼──────────┐         │
│                           │   Agent    │  │ PDF          │         │
│                           │   Engine   │  │ Processor    │         │
│                           │            │  │              │         │
│                           │ • Runner   │  │ • pdfplumber │         │
│                           │ • Context  │  │ • Pillow     │         │
│                           │   Manager  │  │ • text+table │         │
│                           │ • Tools    │  │   extraction │         │
│                           │ • Cost     │  │ • page       │         │
│                           │   Tracker  │  │   rendering  │         │
│                           └──┬─────┬──┘  └──────┬───────┘         │
│                              │     │            │                   │
│                    ┌─────────▼┐  ┌─▼────────┐  │                   │
│                    │ LiteLLM  │  │  Redis    │  │                   │
│                    │          │  │           │  │                   │
│                    │ Claude/  │  │ • Arq job │  │                   │
│                    │ Gemini   │  │   queue   │  │                   │
│                    │          │  │ • Pub-sub │  │                   │
│                    └──────────┘  └─────┬─────┘  │                   │
│                                        │        │                   │
│                              ┌─────────▼────────▼──────┐           │
│                              │      PostgreSQL          │           │
│                              │                          │           │
│                              │  Sessions, Messages,     │           │
│                              │  Documents, Pages,       │           │
│                              │  Fields, Corrections,    │           │
│                              │  Entities, Relationships,│           │
│                              │  CostRecords, Patterns   │           │
│                              └──────────────────────────┘           │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

Six components, each with a single job:

- **API Server (FastAPI):** Receives HTTP requests. Routes chat messages to the agent engine. Serves read-only data to the frontend. Handles file uploads. Stateless — all state lives in PostgreSQL and Redis.

- **Agent Engine:** The brain. Contains the AgentRunner (while loop), ContextManager (sliding window), ToolRegistry (tool dispatch), and CostTracker. Invoked by the API server for sync requests or by Arq workers for async jobs. It is a Python module, not a separate process.

- **PDF Processor:** Deterministic service. Takes raw PDFs, produces structured page data (text, tables, PNG images, dimensions). Runs at upload time before the agent ever sees the document. Uses pdfplumber + Pillow.

- **PostgreSQL:** Single source of truth. Stores everything — sessions, full message history, documents, extracted fields, corrections, entities, cost records, correction patterns.

- **Redis:** Job queue backend (Arq) for async agent execution. Also pub-sub channel for real-time progress updates to the frontend.

- **Frontend (React):** Chat interface, PDF viewer with citation highlights, field review panel. Pure display layer — only reads data and sends chat messages.

---

## 2. Data Model

```
┌─────────────┐
│   Session    │
│              │
│  id          │
│  created_at  │
│  status      │
│  head_ptr    │──── sequence number where active context starts
│  compact_sum │──── summarized text of older messages
└──────┬───────┘
       │
       │ has many
       ▼
┌──────────────┐     ┌────────────────┐
│   Message    │     │   Document     │
│              │     │                │
│  seq_number  │     │  filename      │
│  role        │     │  pump_tag      │
│  content     │     │  format_type   │
│  tool_calls  │     │  status        │
│  tool_result │     │  num_pages     │
│  token_count │     └───────┬────────┘
│  is_compacted│             │
└──────────────┘             │ has many
                             ▼
                    ┌─────────────────┐
                    │  DocumentPage   │
                    │                 │
                    │  page_number    │
                    │  raw_text       │
                    │  layout_text    │
                    │  tables_json    │
                    │  image_path     │
                    │  width, height  │
                    └─────────────────┘

┌──────────────────┐         ┌──────────────────────┐
│ ExtractedField   │         │ EquipmentEntity      │
│                  │         │                      │
│  field_name      │         │  tag (P-718)         │
│  display_name    │         │  entity_type         │
│  raw_value       │         │  name                │
│  unit            │         │  metadata_json       │
│  confidence      │         │                      │
│  status          │         │  ──→ Documents       │
│  section         │         │  ──→ Fields          │
│  data_type       │         │  ──→ Relationships   │
│                  │         └──────────────────────┘
│  citation_page   │
│  citation_bbox   │                ┌────────────────────┐
│  citation_text   │                │ EntityRelationship  │
│                  │                │                     │
│  ──→ Document    │                │  entity_a           │
│  ──→ Entity      │                │  entity_b           │
│  ──→ Corrections │                │  type (sibling,     │
└──────────────────┘                │   parent, refs)     │
         │                          └─────────────────────┘
         │ has many
         ▼
┌──────────────────┐         ┌──────────────────────┐
│ FieldCorrection  │         │ CorrectionPattern    │
│                  │         │ (global, cross-       │
│  original_value  │         │  session)             │
│  corrected_value │         │                      │
│  reason          │         │  description          │
│  corrected_by    │         │  guidance_text        │
│  created_at      │         │  frequency            │
└──────────────────┘         └──────────────────────┘

┌──────────────────┐
│  CostRecord      │
│                  │
│  operation       │
│  model           │
│  input_tokens    │
│  output_tokens   │
│  cost_usd        │
│  duration_sec    │
│  ──→ Session     │
└──────────────────┘
```

The **knowledge graph** is formed by EquipmentEntity and EntityRelationship. Entities link to documents and fields. Relationships connect entities to each other (P-718 sibling P-818, both in Hydrocracking Unit 032). This is what enables cross-document queries like "compare these two pumps" or "what equipment is in this unit."

---

## 3. Chat Message Flow (the critical path)

Every interaction — extraction, question, correction — follows this flow:

```
User types message
       │
       ▼
┌──────────────┐     ┌─────────────────────────────────────────────┐
│   Frontend   │────►│  API Server                                 │
│   POST /chat │     │                                             │
└──────────────┘     │  1. Validate session                        │
                     │  2. Save user message to DB                 │
                     │  3. Decide: sync or async?                  │
                     │     ├── Simple query → run agent inline     │
                     │     └── Heavy task  → enqueue Arq job       │
                     └──────────────┬──────────────────────────────┘
                                    │
                                    ▼
                     ┌──────────────────────────────┐
                     │        Agent Engine           │
                     │                               │
                     │  ┌─────────────────────────┐  │
                     │  │ Build context window     │  │
                     │  │ (system + summary +      │  │
                     │  │  recent messages)        │  │
                     │  └────────────┬────────────┘  │
                     │               │               │
                     │  ┌────────────▼────────────┐  │
                     │  │ Call LLM via LiteLLM    │◄─┼──── LOOP
                     │  └────────────┬────────────┘  │       │
                     │               │               │       │
                     │          ┌────▼────┐          │       │
                     │          │ Tool    │          │       │
                     │     NO   │ calls?  │  YES     │       │
                     │    ┌─────┤         ├─────┐    │       │
                     │    │     └─────────┘     │    │       │
                     │    ▼                     ▼    │       │
                     │  Return              Execute  │       │
                     │  text to             tools,   │       │
                     │  user                save     │       │
                     │                      results  ├───────┘
                     │                      to DB    │
                     │                               │
                     └───────────────────────────────┘
                                    │
                                    ▼
                     Response returned to frontend
                     (directly if sync, via Redis pub-sub if async)
```

The loop continues until the LLM responds with plain text (no tool calls) or a budget guard is hit.

---

## 4. Document Upload Flow

```
User selects PDFs
       │
       ▼
┌──────────────┐         ┌─────────────────────────────────────┐
│   Frontend   │────────►│  API Server                         │
│   POST       │         │                                     │
│   /upload    │         │  1. Save files to disk               │
└──────────────┘         │  2. Create Document records (DB)     │
                         │  3. Trigger PDF Processor per file   │
                         └──────────────┬──────────────────────┘
                                        │
                         ┌──────────────▼──────────────────────┐
                         │        PDF Processor                 │
                         │                                      │
                         │  For each page:                      │
                         │   • Extract text (layout-aware)      │
                         │   • Detect and parse tables          │
                         │   • Render page → PNG at 200 DPI    │
                         │   • Measure dimensions               │
                         │                                      │
                         │  Also:                               │
                         │   • Detect format type               │
                         │     (French-form vs English-tabular) │
                         │   • Extract equipment tag            │
                         │     from filename or content         │
                         │                                      │
                         │  Save DocumentPage records to DB     │
                         │  Update Document status → "uploaded" │
                         └──────────────────────────────────────┘

Time: 2-3 seconds per document. All CPU, no LLM.
The agent is NOT involved here. It discovers documents
via get_session_documents tool when the user asks for extraction.
```

---

## 5. Extraction Flow

```
User: "Extract all fields from these datasheets"
       │
       ▼
┌──────────────────────────────────────────────────────────┐
│                  ORCHESTRATOR AGENT                       │
│                                                          │
│  1. get_session_documents()                              │
│     → sees 4 docs ready                                  │
│                                                          │
│  2. For each document (sequential):                      │
│     │                                                    │
│     │  spawn_extraction_agent(doc_id)                    │
│     │         │                                          │
│     │         ▼                                          │
│     │  ┌──────────────────────────────────────────┐      │
│     │  │     EXTRACTION SUB-AGENT                 │      │
│     │  │     (own context window, own budget)     │      │
│     │  │                                          │      │
│     │  │  get_document_info(doc_id)               │      │
│     │  │  get_correction_history(doc_id)          │      │
│     │  │                                          │      │
│     │  │  For each page:                          │      │
│     │  │    get_page_content(doc_id, page_num)    │      │
│     │  │    → receives image + text + tables      │      │
│     │  │                                          │      │
│     │  │    LLM sees image (spatial layout)       │      │
│     │  │    LLM reads text (exact strings)        │      │
│     │  │    LLM identifies fields                 │      │
│     │  │                                          │      │
│     │  │    save_extracted_field() × N            │      │
│     │  │    (each field saved to DB immediately)  │      │
│     │  │                                          │      │
│     │  │  get_extraction_progress(doc_id)         │      │
│     │  │  → review: any gaps? inconsistencies?    │      │
│     │  │                                          │      │
│     │  │  create_equipment_entity(tag, type, name)│      │
│     │  │  mark_extraction_complete(doc_id)        │      │
│     │  │                                          │      │
│     │  │  Returns: "Extracted 28 fields from P718"│      │
│     │  └──────────────────────────────────────────┘      │
│     │                                                    │
│     │  (orchestrator only sees the summary,              │
│     │   not the raw page data — context isolation)       │
│     │                                                    │
│     │  Next document...                                  │
│                                                          │
│  3. After all docs extracted:                            │
│     │                                                    │
│     │  spawn_validation_agent()                          │
│     │         │                                          │
│     │         ▼                                          │
│     │  ┌──────────────────────────────────────────┐      │
│     │  │     VALIDATION SUB-AGENT                 │      │
│     │  │                                          │      │
│     │  │  Loads all fields from all docs          │      │
│     │  │  Checks:                                 │      │
│     │  │   • Naming consistency across docs       │      │
│     │  │   • Unit consistency                     │      │
│     │  │   • Coverage gaps                        │      │
│     │  │   • Entity relationships to create       │      │
│     │  │                                          │      │
│     │  │  Auto-fixes simple issues                │      │
│     │  │  Flags ambiguous issues as warnings      │      │
│     │  │                                          │      │
│     │  │  Returns: validation report              │      │
│     │  └──────────────────────────────────────────┘      │
│                                                          │
│  4. Compose final response to user:                      │
│     "Extracted 106 fields across 4 documents.            │
│      4 equipment entities created.                       │
│      1 warning: P-300228 missing motor_power field."     │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

Sequential extraction is deliberate — each sub-agent benefits from previous ones via shared DB state (correction patterns, naming conventions).

---

## 6. Correction Flow (HITL Through Conversation)

```
User: "The impeller material for P-718 is wrong. Should be SS 316."
       │
       ▼
┌────────────────────────────────────────────────────────────────┐
│                     AGENT                                      │
│                                                                │
│  1. search_fields("impeller_material", doc="P718")             │
│     → finds field: value="CS", confidence=0.92                 │
│                                                                │
│  2. update_extracted_field(                                    │
│       field_id, corrected_value="SS 316",                     │
│       reason="spec updated last month"                         │
│     )                                                          │
│     → field status changes to "corrected"                      │
│     → FieldCorrection record created (preserves original)      │
│     → original extraction never deleted                        │
│                                                                │
│  3. Agent checks entity relationships                          │
│     → P-818 is a sibling pump                                  │
│                                                                │
│  4. Responds: "Corrected. P-718 impeller updated CS → SS 316.  │
│     P-818 is a sibling pump — want me to re-extract it?"       │
│                                                                │
│  User: "Yes"                                                   │
│                                                                │
│  5. spawn_extraction_agent(doc_id="P818")                      │
│     → sub-agent prompt includes correction history             │
│     → sub-agent knows: "impeller material was corrected        │
│        from CS to SS 316 on sibling pump P-718"                │
│     → re-extracts with this awareness                          │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

No separate HITL API. The agent IS the HITL interface. It has full context, can push back, can proactively suggest related fixes.

---

## 7. Context Window Management

```
DATABASE (full fidelity, never lost):
┌─────┬─────┬─────┬─────┬─────┬─────┬─────┬─────┬─────┬─────┐
│ m1  │ m2  │ m3  │ m4  │ m5  │ m6  │ m7  │ m8  │ m9  │ m10 │
└─────┴─────┴─────┴─────┴─────┴─────┴─────┴─────┴─────┴─────┘
  ▲                       ▲                              ▲
  │                       │                              │
  compacted               head pointer                   latest


WHAT THE LLM SEES ON EACH CALL:
┌─────────────────────────────────────────────────────────────┐
│  System prompt                                    ~2k tok   │
│  "You are an extraction specialist..."                      │
│  + guardrails + global correction patterns                  │
├─────────────────────────────────────────────────────────────┤
│  Compact summary of m1..m4                        ~1-2k tok │
│  "User uploaded 4 datasheets. P718 extracted                │
│   (28 fields). User corrected impeller_material             │
│   from CS to SS 316 (spec updated)."                        │
├─────────────────────────────────────────────────────────────┤
│  Full messages m5..m10                            ~5-8k tok │
│  (recent tool calls, results, conversation)                 │
└─────────────────────────────────────────────────────────────┘
                                             TOTAL: ~8-12k tok
                                             (CONSTANT regardless
                                              of session length)
```

**When the active zone gets too big:**

```
Before compaction:
  compact_summary = "P718 extracted..."
  head_ptr = m5
  active = [m5, m6, m7, m8, m9, m10, m11, m12, m13, m14]  ← too big

Compaction runs:
  1. Take oldest batch [m5, m6, m7, m8]
  2. Summarize via cheap LLM call → "P818 also extracted (26 fields)..."
  3. Merge with existing summary
  4. Mark m5-m8 as compacted
  5. Advance head_ptr to m9

After compaction:
  compact_summary = "P718 extracted... P818 also extracted..."
  head_ptr = m9
  active = [m9, m10, m11, m12, m13, m14]  ← fits now
```

The DB always has everything. Compaction only affects what the LLM sees. The agent can still retrieve old data via its tools (get_extraction_progress, search_fields, etc.) because those read from the database, not from the context window.

---

## 8. Feedback Learning (Three Levels)

```
┌──────────────────────────────────────────────────────────────┐
│  LEVEL 1: Within Session (immediate)                         │
│                                                              │
│  User corrects field → agent sees it in conversation         │
│  → next extraction in this session benefits immediately      │
│  → no extra mechanism needed, it's just conversation context │
└──────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────┐
│  LEVEL 2: Re-extraction (per document)                       │
│                                                              │
│  User triggers re-extract → agent spawns new sub-agent       │
│  → sub-agent's prompt includes all FieldCorrection records   │
│    for this doc and similar docs:                            │
│                                                              │
│    "flow_nominal was extracted as 335, corrected to 3.35.    │
│     Reason: misread decimal point. Be careful with decimals."│
│                                                              │
│  → sub-agent extracts with correction awareness              │
└──────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────┐
│  LEVEL 3: Cross-Session (global patterns)                    │
│                                                              │
│  Background job scans FieldCorrection across ALL sessions    │
│  Same correction × 3+ sessions → CorrectionPattern           │
│                                                              │
│  Example pattern:                                            │
│    "In French-form datasheets, PRESSION ASPIRATION means     │
│     suction pressure, not discharge. Extract as              │
│     suction_pressure."                                       │
│                                                              │
│  → Injected into ALL future agent system prompts             │
│  → System gets smarter without model fine-tuning             │
│                                                              │
│  Quality control:                                            │
│   • Requires 3 independent corrections to activate           │
│   • Can be deactivated manually if wrong                     │
│   • Plain text — inspectable, editable, understandable       │
└──────────────────────────────────────────────────────────────┘
```

---

## 9. Guardrails

### Extraction Guardrails

Every extracted value MUST have a citation (page, bounding box, source text). No citation = no extraction. The agent's system prompt enforces this:

```
"Not found" is a valid answer. Never invent values.
If unclear, extract with low confidence and explain why.
Three confidence tiers:
  High  (0.8-1.0): clearly printed, unambiguous
  Medium (0.5-0.8): readable but could be misread
  Low   (0.0-0.5): guessing, partially obscured
```

### Budget Guardrails

```
Per request:     max 30 iterations, 50k tokens
Per session:     max 500k tokens, $5.00
Per sub-agent:   max 20 iterations, 30k tokens
Per tool call:   30 second timeout
Context window:  max 16k tokens per LLM call
```

When approaching a limit → agent gets "wrap up" message.
When hitting a limit → loop breaks, progress is already saved to DB.

### Data Guardrails

Original extractions are never deleted. Corrections create new records. Every change is tracked: who, when, what, why. Full audit trail.

---

## 10. Error Handling

| Failure | What Happens |
|---------|-------------|
| **LLM API fails** (rate limit, 500, timeout) | LiteLLM retries with backoff. Arq retries the job. After exhaustion → error saved to conversation, document status → "failed" |
| **Tool call fails** (DB error, bad args) | Tool returns error message. LLM sees it in conversation and can adapt — retry, skip, or inform user |
| **Budget limit hit** | Progress already saved (fields are in DB). Agent returns explanation. User can continue with next message |
| **Server crashes mid-extraction** | Arq job persists in Redis. Conversation persists in PostgreSQL. On restart, worker picks up job. Agent sees prior state via DB tools |
| **Compaction fails** | Fallback: truncate oldest messages from context (lossy). Originals safe in DB |

The agent approach is naturally resilient: fields are saved to DB immediately via tools (not batched at the end), so any crash preserves all work completed up to that point.

---

## 11. Performance

| Operation | Time | Cost (Sonnet) | Cost (Gemini Flash) |
|-----------|------|---------------|---------------------|
| PDF pre-processing (per doc) | 2-3 sec | Free (CPU only) | Free (CPU only) |
| Extract one 3-page datasheet | 30-60 sec | ~$0.20 | Free |
| Extract 6 docs + validation | 3-6 min | ~$1.35 | Free |
| Simple query | 3-5 sec | ~$0.01 | Free |
| Context compaction (per batch) | 2-4 sec | ~$0.005 | Free |

Cost is dominated by extraction — multiple LLM turns per page, each carrying page images (~1,600 tokens per image). Queries are cheap because they only need 1-2 LLM turns with text-only tool results.

---

## 12. Scalability

```
More concurrent sessions?
  → API server is stateless, scale horizontally
  → Each session is independent, no cross-session coordination
  → PostgreSQL and Redis handle the concurrent load

Larger documents (10+ pages)?
  → Context management keeps per-call cost constant
  → Sub-agent processes one page at a time
  → Total time grows linearly with page count

More documents per session?
  → Sequential extraction time grows linearly
  → Can extend to parallel sub-agents if needed
    (trade-off: lose cross-doc learning during extraction,
     validation pass catches inconsistencies after)

Different document types (P&IDs, SOPs)?
  → Field taxonomy is just prompt text, not hardcoded
  → Tools are generic (save_field works for any doc type)
  → Entity system supports any type and relationship
  → New doc types = new prompts + maybe new tools
  → Agent engine, context management, data model stay same
```

---

## 13. What Makes This Different

A traditional pipeline: upload PDF → extract → return JSON. One shot, no conversation, no learning.

This system:

```
Traditional Pipeline          This System
─────────────────────         ────────────────────────────────
One-shot extraction     →     Agent REASONS about extraction,
                              self-corrects, cross-references

Fixed output            →     Conversational — user asks
                              follow-ups, corrects, guides

Field name + value      →     Field + value + unit + confidence
                              + page + bbox + exact source text

No learning             →     Corrections improve future
                              extractions within session AND
                              across sessions (global patterns)

Flat field list         →     Knowledge graph — entities link
                              docs → fields → relationships
                              enabling rich cross-doc queries
```
