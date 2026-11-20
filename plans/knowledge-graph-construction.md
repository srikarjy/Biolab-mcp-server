# Knowledge Graph Construction Plan

Status: implemented through deterministic extraction and hybrid project RAG; production browser verification remains a deployment gate.
Owner: Biolab workspace
Scope: project-scoped, evidence-backed scientific knowledge graph

## Objective

Add a human-reviewed knowledge graph that connects selected papers, parsed
evidence blocks, scientific entities, and claims. The graph must improve project
RAG context without becoming an untraceable source of scientific truth.

## Existing constraints

- Turso/libSQL is the durable metadata store.
- Neon Postgres with pgvector stores accepted RAG chunks and embeddings.
- Every project is owner-scoped and has an audit ledger.
- Parser blocks already have page, section, metadata, review state, and hashes.
- Unreviewed parser content must not become trusted RAG context.
- Vercel `/tmp` is temporary and must not hold graph state.

## Target architecture

```text
selected paper
    -> parser blocks
    -> entity and relationship proposals
    -> human review queue
    -> accepted graph nodes/edges in Turso
    -> graph expansion + pgvector retrieval
    -> cited project answer
```

The graph is a Turso projection of reviewed evidence. It is not an independent
truth store. Every accepted edge must point to one or more exact parser blocks
or preserved retrieval records.

## Dependency graph

```text
A schema and provenance contract
        |
        +--> B graph repository and project isolation
        |          |
        |          +--> D graph proposal extraction
        |                         |
        +--> C review API and audit events
                                   |
                                   +--> E review UI
        B + C + D + E -------------> F MCP graph tools
        F + existing RAG ----------> G hybrid retrieval
        G --------------------------> H end-to-end verification
```

Steps A, B, and C should be serial because they define shared contracts. D can
begin after A. E can begin after C. F and G are serial after the review path is
safe. H is the release gate.

## Step A — schema and provenance contract

### Context brief

The graph must live beside existing project, retrieval, parser, and audit data.
The schema must support corrections, conflicts, review decisions, and exact
citations without overwriting the original proposal.

### Deliverables

Add Turso tables and indexes:

`graph_nodes`

- `node_id`, `project_id`, `node_type`, `namespace`
- `canonical_name`, `normalized_name`, `external_id`
- `properties_json`, `status`, `created_by`, `created_at`
- unique `(project_id, node_type, normalized_name, external_id)`

`graph_edges`

- `edge_id`, `project_id`
- `subject_node_id`, `predicate`, `object_node_id`
- `properties_json`, `confidence`, `status`
- `created_by`, `created_at`
- composite project-scoped references for both node endpoints

`graph_provenance`

- `edge_id`, `node_id`
- `retrieval_id`, `parser_run_id`, `block_id`
- `page_number`, `locator_json`, `content_hash`, `excerpt`
- `parser_version`, `extractor_version`, `created_at`

`graph_proposals`

- immutable proposal payload, proposal version, input block hashes,
  extraction run ID, and original payload hash

`graph_reviews`

- append-only `review_id`, `proposal_id`, `decision`, `edited_value_json`
- `reviewer_id`, `reviewer_role`, `reason`, `created_at`, `decision_hash`

`graph_index_outbox`

- accepted graph/parser change, target vector state, idempotency key,
  retry count, last error, and timestamps

Allowed proposal states are `pending`, `accepted`, `edited`, `rejected`,
`deferred`, and `superseded`. Review rows are never updated or deleted. Subject
and object nodes must be accepted before an edge becomes accepted. If an
accepted node or evidence block is later rejected, dependent edges become
`superseded` or `stale` and cannot support answers.

Provenance must contain either a valid retrieval citation or a parser-block
citation from the same project; nodes require provenance as well as edges. Add
indexes for project, normalized entity name, subject/predicate/object, and
status.

### Verification

- Schema initializes on a new SQLite database and a Turso connection.
- Existing databases upgrade without losing records.
- A graph record cannot reference another project.
- Provenance requires a retrieval or parser citation.
- A cited parser block belongs to the same project as the proposal.
- A proposal cannot be accepted by its own MCP proposal call.

### Exit criteria

Schema tests pass and the storage status endpoint still reports durable metadata.

## Step B — repository and service layer

### Context brief

Follow the existing service boundary. Web, MCP, and tests must not issue ad hoc
graph SQL or bypass project access checks.

### Deliverables

Implement repository/service functions for:

- create or find a normalized node proposal
- create an edge proposal
- list pending proposals for a project
- accept, edit, reject, or defer a proposal
- expand an accepted node by one bounded hop
- trace a node or edge to provenance
- list graph statistics for a project
- compute an accepted-graph fingerprint
- enqueue and retry idempotent Neon indexing work

All read and write paths must call the existing project-access checks. Graph
expansion must default to accepted records and require an explicit flag to show
pending proposals to reviewers.

### Verification

- Alice cannot read or mutate Bob's graph.
- Repeating the same proposal is idempotent.
- Rejected edges never appear in trusted expansion.
- Every accepted edge returns its citation records.
- Rejected nodes quarantine dependent accepted edges.
- Organization-member review permissions are explicit and tested.

### Exit criteria

Repository and service tests cover ownership, idempotency, review states, and
bounded traversal.

## Step C — review API and audit integration

### Context brief

Human review is a release gate. AI extraction may propose data, but only a
review action can make it trusted.

### Deliverables

Add project-scoped REST endpoints:

- `GET /v1/projects/{id}/graph/proposals`
- `POST /v1/projects/{id}/graph/proposals`
- `POST /v1/projects/{id}/graph/reviews`
- `GET /v1/projects/{id}/graph/nodes/{node_id}`
- `GET /v1/projects/{id}/graph/expand`
- `GET /v1/projects/{id}/graph/stats`

Record audit events for proposal, acceptance, edit, rejection, deferral, and
graph expansion used in an answer. Preserve the original proposal when a human
edits it.

Audit writes need a per-project sequence or serialized predecessor selection so
concurrent requests cannot create two events with the same predecessor. Audit
payloads include proposal/review hashes, graph revision, provenance IDs, and
exact evidence IDs.

### Verification

- Missing project access returns the same safe not-found behavior as existing
  project endpoints.
- Review actions are authenticated and attributed to the acting identity.
- Audit history identifies the entity, decision, reviewer, and evidence.

### Exit criteria

An API test can propose, review, trace, and audit one paper-derived edge.

## Step D — extraction and normalization

### Context brief

Scientific papers contain ambiguous names, abbreviations, tables, and claims.
Extraction must remain a proposal generator, not a validation system.

### Deliverables

Start with deterministic extraction for stable fields:

- paper title and identifiers
- authors and organizations
- gene/protein symbols
- disease terms
- drugs or compounds
- methods and pathways

Normalize names with a type-aware key and preserve external identifiers when
available. Store the source text and parser-block citation with every proposal.
Use a provider adapter later for model-based extraction; provider output must
pass the same schema and citation validation.

Each extraction run records an ID, parser run, extractor/model versions, input
block hashes, output schema version, status, and failure reason. Table cells,
captions, and figure regions retain structured locators. Large-paper extraction
becomes a durable resumable job rather than an in-memory queue.

### Verification

- Malformed or uncited proposals are rejected.
- Conflicting names remain separate until reviewed.
- Extraction never marks a proposal accepted automatically.
- Table and figure provenance remains attached.

### Exit criteria

Three parsed papers produce reviewable proposals with valid block citations and
no trusted graph changes before review.

## Step E — project review UI

### Context brief

Reviewers need to see the proposed relationship and its source before accepting
it. The UI should be compact and consistent with the existing parser review.

### Deliverables

Add a project graph review panel showing:

- subject, predicate, and object
- confidence and extraction method
- source paper and page/section
- evidence excerpt and content hash
- accept, edit, reject, and defer controls
- pending/accepted/rejected filters

Add counts for pending proposals and accepted entities/edges. Do not show an
accepted badge until the review response succeeds.

### Verification

- Reload preserves decisions.
- Rejected proposals disappear from trusted graph results.
- Edited values retain original provenance.
- Keyboard and accessibility checks cover the review controls.

### Exit criteria

A reviewer can approve one relationship and immediately trace it back to the
source block from the project page.

## Step F — MCP graph tools

### Context brief

An AI connected through MCP should gain project context without gaining an
unreviewed write path.

### Deliverables

Add read tools:

- `search_project_graph`
- `get_graph_entity`
- `expand_graph_entity`
- `find_related_papers`
- `trace_graph_claim`

Add proposal-only tools if needed:

- `propose_graph_entity`
- `propose_graph_relationship`

MCP responses must include project ID, node/edge IDs, review state, and citation
provenance. Proposal tools must never accept or index their own output.

### Verification

- MCP calls are project-scoped to the authenticated identity.
- Pending edges are labeled and excluded from grounded context by default.
- Tool output is sufficient for an AI to cite the source block.

### Exit criteria

An MCP client can read accepted graph context and create a pending proposal, but
cannot promote it to trusted evidence.

## Step G — hybrid graph and vector retrieval

### Context brief

Graph traversal finds explicit relationships; pgvector finds semantically
similar passages. Answers should use both without allowing graph facts to lose
their citations.

### Deliverables

For a project question:

1. Retrieve accepted vector chunks from Neon.
2. Extract candidate entities from the question.
3. Expand accepted graph nodes with a small hop/row limit.
4. Deduplicate and rank evidence.
5. Return passages plus graph relationships and citations.
6. Cache the result with deterministic graph and vector corpus fingerprints.

The accepted-graph fingerprint is derived from sorted accepted node/edge IDs,
review revisions, provenance hashes, ontology version, extraction schema
version, and graph ranking version. Cached results store the exact node/edge IDs
used and are invalidated after acceptance, edit, rejection, supersession, or
provenance correction.

### Verification

- Updating an accepted edge invalidates the relevant cached answer.
- Pending/rejected graph data cannot affect trusted answers.
- Answers remain useful when the graph is empty or pgvector is unavailable.
- Audit history records graph evidence used.

### Exit criteria

The three-paper acceptance workflow answers a graph-related question with both
vector passages and graph citations, or clearly reports which source is absent.

## Step H — adversarial review and release gate

Review the implementation for:

- cross-project leakage
- automatic trust of model output
- uncited nodes or edges
- unbounded graph traversal
- stale RAG cache after graph edits
- audit entries that can be rewritten
- schema migration failures on Turso
- Vercel cold-start and connection behavior
- Turso/Neon partial failure and stale outbox records
- concurrent audit writers and duplicate review submissions

Required checks:

```text
pytest -q tests/test_db.py tests/test_web.py tests/test_server.py
pytest -q tests/test_rag.py tests/test_rag_pgvector.py -m integration
pytest -q tests/test_web.py -k three_paper
curl /v1/storage with the authenticated production key
```

Production acceptance must verify: propose -> review -> graph expand -> hybrid
RAG query -> audit -> report download, across separate requests.

## Rollback strategy

- Keep graph tables additive; do not modify existing evidence tables in place.
- Disable graph extraction and MCP graph reads with a feature flag if needed.
- Continue serving existing vector-only RAG when graph storage is unavailable.
- Mark graph/vector indexes stale and preserve proposals, reviews, provenance,
  and audit events.
- Never delete source retrievals, parser blocks, or accepted notes as rollback.

## Decision pending

The graph should initially be stored in Turso as a project-scoped relational
projection. A separate graph database is deferred until measured traversal or
scale requirements justify its operational cost.
