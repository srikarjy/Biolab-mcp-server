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
    inference,
    pubmed_client,
    rag,
    ranking,
    retrieval_log,
    tools,
    web,
    workspace,
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
    result = rag.search_parser_context(project_id, question, top_k, sections)
    audit.record(_conn, project_id, "rag_queried", "project", project_id, owner, {"question": question, "status": result.get("status", "")})
    _conn.commit()
    return result


@mcp.tool(annotations=READ_ANNOTATIONS)
def get_project_audit(project_id: str, ctx: Context) -> dict:
    """Verify and return the hash-chained activity history for one project."""
    owner = _bound("mcp:user", ctx)
    if not workspace.has_project_access(_conn, project_id, owner):
        raise ValueError("project not found")
    valid, broken_id = audit.verify_project(_conn, project_id)
    return {"project_id": project_id, "valid": valid, "first_broken_event_id": broken_id, "events": audit.list_events(_conn, project_id)}


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
    app.add_middleware(ApiKeyAuthMiddleware)
    return app


if __name__ == "__main__":
    import uvicorn

    check_exposure(mcp.settings.host)
    app = create_hosted_app()

    try:
        uvicorn.run(app, host=mcp.settings.host, port=mcp.settings.port)
    finally:
        retrieval_log.stop_writer()
