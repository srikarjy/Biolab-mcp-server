# Biolab API Architecture and Integration Phases

## Goal

Build Biolab as an API-first, audit-first research workspace that can be used
from its web UI, ChatGPT, Claude, MCP clients, notebooks, and other research
tools without giving those clients direct database access.

## Target architecture

```text
Browser UI
    |
    v
FastAPI / Starlette API
    |
    +-- authentication and project permissions
    +-- project service
    +-- retrieval service
    +-- parser and review service
    +-- RAG service
    +-- knowledge-graph service
    +-- audit service
    +-- MCP adapter
            |
            +-- ChatGPT
            +-- Claude
            +-- other research tools

Storage
    +-- Turso/libSQL: projects, provenance, reviews, notes, audit events
    +-- Neon PostgreSQL + pgvector: accepted chunks and embeddings
    +-- optional object storage: original papers and parser artifacts
```

Use a modular monolith first. Keep the existing Starlette/MCP application and
move toward explicit service boundaries without introducing microservices or a
queue until the workload requires them.

## API boundaries

All public endpoints use `/v1`:

```text
/v1/projects
/v1/evidence
/v1/parser
/v1/rag
/v1/graph
/v1/audit
```

External AI clients use the MCP adapter or the REST API. They never receive
Turso, Neon, embedding-provider, or generation-provider credentials.

Every request should eventually support:

```text
Authorization
X-Agent-ID
Idempotency-Key
request_id
audit_event_id
```

## Data responsibilities

Turso/libSQL stores project metadata, retrieval provenance, parser runs and
blocks, review decisions, notes, graph proposals/reviews, cached query
metadata, and audit events.

Neon/pgvector stores only accepted parser chunks and their embeddings. Each
vector row carries project ID, parser-run ID, block ID, content hash, provider,
model, and embedding revision so indexes cannot be mixed silently.

## Audit boundary

The server owns audit creation. Every project mutation follows:

```text
authenticate -> authorize -> perform operation -> write audit event -> respond
```

Audit events identify the actor, project, action, request/response hashes,
source IDs, model/provider, previous event hash, current event hash, and time.
Clients cannot supply or alter chain hashes.

## MCP tool surface

The stable tool surface should include:

```text
list_projects
get_project
search_project_rag
answer_project_question
get_citations
trace_citation
get_graph_context
propose_graph_entity
propose_graph_relationship
list_audit_events
verify_project_audit
```

Read-only tools are automatic. Proposal tools are logged. Accepting graph facts,
deleting evidence, or other irreversible mutations require explicit human
review.

## Implementation phases

### Phase 1: API foundation

- Standardize FastAPI/Pydantic request and response contracts.
- Preserve current routes while introducing explicit API modules/services.
- Centralize authentication and project authorization.
- Add request IDs and structured error responses.
- Define idempotency behavior for mutation endpoints.
- Add API contract tests.

### Phase 2: Audit completion

- Centralize audit event creation.
- Record retrieval, selection, parsing, review, indexing, queries, notes,
  graph changes, downloads, and rejected operations.
- Implement hash-chain verification and broken-chain diagnostics.
- Add project audit history and export endpoints.
- Test tampering, missing events, replay, and cross-project access.

### Phase 3: External AI integration

- Stabilize MCP tool names, schemas, permissions, and result envelopes.
- Document ChatGPT and Claude MCP connection setup.
- Record external agent identity on every tool call.
- Run one complete project workflow through each client.

### Phase 4: Research-tool integrations

- Add Zotero paper and metadata import.
- Add Jupyter/notebook query and citation export.
- Add GitHub research-artifact export.
- Add Notion/Obsidian cited-note export.
- Add more literature and lab-data connectors behind provider adapters.

### Phase 5: Operational hardening

- Add background jobs for large-paper parsing and indexing.
- Add retries and provider-failure handling.
- Add quotas and per-agent rate limits.
- Add observability and deployment smoke tests.
- Add backup/restore and retention policies.

## Acceptance criteria

Phase 1 is complete when the same project operation has:

1. A documented request and response schema.
2. Authentication and project authorization.
3. A stable request ID.
4. A deterministic error envelope.
5. Idempotent behavior where applicable.
6. Automated tests for success, invalid input, unauthorized access, and
   repeated requests.

The first implementation target is Phase 1. Phase 2 should begin only after
these API contracts are stable.
