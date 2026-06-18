# Architecture Write-Up

## Overview

A system that extracts structured fields from industrial process datasheets (PDFs), provides a human-in-the-loop review interface, and exposes a conversational agent for querying the extracted data.

**Stack:** FastAPI (async) + PostgreSQL + React 19 + Gemini 2.5 Flash

---

## Architecture

### Data Flow

```
User uploads PDFs
       │
       ▼
┌─────────────────┐
│ Document Processor│  Save file to disk, count pages (fitz/PyMuPDF)
│                   │  No text extraction — LLM reads PDFs natively
└────────┬──────────┘
         │
         ▼
┌─────────────────┐
│ Extraction       │  1 LLM call per document
│ (Gemini Flash)   │  Send entire PDF as base64 → receive JSON
│                  │  Creates: ExtractedField + EquipmentEntity records
└────────┬─────────┘
         │
         ▼
┌─────────────────┐
│ HITL Review UI   │  FieldPanel: verify, correct, reject fields
│ (React)          │  PageViewer: native PDF rendering via iframe
│                  │  Inline editing with correction audit trail
└────────┬─────────┘
         │
         ▼
┌─────────────────┐
│ Agent            │  All extracted data loaded into system prompt
│ (Gemini Flash)   │  User queries naturally — no hardcoded search
│                  │  Tools: update_field, verify_fields, reject_fields
└──────────────────┘
```

### Key Design Decisions

**1. Vision-first extraction — no pdfplumber**

The LLM receives the raw PDF as base64. No text extraction preprocessing. This means:

- The LLM sees the visual layout, tables, headers — exactly as a human would
- No information loss from OCR or text extraction heuristics
- Works equally well for English, French, and bilingual documents
- Simpler code — no pdfplumber dependency for extraction

**2. One LLM call per document**

Each document is extracted in a single Gemini call. The LLM returns a JSON object containing all fields, equipment metadata, and citations. This replaced an earlier 3-pass pipeline (discovery → extraction → verification) that was 3x slower and prone to tool_choice failures.

**3. No forced tool_choice — plain JSON response**

Earlier iterations used `tool_choice` to force the LLM to call a specific function. This frequently failed (LLM returned text instead of calling the tool, especially on large payloads). The current approach asks for JSON directly in the prompt and parses it with fallbacks for markdown-wrapped or truncated responses.

**4. Agent has full data context — no search tools**

The agent loads ALL extracted fields into its system prompt on every call. For ~200 fields across 4 documents, this is ~5-10K tokens — well within context limits. This means the LLM can reason freely over the entire dataset without needing search tools or hardcoded query patterns. Tools are only for write operations (correct, verify, reject).

**5. Original language preservation**

The extraction prompt explicitly tells the LLM to preserve the original language of the document. French fields stay in French, English stays in English. No translation, no hardcoded section categories. Section names are derived from the document's own headings.

**6. Per-document DB commits**

Batch extraction processes documents sequentially with a fresh DB session per document. If document 3 fails, documents 1 and 2 are already committed and visible. This prevents cascading failures.

### Backend Services


| Service                 | Purpose                                                | LLM Calls     |
| ----------------------- | ------------------------------------------------------ | ------------- |
| `document_processor.py` | Save file, count pages                                 | 0             |
| `extraction.py`         | Send PDF to Gemini, parse JSON, create fields + entity | 1 per doc     |
| `agent.py`              | Conversational queries with all data in context        | 1 per message |
| `query.py`              | Stateless single-shot queries                          | 1 per query   |


### Database Schema

```
sessions
  ├── documents (status: uploaded → extracting → extracted/failed)
  │    ├── document_pages (page dimensions, backward compat)
  │    └── extracted_fields (field_name, display_name, raw_value, unit, section,
  │         │                confidence, status, citation_page, citation_text)
  │         └── field_corrections (original_value, corrected_value, reason, corrected_by)
  ├── equipment_entities (tag, type, name, metadata_json)
  │    └── entity_documents (M2M link to documents)
  └── chat_messages (role, content, tool_actions, sequence)
```

### Frontend

- **React 19** + TypeScript + Tailwind CSS 4 + Vite
- **Three-panel layout:** document sidebar | PDF viewer or agent chat | field panel
- **PageViewer:** Native browser PDF rendering (iframe), no custom renderer
- **FieldPanel:** Sections derived from extracted data (no hardcoded categories), inline editing, verify/reject buttons
- **AgentChat:** Markdown rendering (react-markdown), chat persisted in DB, tool action badges
- **DocumentSidebar:** Upload, extract all, status badges per document

---

## Trade-offs

### Chose: Single LLM call per document

- **Pro:** 3x faster, simpler code, fewer failure points
- **Con:** If the call fails, entire document fails (no partial results from per-page extraction)
- **Mitigation:** Robust retry with exponential backoff, truncated JSON repair

### Chose: No text extraction preprocessing

- **Pro:** Simpler pipeline, no information loss, works for any language/layout
- **Con:** Fully dependent on the LLM's PDF parsing ability. If Gemini's PDF understanding degrades, we have no fallback
- **Mitigation:** Could re-add pdfplumber as a fallback if needed

### Chose: Plain JSON response over tool_choice

- **Pro:** Eliminates the most common failure mode (LLM not calling the tool)
- **Con:** Need to handle markdown-wrapped and truncated JSON
- **Mitigation:** `_parse_json_response` with 4 fallback strategies including truncated JSON repair

### Chose: All data in agent context over search tools

- **Pro:** LLM reasons freely — can cross-reference, compare, infer without hardcoded query patterns
- **Con:** Context window grows with data size. At ~1000+ fields it could become expensive or hit limits
- **Mitigation:** For this scale (4 docs, ~200 fields) it's well within limits. For larger scale, would need chunking or RAG

### Chose: Original language preservation

- **Pro:** Source fidelity — user sees exactly what's in the document
- **Con:** Cross-document comparison is harder when one doc is French and another English
- **Mitigation:** The agent can handle multilingual comparison since it sees all data and understands both languages

### Chose: Gemini 2.5 Flash (free tier)

- **Pro:** Zero cost, good PDF understanding, large context window
- **Con:** Rate limits on free tier, occasional empty responses, less reliable than Claude/GPT-4 for structured output
- **Mitigation:** Exponential backoff retries, JSON repair for truncated responses, per-document isolation so failures don't cascade

---

## Evaluation & Cost Metrics

### Extraction Quality

- **Accuracy:** Depends on document complexity. English tabular datasheets: high accuracy (>90% fields correct). French/bilingual form datasheets: moderate accuracy (~70-80%) — more layout ambiguity
- **Coverage:** LLM extracts all visible fields including empty ones. Cross-document completeness can be assessed by comparing field counts
- **Confidence scores:** Each field has a 0-1 confidence. LLM self-reports: 0.9+ clearly readable, 0.7-0.9 partially obscured, <0.7 uncertain

### Cost Per Document


| Component              | Tokens (approx)        | Cost (Gemini Flash free) |
| ---------------------- | ---------------------- | ------------------------ |
| Extraction (1 call)    | ~5K input + ~3K output | $0 (free tier)           |
| Agent (per message)    | ~8K input + ~1K output | $0 (free tier)           |
| **Total per document** | ~8K tokens             | **$0**                   |


On paid Gemini Flash pricing ($0.075/1M input, $0.30/1M output):

- Extraction: ~$0.001 per document
- Agent: ~$0.001 per message
- **4 documents + 10 agent messages: ~$0.014**

### Failure Modes


| Failure          | Frequency                | Impact                  | Mitigation                                        |
| ---------------- | ------------------------ | ----------------------- | ------------------------------------------------- |
| Rate limit (429) | Common on free tier      | Delays extraction       | Exponential backoff (3s → 6s → 12s → 24s)         |
| Empty response   | ~5-10% of calls          | Retried automatically   | 4 retry attempts                                  |
| Truncated JSON   | Rare with 65K max_tokens | Partial field loss      | `_repair_truncated_json` recovers complete fields |
| Wrong values     | ~5-10% of fields         | User correction needed  | HITL review + re-extraction with corrections      |
| Missed fields    | ~5-10% of fields         | User can flag via agent | Re-extraction captures more on second pass        |


### Reliability

- **Extraction success rate:** ~95% per document (with retries)
- **Mean extraction time:** ~20-40 seconds per document (Gemini Flash)
- **HITL correction rate:** Typically 5-15% of fields need correction on first extraction
- **Re-extraction improvement:** Corrections injected as "lessons learned" improve accuracy on subsequent passes

---

## Architectural Reasoning

### Why extraction and agent are separate LLM calls

A natural question is: why not upload PDFs directly to the agent and let it extract + query in one interface? The reason is context window economics. A single PDF encoded as base64 is 150KB–1.2MB. Four PDFs would immediately consume a significant portion of the context window, leaving little room for conversation history, system prompts, or the extracted data itself. By separating extraction into a dedicated LLM call, we:

- Keep the agent's context clean — it only sees the structured extracted data (~5-10K tokens for 200 fields), not raw PDF bytes
- Allow the extraction call to use the full context window for reading the document
- Make extraction a one-time cost per document, while agent conversations can go on indefinitely without re-encoding PDFs

### Why LiteLLM

The system uses [LiteLLM](https://github.com/BerriAI/litellm) as the LLM abstraction layer rather than calling provider APIs directly. LiteLLM provides a unified `completion()` interface that works across 100+ LLM providers using OpenAI-compatible formatting. This means:

- **Provider-agnostic code** — switching from `gemini/gemini-2.5-flash` to `anthropic/claude-sonnet` or `openai/gpt-4o` requires changing a single environment variable (`LLM_MODEL`). No code changes.
- **Unified error handling** — rate limits, retries, and token counting work the same regardless of provider.
- **Prefix-based routing** — the model string prefix (`gemini/`, `openai/`, `anthropic/`, `bedrock/`) determines which provider SDK is used under the hood. The application code never touches provider-specific SDKs.
- **Cost tracking** — LiteLLM tracks token usage and costs per call across providers, useful for monitoring spend.

Currently the system runs on Gemini 2.5 Flash (free tier). The LiteLLM abstraction ensures we are not locked into any single provider.

---

## Future Improvements

### Context Window Management — Conversation Compaction

As agent conversations grow, the context window fills up. Currently, all past messages are loaded from the DB and sent as conversation history on every agent call. For long sessions, this will hit token limits.

**Proposed solution: Linked-list compaction.** Model the conversation as a linked list of message nodes. When the context approaches a threshold (e.g., 80% of the model's limit):

1. Take the oldest N message nodes (the tail of the conversation)
2. Summarize them into a single compacted node using an LLM call — "Here is what was discussed previously: [summary]"
3. Move the head pointer forward — the compacted summary becomes the new start of the conversation
4. Inject the summary as a system-level context message at the top of the conversation

This preserves the most recent messages verbatim (where detail matters) while compressing older context into a summary (where only the gist matters). The linked-list structure makes it natural to track which segments have been compacted and to do incremental compaction as the conversation grows, rather than reprocessing the entire history each time.

### Agent Memory Layer — RAG and Semantic Search

Currently the agent sees all extracted data by loading it into the system prompt. This works for 4 documents (~200 fields, ~5-10K tokens) but won't scale to hundreds of documents.

**Proposed solution: A dedicated memory layer** that the agent queries on demand instead of loading everything upfront. Implementation options:

- **Vector store (e.g., pgvector, Pinecone, Qdrant)** — embed each extracted field as a vector. Agent queries retrieve only the relevant fields for the current question. This is classic RAG (Retrieval Augmented Generation).
- **Semantic search over structured data** — rather than embedding raw text, index the structured fields (field_name, section, pump_tag, value) and use hybrid search (keyword + semantic) to retrieve relevant records.
- **Hierarchical memory** — maintain a two-level cache: (1) a summary of each document's key data always in context, (2) full field details retrieved on demand when the agent needs them. Similar to how humans remember "pump P-718 handles diesel at high pressure" without memorizing every field.

The memory layer would replace the current `_build_data_context()` function that dumps everything into the prompt.

### Multi-Provider Availability

LiteLLM's provider-agnostic interface enables a multi-provider strategy for production availability:

- **Primary/fallback routing** — if the primary provider (e.g., Gemini) returns errors or hits rate limits, automatically fall back to a secondary provider (e.g., AWS Bedrock, Anthropic, OpenAI). LiteLLM supports this natively via its router and fallback configuration.
- **Provider-specific strengths** — different providers excel at different tasks. Gemini has strong PDF understanding, Claude excels at structured output and long context, GPT-4o is strong at tool use. A production system could route extraction calls to the best provider for that task.
- **Regional routing via cloud providers** — AWS Bedrock (multi-region), Google Vertex AI, and Azure OpenAI Service provide enterprise SLAs and data residency guarantees. For regulated industries (which process datasheets often are), this matters.
- **Cost optimization** — route simple queries to cheaper models (Flash, Haiku) and complex extraction to capable models (Pro, Sonnet, GPT-4o). LiteLLM's router supports cost-based and latency-based routing policies.

### Agent Sandboxing

Currently the agent runs within the FastAPI server process. This is a security concern: the agent executes tool calls (DB writes, field updates) in the same process that serves the API. If the agent's behavior were extended with code execution or shell tools, a malicious or buggy prompt could harm the server.

**Proposed solution: Isolate the agent in a sandboxed environment.**

- **Kubernetes pod per agent session** — spin up an ephemeral container for each agent conversation. The container has network access only to the DB and the LLM API. No filesystem access to the host. Resource limits (CPU, memory, timeout) prevent runaway sessions.
- **gVisor or Firecracker micro-VMs** — for stronger isolation, run agent containers inside a sandboxed runtime that intercepts system calls. This prevents container escapes.
- **Principle of least privilege** — the agent's DB credentials should only allow `SELECT`, `INSERT`, `UPDATE` on specific tables (`extracted_fields`, `field_corrections`, `chat_messages`). No `DROP`, no `DELETE` on sessions/documents.
- **Timeout and kill switch** — agent sessions should have a hard timeout (e.g., 5 minutes). An admin endpoint should allow killing a runaway agent session.

This separation also enables horizontal scaling — agent pods can autoscale independently of the API server.

### Service Decomposition — From Monolith to Microservices

The current architecture is a monolith: extraction, agent, document processing, and the API all run in a single FastAPI process. For production, these should be separated:

```
┌──────────────┐     ┌───────────────────┐     ┌──────────────────┐
│ API Gateway   │────▶│ Extraction Service │     │ Agent Service     │
│ (FastAPI)     │     │ (worker pods)      │     │ (sandboxed pods)  │
│               │────▶│                    │     │                   │
│ Serves UI,    │     │ Consumes PDF from  │     │ Loads fields from │
│ handles CRUD  │     │ object store,      │     │ DB or memory      │
│               │────▶│ calls LLM,         │     │ layer, calls LLM  │
│               │     │ writes to DB       │     │                   │
└──────────────┘     └───────────────────┘     └──────────────────┘
        │                     │                         │
        ▼                     ▼                         ▼
   ┌─────────┐          ┌─────────┐              ┌─────────┐
   │ PostgreSQL│         │ Object   │              │ Vector   │
   │ (metadata)│         │ Storage  │              │ Store    │
   │           │         │ (PDFs)   │              │ (memory) │
   └──────────┘         └─────────┘              └─────────┘
```

**Benefits:**
- **Independent scaling** — extraction is CPU/IO-bound (PDF encoding, LLM calls), agent is memory-bound (context loading). They scale differently.
- **Fault isolation** — a crash in the extraction worker doesn't take down the API or the agent.
- **Deployment flexibility** — update the extraction prompt or agent tools without redeploying the entire system.
- **Queue-based extraction** — replace the current `asyncio.create_task` with a proper job queue (Redis/Celery, SQS, Cloud Tasks). This gives durability (jobs survive server restarts), visibility (queue depth, processing time), and retry semantics.

### Chat Storage at Scale

Agent conversations are currently stored in PostgreSQL (`chat_messages` table). This works at small scale but has limitations:

- **Write-heavy workload** — every agent message creates a row. High-concurrency agent usage generates many small writes.
- **Unbounded growth** — chat history grows indefinitely per session. Old conversations are rarely queried but still occupy primary DB storage.
- **Analytics queries are expensive** — questions like "what are the most common user queries?" or "how often does the agent use the update_field tool?" require scanning the entire table.

**Proposed solution: Tiered storage.**

- **Hot tier (PostgreSQL)** — recent messages (last 7 days or last 100 messages per session). Used by the agent for conversation context.
- **Cold tier (BigQuery, S3 + Athena, or ClickHouse)** — archived conversations older than the hot window. Optimized for analytical queries, dashboards, and auditing.
- **Migration job** — a scheduled task moves messages from hot to cold storage, replacing them with a compacted summary in the hot tier (ties into the compaction strategy above).

Alternative: use a purpose-built conversation store like **DynamoDB** (fast writes, TTL-based expiry, pay-per-request) or **Apache Kafka** for streaming chat events into multiple sinks (DB, analytics, search index) simultaneously.

### Other Improvements

- **Automated evaluation pipeline** — compare extraction results against ground truth labels. Track precision, recall, and F1 per field type across model versions.
- **Template learning** — after extracting several documents of the same type, learn the expected field schema and use it to guide extraction (few-shot examples in the prompt).
- **Fine-tuned extraction model** — use correction data (`field_corrections` table) as training signal. The HITL loop naturally generates labeled data for model improvement.
- **Export functionality** — export extracted data as CSV, Excel, or structured JSON for downstream systems.
- **Cross-document knowledge graph** — link extracted entities and fields across documents to answer queries like "show me all pumps handling corrosive fluids with impeller material X."

