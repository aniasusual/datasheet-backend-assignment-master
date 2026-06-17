# Agent Design: How the Extraction Agent Works

## What the Agent Is

The agent is a conversational AI that lives inside a session. It is the primary interface to the entire system. The user talks to it, it talks back, it uses tools to read documents, extract fields, build knowledge, and answer questions. It also handles corrections — when the user says something is wrong, the agent fixes it, records the correction, and learns from it.

Think of it as a senior engineer sitting at a desk with a stack of datasheets. You hand them documents, ask them to extract data, ask questions about what they found, correct their mistakes, and they learn from those corrections. That engineer doesn't disappear after extraction — they stay available for the whole session.

The agent is not a background job. It is not a pipeline. It is the interface.

---

## The Agent Loop

The core of the agent is a while loop. When the user sends a message, the agent receives it and enters the loop. In each iteration, the agent sends the conversation history to the LLM and gets back a response. If the response contains tool calls, the agent executes those tools, appends the results to the conversation, and loops again. If the response is plain text with no tool calls, the agent returns that text to the user and exits the loop.

A simple question like "what's the flow rate for P-718?" might take one iteration — the agent calls a search tool, gets the answer, and responds. A full extraction of 4 documents might take 50+ iterations across multiple sub-agent spawns, with the agent calling tools to read pages, save fields, create entities, and verify its work.

Every message in the loop — user messages, assistant responses, tool calls, tool results — is persisted to the database immediately. Nothing is kept only in memory. If the server crashes mid-extraction, the full conversation up to that point is preserved in the database.

The loop has guardrails to prevent runaway execution. There's a maximum number of iterations per user message (default 30), a token budget per request, a cost budget per session, and timeouts on individual tool calls. When any limit is approaching, the agent gets a system message telling it to wrap up. If the hard limit is hit, the loop breaks and returns an explanation.

---

## Context Window Management

Every LLM call sends the conversation history as input. A session that's been running for an hour might have 200+ messages — user messages, agent responses, tool calls, tool results. Sending all of that every time would be enormously expensive (cost grows quadratically because each call resends everything) and would eventually exceed the model's context limit.

The solution is a linked-list structure with compaction. All messages live in the database, ordered by sequence number. A head pointer divides them into two zones: the compacted zone (everything before the pointer) and the active zone (everything from the pointer to the latest message).

When the agent needs to call the LLM, it builds the context window from three parts. First, the system prompt — always included, contains the agent's instructions, guardrails, and any global correction patterns. Second, a compact summary of everything in the compacted zone — a short paragraph describing what has happened earlier in the session (what documents were uploaded, what was extracted, what corrections were made, key instructions from the user). Third, the full verbatim messages from the head pointer to the latest message.

This keeps the context window at roughly 8-12k tokens per call, regardless of how long the session has been running. The cost per LLM call stays constant.

When the active zone grows too large and the total context approaches the limit, compaction happens. The system takes the oldest batch of full messages from the active zone, summarizes them using a fast cheap LLM call, merges that summary into the existing compact summary, marks those messages as compacted in the database, and advances the head pointer forward. The original messages are never deleted from the database — compaction only affects what gets sent to the LLM.

The compact summary preserves what matters: what documents exist and their status, what fields were extracted (counts and key values), what corrections the user made and why, what the agent learned about conventions and patterns, and key instructions from the user. What gets lost is the raw page images from old tool calls, verbose tool result payloads, intermediate reasoning steps, and the exact conversation flow. But all of this is recoverable — the agent can always use its tools to query the database for old data if it needs it.

---

## Tools

Tools are how the agent interacts with the world. They are Python functions that the agent can call by name, with arguments. The LLM decides which tools to call and when, based on the conversation and the task at hand.

The tools fall into five categories.

**Document tools** let the agent see what's in the session. It can list all documents, get metadata about a specific document (filename, page count, format type, processing status), fetch the full content of a specific page (the rendered PNG image, the extracted text, and any tables), or get just the text from all pages without images (cheaper, for quick scanning).

**Extraction tools** let the agent save and manage extracted fields. It can save a new field with all its metadata (field name, value, unit, confidence, section, citation details), update an existing field, delete a wrongly created field, check its extraction progress for a document (how many fields saved, grouped by section, broken down by confidence level), and mark a document's extraction as complete.

**Entity tools** let the agent build the knowledge graph. It can create an equipment entity with a tag, type, and name, link an entity to a document or to specific extracted fields, create relationships between entities (sibling, parent, references), search entities by various filters, and get full detail about an entity including all its linked documents, fields, and relationships.

**Query tools** let the agent search and retrieve information. It can search across extracted fields by name, value, or section, retrieve the correction history for a specific document or field, and fetch global correction patterns that have been learned across sessions.

**Sub-agent tools** let the agent delegate intensive tasks. It can spawn an extraction sub-agent for a specific document, or spawn a validation sub-agent to cross-check all extractions in the session. These are described in detail below.

---

## Sub-Agents

The orchestrator agent can spawn sub-agents for tasks that are intensive enough to warrant their own isolated context. A sub-agent uses the same agent loop code, but with its own fresh context window, a focused system prompt, a subset of tools relevant to its specific task, and its own budget limits (lower than the orchestrator's).

The reason for sub-agents is context isolation. Extraction involves loading page images, which are large — roughly 1,600 tokens per page image. If the orchestrator loaded all pages from all documents into its own context, it would quickly blow past any reasonable context limit. Instead, the orchestrator spawns a sub-agent per document. The sub-agent works in its own clean context, saves everything to the database as it goes, and returns a short text summary to the orchestrator. The orchestrator's context stays lean — it only ever sees summaries, never raw page images.

There are two types of sub-agents.

**Extraction sub-agents** are spawned once per document, sequentially. The orchestrator processes documents one at a time, not in parallel. This is a deliberate choice: each subsequent extraction benefits from what previous ones found. When the second sub-agent starts, it can check the database to see what fields and naming conventions the first sub-agent used, and be consistent. Correction history from earlier in the session is also available to later sub-agents.

An extraction sub-agent's job is to extract all structured fields from one document. It works page by page — fetching the page content (image plus text plus tables), identifying fields, saving each one to the database with full citation metadata, and then moving to the next page. After all pages, it reviews its own work by checking the extraction progress, looking for gaps or inconsistencies. It also creates an equipment entity for the main equipment on the datasheet and links it to the document and fields. When done, it marks the document as complete and returns a summary to the orchestrator.

The extraction sub-agent has explicit instructions in its prompt: work methodically page by page, extract every field that has a value, never invent values, separate values from units, use the field taxonomy for consistent naming, and report low confidence honestly. It also receives any correction history relevant to this document or similar documents, so it can avoid known mistakes.

**Validation sub-agents** are spawned once after all extractions complete. The validation sub-agent's job is to cross-check all extractions across all documents in the session. It looks at the big picture: are field names consistent across documents (did one extraction use "flow_nominal" while another used "nominal_flow")? Are units consistent or explicitly different? Are there coverage gaps (one document has 28 fields but another similar one only has 15)? Are there entity relationships to create (P-718 and P-818 are sibling pumps in the same unit)? Are there cross-references in notes that should be linked?

The validation sub-agent can auto-fix simple issues like inconsistent field naming. For more ambiguous issues, it generates warnings that the agent surfaces to the user. It returns a summary to the orchestrator describing what it found and what it fixed.

---

## How Corrections Work

Corrections flow through the conversation, not through a separate API. When the user tells the agent something is wrong, the agent handles it directly.

The agent uses its update tool to modify the field in the database. The tool creates a correction record preserving the original value, the new value, the reason, and who made the correction. The original extraction is never destroyed — corrections are additive. The agent then confirms the change and, because it's in the conversation, it has full context about what happened. It might proactively offer to check related documents for the same issue.

When the user later asks the agent to re-extract a document, the agent spawns a new extraction sub-agent that receives all past corrections as part of its prompt context. The sub-agent knows: "This field was previously extracted as X but corrected to Y because of Z. Be careful about this." This is how corrections feed back into extraction quality without any model fine-tuning.

The agent can also be instructed to flag uncertain extractions proactively. Its system prompt tells it: when confidence is below a threshold, ask the user for verification instead of silently saving a potentially wrong value. This is the agent deciding it needs human intervention, driven by its prompt — not by a separate HITL system.

---

## The System Prompt

The system prompt defines the agent's behavior and is always the first message in the context window. It covers several areas.

The agent's identity and capabilities: it specializes in industrial process plant documentation, extracts structured fields from datasheets, answers questions with source citations, builds a knowledge graph of equipment, and learns from corrections.

Guardrail rules: citations are mandatory for every factual claim (page number, source text, confidence score). The agent never hallucinate — if a value is not in the document, it says "not found." If a value is ambiguous, it reports low confidence and explains. "I don't know" is always better than a wrong answer.

Extraction guidelines: work page by page, extract all fields with values, separate values from units, normalize field names using the taxonomy, use English names for bilingual documents, treat notes as fields too.

The available tools and their descriptions, so the LLM knows what it can call.

Global correction patterns learned from past sessions, so the agent starts with accumulated knowledge about common mistakes and how to avoid them.

The prompt is the same for the orchestrator agent. Sub-agents get their own specialized prompts — the extraction sub-agent gets a focused extraction prompt with the field taxonomy and document-specific correction history, while the validation sub-agent gets a cross-checking prompt.

---

## Budget Guards

The system has multiple layers of cost and runaway protection.

Per-request limits cap how much work the agent does for a single user message. There's a maximum number of LLM iterations (default 30) and a cumulative token budget per request. This prevents a single "extract everything" request from running indefinitely.

Per-session limits cap the total cost of a session across its lifetime. There's a maximum total token count and a maximum dollar cost. When these limits approach, the agent is warned. When they're hit, execution stops.

Per-tool limits set timeouts on individual tool executions, preventing a single tool call from hanging.

Per-sub-agent limits give sub-agents their own budgets that are lower than the orchestrator's. If an extraction sub-agent goes haywire on one document, it burns its own budget without affecting the rest of the session.

Context window limits cap how many tokens can go into a single LLM call, triggering compaction when exceeded.

---

## Design Rationale

The agent-first design (everything through conversation, no separate HITL endpoints) was chosen because it keeps the agent in the loop for every interaction. The agent always has context. It can proactively act on corrections, ask clarifying questions, and learn within the session. A separate correction API would create a blind spot where fields change without the agent knowing.

Sub-agents exist for context isolation, not for parallelism. Extraction loads heavy content (page images) that would bloat the orchestrator's context. Sub-agents work in clean contexts, save to the database, and return summaries. The orchestrator stays lean.

Sequential extraction over parallel was chosen because each subsequent extraction benefits from the previous one. The second document's sub-agent can see what naming conventions and patterns the first one used. For 4-6 datasheets of 2-3 pages each, sequential takes 2-4 minutes versus 1-2 minutes for parallel. The quality improvement from cross-document consistency is worth the extra minute.

The native Python loop over any framework was chosen because the loop is simple and the value of frameworks (abstraction, orchestration) doesn't apply when you have one loop, one set of tools, and one conversation pattern. What matters is what we build on top of the loop — the context management, the cost tracking, the tool implementations, the prompt design. Those are where the real engineering is.

LiteLLM was chosen because swapping between free Gemini for development and Claude for production is a one-variable change. The cost savings during development are real, and the provider-agnostic design means we're not locked into any single model vendor.

The linked-list context with compaction was chosen because sessions need to be long-lived. A user might work with the agent for hours across dozens of interactions. The database stores everything permanently, but the LLM only sees what it needs — a summary of the past plus the recent conversation. Cost per turn stays constant regardless of session age.
