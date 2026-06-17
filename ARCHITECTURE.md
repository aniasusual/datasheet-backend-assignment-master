# Architecture: Datasheet Extraction System

## What This System Does

This is a conversational AI system that extracts structured, citable knowledge from industrial process datasheets. Users interact with the system through sessions — uploading documents, instructing the agent to extract data, asking questions about equipment, correcting mistakes, and watching the system get smarter from those corrections over time.

The design mirrors what Raven (StarRaven) builds: a retrieval-first AI system for industrial plant documentation where every answer carries source citations, the system has guardrails against hallucination, and human feedback creates a continuous improvement loop.

---

## The Core Idea

The system is built around one central concept: the agent is the interface. There is no separate extraction pipeline, no separate query engine, no separate correction workflow. The agent handles everything through conversation. When the user wants to extract fields, they tell the agent. When they want to ask a question, they ask the agent. When they want to correct a mistake, they tell the agent. The agent decides how to handle each request using its tools.

This is important because it means there are only two kinds of API endpoints in the system: the chat endpoint where users talk to the agent, and read-only data endpoints where the frontend fetches data to display. There is no HITL-specific router or correction-specific API. Corrections flow through the conversation — the user says "that's wrong, it should be X" and the agent handles it. This keeps the architecture simple and means the agent always has full context about what's happening.

---

## Tech Stack

The backend is Python with FastAPI. Python was chosen because it has the best ecosystem for PDF processing and AI integration. FastAPI is async-native and production-grade.

PDF processing uses pdfplumber with Pillow. pdfplumber extracts text with spatial coordinates, detects and parses tables, and provides page dimensions. Pillow renders pages to PNG images that can be sent to the LLM's vision capability. Together they give us both the raw text (for citations) and the visual layout (for the LLM to understand complex grid forms).

LLM access goes through LiteLLM, which is an open-source Python library (MIT license, no cost) that wraps multiple LLM providers behind a unified API. This lets us use free Gemini 2.0 Flash during development and switch to Claude Sonnet in production by changing one environment variable. No code changes needed — LiteLLM handles the differences in message formats, tool call conventions, and vision payload structures between providers.

The agent itself is a native Python loop — about 80 lines of code, no framework. We considered LangChain, LangGraph, Google ADK, and CrewAI, and rejected all of them. The agent loop is simple enough that a framework adds complexity without value. More importantly, we need full control over context management, cost tracking, and tool execution, which frameworks tend to abstract away.

The database is PostgreSQL with SQLAlchemy. We chose PostgreSQL over SQLite because the system can have multiple agent loops writing to the database simultaneously (the orchestrator plus its sub-agents). SQLite doesn't handle concurrent writes well. PostgreSQL also gives us better JSON support for storing tool call payloads and entity metadata.

Job execution uses Arq with Redis. When a user sends a message that triggers a long extraction (potentially 2-4 minutes for multiple documents), we need the agent to run asynchronously. Arq is a lightweight async job queue that gives us retries with exponential backoff, job persistence across server restarts, and concurrency control. Redis serves double duty as the Arq backend and as a pub-sub channel for real-time status updates to the frontend.

The frontend will be React with a chat interface, a PDF viewer with highlight overlays, and a field review panel. But the frontend is purely a display layer — all intelligence lives in the backend agent.

---

## Sessions

A session is the fundamental unit of the system. It represents a persistent conversation between a user and the agent.

Everything belongs to a session: documents, extracted fields, equipment entities, message history, cost records. When the user creates a new session, it starts completely empty — no documents, no context, no history. When they upload documents, those documents belong to that session. When the agent extracts fields, those fields belong to that session. When the user asks questions or makes corrections, those interactions are part of that session's message history.

Different sessions are completely isolated from each other. Session A knows nothing about Session B's documents, fields, or conversation. The only thing that crosses session boundaries is global correction patterns — if the same extraction mistake has been corrected the same way across three or more sessions, that pattern becomes part of the system's permanent knowledge and gets included in all future agent prompts.

A session's lifecycle is open-ended. The user creates it, uploads documents, asks the agent to extract, has a conversation about the results, corrects mistakes, uploads more documents, re-extracts, and so on. There is no "extraction phase" that ends — the session continues as long as the user wants, and new documents can be added at any time.

---

## Data Model

The data model has seven main entities.

A Session holds metadata like creation time and status, plus a reference to the compact summary of its conversation history (used for context window management, explained in the agent design document).

Messages store the complete conversation history for a session. Every user message, every agent response, every tool call and its result — all persisted with full fidelity. Each message has a sequence number for ordering, a role (user, assistant, tool, or system), the content, and metadata like token count. Messages also have a flag indicating whether they've been compacted (summarized and removed from the active context window). The full message is never deleted — compaction only affects what gets sent to the LLM.

Documents represent uploaded PDFs. Each document belongs to a session and has metadata like filename, number of pages, processing status, and the equipment tag extracted from the filename or content. A document has multiple DocumentPages, each storing the raw extracted text, any tables found by pdfplumber, the path to the rendered PNG image, and page dimensions. This pre-processing happens at upload time, before the agent ever sees the document — it's deterministic work that doesn't need an LLM.

ExtractedFields are the core output. Each field belongs to a document and represents one piece of extracted information: a field name (snake_case, normalized), a human-readable display name, the raw value exactly as it appears in the document, the unit if applicable, a data type (numeric, text, boolean, or reference), a section grouping (like operating_conditions or construction_materials), and a confidence score from 0 to 1. Crucially, every field has a citation: the page number, the bounding box coordinates on the page, and the exact source text snippet. Fields also track their status — extracted, verified (human confirmed correct), corrected (human changed the value), or rejected (human said it's wrong). When a field is corrected, the correction is stored as a separate FieldCorrection record that preserves the original value, the corrected value, the reason for the correction, and who made it. The original extraction is never destroyed.

EquipmentEntities form a lightweight knowledge graph. When the agent extracts from a datasheet, it identifies the main equipment (like pump P-718) and creates an entity for it. The entity has a tag (P-718), a type (centrifugal_pump), a name (Diesel Product Pump), and arbitrary metadata. Entities link to their source documents and to their extracted fields. Entities can also have relationships with each other — P-718 and P-818 might be siblings (parallel pumps in the same unit), or an entity might reference another one found in a note. This entity layer is what Raven calls their "plant data/model layer." When the user asks "tell me about P-718," the agent queries the entity and gets all linked documents, all extracted fields, and all related equipment in one shot.

CostRecords track every LLM call's token usage and dollar cost, linked to the session and the operation type (extraction, validation, query, re-extraction). This gives us per-session, per-document, and per-operation cost breakdowns.

---

## API Design

The API has two categories of endpoints: the chat endpoint where users interact with the agent, and read-only endpoints where the frontend fetches data for display.

The chat endpoint is the main interaction point. The user sends a message to a session, and the agent processes it — potentially making multiple tool calls before responding. The response comes back as text. This single endpoint handles extraction requests, questions, corrections, re-extraction requests, and anything else the user wants. The agent decides what to do based on the message content and its tools.

The read-only endpoints exist because the frontend needs to display data without invoking the agent. These include listing sessions, getting session details, listing documents in a session, fetching document pages and their rendered images, querying extracted fields with filters (by document, section, confidence level, status), fetching equipment entities and their relationships, getting message history for a session, and retrieving cost breakdowns. None of these endpoints modify data or trigger agent actions — they just read from the database.

There is no separate corrections endpoint. When the user corrects a field, they do it through the chat: "The impeller material for P-718 is wrong, it should be SS 316." The agent processes this, uses its update tool to modify the field and record the correction, and responds confirming the change. This way the agent has full context — it knows what was corrected, why, and can proactively apply the same correction to related documents.

---

## Feedback and Learning

The system learns from human corrections at three levels.

At the session level, corrections are immediate. When the user corrects a field, the agent is right there in the conversation. It sees the correction, understands the reason, and carries that knowledge forward for the rest of the session. If the user then asks the agent to re-extract another document, the agent already knows about the correction and can apply it.

At the re-extraction level, corrections are injected into the agent's prompt. When the agent spawns a sub-agent to re-extract a document, it gathers all past corrections for that document (and related documents in the session) and includes them in the sub-agent's system prompt as explicit guidance: "This field was previously extracted as X but corrected to Y because Z. Apply similar care." This is few-shot learning through prompt engineering — no model fine-tuning needed.

At the global level, the system detects patterns across sessions. If the same field is corrected the same way in three or more different sessions, that correction becomes a permanent pattern. These patterns are stored in the database and included in every future agent's system prompt, regardless of session. For example, if reviewers keep correcting the system's interpretation of "PRESSION ASPIRATION" in French-form datasheets, that becomes permanent guidance: "In French-form datasheets, PRESSION ASPIRATION means suction pressure, not discharge pressure." The system gets better over time without any model training.

Accuracy is tracked continuously. The system knows, per field name, per section, and per document format type, what percentage of extractions were verified correct versus corrected versus rejected. This data surfaces in a dashboard showing extraction quality trends, most-problematic fields, and improvement over time.

---

## Guardrails

The system follows Raven's principle: "If the fact isn't in the corpus, say so."

Every extracted value must have a citation — page number, bounding box, and exact source text. If the agent cannot find a source for a value, it does not extract it. If the agent is unsure, it extracts with low confidence and explains the ambiguity. The agent is explicitly instructed in its system prompt to never invent values, never guess, and to treat "not found" as a perfectly valid answer.

Confidence is reported in three tiers: high (0.8-1.0) means clearly printed and unambiguous, medium (0.5-0.8) means readable but could be misread or is inferred from context, and low (below 0.5) means the agent is guessing or the value is partially obscured. The agent flags low-confidence extractions to the user and asks for verification.

The agent also has budget guardrails to prevent runaway costs. There are hard limits on iterations per request, tokens per request, tokens per session, and dollar cost per session. When any limit is approaching, the agent gets a system message telling it to wrap up. If the hard limit is hit, the loop breaks and the agent explains what happened. Individual tool calls have timeouts, and sub-agents have their own separate budgets that are lower than the orchestrator's.

Data integrity is maintained through immutable history. Original extractions are never deleted — corrections create new records. Every change to a field is tracked with who changed it, when, and why. This gives a complete audit trail.

---

## Cost Model

Extraction cost depends on the LLM provider. With Claude Sonnet, a typical 3-page datasheet costs roughly $0.20 to extract (accounting for multiple agent turns, page images, and output tokens). A session with 6 documents plus validation and a few user queries runs about $1.35.

With Gemini 2.0 Flash, extraction is free during development thanks to Google's free tier. This is why LiteLLM is valuable — we develop and test for free, then switch to Claude for production quality with one config change.

The cost tracking system records every LLM call with input tokens, output tokens, calculated cost, and wall-clock duration. This data is available per session and per document, so we can report exact cost-per-document figures and identify optimization opportunities.

---

## Key Architectural Decisions

Session as the core abstraction, not document. This mirrors Raven's product where the agent is a persistent interface, not a one-shot extraction tool. Documents belong to sessions, not the other way around.

Agent as the sole interaction point. There are no separate HITL endpoints or correction APIs. Everything flows through the conversation. This keeps the architecture simple, gives the agent full context, and means the agent can proactively act on corrections.

Native Python agent loop with no framework. The loop is about 80 lines of code. Frameworks like LangChain would add thousands of lines of abstraction for the same result while making debugging harder, context management opaque, and the system harder to explain.

LiteLLM for provider-agnostic LLM access. Free Gemini for development, Claude for production. One environment variable to switch.

PostgreSQL for concurrent writes. Multiple agent loops (orchestrator plus sub-agents) write to the database simultaneously. SQLite would choke on this.

Arq plus Redis for job persistence. Agent runs can take minutes. If the server restarts, Arq preserves the jobs. Built-in retries handle transient API failures.

Knowledge graph via equipment entities. Documents are not isolated — they represent equipment that has relationships. The entity layer connects documents to fields to each other, enabling cross-document queries.

Linked-list context management with compaction. Sessions can have hundreds of messages. The database stores everything, but the LLM only sees a sliding window: a compact summary of old messages plus full recent messages. This keeps cost predictable and context size bounded regardless of session length. Details are in the agent design document.

Correction-as-prompt for learning. Human corrections are injected into agent prompts as few-shot examples. No fine-tuning infrastructure needed. Common corrections across sessions become permanent system knowledge.

Guardrails as first-class. Every value needs a citation. "Not found" is valid. The agent never guesses. This mirrors Raven's approach: "If it's not in the corpus, say so."
