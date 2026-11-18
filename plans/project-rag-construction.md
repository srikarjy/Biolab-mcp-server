# Project Paper RAG Construction Plan

## Objective

Build and verify a free-first, project-scoped paper RAG that lets a scientist
select preserved papers, parse paragraphs/tables/figures, review evidence,
index accepted blocks, ask grounded questions, save cited notes, and inspect an
audit trail. The system must distinguish confirmed implementation from
provider-dependent or deployment-dependent behavior.

## Current baseline

Already implemented and verified locally:

- selected MCP retrievals flow into project parser runs;
- parser chunks preserve page, section, block type, bbox, metadata, and hashes;
- pending/accepted/excluded review states gate indexing;
- deterministic, local, NVIDIA, and compatible embedding paths exist;
- Neon pgvector projection and project-scoped search exist;
- retrieval hits have preliminary provenance citations;
- extractive grounded answers refuse when matching evidence is absent, but
  citation validation is not yet complete;
- REST and MCP answer paths cache repeated questions, but cache invalidation is
  not yet corpus/provider aware;
- project audit events are hash chained;
- local end-to-end acceptance coverage exists.

Known boundary: the deployed demo's SQLite/libSQL metadata is not durable unless
Turso is configured, and Neon pgvector is not proven live unless its DSN and
integration test are run.

## Non-negotiable invariants

1. A selected paper remains linked to its immutable retrieval record.
2. Parser output is derived evidence, never a replacement for the source.
3. Only accepted blocks enter the vector index.
4. Every citation identifies project, retrieval, source, external ID, parser
   run, document, block, page, type, content hash, and serialization version;
   the server validates those fields before returning or saving it.
5. Project authorization is checked in shared services, not only in the UI.
6. Unsupported questions return insufficient evidence; no plausible completion
   is presented as a scientific result.
7. Embedding model, provider, revision, and dimension cannot silently mix.
8. Every state-changing operation is visible in project audit history.
9. Hosted project access never uses the shared literal `anonymous` as a user
   identity.
10. Retrieval selection requires retrieval ownership, organization membership,
    or explicit share permission; an ID alone is not authorization.

## Dependency graph

```text
0 caller identity and project isolation
          |
1 durable metadata decision
          |
2 live Neon/Turso integration gate
          |
3 retrieval quality and evaluation
       /       \
4 table/figure answer handling   5 project context contract
       \       /
          6 security/guardrail audit
                  |
          7 deployment and final E2E
```

Steps 4 and 5 can be developed in parallel after step 3 because they have
disjoint primary files. Step 6 is serial after both are merged. Deployment is
last.

Every step is one coherent commit/PR-sized unit. Each handoff must name changed
files, verification commands, expected evidence artifacts, rollback behavior,
and provider-dependent limitations. If a dependency is blocked, split it into
local contract/tests and live integration rather than claiming completion.

## Build steps

### Step 0 — Establish non-shared caller identity and isolation

Context: anonymous demo mode currently maps unauthenticated callers to the same
literal identity. That is acceptable only for a disposable demo, not for
private projects, RAG answers, notes, or audit history.

Tasks:

- require a verified API key or authenticated session for hosted project/RAG
  reads and writes;
- keep anonymous mode limited to explicitly marked demo routes, or issue a
  signed isolated session identity that cannot collide with another caller;
- test two independent callers across REST, MCP, and dashboard for project,
  parser, RAG, notes, download, and audit access;
- document the hosted demo policy in the UI and README.

Exit criteria: changing a project ID cannot expose another caller's project,
including when both callers begin unauthenticated.

Rollback: retain `require_auth=False` only for loopback/local test databases;
never use it as a private deployment claim.

### Step 1 — Choose and wire durable metadata storage

Context: project metadata, audit history, parser records, and query cache use
SQLite/libSQL; vectors use optional Neon pgvector. Vercel's default filesystem
is ephemeral.

Tasks:

- decide whether hosted metadata uses Turso/libSQL or a Neon metadata schema;
- document the free-tier setup and required environment variables;
- keep local SQLite as the zero-credential development path;
- add startup/configuration checks that distinguish local, remote-durable, and
  partially configured deployments;
- ensure cache keys and audit events remain stable across process restarts.
- choose one hosted metadata backend before migration work;
- report `configured`, `connected`, `schema_ready`, `read_write_verified`, and
  `durable_claim_allowed` separately;
- document partial failure when metadata commits but vector indexing fails, and
  provide reconciliation/rebuild status.
- use explicit states: `metadata_only`, `index_pending`, `index_active`, and
  `reconcile_required`;
- choose Turso or a Neon metadata schema before implementation starts.
- add export/import or backfill with migration checksum and row-count checks;
- define cutover, rollback, and handling for projects created during migration.

Verification:

```text
pytest -q tests/test_web.py tests/test_rag.py
GET /v1/storage
restart process -> project, audit, and cache still exist
second process/instance -> the same records are readable
```

Exit criteria: no hosted configuration claims durability unless a remote
metadata backend is actually connected and verified.

Rollback: retain SQLite as the default and disable the remote adapter without
deleting local data.

### Step 2 — Run the real free vector integration gate

Tasks:

- start the local pgvector harness or connect to the configured free Neon
  project;
- run schema creation, typed-chunk upsert, project-scoped search, exclusion,
  rebuild, and idempotent retry checks;
- verify the configured embedding provider and dimension match the pgvector
  column;
- create an embedding fingerprint containing provider, model, revision,
  dimension, normalization, and serialization version;
- store and filter by that fingerprint; provider/revision changes fail closed
  until a versioned rebuild completes;
- add retrieval ID, source, external ID, source URL, and document mode to vector
  rows and search citations;
- preserve figure-specific provenance: source document hash, page render or
  image-region hash, bounding box, figure/asset ID, and `caption_only` versus
  `visual_content_parsed` mode;
- preserve a normalized table serialization hash in addition to the block hash;
- remove stale rows during rebuild when blocks are no longer accepted;
- define the corpus fingerprint deterministically over accepted block IDs,
  content hashes, review states, parser/serialization version, active embedding
  fingerprint, and index generation;
- activate a new index generation only after its full projection rebuild
  succeeds; failed rebuilds retain the previous active generation;
- record live results separately from mocked/local results.

Verification:

```text
BIOLAB_PGVECTOR_INTEGRATION=1 pytest -q tests/test_rag_pgvector.py
```

Exit criteria: accepted blocks retrieve, excluded blocks do not, retries do
not duplicate rows, projects cannot cross-retrieve, and incompatible embedding
fingerprints cannot mix.

Rollback: restore the previous active index generation, quarantine incompatible
cache entries, and preserve parser blocks, retrievals, review decisions, and
audit records. Never leave cache entries pointing at a deleted generation.

### Step 3 — Add retrieval evaluation and quality controls

Tasks:

- create a small fixture corpus with answers, distractors, duplicate passages,
  excluded blocks, tables, and two projects;
- measure recall@k, MRR, citation validity, project isolation, review isolation,
  and cache correctness;
- add deterministic deduplication and document-diversity rules;
- version the project corpus/index fingerprint and include it in cache keys;
- include embedding fingerprint, retrieval algorithm version, question, top-k,
  sections, and mode in cache keys;
- invalidate or version cache entries after selection, parsing, review,
  indexing, serialization, or provider changes;
- evict and recompute cached answers whose citations fail validation;
- return explicit retrieval status when vectors are unavailable or evidence is
  insufficient;
- do not add an LLM reranker until the fixture shows a measured need.

Minimum quality gate: the new retrieval path must meet or exceed the current
basic cosine baseline on recall@5 and MRR, achieve 100% project/review
isolation and citation-validity checks, and document fixture size plus any case
where it does not improve on baseline.

Exit criteria: the evaluation report names corpus size, labels, provider,
algorithm version, and known limitations.

Rollback: keep the current basic cosine retrieval and disable the quality
post-processing with a feature flag.

### Step 4 — Make tables and figures answerable

Tasks:

- serialize table captions, headers, rows, and IDs into searchable text while
  retaining structured metadata;
- associate figure captions with nearby explanatory blocks without crossing a
  page boundary;
- expose block type and layout metadata in the project UI and citations;
- add table-specific and figure-specific fixtures and tests.
- distinguish `abstract`, `full_text`, and `layout_document` parsing modes;
- fail clearly when a selected MCP paper has only metadata/abstract and no
  parseable full document; never imply diagrams or tables were parsed;
- include retrieval/source provenance and serialization version in every
  citation;
- validate each citation against the current project, accepted corpus, parser
  block, and content hash before returning it or saving it as a note.
- require every answer sentence to map to a returned accepted chunk and exact
  excerpt/span; cached answers must pass the same validation before delivery.

Exit criteria: questions about a table or figure retrieve the typed block and
display its caption/page provenance.

Rollback: index only the original parser text while preserving metadata.

### Step 5 — Define project research context

Tasks:

- add an explicit project objective, research question, and optional scope
  fields;
- store query history and accepted notes as project records;
- pass context to MCP tools only when the caller selects the project;
- never represent project context as permanent ChatGPT learning or memory;
- add tests proving context cannot cross project boundaries.

Exit criteria: the same question is interpreted with the selected project
scope, while a new project starts with no inherited context.

Rollback: make context optional and return to question-only retrieval.

### Step 6 — Security and guardrail audit

Tasks:

- test cross-project and cross-user reads/writes through REST, MCP, and the
  dashboard;
- test attaching another caller's retrieval by ID through REST, MCP, and the
  dashboard; reject it without ownership, membership, or share permission;
- validate citation IDs against returned context before displaying an answer;
- validate citations again before saving notes or creating downloadable
  evidence bundles;
- bound question length, top-k, parser size, and metadata payload size;
- treat paper text, captions, and retrieved content as untrusted data, never as
  instructions;
- verify audit tampering detection and ensure secrets never enter logs or
  returned metadata.

Exit criteria: guardrail tests pass and every remaining limitation is written
as a deployment or product boundary.

### Step 7 — Deploy and final end-to-end verification

Tasks:

- configure only free services and required environment variables;
- deploy the tested commit;
- verify health, storage status, project creation, paper selection, parsing,
  review, indexing, query, cache, notes, download, and audit endpoints against
  the live deployment;
- update README with confirmed live behavior and exact limitations;
- save an evidence report containing deployment URL, commit SHA, health and
  storage responses, authenticated caller identities, project A/B isolation,
  real paper selection, typed-block inspection, accepted/excluded index
  behavior, live Neon query, cache hit/invalidation, audit verification, note
  citation validation, download verification, MCP invocation, and whether
  metadata is durable or ephemeral;
- push the verified commit and report deployment evidence separately from local
  test evidence.

Exit criteria: live workflow completes without mocks, or the exact external
blocker is reported without claiming completion.

## ECC cold-start execution cards

Each card is independently executable from this repository. Work stays on the
current feature branch until verification passes; then use one commit and open
or update one PR against `main`. CI must run the focused tests plus the full
suite. Use the default coding model for implementation, and use the strongest
available review model only for the adversarial review gate.

| Step | Context and primary files | Exact tests/evidence | Artifact and rollback |
| --- | --- | --- | --- |
| 0 | `biolab/auth.py`, `workspace.py`, `server.py`, `web.py` | `tests/test_auth.py`, new isolation tests | `artifacts/rag/step-0-isolation.json`; revert auth changes, keep local-only mode |
| 1 | `biolab/db.py`, storage adapter, `README.md`, `docs/VERCEL_DEPLOYMENT.md` | `tests/test_web.py`, storage probe, restart/second-process check | `artifacts/rag/step-1-storage.json`; disable remote adapter without deleting SQLite |
| 2 | `biolab/rag.py`, `embedding_client.py`, migrations, pgvector tests | `tests/test_rag.py`, `tests/test_rag_pgvector.py` with integration flag | `artifacts/rag/step-2-vector.json`; restore previous index generation |
| 3 | `biolab/rag.py`, `tests/fixtures/rag/`, evaluation script/report | new retrieval evaluation suite and metrics report | `artifacts/rag/step-3-eval.json`; feature-flag quality post-processing |
| 4 | `hf_parser.py`, `document_parser.py`, `rag.py`, project-page UI | typed table/figure fixtures and web tests | `artifacts/rag/step-4-layout.json`; retain metadata and index original text |
| 5 | `workspace.py`, `db.py`, `server.py`, `web.py` | project-context and cross-project tests | `artifacts/rag/step-5-context.json`; make context optional |
| 6 | `auth.py`, shared services, citation/note/export paths | security, stale-cache, citation-span tests | `artifacts/rag/step-6-guards.json`; disable new guard only locally, never in hosted mode |
| 7 | `vercel.json`, README, deployment config, smoke script | live evidence report with two identities and fresh namespace | `artifacts/rag/step-7-live.json`; rollback deployment and preserve data |

Plan mutation protocol: if a card is blocked, append a dated note to the plan
describing the blocker, split the card into contract and live-integration
substeps, update dependency edges, and rerun the adversarial review before
continuing.

## Review gate

Before implementation proceeds beyond Step 1, an adversarial review must check:

- dependency order and rollback safety;
- accidental claims of scientific validity;
- cross-project leakage;
- parser/table/figure provenance loss;
- provider and vector-dimension mixing;
- cache poisoning or stale-answer behavior;
- durability claims unsupported by live configuration;
- whether tests actually exercise the user-visible path.

Critical findings must be resolved in the plan before they are implemented.

## Working protocol

For every step: plan the smallest change, add/adjust tests, implement, run the
focused suite, review the diff, commit one coherent unit, and report confirmed
results versus environment-dependent results. Do not deploy or enter provider
credentials as part of local implementation.
