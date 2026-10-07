"""MCP, REST, and dashboard server for the Biolab evidence workspace."""

import json
import os
from collections.abc import Mapping

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse

from biolab import (
    artifacts,
    auth,
    biorxiv_client,
    clinicaltrials_client,
    db,
    europepmc_client,
    executions,
    pubmed_client,
    retrieval_log,
    tools,
    web,
    workspace,
)
from biolab import evidence as evidence_service

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
def plugin_fetch(id: str) -> dict:  # noqa: A002 - MCP knowledge-tool schema calls this field id
    """Fetch one preserved evidence record by retrieval ID."""
    record = retrieval_log.get_retrieval(_conn, id)
    if record is None:
        raise ValueError("retrieval not found")
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
def create_project(name: str, description: str = "") -> dict:
    """Create a persistent scientist research project."""
    return workspace.create_project(_conn, name, description)


@mcp.tool(annotations=READ_ANNOTATIONS)
def list_projects() -> dict:
    """List scientist research projects."""
    return {"projects": workspace.list_projects(_conn)}


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def create_evidence_collection(project_id: str, name: str, description: str = "") -> dict:
    """Create a reviewed evidence collection in a project."""
    return workspace.create_collection(_conn, project_id, name, description)


@mcp.tool(annotations=READ_ANNOTATIONS)
def list_evidence_collections(project_id: str | None = None) -> dict:
    """List evidence collections, optionally within one project."""
    return {"collections": workspace.list_collections(_conn, project_id)}


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def add_evidence_to_collection(
    collection_id: str,
    retrieval_id: str,
    relevance: str = "unreviewed",
    note: str = "",
) -> dict:
    """Add or update a retrieval in a scientist-reviewed evidence collection."""
    return workspace.add_collection_item(_conn, collection_id, retrieval_id, note, relevance)


@mcp.tool(annotations=READ_ANNOTATIONS)
def get_evidence_collection(collection_id: str) -> dict:
    """Return a collection and its reviewed evidence items."""
    collection = workspace.get_collection(_conn, collection_id)
    if collection is None:
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
    return workspace.create_watch(
        _conn, project_id, source, query, _bound(agent_id, ctx), max_results
    )


@mcp.tool(annotations=READ_ANNOTATIONS)
def list_evidence_watches(project_id: str | None = None) -> dict:
    """List saved evidence watches, optionally within one project."""
    return {"watches": workspace.list_watches(_conn, project_id)}


@mcp.tool(annotations=SEARCH_ANNOTATIONS)
def run_evidence_watch(watch_id: str) -> dict:
    """Run one evidence watch and record added, removed, or revised evidence."""
    return workspace.run_watch(_conn, watch_id)


@mcp.tool(annotations=READ_ANNOTATIONS)
def list_evidence_events(watch_id: str) -> dict:
    """List change events previously detected by an evidence watch."""
    return {"events": workspace.list_watch_events(_conn, watch_id)}


@mcp.tool(annotations=SEARCH_ANNOTATIONS)
def run_all_evidence_watches() -> dict:
    """Run all active watches and return explicit per-watch failures."""
    return workspace.run_active_watches(_conn)


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def create_claim(project_id: str, claim: str) -> dict:
    """Create a scientific claim record without asserting that it is true."""
    return workspace.create_claim(_conn, project_id, claim)


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def link_claim_evidence(
    claim_id: str,
    retrieval_id: str,
    stance: str,
    assessment: str = "agent_proposed",
    note: str = "",
) -> dict:
    """Propose or record how one retrieval relates to a claim."""
    return workspace.link_claim_evidence(
        _conn, claim_id, retrieval_id, stance, assessment, note
    )


@mcp.tool(annotations=READ_ANNOTATIONS)
def trace_claim(claim_id: str) -> dict:
    """Trace a claim to supporting, conflicting, contextual, or insufficient evidence."""
    result = workspace.trace_claim(_conn, claim_id)
    if result is None:
        raise ValueError("claim not found")
    return result


@mcp.tool(annotations=READ_ANNOTATIONS)
def get_project_report(project_id: str) -> dict:
    """Return collections, claims, evidence links, watches, and events for a project."""
    result = workspace.get_project_report(_conn, project_id)
    if result is None:
        raise ValueError("project not found")
    return result


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
def get_execution(execution_id: str) -> dict:
    """Fetch one execution record, including output artifact hashes."""
    record = executions.get_execution(_conn, execution_id)
    if record is None:
        raise ValueError("execution not found")
    return record


@mcp.tool(annotations=READ_ANNOTATIONS)
def list_executions(tool: str | None = None, limit: int = 50) -> dict:
    """List recent execution records, newest first."""
    return {"executions": executions.list_executions(_conn, tool, limit)}


@mcp.tool(annotations=READ_ANNOTATIONS)
def get_artifact(sha256: str) -> dict:
    """Fetch an output artifact by its sha256 (text only; capped in size)."""
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
def replay_execution(execution_id: str) -> dict:
    """Re-run a recorded execution and report whether its outputs reproduced. Writes nothing."""
    return executions.replay(_conn, _artifacts, _registry, execution_id)


@mcp.tool(annotations=READ_ANNOTATIONS)
def get_retrieval(retrieval_id: str) -> dict:
    """Retrieve a full retrieval record by its retrieval_id.

    Args:
        retrieval_id: the UUID returned by search_pubmed
    """
    if not retrieval_id.strip():
        raise ValueError("retrieval_id must not be empty")

    record = retrieval_log.get_retrieval(_conn, retrieval_id)
    if record is None:
        raise ValueError(f"no retrieval found for id: {retrieval_id!r}")

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

        if scope.get("path", "") in {"/", "/health", "/openapi.json"}:
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


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def check_exposure(host: str, env: Mapping[str, str] | None = None) -> None:
    """Refuse to listen beyond loopback without auth unless anonymous access is explicit.

    Anonymous callers can create projects, claims and watches and have retrievals
    recorded under a self-declared identity. That is acceptable for a deliberately
    public demo, never as an accident of a default.
    """
    env = os.environ if env is None else env
    if host in LOOPBACK_HOSTS:
        return
    if env.get("BIOLAB_REQUIRE_AUTH", "").lower() in {"1", "true", "yes"}:
        return
    if env.get("BIOLAB_ALLOW_ANONYMOUS", "").lower() in {"1", "true", "yes"}:
        return
    raise SystemExit(
        f"Refusing to listen on {host} without authentication. Set BIOLAB_REQUIRE_AUTH=true "
        "(recommended) or BIOLAB_ALLOW_ANONYMOUS=true for a deliberately public instance."
    )


def create_hosted_app():
    """Create one ASGI app serving MCP, REST, health, and the dashboard."""
    app = mcp.streamable_http_app()
    app.state.conn = _conn
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
