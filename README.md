# Datasheet Backend Assignment

A large part of working at Raven is to extract knowledge and insights from a factory plant's documentation. In this example, you are going to extract structured fields from a process datasheet.

These structured fields can then be used for other applications like search, technical bid evaluation and more.

## Context

A process datasheet is a structured technical document that spells out what the equipment does and under what conditions (flows, temperatures, pressures, materials, etc). See the PDF files in the repo.

You can imagine a search agent using the knowledge in form of queries:
- What is the material for impeller in pump P300228?
- For P300228, what fluid is pumped, and what are the nominal and maximum flow rates?
- For P300228, whether the pump will corrode / erode over time?
- For P600173, what estimated efficiency of the motor?

Extracting process datasheets requires a few challenges: handling complex layouts, performance curves and more. In this assignment, you are going to work with a few simplified datasheets to extract relevant information.

## What You'll Build

1. **Extraction pipeline** — ingest a datasheet PDF, produce structured fields with citations.
2. **HITL feedback loop** — a web interface to review/correct extractions and explain how feedback will be used to improve the pipeline (can be manual or automated).

## Inputs Provided

Four datasheets: - `pds-P718.pdf`, `pds-P818.pdf`, `pds-P300228.pdf`, `pds-P600173.pdf`

## Output Schema

You are free to define the output schema on your own. However, note that it must be:
- generic enough to support a wide variety of fields
- generic enough to support a wide variety of use cases

Prefer schemas that balance flexibility with queryability and provenance.

## Logistics

- **Time:** 24 hours.
- **Tools:** Anything.
- **Stack:** Anything.
- **Submission:** Create a private fork of this github repo, and share it with the evaluator at the end of the task.

In your submission, present:
- A recorded demo of the appplication
- A write-up explaining: architecture, trade-offs, evaluation and cost metrics, future improvements.

If you are unsure, ask questions to clarify whether something is allowed.

## Evaluation Criteria

You will be evaluated on the following metrics:
- Extraction quality: accuracy, coverage
- Reliability and cost: cost per doc, understanding of failure modes
- Human-in-the-loop: how feedback is used to improve results and how ergonomic is it to provide feedback
- Communication: conciseness, clarity and the ability to explain yourself.

We are not expecting production-grade accuracy or model training. We care most about engineering judgment, system design, reliability, evaluation, and thoughtful trade-offs.

## What We're Not Looking For

Do not invest effort in:
- Authentication
- Deployment
- Fancy UI: beyond ergonomics for HITL interface