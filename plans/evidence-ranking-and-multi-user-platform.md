# Biolab Evidence Ranking and Multi-User Review Platform

## Objective

Build a transparent paper-review workflow on top of the existing Biolab
retrieval ledger. A scientist should be able to ask a focused question, inspect
the top five returned papers, see exactly why each paper was ranked, correct or
confirm the ranking, save reviewed evidence into a private project, export the
review as PDF/Markdown/JSON, and remove temporary parser data without deleting
the preserved source record by accident.

The same workflow must be usable through the dashboard, REST, MCP, Claude
clients, ChatGPT-compatible remote MCP clients where enabled, and A2A agents.

## Non-negotiable product boundaries

1. A ranking score means “fit to this query and review context,” not “truth,”
   “clinical validity,” or “best paper in the field.”
2. Every score component must be stored and displayed. No opaque final score.
3. Source retrievals remain provenance records. Parser output and ranking runs
   are derived, disposable material.
4. Human review is required before a paper becomes reviewed project evidence or
   supports a claim.
5. Every tenant-scoped read and write must enforce organization/project access
   in the service layer, not only in the UI.
6. A failed parser, ranking job, export, or source request must be visible as a
   failed operation, never represented as an empty successful result.

## Target scientist workflow

```text
question
  -> retrieve and preserve source records
  -> normalize and deduplicate candidates
  -> transparent ranking run
  -> top-five review queue with score explanations
  -> scientist labels, notes, and corrections
  -> save selected papers to a project collection
  -> generate report and download PDF/JSON/Markdown
  -> flush disposable parser/ranking artifacts
```

The original retrieval records remain available for audit unless an explicit
retention/deletion policy says otherwise.

## Ranking design

### Ranking run contract

Create a first-class `ranking_run` for each question and candidate set:

```text
ranking_run_id
project_id (nullable for unassigned review)
question
query_facets (JSON)
model_version
algorithm_version
requested_limit
status: queued | running | succeeded | partial | failed
created_by
created_at / completed_at
```

Each candidate is linked by immutable `retrieval_id`:

```text
ranking_candidate_id
ranking_run_id
retrieval_id
rank
score_total
score_components (JSON)
reason_codes (JSON)
matched_terms (JSON)
missing_facets (JSON)
review_state: unreviewed | accepted | rejected | conflicting
review_note
reviewed_by / reviewed_at
```

### Explainable hybrid score

The first implementation should be deterministic and cheap enough to run
locally. It should not require an external embedding or LLM provider.

Recommended normalized score, with the weights stored in the ranking run:

```text
score_total =
    0.45 * query_fit
  + 0.20 * entity_fit
  + 0.15 * study_design_fit
  + 0.10 * recency_fit
  + 0.10 * completeness
```

The initial components are:

- `query_fit`: BM25/TF-IDF-style matching over title, abstract, keywords, and
  source-provided subjects. Store matched terms and field-level contributions.
- `entity_fit`: normalized gene, disease, drug, phenotype, intervention, and
  organism matches extracted from the question and record. Exact identifiers
  beat aliases; aliases beat untyped string matches.
- `study_design_fit`: transparent publication/trial-type bonuses for the query
  facet, such as randomized trial, systematic review, cohort, or preprint. Do
  not invent a design when the source did not provide one.
- `recency_fit`: a configurable decay only when recency is relevant to the
  question. The report must show the publication date and decay setting.
- `completeness`: presence of source metadata useful for review, such as title,
  abstract, authors, date, identifier, and journal. Completeness is not a
  quality claim.

Do not add a generic “journal prestige” score in v1. It is difficult to justify,
can encode bias, and is not needed to explain query relevance.

### What the scientist sees

For each of the top five papers, show:

- rank and total score;
- a score breakdown with the configured weights;
- matched query terms and entities;
- missing or weak facets;
- source, identifier, publication type, date, and direct source link;
- a short generated explanation assembled from stored reason codes;
- the original preserved retrieval ID;
- controls for accept, reject, mark conflicting, add note, and save to project.

The UI must say “ranked for query fit” rather than “most trustworthy.”

### Evaluation before claiming usefulness

Add a small reviewer-labeled evaluation set with query, retrieval ID, and
relevance label. Measure Precision@5, Recall@5, and NDCG@5. Report the dataset
size, source mix, label policy, and algorithm version. Do not describe the
ranking as validated until scientists have labeled a meaningful evaluation set.

## Build sequence

### Step 1 — Ranking schema and deterministic scorer

Context: existing `retrievals` and `workspace.py` already provide immutable
retrieval IDs, projects, collections, and claims.

Tasks:

- Add migrations for `ranking_runs` and `ranking_candidates`.
- Add a source-neutral document projection from preserved snapshots.
- Implement query facet extraction with explicit, testable rules.
- Implement deterministic scoring and reason-code generation.
- Add unit tests for exact matches, aliases, missing abstracts, preprints, date
  decay, ties, and deterministic ordering.

Exit criteria:

- Same inputs and algorithm version produce the same ranking.
- Every score can be reconstructed from stored components.
- No source response is modified by ranking.

### Step 2 — Ranking API and top-five review queue

Tasks:

- Add `POST /v1/rankings` and `GET /v1/rankings/{id}`.
- Add MCP tools for starting a ranking, inspecting explanations, and recording
  human review.
- Add a dashboard review queue for the top five candidates.
- Make review state and notes persist in the project database.

Exit criteria:

- A reviewer can understand why each result appears without reading code.
- A review action is attributed to a user/agent and timestamped.
- Partial rankings expose failed candidates and source errors.

### Step 3 — Save reviewed evidence and project reporting

Tasks:

- Add an atomic “save selected papers” operation that only accepts reviewed
  candidates or requires an explicit override note.
- Include ranking run, score breakdown, review decision, and retrieval links in
  JSON and Markdown project reports.
- Keep claims linked to retrieval IDs and review assessments.

Exit criteria:

- A project report can trace every selected paper to its source response and
  the scientist’s review decision.
- No ranking score is silently converted into a scientific claim.

### Step 4 — Multi-user identity, tenancy, and security

Tasks:

- Add `users`, `organizations`, `organization_members`, and project membership
  tables with roles: owner, editor, reviewer, viewer.
- Add `organization_id` to projects, ranking runs, exports, and disposable
  analysis records.
- Enforce authorization in shared service functions used by CLI, REST, MCP,
  dashboard, and A2A.
- Hash API keys, avoid logging raw tokens or source payloads, and add request
  identity to audit metadata.
- Add per-organization rate limits and maximum concurrent ranking/export jobs.
- Add security tests for cross-project and cross-organization access.

Exit criteria:

- Ten concurrent users can operate on separate projects without data leakage.
- A user cannot read, modify, export, or delete another organization’s data by
  changing an ID in a request.
- Anonymous hosted mode is explicitly limited to the demo policy.

### Step 5 — Disposable parser/ranking storage and retention

Separate durable provenance from temporary derived data:

```text
durable: retrievals, review decisions, project links, claims, audit events
temporary: parser documents, chunks, embeddings, ranking intermediates, export workspaces
```

Tasks:

- Add an `analysis_run`/artifact namespace with owner, project, status, and
  retention deadline.
- Add “flush parser data” that deletes only temporary derived artifacts after a
  successful export or explicit confirmation.
- Keep an audit event recording what was flushed, by whom, when, and which run
  was affected; never log raw sensitive content in that event.
- Make deletion idempotent and return the exact counts and IDs affected.
- Add a retention policy for source payloads separately from parser artifacts.

Exit criteria:

- Flushing parser data does not delete retrieval provenance, reviewed evidence,
  or claims.
- A reviewer can prove which derived artifacts were removed.

### Step 6 — PDF/JSON/Markdown export

Tasks:

- Build one report model and render it to Markdown, JSON, and PDF.
- Include question, retrieval time, source, identifier, ranking version,
  component scores, reason codes, review decisions, notes, and provenance URLs.
- Add export records with owner, project, format, status, checksum, and created
  time.
- Use a controlled HTML-to-PDF renderer or a tested PDF library; do not depend
  on a user’s browser state for server exports.
- Add export download authorization and bounded file sizes.

Exit criteria:

- A downloaded PDF can be traced back to the same JSON report and ranking run.
- Exporting one user’s project cannot expose another user’s data.

### Step 7 — Claude, ChatGPT, MCP, and A2A integration

Tasks:

- Keep MCP as the primary tool contract for search, ranking, review, project
  save, report, export, and flush.
- Update the MCP tool annotations so read-only, external-data, and destructive
  flush operations are distinguishable.
- Keep the Agent Card and A2A task adapter aligned with the same service layer.
- Add an explicit capability response for clients that do not support file
  download or long-running tasks.
- Document that ChatGPT integration depends on the client’s current remote-MCP
  or connector support; do not promise native UI behavior without testing it.
- Add end-to-end contract tests for MCP and A2A against the same ranking run.

Exit criteria:

- Claude/MCP and A2A return the same ranking explanations and retrieval IDs as
  REST.
- A client can ask for the top five, inspect reasons, save reviewed evidence,
  request a report, and receive an export link without a parallel data model.

## Concurrency and storage model for ten users

For the first multi-user release:

- Use Turso/libSQL or Postgres for shared hosted state; do not use Vercel `/tmp`
  for durable data.
- Use one short-lived database connection per request or job, not a global
  mutable connection shared across concurrent requests.
- Keep retrieval writes serialized through the existing audit writer boundary.
- Queue ranking and PDF jobs so a slow source or export does not block the web
  request.
- Add indexes on organization/project IDs, retrieval IDs, ranking run IDs, and
  export status.
- Enforce bounded query sizes, maximum five default review candidates, and job
  timeouts.
- Store large raw responses and generated PDFs in an object store only after
  adding signed URLs, ownership checks, checksums, and retention rules.

## Security model

Minimum production controls:

- authenticated users and organization membership;
- project-level authorization on every route and tool;
- hashed API keys and rotated secrets;
- TLS, secure cookies where browser sessions are used, and CSRF protection for
  browser mutations;
- input validation and output escaping in the dashboard;
- rate limits and job quotas per organization;
- no raw bearer tokens, source payloads, or private notes in logs;
- export and flush actions recorded as audit events;
- backups, restore tests, and an explicit retention policy.

This is an engineering baseline, not a HIPAA, FDA, or institutional security
certification.

## Verification gates

Every step should preserve these gates:

```bash
pytest -m "not live and not benchmark"
ruff check .
mypy biolab
```

Additional gates for the ranking release:

- deterministic ranking fixture tests;
- explanation reconstruction tests;
- cross-tenant authorization tests;
- export checksum and authorization tests;
- flush-scope and idempotency tests;
- MCP/REST/A2A contract parity tests;
- reviewer-labeled Precision@5, Recall@5, and NDCG@5 report.

## Immediate next implementation

Start with Step 1: deterministic ranking schema and scorer. It is the smallest
vertical slice that can be evaluated honestly and gives the later UI, storage,
export, MCP, and A2A work a stable contract.

## DeepSeek-style inference and citation layer

The referenced DeepSeek chat-demo search experience is a useful interaction
reference for streaming search state, source lists, link reading, Markdown
rendering, and inline citations. It should be treated as a UI/protocol pattern,
not as a scientific validation layer or a source of ranking truth. The project
is MIT-licensed, but Biolab should not copy code without preserving the license
and attribution requirements.

### Evidence-grounded inference contract

Add an `inference_run` that is created only from preserved retrievals or a
reviewed project collection:

```text
inference_run_id
project_id
question
input_retrieval_ids
ranking_run_id (nullable)
model_provider / model_name
prompt_version
status: queued | streaming | succeeded | partial | failed
created_by / created_at / completed_at
```

The model context should contain labeled evidence blocks, not an unstructured
copy of the entire database:

```text
[EVIDENCE R1]
source=pubmed
external_id=...
retrieved_at=...
title=...
abstract=...
source_url=...
```

The response must use citation markers such as `[R1]`. A validator then checks
that every marker maps to an input retrieval and that every cited claim has a
stored supporting span or source field. Unsupported citations are rendered as
“citation unavailable” and flagged for review; the system must not invent a
URL, PMID, DOI, quote, or retrieval ID.

### Citation data model

Store citations separately from generated text:

```text
inference_claims
  claim_id, inference_run_id, text, stance, review_state

claim_citations
  claim_id, retrieval_id, locator_type, locator_value, support_type

citation_spans
  citation_id, retrieval_id, field_name, start_offset, end_offset,
  excerpt_hash, captured_at
```

`locator_type` may be `title`, `abstract`, `author`, `publication_date`,
`external_id`, or `source_url`. Store an excerpt hash and offsets rather than
silently treating a model-generated quotation as verbatim source text.

### UI adaptation

Use the DeepSeek-style interaction pattern while keeping Biolab’s audit model:

- stream a visible “retrieving / ranking / generating” state;
- show inline citation markers in the answer;
- make each marker open the exact preserved evidence card;
- show a source rail with rank, score breakdown, review state, and retrieval ID;
- allow the scientist to expand the supporting span and open the original source;
- let the scientist accept, reject, or correct an inferred claim;
- preserve the prompt version, model, input retrieval IDs, and output checksum;
- support conversation export, but never export API keys or private tenant data.

The answer header must state that it is an inference over preserved evidence,
not a new source search and not a clinical or biological conclusion.

### Client integration rule

MCP, Claude, ChatGPT-compatible remote MCP clients, and A2A should call the
same `inference_run` service. They must receive the same claim/citation objects,
not independently formatted citations. Client-specific rendering can turn the
objects into inline links, cards, or downloadable reports.
