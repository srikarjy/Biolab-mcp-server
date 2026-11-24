"""MCP, REST, and dashboard server for the Biolab evidence workspace."""

import base64
import json
import os

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse

from biolab import (
    a2a,
    artifacts,
    audit,
    auth,
    biorxiv_client,
    clinicaltrials_client,
    db,
    document_parser,
    europepmc_client,
    executions,
    exports,
    graph,
    inference,
    pubmed_client,
    rag,
    ranking,
    retrieval_log,
    tools,
    web,
    workspace,
    zotero,
)
from biolab import evidence as evidence_service
from biolab.auth import check_exposure

DB_PATH = os.environ.get("BIOLAB_DB_PATH", "biolab.db")
MAX_RESULTS_CAP = 50  # hard ceiling — an uncapped max_results lets a caller force
                        # unbounded memory use and DB writes per call; 50 matches
                        # PubMed's own esearch API default retmax

mcp = FastMCP(
    "biolab",
    host=os.environ.get("BIOLAB_HOST", "127.0.0.1"),
    port=int(os.environ.get("BIOLAB_PORT", "8000")),
)
_conn = db.connect(DB_PATH)

ARTIFACT_DIR = os.environ.get("BIOLAB_ARTIFACT_DIR", "biolab_artifacts")
_artifacts = artifacts.ArtifactStore(ARTIFACT_DIR)
_registry = tools.default_registry()
MAX_ARTIFACT_RETURN_BYTES = 1_000_000

# Start background writer for thread-safe DB writes
retrieval_log.start_writer(DB_PATH)

SEARCH_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
READ_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
WRITE_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)


def _bound(agent_id: str, ctx: Context) -> str:
    """Attribute a write to the authenticated caller, not the self-declared agent_id."""
    try:
        request = ctx.request_context.request
    except (ValueError, AttributeError):
        request = None
    return auth.bind_agent_id(agent_id, request)


def _assert_mcp_owner(owner_id: str, ctx: Context) -> None:
    """Require ownership for authenticated MCP callers; explicit public mode stays public."""
    try:
        request = ctx.request_context.request
    except (ValueError, AttributeError):
        request = None
    if auth.is_authenticated(request) and owner_id != auth.request_identity(request):
        raise ValueError("resource not found")


@mcp.tool(annotations=SEARCH_ANNOTATIONS)
def search_evidence(
    query: str,
    agent_id: str,
    ctx: Context,
    sources: list[str] | None = None,
    max_results: int = 5,
) -> dict:
    """Search several scientific sources in one explicitly audited workflow.

    Args:
        query: scientific evidence query sent to each selected source
        agent_id: calling agent or workflow identity
        sources: any of pubmed, europepmc, clinicaltrials, uniprot, opentargets
        max_results: maximum results to preserve per source (1-50)
    """
    return evidence_service.search_evidence(
        _conn,
        query=query,
        agent_id=_bound(agent_id, ctx),
        sources=sources or list(evidence_service.SUPPORTED_SOURCES),
        max_results=max_results,
    )


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def rank_evidence(
    question: str,
    retrieval_ids: list[str],
    agent_id: str,
    ctx: Context,
    project_id: str | None = None,
    requested_limit: int = 5,
    as_of: str | None = None,
) -> dict:
    """Rank preserved retrievals with deterministic score components and reasons."""
    if project_id and not workspace.has_project_access(_conn, project_id, _bound("mcp:user", ctx)):
        raise ValueError("project not found")
    return ranking.rank_retrievals(
        _conn,
        question,
        retrieval_ids,
        created_by=_bound(agent_id, ctx),
        project_id=project_id,
        requested_limit=requested_limit,
        as_of=as_of,
    )


@mcp.tool(annotations=READ_ANNOTATIONS)
def get_ranking(ranking_run_id: str, ctx: Context) -> dict:
    """Fetch a ranking run and its stored explanations and review states."""
    result = ranking.get_ranking(_conn, ranking_run_id)
    if result is None:
        raise ValueError("ranking run not found")
    if result.get("project_id"):
        if not workspace.has_project_access(_conn, result["project_id"], _bound("mcp:user", ctx)):
            raise ValueError("ranking run not found")
    else:
        _assert_mcp_owner(str(result.get("created_by", "")), ctx)
    return result


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def review_ranked_candidate(
    ranking_run_id: str,
    ranking_candidate_id: str,
    review_state: str,
    reviewed_by: str,
    review_note: str = "",
    ctx: Context | None = None,
) -> dict:
    """Record accepted, rejected, or conflicting scientist review for a candidate."""
    if ctx is None:
        raise ValueError("authenticated context is required")
    existing = ranking.get_ranking(_conn, ranking_run_id)
    if existing is None:
        raise ValueError("ranking run not found")
    if existing.get("project_id"):
        if not workspace.has_project_access(_conn, existing["project_id"], _bound("mcp:user", ctx)):
            raise ValueError("ranking run not found")
    else:
        _assert_mcp_owner(str(existing.get("created_by", "")), ctx)
    return ranking.review_candidate(
        _conn, ranking_run_id, ranking_candidate_id,
        review_state=review_state, reviewed_by=reviewed_by, review_note=review_note,
    )


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def create_grounded_insights(
    question: str,
    retrieval_ids: list[str],
    created_by: str,
    ctx: Context,
    project_id: str | None = None,
    ranking_run_id: str | None = None,
    max_insights: int = 10,
) -> dict:
    """Extract insights only from preserved evidence and return citation spans."""
    if project_id and not workspace.has_project_access(_conn, project_id, _bound("mcp:user", ctx)):
        raise ValueError("project not found")
    return inference.create_inference(
        _conn, question, retrieval_ids,
        created_by=_bound(created_by, ctx), project_id=project_id,
        ranking_run_id=ranking_run_id, max_insights=max_insights,
    )


@mcp.tool(annotations=READ_ANNOTATIONS)
def get_grounded_insights(inference_run_id: str, ctx: Context) -> dict:
    """Fetch grounded claims and their validated retrieval citations."""
    result = inference.get_inference(_conn, inference_run_id)
    if result is None:
        raise ValueError("inference run not found")
    if result.get("project_id"):
        if not workspace.has_project_access(_conn, result["project_id"], _bound("mcp:user", ctx)):
            raise ValueError("inference run not found")
    else:
        _assert_mcp_owner(str(result.get("created_by", "")), ctx)
    return result


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def flush_grounded_parser_data(inference_run_id: str, ctx: Context) -> dict:
    """Delete temporary parser chunks while preserving claims and citations."""
    result = get_grounded_insights(inference_run_id, ctx)
    del result
    return inference.flush_parser_chunks(_conn, inference_run_id)


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def parse_document_with_huggingface(filename: str, content_base64: str, ctx: Context) -> dict:
    """Parse a PDF through the configured Hugging Face document parser."""
    max_encoded = ((document_parser.hf_parser.MAX_DOCUMENT_BYTES + 2) // 3) * 4
    if len(content_base64) > max_encoded:
        raise ValueError("encoded document exceeds maximum size")
    return document_parser.create_parser_run(
        _conn, base64.b64decode(content_base64, validate=True), filename, _bound("mcp:parser", ctx)
    )


@mcp.tool(annotations=READ_ANNOTATIONS)
def get_parsed_document(parser_run_id: str, ctx: Context) -> dict:
    """Inspect page-aware Hugging Face parser blocks."""
    return document_parser.assert_run_access(_conn, parser_run_id, _bound("mcp:parser", ctx))


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def flush_parsed_document(parser_run_id: str, ctx: Context) -> dict:
    """Delete temporary parsed blocks while retaining audit claims."""
    owner = _bound("mcp:parser", ctx)
    document_parser.assert_run_access(_conn, parser_run_id, owner)
    return document_parser.flush_parser_run(_conn, parser_run_id)


@mcp.tool(name="search", annotations=SEARCH_ANNOTATIONS)
def plugin_search(query: str, ctx: Context) -> dict:
    """Search all evidence sources for ChatGPT/Codex knowledge compatibility."""
    result = evidence_service.search_evidence(
        _conn, query, _bound("openai:search", ctx), list(evidence_service.SUPPORTED_SOURCES), 5
    )
    flattened = []
    for records in result["sources"].values():
        for record in records:
            snapshot = record["snapshot"]
            flattened.append({
                "id": record["retrieval_id"],
                "title": snapshot.get("title") or snapshot.get("name") or record["external_id"],
                "url": snapshot.get("canonical_url") or snapshot.get("url"),
            })
    return {"results": flattened, "errors": result["errors"]}


@mcp.tool(name="fetch", annotations=READ_ANNOTATIONS)
def plugin_fetch(id: str, ctx: Context) -> dict:  # noqa: A002 - MCP knowledge-tool schema calls this field id
    """Fetch one preserved evidence record by retrieval ID."""
    record = retrieval_log.get_retrieval(_conn, id)
    if record is None:
        raise ValueError("retrieval not found")
    _assert_mcp_owner(record.agent_id, ctx)
    snapshot = json.loads(record.snapshot)
    return {
        "id": record.retrieval_id,
        "title": snapshot.get("title") or snapshot.get("name") or record.external_id,
        "text": record.snapshot,
        "url": snapshot.get("canonical_url") or snapshot.get("url"),
        "metadata": {
            "source": record.source,
            "external_id": record.external_id,
            "retrieved_at": record.retrieved_at,
            "response_hash": record.response_hash,
        },
    }


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def create_project(name: str, ctx: Context, description: str = "") -> dict:
    """Create a persistent scientist research project."""
    return workspace.create_project(_conn, name, description, _bound("mcp:user", ctx))


@mcp.tool(annotations=READ_ANNOTATIONS)
def list_projects(ctx: Context) -> dict:
    """List scientist research projects."""
    return {"projects": workspace.list_projects(_conn, _bound("mcp:user", ctx))}


@mcp.tool(annotations=READ_ANNOTATIONS)
def list_project_papers(project_id: str, ctx: Context) -> dict:
    """List papers selected for a project before parsing."""
    owner = _bound("mcp:user", ctx)
    return {"papers": workspace.list_project_retrievals(_conn, project_id, owner)}


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def parse_project_paper(project_id: str, retrieval_id: str, ctx: Context) -> dict:
    """Parse one selected project paper into reviewable blocks."""
    owner = _bound("mcp:user", ctx)
    result = document_parser.create_parser_run_from_retrieval(_conn, project_id, retrieval_id, owner)
    audit.record(_conn, project_id, "paper_parsed", "parser_run", result["parser_run_id"], owner, {"retrieval_id": retrieval_id, "block_count": result.get("block_count", 0)})
    _conn.commit()
    return result


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def review_project_block(parser_run_id: str, block_id: str, review_state: str, ctx: Context, note: str = "") -> dict:
    """Accept or exclude a parsed block; accepted blocks enter the project RAG."""
    owner = _bound("mcp:user", ctx)
    result = document_parser.review_block(_conn, parser_run_id, block_id, review_state, owner, note)
    run = document_parser.get_parser_run(_conn, parser_run_id)
    if run and run.get("project_id"):
        audit.record(_conn, run["project_id"], "parser_block_reviewed", "parsed_block", block_id, owner, {"review_state": result.get("review_state", "")})
        _conn.commit()
    return result


@mcp.tool(annotations=SEARCH_ANNOTATIONS)
def search_project_rag(project_id: str, question: str, ctx: Context, top_k: int = 8, sections: list[str] | None = None) -> dict:
    """Query only accepted parser blocks from one project's paper RAG."""
    owner = _bound("mcp:user", ctx)
    if not workspace.has_project_access(_conn, project_id, owner):
        raise ValueError("project not found")
    corpus_fingerprint = document_parser.project_corpus_fingerprint(_conn, project_id)
    context_fingerprint = workspace.project_context_fingerprint(_conn, project_id) + ":" + graph.accepted_fingerprint(_conn, project_id, owner_id=owner)
    key = rag.query_key(question, top_k, sections, "search", corpus_fingerprint, context_fingerprint=context_fingerprint)
    cached = _conn.execute("SELECT result_json FROM rag_query_cache WHERE project_id = ? AND question_key = ?", (project_id, key)).fetchone()
    if cached:
        result = json.loads(cached[0])
        result["cached"] = True
    else:
        result = rag.search_parser_context(project_id, question, top_k, sections)
        result["graph_context"] = graph.query_context(_conn, project_id, question, owner_id=owner, limit=top_k)
        result["cached"] = False
        if result.get("status") == "active":
            _conn.execute("INSERT OR REPLACE INTO rag_query_cache (project_id, question_key, question, result_json, created_at, embedding_model) VALUES (?, ?, ?, ?, datetime('now'), ?)", (project_id, key, question, json.dumps(result), result.get("embedding_model", rag.embedding_client.model_name())))
    audit.record(_conn, project_id, "rag_queried", "project", project_id, owner, {"question": question, "status": result.get("status", ""), "cached": bool(cached), "graph_edge_ids": [item.get("edge_id") for item in result.get("graph_context", [])]})
    workspace.record_query(_conn, project_id, question, "search", bool(cached), result.get("status", ""), owner)
    _conn.commit()
    return result


@mcp.tool(annotations=SEARCH_ANNOTATIONS)
def answer_project_rag(project_id: str, question: str, ctx: Context, top_k: int = 8, sections: list[str] | None = None) -> dict:
    """Return an extractive answer grounded in accepted blocks from one project."""
    owner = _bound("mcp:user", ctx)
    if not workspace.has_project_access(_conn, project_id, owner):
        raise ValueError("project not found")
    corpus_fingerprint = document_parser.project_corpus_fingerprint(_conn, project_id)
    context_fingerprint = workspace.project_context_fingerprint(_conn, project_id) + ":" + graph.accepted_fingerprint(_conn, project_id, owner_id=owner)
    key = rag.query_key(question, top_k, sections, "answer", corpus_fingerprint, context_fingerprint=context_fingerprint)
    cached = _conn.execute("SELECT result_json FROM rag_query_cache WHERE project_id = ? AND question_key = ?", (project_id, key)).fetchone()
    if cached:
        result = json.loads(cached[0])
        try:
            document_parser.validate_rag_result(_conn, project_id, result)
        except ValueError:
            _conn.execute("DELETE FROM rag_query_cache WHERE project_id = ? AND question_key = ?", (project_id, key))
            cached = None
    if not cached:
        result = rag.answer_parser_context(project_id, question, top_k, sections)
        result["graph_context"] = graph.query_context(_conn, project_id, question, owner_id=owner, limit=top_k)
        document_parser.validate_rag_result(_conn, project_id, result)
        result["cached"] = False
        if result.get("status") == "active":
            _conn.execute("INSERT OR REPLACE INTO rag_query_cache (project_id, question_key, question, result_json, created_at, embedding_model) VALUES (?, ?, ?, ?, datetime('now'), ?)", (project_id, key, question, json.dumps(result), rag.embedding_client.model_name()))
    else:
        result["cached"] = True
        result.setdefault("graph_context", graph.query_context(_conn, project_id, question, owner_id=owner, limit=top_k))
    audit.record(_conn, project_id, "rag_answered", "project", project_id, owner, {"question": question, "grounded": result.get("grounded", False), "cached": bool(cached)})
    workspace.record_query(_conn, project_id, question, "answer", bool(cached), result.get("status", ""), owner)
    _conn.commit()
    return result


@mcp.tool(annotations=READ_ANNOTATIONS)
def get_project_context(project_id: str, ctx: Context) -> dict:
    """Return the selected project's research context only."""
    context = workspace.get_project_context(_conn, project_id, _bound("mcp:user", ctx))
    if context is None:
        raise ValueError("project not found")
    return context


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def update_project_context(project_id: str, ctx: Context, objective: str = "", research_question: str = "", scope: str = "") -> dict:
    """Update research context for the selected project; it is not global memory."""
    return workspace.update_project_context(_conn, project_id, _bound("mcp:user", ctx), objective, research_question, scope)


@mcp.tool(annotations=READ_ANNOTATIONS)
def list_project_query_history(project_id: str, ctx: Context, limit: int = 50) -> dict:
    """List only the selected project's recorded RAG queries."""
    return {"queries": workspace.list_query_history(_conn, project_id, _bound("mcp:user", ctx), limit)}


@mcp.tool(annotations=READ_ANNOTATIONS)
def get_project_audit(project_id: str, ctx: Context) -> dict:
    """Verify and return the hash-chained activity history for one project."""
    owner = _bound("mcp:user", ctx)
    if not workspace.has_project_access(_conn, project_id, owner):
        raise ValueError("project not found")
    valid, broken_id = audit.verify_project(_conn, project_id)
    return {"project_id": project_id, "valid": valid, "first_broken_event_id": broken_id, "events": audit.list_events(_conn, project_id)}


@mcp.tool(annotations=READ_ANNOTATIONS)
def verify_project_audit(project_id: str, ctx: Context) -> dict:
    """Return a compact integrity result for an external AI or review client."""
    owner = _bound("mcp:user", ctx)
    if not workspace.has_project_access(_conn, project_id, owner):
        raise ValueError("project not found")
    valid, broken_id = audit.verify_project(_conn, project_id)
    events = audit.list_events(_conn, project_id)
    return {
        "project_id": project_id,
        "valid": valid,
        "audit_chain_valid": valid,
        "event_count": len(events),
        "first_broken_event_id": broken_id,
    }


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def export_project_audit(project_id: str, ctx: Context) -> dict:
    """Create an auditable JSON-ready project history for an external reviewer."""
    owner = _bound("mcp:user", ctx)
    if not workspace.has_project_access(_conn, project_id, owner):
        raise ValueError("project not found")
    valid, broken_id = audit.verify_project(_conn, project_id)
    if not valid:
        raise ValueError(f"audit chain is broken at {broken_id}")
    event = audit.record(_conn, project_id, "audit_exported", "project", project_id, owner, {"format": "json", "actor": "mcp"})
    _conn.commit()
    events = audit.list_events(_conn, project_id)
    return {
        "project_id": project_id,
        "valid": audit.verify_project(_conn, project_id)[0],
        "event_count": len(events),
        "export_event_id": event["event_id"],
        "events": events,
    }


@mcp.tool(annotations=READ_ANNOTATIONS)
def list_graph_proposals(project_id: str, ctx: Context, status: str | None = None, limit: int = 100) -> dict:
    """List project-scoped knowledge-graph proposals awaiting or having received review."""
    return {"proposals": graph.list_proposals(_conn, project_id, owner_id=_bound("mcp:user", ctx), status=status, limit=limit)}


@mcp.tool(annotations=READ_ANNOTATIONS)
def get_graph_stats(project_id: str, ctx: Context) -> dict:
    """Return counts and the accepted graph fingerprint for one project."""
    return graph.stats(_conn, project_id, owner_id=_bound("mcp:user", ctx))


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def extract_project_graph(project_id: str, parser_run_id: str, ctx: Context) -> dict:
    """Extract pending, provenance-backed graph proposals from accepted parser blocks."""
    owner = _bound("mcp:user", ctx)
    run = _conn.execute("SELECT project_id FROM parser_runs WHERE parser_run_id = ?", (parser_run_id,)).fetchone()
    if run is None or run[0] != project_id:
        raise ValueError("parser run not found")
    return graph.extract_parser_run(_conn, parser_run_id, owner_id=owner)


@mcp.tool(annotations=READ_ANNOTATIONS)
def query_project_graph(project_id: str, question: str, ctx: Context, limit: int = 8) -> dict:
    """Return accepted graph facts relevant to a project question."""
    return {"facts": graph.query_context(_conn, project_id, question, owner_id=_bound("mcp:user", ctx), limit=limit)}


@mcp.tool(annotations=READ_ANNOTATIONS)
def expand_graph_entity(project_id: str, node_id: str, ctx: Context, limit: int = 50) -> dict:
    """Expand one accepted graph node; pending relationships are never returned."""
    return graph.expand_node(_conn, project_id, node_id, owner_id=_bound("mcp:user", ctx), limit=limit)


@mcp.tool(annotations=READ_ANNOTATIONS)
def trace_graph_entity(project_id: str, entity_id: str, ctx: Context) -> dict:
    """Trace a graph node, edge, or proposal to preserved source provenance."""
    return {"provenance": graph.trace_entity(_conn, project_id, entity_id, owner_id=_bound("mcp:user", ctx))}


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def propose_graph_entity(
    project_id: str, node_type: str, canonical_name: str, provenance: list[dict], ctx: Context,
    namespace: str = "", external_id: str = "", properties: dict | None = None,
    extraction_run_id: str | None = None,
) -> dict:
    """Propose a graph node with source provenance; this tool cannot accept it."""
    owner = _bound("mcp:user", ctx)
    return graph.create_node_proposal(
        _conn, project_id, node_type, canonical_name, owner_id=owner, namespace=namespace,
        external_id=external_id, properties=properties or {}, extraction_run_id=extraction_run_id,
        provenance=provenance, created_by=owner,
    )


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def propose_graph_relationship(
    project_id: str, subject_node_id: str, predicate: str, object_node_id: str,
    provenance: list[dict], ctx: Context, properties: dict | None = None,
    confidence: float | None = None, extraction_run_id: str | None = None,
) -> dict:
    """Propose a graph relationship with source provenance; review remains human-gated."""
    owner = _bound("mcp:user", ctx)
    return graph.create_edge_proposal(
        _conn, project_id, subject_node_id, predicate, object_node_id, owner_id=owner,
        properties=properties or {}, confidence=confidence, extraction_run_id=extraction_run_id,
        provenance=provenance, created_by=owner,
    )


@mcp.tool(annotations=READ_ANNOTATIONS)
def get_storage_status(ctx: Context) -> dict:
    """Report whether project metadata and RAG vectors are durably configured."""
    _bound("mcp:user", ctx)
    return {"metadata": db.storage_status(_conn), "vectors": rag.storage_status()}


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def create_evidence_collection(project_id: str, name: str, ctx: Context, description: str = "") -> dict:
    """Create a reviewed evidence collection in a project."""
    return workspace.create_collection(_conn, project_id, name, description, _bound("mcp:user", ctx))


@mcp.tool(annotations=READ_ANNOTATIONS)
def list_evidence_collections(ctx: Context, project_id: str | None = None) -> dict:
    """List evidence collections, optionally within one project."""
    owner = _bound("mcp:user", ctx)
    if project_id and not workspace.has_project_access(_conn, project_id, owner):
        raise ValueError("project not found")
    return {"collections": workspace.list_collections(_conn, project_id, owner)}


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def add_evidence_to_collection(
    collection_id: str,
    retrieval_id: str,
    ctx: Context,
    relevance: str = "unreviewed",
    note: str = "",
) -> dict:
    """Add or update a retrieval in a scientist-reviewed evidence collection."""
    collection = workspace.get_collection(_conn, collection_id)
    if collection is None or not workspace.has_project_access(_conn, collection["project_id"], _bound("mcp:user", ctx)):
        raise ValueError("collection not found")
    return workspace.add_collection_item(_conn, collection_id, retrieval_id, note, relevance)


@mcp.tool(annotations=READ_ANNOTATIONS)
def get_evidence_collection(collection_id: str, ctx: Context) -> dict:
    """Return a collection and its reviewed evidence items."""
    collection = workspace.get_collection(_conn, collection_id)
    if collection is None:
        raise ValueError("collection not found")
    if not workspace.has_project_access(_conn, collection["project_id"], _bound("mcp:user", ctx)):
        raise ValueError("collection not found")
    return collection


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def create_evidence_watch(
    project_id: str,
    source: str,
    query: str,
    agent_id: str,
    ctx: Context,
    max_results: int = 10,
) -> dict:
    """Create a saved query for explicit evidence-change monitoring."""
    owner = _bound("mcp:user", ctx)
    return workspace.create_watch(
        _conn, project_id, source, query, _bound(agent_id, ctx), max_results, owner
    )


@mcp.tool(annotations=READ_ANNOTATIONS)
def list_evidence_watches(ctx: Context, project_id: str | None = None) -> dict:
    """List saved evidence watches, optionally within one project."""
    owner = _bound("mcp:user", ctx)
    if project_id and not workspace.has_project_access(_conn, project_id, owner):
        raise ValueError("project not found")
    return {"watches": workspace.list_watches(_conn, project_id, owner)}


@mcp.tool(annotations=SEARCH_ANNOTATIONS)
def run_evidence_watch(watch_id: str, ctx: Context) -> dict:
    """Run one evidence watch and record added, removed, or revised evidence."""
    return workspace.run_watch(_conn, watch_id, _bound("mcp:user", ctx))


@mcp.tool(annotations=READ_ANNOTATIONS)
def list_evidence_events(watch_id: str, ctx: Context) -> dict:
    """List change events previously detected by an evidence watch."""
    return {"events": workspace.list_watch_events(_conn, watch_id, _bound("mcp:user", ctx))}


@mcp.tool(annotations=SEARCH_ANNOTATIONS)
def run_all_evidence_watches(ctx: Context) -> dict:
    """Run all active watches and return explicit per-watch failures."""
    return workspace.run_active_watches(_conn, _bound("mcp:user", ctx))


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def create_claim(project_id: str, claim: str, ctx: Context) -> dict:
    """Create a scientific claim record without asserting that it is true."""
    return workspace.create_claim(_conn, project_id, claim, _bound("mcp:user", ctx))


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def link_claim_evidence(
    claim_id: str,
    retrieval_id: str,
    stance: str,
    assessment: str = "agent_proposed",
    note: str = "",
    ctx: Context | None = None,
) -> dict:
    """Propose or record how one retrieval relates to a claim."""
    if ctx is None:
        raise ValueError("authenticated context is required")
    return workspace.link_claim_evidence(
        _conn, claim_id, retrieval_id, stance, assessment, note, _bound("mcp:user", ctx)
    )


@mcp.tool(annotations=READ_ANNOTATIONS)
def trace_claim(claim_id: str, ctx: Context) -> dict:
    """Trace a claim to supporting, conflicting, contextual, or insufficient evidence."""
    result = workspace.trace_claim(_conn, claim_id, _bound("mcp:user", ctx))
    if result is None:
        raise ValueError("claim not found")
    return result


@mcp.tool(annotations=READ_ANNOTATIONS)
def get_project_report(project_id: str, ctx: Context) -> dict:
    """Return collections, claims, evidence links, watches, and events for a project."""
    result = workspace.get_project_report(_conn, project_id, _bound("mcp:user", ctx))
    if result is None:
        raise ValueError("project not found")
    return result


@mcp.tool(annotations=READ_ANNOTATIONS)
def export_project_report(project_id: str, ctx: Context, format: str = "markdown") -> dict:
    """Render a project report as JSON, Markdown, or base64-encoded PDF."""
    report = workspace.get_project_report(_conn, project_id, _bound("mcp:user", ctx))
    if report is None:
        raise ValueError("project not found")
    if format == "json":
        content = json.dumps(report, indent=2, sort_keys=True).encode()
        media_type = "application/json"
    elif format == "markdown":
        content = workspace.render_project_markdown(report).encode()
        media_type = "text/markdown"
    elif format == "pdf":
        content = exports.markdown_to_pdf(workspace.render_project_markdown(report))
        media_type = "application/pdf"
    else:
        raise ValueError("format must be json, markdown, or pdf")
    record = exports.record_export(_conn, project_id, format, content, "mcp")
    return {
        "export": record,
        "media_type": media_type,
        "content_utf8": content.decode("utf-8", "replace") if format != "pdf" else None,
        "content_base64": base64.b64encode(content).decode() if format == "pdf" else None,
    }


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def export_project_notebook(project_id: str, ctx: Context) -> dict:
    """Export a cited Jupyter notebook with project context and audit status."""
    owner = _bound("mcp:user", ctx)
    report = workspace.get_project_report(_conn, project_id, owner)
    if report is None:
        raise ValueError("project not found")
    query_history = workspace.list_query_history(_conn, project_id, owner)
    notes = workspace.list_project_notes(_conn, project_id, owner)
    valid, broken_id = audit.verify_project(_conn, project_id)
    audit_status = {"valid": valid, "event_count": len(audit.list_events(_conn, project_id)), "first_broken_event_id": broken_id}
    content = exports.project_notebook(report, query_history, notes, audit_status)
    record = exports.record_export(_conn, project_id, "ipynb", content, owner)
    audit.record(_conn, project_id, "notebook_exported", "project", project_id, owner, {"format": "ipynb", "size_bytes": len(content), "actor": "mcp"})
    _conn.commit()
    return {
        "export": record,
        "media_type": "application/x-ipynb+json",
        "filename": f"{project_id}.ipynb",
        "content_base64": base64.b64encode(content).decode(),
    }


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def import_zotero_items(project_id: str, items: list[dict], ctx: Context) -> dict:
    """Import Zotero JSON metadata into a project as preserved retrievals."""
    owner = _bound("mcp:user", ctx)
    result = zotero.import_items(_conn, project_id, items, owner)
    audit.record(_conn, project_id, "zotero_imported", "project", project_id, owner, {"imported_count": result["imported_count"], "duplicate_count": result["duplicate_count"], "metadata_only": True, "actor": "mcp"})
    _conn.commit()
    return result


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def export_project_github_bundle(project_id: str, ctx: Context) -> dict:
    """Export a base64 GitHub-ready research bundle without pushing to GitHub."""
    owner = _bound("mcp:user", ctx)
    report = workspace.get_project_report(_conn, project_id, owner)
    if report is None:
        raise ValueError("project not found")
    query_history = workspace.list_query_history(_conn, project_id, owner)
    notes = workspace.list_project_notes(_conn, project_id, owner)
    valid, broken_id = audit.verify_project(_conn, project_id)
    audit_status = {"valid": valid, "event_count": len(audit.list_events(_conn, project_id)), "first_broken_event_id": broken_id}
    notebook = exports.project_notebook(report, query_history, notes, audit_status)
    files = {
        "README.md": f"# {report['project'].get('name', 'Biolab project')}\n\nGenerated by Biolab.\n".encode(),
        "biolab-project.json": json.dumps(report, indent=2, sort_keys=True).encode(),
        "project-notebook.ipynb": notebook,
        "audit.json": json.dumps({"project_id": project_id, **audit_status, "events": audit.list_events(_conn, project_id)}, indent=2, sort_keys=True).encode(),
        "query-history.json": json.dumps(query_history, indent=2, sort_keys=True).encode(),
        "notes.json": json.dumps(notes, indent=2, sort_keys=True).encode(),
    }
    content = exports.github_bundle(files)
    record = exports.record_export(_conn, project_id, "github-bundle", content, owner)
    audit.record(_conn, project_id, "github_bundle_exported", "project", project_id, owner, {"format": "zip", "size_bytes": len(content), "file_count": len(files), "actor": "mcp"})
    _conn.commit()
    return {"format": "github-bundle", "filename": f"{project_id}-research-bundle.zip", "media_type": "application/zip", "content_base64": base64.b64encode(content).decode(), "export": record}


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def save_reviewed_evidence(
    project_id: str, ranking_run_id: str, candidate_ids: list[str], ctx: Context, override_note: str = ""
) -> dict:
    """Save accepted ranked candidates to the project's reviewed evidence collection."""
    return workspace.save_reviewed_candidates(
        _conn, project_id, ranking_run_id, candidate_ids, owner_id=_bound("mcp:user", ctx), override_note=override_note
    )


@mcp.tool(annotations=SEARCH_ANNOTATIONS)
def search_pubmed(query: str, agent_id: str, ctx: Context, max_results: int = 5) -> dict:
    """Search PubMed and log every retrieved paper to the audit trail.

    Args:
        query: exact search string, sent to PubMed verbatim — no normalization
        agent_id: which agent is asking, e.g. "aletheia:advocate"
        max_results: how many papers to retrieve (default 5)
    """
    if not query.strip():
        raise ValueError("query must not be empty")
    if not agent_id.strip():
        raise ValueError("agent_id must not be empty")
    if not 1 <= max_results <= MAX_RESULTS_CAP:
        raise ValueError(f"max_results must be between 1 and {MAX_RESULTS_CAP}")

    papers = pubmed_client.search_and_fetch(query, max_results)
    if not papers:
        raise ValueError(f"no PubMed results for query: {query!r}")

    results = []
    for paper in papers:
        retrieval_input = pubmed_client.paper_to_retrieval_input(paper)
        record = retrieval_log.write_retrieval(
            _conn,
            query_text=query,
            external_id=retrieval_input["external_id"],
            agent_id=_bound(agent_id, ctx),
            source=retrieval_input["source"],
            source_metadata=retrieval_input["source_metadata"],
            raw_response=retrieval_input["raw_response"],
            snapshot=retrieval_input["snapshot"],
        )
        results.append({
            "pmid": paper.pmid,
            "retrieval_id": record.retrieval_id,
            "title": paper.title,
            "abstract": paper.abstract,
        })

    return {"query_echo": query, "papers": results}


@mcp.tool(annotations=SEARCH_ANNOTATIONS)
def search_europepmc(query: str, agent_id: str, ctx: Context, max_results: int = 5) -> dict:
    """Search Europe PMC and log every retrieved article to the audit trail.

    Args:
        query: exact search string, sent to Europe PMC verbatim — no normalization
        agent_id: which agent is asking, e.g. "aletheia:advocate"
        max_results: how many articles to retrieve (default 5)
    """
    if not query.strip():
        raise ValueError("query must not be empty")
    if not agent_id.strip():
        raise ValueError("agent_id must not be empty")
    if not 1 <= max_results <= MAX_RESULTS_CAP:
        raise ValueError(f"max_results must be between 1 and {MAX_RESULTS_CAP}")

    articles = europepmc_client.search_and_fetch(query, max_results)
    if not articles:
        raise ValueError(f"no Europe PMC results for query: {query!r}")

    results = []
    for article in articles:
        retrieval_input = europepmc_client.paper_to_retrieval_input(article)
        record = retrieval_log.write_retrieval(
            _conn,
            query_text=query,
            external_id=retrieval_input["external_id"],
            agent_id=_bound(agent_id, ctx),
            source=retrieval_input["source"],
            source_metadata=retrieval_input["source_metadata"],
            raw_response=retrieval_input["raw_response"],
            snapshot=retrieval_input["snapshot"],
        )
        results.append({
            "id": article.id,
            "retrieval_id": record.retrieval_id,
            "title": article.title,
            "abstract": article.abstract_text,
        })

    return {"query_echo": query, "articles": results}


@mcp.tool(annotations=SEARCH_ANNOTATIONS)
def search_clinicaltrials(query: str, agent_id: str, ctx: Context, max_results: int = 5) -> dict:
    """Search ClinicalTrials.gov and log every retrieved study to the audit trail.

    Args:
        query: condition/disease search string, sent to ClinicalTrials.gov verbatim
        agent_id: which agent is asking, e.g. "aletheia:advocate"
        max_results: how many studies to retrieve (default 5)
    """
    if not query.strip():
        raise ValueError("query must not be empty")
    if not agent_id.strip():
        raise ValueError("agent_id must not be empty")
    if not 1 <= max_results <= MAX_RESULTS_CAP:
        raise ValueError(f"max_results must be between 1 and {MAX_RESULTS_CAP}")

    studies = clinicaltrials_client.search_and_fetch(query, max_results)
    if not studies:
        raise ValueError(f"no ClinicalTrials.gov results for query: {query!r}")

    results = []
    for study in studies:
        retrieval_input = clinicaltrials_client.paper_to_retrieval_input(study)
        record = retrieval_log.write_retrieval(
            _conn,
            query_text=query,
            external_id=retrieval_input["external_id"],
            agent_id=_bound(agent_id, ctx),
            source=retrieval_input["source"],
            source_metadata=retrieval_input["source_metadata"],
            raw_response=retrieval_input["raw_response"],
            snapshot=retrieval_input["snapshot"],
        )
        results.append({
            "nct_id": study.nct_id,
            "retrieval_id": record.retrieval_id,
            "title": study.brief_title,
            "status": study.overall_status,
            "phase": study.phase,
        })

    return {"query_echo": query, "studies": results}


@mcp.tool(annotations=SEARCH_ANNOTATIONS)
def search_biorxiv(
    category: str, agent_id: str, ctx: Context, max_results: int = 5, server: str = "biorxiv"
) -> dict:
    """List bioRxiv/medRxiv preprints by category and log each to the audit trail.

    No free-text search exists on this API — only category listing (last 30 days).

    Args:
        category: category, e.g. "neuroscience", "bioinformatics", or "all"
        agent_id: which agent is asking, e.g. "aletheia:advocate"
        max_results: how many preprints to retrieve (default 5)
        server: "biorxiv" or "medrxiv" (default "biorxiv")
    """
    if not category.strip():
        raise ValueError("category must not be empty")
    if not agent_id.strip():
        raise ValueError("agent_id must not be empty")
    if not 1 <= max_results <= MAX_RESULTS_CAP:
        raise ValueError(f"max_results must be between 1 and {MAX_RESULTS_CAP}")
    if server not in ("biorxiv", "medrxiv"):
        raise ValueError('server must be "biorxiv" or "medrxiv"')

    preprints = biorxiv_client.list_and_fetch(server, category, max_results)
    if not preprints:
        raise ValueError(f"no {server} results for category: {category!r}")

    query_text = f"category:{category}"
    results = []
    for preprint in preprints:
        retrieval_input = biorxiv_client.paper_to_retrieval_input(preprint, server)
        record = retrieval_log.write_retrieval(
            _conn,
            query_text=query_text,
            external_id=retrieval_input["external_id"],
            agent_id=_bound(agent_id, ctx),
            source=retrieval_input["source"],
            source_metadata=retrieval_input["source_metadata"],
            raw_response=retrieval_input["raw_response"],
            snapshot=retrieval_input["snapshot"],
        )
        results.append({
            "doi": preprint.doi,
            "retrieval_id": record.retrieval_id,
            "title": preprint.title,
            "date": preprint.date,
            "category": preprint.category,
        })

    return {"category_echo": category, "preprints": results}


@mcp.tool(annotations=READ_ANNOTATIONS)
def list_execution_tools() -> dict:
    """List runnable tools with their pinned versions, containers and devices."""
    return {
        "tools": [
            {"name": t.name, "version": t.version, "container": t.container, "device": t.device,
             "deterministic": t.deterministic}
            for t in _registry.tools.values()
        ]
    }


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def run_tool(
    tool: str,
    inputs: dict,
    agent_id: str,
    ctx: Context,
    retrieval_ids: list[str] | None = None,
) -> dict:
    """Run a registered tool and seal an ExecutionRecord (inputs, versions, output hashes).

    Args:
        tool: tool name from list_execution_tools
        inputs: tool inputs (JSON object); stored verbatim so the run can be replayed
        agent_id: calling agent or workflow identity
        retrieval_ids: evidence retrievals this run was derived from, for provenance links
    """
    return executions.execute(
        _conn, _artifacts, _registry, tool, inputs, _bound(agent_id, ctx), retrieval_ids
    )


@mcp.tool(annotations=READ_ANNOTATIONS)
def get_execution(execution_id: str, ctx: Context) -> dict:
    """Fetch one execution record, including output artifact hashes."""
    record = executions.get_execution(_conn, execution_id)
    if record is None:
        raise ValueError("execution not found")
    _assert_mcp_owner(str(record.get("agent_id", "")), ctx)
    return record


@mcp.tool(annotations=READ_ANNOTATIONS)
def list_executions(tool: str | None = None, limit: int = 50, ctx: Context | None = None) -> dict:
    """List recent execution records, newest first."""
    if ctx is None:
        raise ValueError("authenticated context is required")
    owner = _bound("mcp:user", ctx)
    records = executions.list_executions(_conn, tool, limit)
    if auth.is_authenticated(getattr(ctx.request_context, "request", None)):
        records = [record for record in records if record.get("agent_id") == owner]
    return {"executions": records}


@mcp.tool(annotations=READ_ANNOTATIONS)
def get_artifact(sha256: str, ctx: Context) -> dict:
    """Fetch an output artifact by its sha256 (text only; capped in size)."""
    try:
        request = ctx.request_context.request
    except (ValueError, AttributeError):
        request = None
    if auth.is_authenticated(request):
        owner = auth.request_identity(request)
        owned = any(
            record.get("agent_id") == owner and sha256 in json.dumps(record.get("outputs", {}), sort_keys=True)
            for record in executions.list_executions(_conn, limit=500)
        )
        if not owned:
            raise ValueError("artifact not found")
    data = _artifacts.get(sha256)
    if len(data) > MAX_ARTIFACT_RETURN_BYTES:
        raise ValueError(f"artifact is {len(data)} bytes; limit is {MAX_ARTIFACT_RETURN_BYTES}")
    return {"sha256": sha256, "size": len(data), "text": data.decode("utf-8", errors="replace")}


@mcp.tool(annotations=READ_ANNOTATIONS)
def verify_executions() -> dict:
    """Verify the execution hash chain and that every output artifact is intact."""
    ok, broken = executions.verify_executions(_conn, _artifacts)
    return {"valid": ok, "first_broken_execution_id": broken}


@mcp.tool(annotations=READ_ANNOTATIONS)
def replay_execution(execution_id: str, ctx: Context) -> dict:
    """Re-run a recorded execution and report whether its outputs reproduced. Writes nothing."""
    get_execution(execution_id, ctx)
    return executions.replay(_conn, _artifacts, _registry, execution_id)


@mcp.tool(annotations=READ_ANNOTATIONS)
def get_retrieval(retrieval_id: str, ctx: Context) -> dict:
    """Retrieve a full retrieval record by its retrieval_id.

    Args:
        retrieval_id: the UUID returned by search_pubmed
    """
    if not retrieval_id.strip():
        raise ValueError("retrieval_id must not be empty")

    record = retrieval_log.get_retrieval(_conn, retrieval_id)
    if record is None:
        raise ValueError(f"no retrieval found for id: {retrieval_id!r}")
    _assert_mcp_owner(record.agent_id, ctx)

    return {
        "retrieval_id": record.retrieval_id,
        "source": record.source,
        "external_id": record.external_id,
        "query_text": record.query_text,
        "retrieved_at": record.retrieved_at,
        "agent_id": record.agent_id,
        "source_metadata": json.loads(record.source_metadata),
        "raw_response": record.raw_response,
        "snapshot": json.loads(record.snapshot),
        "response_hash": record.response_hash,
        "prev_hash": record.prev_hash,
    }


class ApiKeyAuthMiddleware:
    """Sets auth.current_identity for every HTTP request, based on an optional
    `Authorization: Bearer <key>` header.

    The server stays open to callers with no header at all (identity =
    "anonymous", a low shared rate-limit budget — see pubmed_client.py). A
    header that doesn't match a valid, non-revoked key is rejected outright
    rather than silently downgraded, so a typo'd key fails loudly instead of
    quietly running at the anonymous tier.

    Plain ASGI (not Starlette's BaseHTTPMiddleware) because streamable-http
    keeps connections open for server-sent events; BaseHTTPMiddleware's
    response buffering doesn't play well with that.
    """

    def __init__(self, app):
        self.app = app
        self.require_auth = os.environ.get("BIOLAB_REQUIRE_AUTH", "").lower() in {
            "1", "true", "yes"
        }

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope.get("path", "") in {
            "/", "/health", "/openapi.json", "/.well-known/agent-card.json"
        }:
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        auth_header = headers.get(b"authorization", b"").decode("latin-1")
        identity = "anonymous"
        if auth_header.startswith("Bearer "):
            raw_key = auth_header[len("Bearer ") :].strip()
            resolved = auth.verify_api_key(_conn, raw_key)
            if resolved is None:
                response = JSONResponse({"error": "invalid or revoked API key"}, status_code=401)
                await response(scope, receive, send)
                return
            identity = resolved
        elif self.require_auth:
            response = JSONResponse({"error": "API key required"}, status_code=401)
            await response(scope, receive, send)
            return

        scope["biolab_identity"] = identity
        token = auth.current_identity.set(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            auth.current_identity.reset(token)


def create_hosted_app():
    """Create one ASGI app serving MCP, REST, health, and the dashboard."""
    app = mcp.streamable_http_app()
    app.state.conn = _conn
    app.state.a2a_tasks = a2a.A2ATaskStore()
    app.routes.extend(web.api_routes())
    # Use the same request-id, auth, structured-error, and idempotency boundary
    # for MCP and REST so external agents cannot bypass API contract controls.
    require_auth = os.environ.get("BIOLAB_REQUIRE_AUTH", "").lower() in {"1", "true", "yes"}
    app.add_middleware(web.RestApiKeyAuthMiddleware, require_auth=require_auth)
    return app


if __name__ == "__main__":
    import uvicorn

    check_exposure(mcp.settings.host)
    app = create_hosted_app()

    try:
        uvicorn.run(app, host=mcp.settings.host, port=mcp.settings.port)
    finally:
        retrieval_log.stop_writer()
