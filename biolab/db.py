"""SQLite/libSQL connection and schema setup.

Local use relies on Python's standard-library SQLite. The optional ``turso``
extra supplies libSQL only when a remote TURSO_DATABASE_URL is configured.
"""

import os
import sqlite3
from typing import Any

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS retrievals (
    retrieval_id     TEXT PRIMARY KEY,
    source           TEXT NOT NULL,
    external_id      TEXT NOT NULL,
    query_text       TEXT NOT NULL,
    retrieved_at     TEXT NOT NULL,
    agent_id         TEXT NOT NULL,
    source_metadata  TEXT NOT NULL,
    raw_response     TEXT NOT NULL,
    snapshot         TEXT NOT NULL,
    response_hash    TEXT NOT NULL,
    prev_hash        TEXT NOT NULL DEFAULT '',
    hash_version     INTEGER NOT NULL DEFAULT 2
);

CREATE INDEX IF NOT EXISTS idx_retrievals_external_id ON retrievals(external_id);
CREATE INDEX IF NOT EXISTS idx_retrievals_agent_id ON retrievals(agent_id);
CREATE INDEX IF NOT EXISTS idx_retrievals_retrieved_at ON retrievals(retrieved_at);
CREATE INDEX IF NOT EXISTS idx_retrievals_source ON retrievals(source);

CREATE TABLE IF NOT EXISTS executions (
    execution_id    TEXT PRIMARY KEY,
    tool            TEXT NOT NULL,
    tool_version    TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    status          TEXT NOT NULL,           -- succeeded | failed
    inputs          TEXT NOT NULL,           -- canonical JSON
    input_hash      TEXT NOT NULL,
    container       TEXT NOT NULL DEFAULT '',  -- image digest or "host"
    device          TEXT NOT NULL DEFAULT '',  -- e.g. cpu, cuda:0
    retrieval_ids   TEXT NOT NULL DEFAULT '[]',  -- evidence this run was fed from
    outputs         TEXT NOT NULL DEFAULT '[]',  -- [{name, sha256, size}]
    error           TEXT NOT NULL DEFAULT '',
    started_at      TEXT NOT NULL,
    finished_at     TEXT NOT NULL,
    prev_hash       TEXT NOT NULL,
    record_hash     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_executions_tool ON executions(tool);
CREATE INDEX IF NOT EXISTS idx_executions_agent ON executions(agent_id);

CREATE TABLE IF NOT EXISTS api_keys (
    key_hash    TEXT PRIMARY KEY,  -- SHA-256 of the raw key; the raw key is never stored
    label       TEXT NOT NULL,     -- human-readable owner, e.g. "alice"
    agent_id    TEXT NOT NULL,     -- identity this key's traffic is attributed to
    created_at  TEXT NOT NULL,
    revoked     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS projects (
    project_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    owner_id     TEXT NOT NULL DEFAULT 'local',
    organization_id TEXT,
    objective TEXT NOT NULL DEFAULT '',
    research_question TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, actor_id TEXT NOT NULL,
    payload TEXT NOT NULL, created_at TEXT NOT NULL, prev_hash TEXT NOT NULL, event_hash TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);
CREATE INDEX IF NOT EXISTS idx_audit_events_project ON audit_events(project_id, created_at);

CREATE TABLE IF NOT EXISTS rag_query_cache (
    project_id TEXT NOT NULL, question_key TEXT NOT NULL, question TEXT NOT NULL,
    result_json TEXT NOT NULL, created_at TEXT NOT NULL, embedding_model TEXT NOT NULL,
    PRIMARY KEY (project_id, question_key), FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS organization_members (
    organization_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (organization_id, member_id),
    FOREIGN KEY (organization_id) REFERENCES organizations(organization_id)
);

CREATE TABLE IF NOT EXISTS evidence_collections (
    collection_id TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL,
    name          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE TABLE IF NOT EXISTS collection_items (
    collection_id TEXT NOT NULL,
    retrieval_id  TEXT NOT NULL,
    note          TEXT NOT NULL DEFAULT '',
    relevance     TEXT NOT NULL DEFAULT 'unreviewed',
    added_at      TEXT NOT NULL,
    PRIMARY KEY (collection_id, retrieval_id),
    FOREIGN KEY (collection_id) REFERENCES evidence_collections(collection_id),
    FOREIGN KEY (retrieval_id) REFERENCES retrievals(retrieval_id)
);

CREATE TABLE IF NOT EXISTS evidence_watches (
    watch_id          TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL,
    source            TEXT NOT NULL,
    query_text        TEXT NOT NULL,
    agent_id          TEXT NOT NULL,
    max_results       INTEGER NOT NULL,
    active            INTEGER NOT NULL DEFAULT 1,
    previous_ids      TEXT NOT NULL DEFAULT '[]',
    created_at        TEXT NOT NULL,
    last_checked_at   TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE TABLE IF NOT EXISTS evidence_events (
    event_id       TEXT PRIMARY KEY,
    watch_id       TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    external_id    TEXT NOT NULL,
    retrieval_id   TEXT,
    payload        TEXT NOT NULL,
    detected_at    TEXT NOT NULL,
    FOREIGN KEY (watch_id) REFERENCES evidence_watches(watch_id)
);

CREATE TABLE IF NOT EXISTS claims (
    claim_id      TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL,
    claim_text    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE TABLE IF NOT EXISTS claim_evidence (
    claim_id       TEXT NOT NULL,
    retrieval_id   TEXT NOT NULL,
    stance         TEXT NOT NULL,
    assessment     TEXT NOT NULL DEFAULT 'unreviewed',
    note           TEXT NOT NULL DEFAULT '',
    added_at       TEXT NOT NULL,
    PRIMARY KEY (claim_id, retrieval_id),
    FOREIGN KEY (claim_id) REFERENCES claims(claim_id),
    FOREIGN KEY (retrieval_id) REFERENCES retrievals(retrieval_id)
);

CREATE INDEX IF NOT EXISTS idx_collections_project ON evidence_collections(project_id);
CREATE INDEX IF NOT EXISTS idx_collection_items_retrieval ON collection_items(retrieval_id);
CREATE INDEX IF NOT EXISTS idx_watches_project ON evidence_watches(project_id);
CREATE INDEX IF NOT EXISTS idx_events_watch ON evidence_events(watch_id, detected_at);
CREATE INDEX IF NOT EXISTS idx_claims_project ON claims(project_id);
CREATE INDEX IF NOT EXISTS idx_claim_evidence_retrieval ON claim_evidence(retrieval_id);

CREATE TABLE IF NOT EXISTS ranking_runs (
    ranking_run_id  TEXT PRIMARY KEY,
    project_id      TEXT,
    question        TEXT NOT NULL,
    query_facets    TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    requested_limit INTEGER NOT NULL,
    as_of           TEXT NOT NULL,
    status          TEXT NOT NULL,
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    completed_at    TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE TABLE IF NOT EXISTS ranking_candidates (
    ranking_candidate_id TEXT PRIMARY KEY,
    ranking_run_id       TEXT NOT NULL,
    retrieval_id         TEXT NOT NULL,
    rank                 INTEGER NOT NULL,
    score_total          REAL NOT NULL,
    score_components     TEXT NOT NULL,
    reason_codes         TEXT NOT NULL,
    matched_terms        TEXT NOT NULL,
    missing_facets       TEXT NOT NULL,
    review_state         TEXT NOT NULL DEFAULT 'unreviewed',
    review_note          TEXT NOT NULL DEFAULT '',
    reviewed_by          TEXT,
    reviewed_at          TEXT,
    UNIQUE (ranking_run_id, retrieval_id),
    FOREIGN KEY (ranking_run_id) REFERENCES ranking_runs(ranking_run_id),
    FOREIGN KEY (retrieval_id) REFERENCES retrievals(retrieval_id)
);

CREATE INDEX IF NOT EXISTS idx_ranking_runs_project ON ranking_runs(project_id);
CREATE INDEX IF NOT EXISTS idx_ranking_candidates_run ON ranking_candidates(ranking_run_id, rank);
CREATE INDEX IF NOT EXISTS idx_ranking_candidates_retrieval ON ranking_candidates(retrieval_id);

CREATE TABLE IF NOT EXISTS inference_runs (
    inference_run_id  TEXT PRIMARY KEY,
    project_id        TEXT,
    ranking_run_id    TEXT,
    question          TEXT NOT NULL,
    parser_version    TEXT NOT NULL,
    model_provider    TEXT NOT NULL,
    model_name        TEXT NOT NULL,
    status            TEXT NOT NULL,
    created_by        TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    completed_at      TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(project_id),
    FOREIGN KEY (ranking_run_id) REFERENCES ranking_runs(ranking_run_id)
);

CREATE TABLE IF NOT EXISTS inference_claims (
    claim_id          TEXT PRIMARY KEY,
    inference_run_id  TEXT NOT NULL,
    claim_text        TEXT NOT NULL,
    review_state      TEXT NOT NULL DEFAULT 'unreviewed',
    created_at        TEXT NOT NULL,
    FOREIGN KEY (inference_run_id) REFERENCES inference_runs(inference_run_id)
);

CREATE TABLE IF NOT EXISTS claim_citations (
    citation_id       TEXT PRIMARY KEY,
    claim_id          TEXT NOT NULL,
    retrieval_id      TEXT NOT NULL,
    field_name        TEXT NOT NULL,
    span_start        INTEGER NOT NULL,
    span_end          INTEGER NOT NULL,
    excerpt_hash      TEXT NOT NULL,
    locator           TEXT NOT NULL,
    source            TEXT NOT NULL DEFAULT '',
    external_id      TEXT NOT NULL DEFAULT '',
    excerpt_text     TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (claim_id) REFERENCES inference_claims(claim_id),
    FOREIGN KEY (retrieval_id) REFERENCES retrievals(retrieval_id)
);

CREATE TABLE IF NOT EXISTS parser_chunks (
    chunk_id          TEXT PRIMARY KEY,
    inference_run_id  TEXT NOT NULL,
    retrieval_id      TEXT NOT NULL,
    field_name        TEXT NOT NULL,
    chunk_index       INTEGER NOT NULL,
    chunk_text        TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    FOREIGN KEY (inference_run_id) REFERENCES inference_runs(inference_run_id),
    FOREIGN KEY (retrieval_id) REFERENCES retrievals(retrieval_id)
);

CREATE INDEX IF NOT EXISTS idx_inference_runs_project ON inference_runs(project_id);
CREATE INDEX IF NOT EXISTS idx_inference_claims_run ON inference_claims(inference_run_id);
CREATE INDEX IF NOT EXISTS idx_claim_citations_claim ON claim_citations(claim_id);
CREATE INDEX IF NOT EXISTS idx_claim_citations_retrieval ON claim_citations(retrieval_id);
CREATE INDEX IF NOT EXISTS idx_parser_chunks_run ON parser_chunks(inference_run_id);

CREATE TABLE IF NOT EXISTS exports (
    export_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, format TEXT NOT NULL,
    status TEXT NOT NULL, checksum TEXT NOT NULL, size_bytes INTEGER NOT NULL,
    created_by TEXT NOT NULL, created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);
CREATE INDEX IF NOT EXISTS idx_exports_project ON exports(project_id, created_at);

CREATE TABLE IF NOT EXISTS parser_runs (
    parser_run_id TEXT PRIMARY KEY, project_id TEXT, document_id TEXT, filename TEXT NOT NULL, parser_model TEXT NOT NULL,
    source_hash TEXT NOT NULL, retrieval_id TEXT, status TEXT NOT NULL, block_count INTEGER NOT NULL,
    created_by TEXT NOT NULL, created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);
CREATE TABLE IF NOT EXISTS project_notes (
    note_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, question TEXT NOT NULL,
    note_text TEXT NOT NULL, citations TEXT NOT NULL DEFAULT '[]', created_by TEXT NOT NULL, created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);
CREATE TABLE IF NOT EXISTS project_query_history (
    query_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, question TEXT NOT NULL,
    mode TEXT NOT NULL, cache_hit INTEGER NOT NULL DEFAULT 0, result_status TEXT NOT NULL,
    created_by TEXT NOT NULL, created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);
CREATE INDEX IF NOT EXISTS idx_project_query_history_project ON project_query_history(project_id, created_at);
CREATE TABLE IF NOT EXISTS parsed_blocks (
    block_id TEXT PRIMARY KEY, parser_run_id TEXT NOT NULL, page_number INTEGER NOT NULL,
    block_index INTEGER NOT NULL, block_type TEXT NOT NULL, section_name TEXT NOT NULL DEFAULT 'unknown',
    text TEXT NOT NULL, review_state TEXT NOT NULL DEFAULT 'pending', review_note TEXT NOT NULL DEFAULT '',
    bbox TEXT, metadata TEXT NOT NULL DEFAULT '{}', content_hash TEXT NOT NULL, created_at TEXT NOT NULL,
    FOREIGN KEY (parser_run_id) REFERENCES parser_runs(parser_run_id)
);
CREATE INDEX IF NOT EXISTS idx_parsed_blocks_run ON parsed_blocks(parser_run_id, page_number, block_index);

CREATE TABLE IF NOT EXISTS graph_extraction_runs (
    extraction_run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    parser_run_id TEXT,
    extractor_type TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    input_block_hashes TEXT NOT NULL DEFAULT '[]',
    output_schema_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'failed')),
    failure_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (project_id, extraction_run_id),
    FOREIGN KEY (project_id) REFERENCES projects(project_id),
    FOREIGN KEY (parser_run_id) REFERENCES parser_runs(parser_run_id)
);
CREATE INDEX IF NOT EXISTS idx_graph_extraction_project ON graph_extraction_runs(project_id, created_at);

CREATE TABLE IF NOT EXISTS graph_nodes (
    node_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    node_type TEXT NOT NULL,
    namespace TEXT NOT NULL DEFAULT '',
    canonical_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    external_id TEXT NOT NULL DEFAULT '',
    properties_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'edited', 'rejected', 'deferred', 'superseded', 'stale')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, node_id),
    UNIQUE (project_id, node_type, namespace, normalized_name, external_id),
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_project ON graph_nodes(project_id, status);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_name ON graph_nodes(project_id, node_type, normalized_name);

CREATE TABLE IF NOT EXISTS graph_edges (
    edge_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    subject_node_id TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object_node_id TEXT NOT NULL,
    properties_json TEXT NOT NULL DEFAULT '{}',
    confidence REAL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'edited', 'rejected', 'deferred', 'superseded', 'stale')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, edge_id),
    UNIQUE (project_id, subject_node_id, predicate, object_node_id),
    FOREIGN KEY (project_id) REFERENCES projects(project_id),
    FOREIGN KEY (project_id, subject_node_id) REFERENCES graph_nodes(project_id, node_id),
    FOREIGN KEY (project_id, object_node_id) REFERENCES graph_nodes(project_id, node_id),
    CHECK (subject_node_id <> object_node_id)
);
CREATE INDEX IF NOT EXISTS idx_graph_edges_project ON graph_edges(project_id, status);
CREATE INDEX IF NOT EXISTS idx_graph_edges_subject ON graph_edges(project_id, subject_node_id, predicate);
CREATE INDEX IF NOT EXISTS idx_graph_edges_object ON graph_edges(project_id, object_node_id, predicate);

CREATE TABLE IF NOT EXISTS graph_proposals (
    proposal_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    proposal_type TEXT NOT NULL CHECK (proposal_type IN ('node', 'edge')),
    node_id TEXT,
    edge_id TEXT,
    extraction_run_id TEXT,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    proposal_version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'edited', 'rejected', 'deferred', 'superseded')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, proposal_id),
    CHECK ((proposal_type = 'node' AND node_id IS NOT NULL AND edge_id IS NULL) OR
           (proposal_type = 'edge' AND edge_id IS NOT NULL AND node_id IS NULL)),
    FOREIGN KEY (project_id) REFERENCES projects(project_id),
    FOREIGN KEY (project_id, node_id) REFERENCES graph_nodes(project_id, node_id),
    FOREIGN KEY (project_id, edge_id) REFERENCES graph_edges(project_id, edge_id),
    FOREIGN KEY (extraction_run_id) REFERENCES graph_extraction_runs(extraction_run_id)
);
CREATE INDEX IF NOT EXISTS idx_graph_proposals_project ON graph_proposals(project_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_graph_proposals_entity ON graph_proposals(node_id, edge_id);

CREATE TABLE IF NOT EXISTS graph_provenance (
    provenance_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    proposal_id TEXT,
    node_id TEXT,
    edge_id TEXT,
    retrieval_id TEXT,
    parser_run_id TEXT,
    block_id TEXT,
    page_number INTEGER,
    locator_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL,
    excerpt TEXT NOT NULL DEFAULT '',
    parser_version TEXT NOT NULL DEFAULT '',
    extractor_version TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    CHECK (retrieval_id IS NOT NULL OR block_id IS NOT NULL),
    CHECK ((node_id IS NOT NULL) OR (edge_id IS NOT NULL) OR (proposal_id IS NOT NULL)),
    FOREIGN KEY (project_id) REFERENCES projects(project_id),
    FOREIGN KEY (project_id, proposal_id) REFERENCES graph_proposals(project_id, proposal_id),
    FOREIGN KEY (project_id, node_id) REFERENCES graph_nodes(project_id, node_id),
    FOREIGN KEY (project_id, edge_id) REFERENCES graph_edges(project_id, edge_id),
    FOREIGN KEY (retrieval_id) REFERENCES retrievals(retrieval_id),
    FOREIGN KEY (parser_run_id) REFERENCES parser_runs(parser_run_id),
    FOREIGN KEY (block_id) REFERENCES parsed_blocks(block_id)
);
CREATE INDEX IF NOT EXISTS idx_graph_provenance_project ON graph_provenance(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_graph_provenance_entity ON graph_provenance(node_id, edge_id, proposal_id);

CREATE TABLE IF NOT EXISTS graph_reviews (
    review_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('accepted', 'edited', 'rejected', 'deferred')),
    edited_value_json TEXT NOT NULL DEFAULT '',
    reviewer_id TEXT NOT NULL,
    reviewer_role TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    decision_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id),
    FOREIGN KEY (project_id, proposal_id) REFERENCES graph_proposals(project_id, proposal_id)
);
CREATE INDEX IF NOT EXISTS idx_graph_reviews_proposal ON graph_reviews(proposal_id, created_at);

CREATE TABLE IF NOT EXISTS graph_index_outbox (
    outbox_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    proposal_id TEXT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    target TEXT NOT NULL CHECK (target IN ('neon_pgvector')),
    state TEXT NOT NULL CHECK (state IN ('pending_index', 'indexed', 'index_failed', 'stale')),
    idempotency_key TEXT NOT NULL UNIQUE,
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (project_id, outbox_id),
    FOREIGN KEY (project_id) REFERENCES projects(project_id),
    FOREIGN KEY (project_id, proposal_id) REFERENCES graph_proposals(project_id, proposal_id)
);
CREATE INDEX IF NOT EXISTS idx_graph_outbox_pending ON graph_index_outbox(state, updated_at);
"""

# Tracks the target (Turso URL or local path) most recently connected to, so
# retrieval_log's writer-matching logic doesn't have to introspect the
# connection object (libSQL's remote-connection PRAGMA behavior isn't
# guaranteed to mirror sqlite3's).
_current_target: str | None = None


def resolve_target(db_path: str) -> tuple[str, str | None]:
    """Return (target, auth_token). target is the Turso URL if configured, else db_path."""
    url = os.environ.get("TURSO_DATABASE_URL")
    if url:
        return url, os.environ.get("TURSO_AUTH_TOKEN")
    return db_path, None


def current_target() -> str | None:
    """The target most recently passed to connect()."""
    return _current_target


def storage_status(conn: Any | None = None) -> dict[str, Any]:
    """Describe metadata durability without exposing database credentials."""
    remote = bool(os.environ.get("TURSO_DATABASE_URL"))
    configured = remote and bool(os.environ.get("TURSO_AUTH_TOKEN"))
    connected = False
    schema_ready = False
    if conn is not None:
        try:
            connected = conn.execute("SELECT 1").fetchone() == (1,)
            schema_ready = bool(conn.execute("SELECT 1 FROM projects LIMIT 1").fetchone() is not None or connected)
        except Exception:
            connected = False
            schema_ready = False
    read_write_verified = connected and schema_ready
    return {
        "backend": "turso-libsql" if remote else "sqlite",
        "configured": configured if remote else True,
        "connected": connected,
        "schema_ready": schema_ready,
        "read_write_verified": read_write_verified,
        "durable_claim_allowed": bool(remote and configured and connected and schema_ready),
        "durable": bool(remote and configured and connected and schema_ready),
        "scope": "project metadata, audit history, parser records, and query cache",
        "warning": "Configure and verify TURSO_DATABASE_URL/TURSO_AUTH_TOKEN for durable deployment metadata." if not remote or not configured or not connected else "",
    }


def open_connection(target: str, token: str | None = None) -> Any:
    """Open a local SQLite connection or an optional remote libSQL connection."""
    if token or target.startswith(("libsql://", "https://")):
        try:
            import libsql
        except ImportError as exc:
            raise RuntimeError(
                "Remote Turso storage requires: pip install 'biolab-mcp[turso]'"
            ) from exc
        return libsql.connect(target, auth_token=token) if token else libsql.connect(target)
    return sqlite3.connect(target, check_same_thread=False)


def _upgrade_retrievals(conn: Any) -> None:
    """Bring a pre-0.5 retrievals table up to date.

    Rows written before hash_version existed were hashed over raw_response only, so
    they are stamped version 1 and verified under that legacy rule; new rows get 2.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(retrievals)").fetchall()}
    if "prev_hash" not in columns:
        conn.execute("ALTER TABLE retrievals ADD COLUMN prev_hash TEXT NOT NULL DEFAULT ''")
    if "hash_version" not in columns:
        conn.execute("ALTER TABLE retrievals ADD COLUMN hash_version INTEGER NOT NULL DEFAULT 1")


def _upgrade_projects(conn: Any) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()}
    if "owner_id" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local'")
    if "organization_id" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN organization_id TEXT")
    for name in ("objective", "research_question", "scope"):
        if name not in columns:
            conn.execute(f"ALTER TABLE projects ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")


def _upgrade_citations(conn: Any) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(claim_citations)").fetchall()}
    for name in ("source", "external_id", "excerpt_text"):
        if name not in columns:
            conn.execute(f"ALTER TABLE claim_citations ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")


def _upgrade_parser_runs(conn: Any) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(parser_runs)").fetchall()}
    if "project_id" not in columns:
        conn.execute("ALTER TABLE parser_runs ADD COLUMN project_id TEXT")
    if "document_id" not in columns:
        conn.execute("ALTER TABLE parser_runs ADD COLUMN document_id TEXT")
    if "retrieval_id" not in columns:
        conn.execute("ALTER TABLE parser_runs ADD COLUMN retrieval_id TEXT")
    block_columns = {row[1] for row in conn.execute("PRAGMA table_info(parsed_blocks)").fetchall()}
    if "section_name" not in block_columns:
        conn.execute("ALTER TABLE parsed_blocks ADD COLUMN section_name TEXT NOT NULL DEFAULT 'unknown'")
    if "review_state" not in block_columns:
        conn.execute("ALTER TABLE parsed_blocks ADD COLUMN review_state TEXT NOT NULL DEFAULT 'pending'")
    if "review_note" not in block_columns:
        conn.execute("ALTER TABLE parsed_blocks ADD COLUMN review_note TEXT NOT NULL DEFAULT ''")
    if "metadata" not in block_columns:
        conn.execute("ALTER TABLE parsed_blocks ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_parser_runs_project ON parser_runs(project_id, created_at)")


def connect(db_path: str) -> Any:
    global _current_target
    target, token = resolve_target(db_path)

    conn = open_connection(target, token)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_V2)
    _upgrade_retrievals(conn)
    _upgrade_projects(conn)
    _upgrade_citations(conn)
    _upgrade_parser_runs(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_owner ON projects(owner_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_org ON projects(organization_id)")
    conn.commit()
    _current_target = target
    return conn
