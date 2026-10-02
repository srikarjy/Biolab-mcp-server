"""Local scientist dashboard and REST API.

This surface deliberately defaults to loopback-only operation. It reuses the
same evidence and retrieval services as the CLI and MCP server.
"""

import json
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route

from biolab import auth, db, evidence, retrieval_log, workspace

OPENAPI_SCHEMA = {
    "openapi": "3.1.0",
    "info": {
        "title": "Biolab Evidence API",
        "version": "0.4.0",
        "description": "Auditable scientific retrieval, projects, monitoring, and claim provenance.",
    },
    "paths": {
        "/health": {"get": {"summary": "Health check"}},
        "/metrics": {"get": {"summary": "Local service counters and chain status"}},
        "/v1/evidence/search": {"post": {"summary": "Search and preserve evidence"}},
        "/v1/retrievals/{retrieval_id}": {"get": {"summary": "Fetch a preserved retrieval"}},
        "/v1/verify": {"get": {"summary": "Verify the audit hash chain"}},
        "/v1/projects": {
            "get": {"summary": "List projects"},
            "post": {"summary": "Create a project"},
        },
        "/v1/projects/{project_id}/report": {"get": {"summary": "Export a project report"}},
        "/v1/collections": {
            "get": {"summary": "List evidence collections"},
            "post": {"summary": "Create an evidence collection"},
        },
        "/v1/collections/{collection_id}": {"get": {"summary": "Get a collection"}},
        "/v1/collections/{collection_id}/items": {
            "post": {"summary": "Add or assess collection evidence"}
        },
        "/v1/watches": {
            "get": {"summary": "List evidence watches"},
            "post": {"summary": "Create an evidence watch"},
        },
        "/v1/watches/run-all": {"post": {"summary": "Run all active watches"}},
        "/v1/watches/{watch_id}/run": {"post": {"summary": "Run one evidence watch"}},
        "/v1/watches/{watch_id}/events": {"get": {"summary": "List watch events"}},
        "/v1/claims": {"post": {"summary": "Create a scientific claim"}},
        "/v1/claims/{claim_id}/evidence": {"post": {"summary": "Link evidence to a claim"}},
        "/v1/claims/{claim_id}": {"get": {"summary": "Trace a claim to evidence"}},
    },
}

DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Biolab Evidence Workspace</title>
  <style>
    :root { color-scheme: light dark; font-family: system-ui, sans-serif; }
    body { max-width: 980px; margin: 3rem auto; padding: 0 1rem; }
    form { display: grid; gap: .8rem; padding: 1rem; border: 1px solid #8885; border-radius: 12px; }
    input, button { padding: .75rem; font: inherit; }
    button { cursor: pointer; font-weight: 650; }
    .panel, .card { padding: 1rem; border: 1px solid #8885; border-radius: 12px; }
    .cards { display: grid; gap: .8rem; margin-top: 1rem; }
    .card h3 { margin: 0 0 .4rem; }
    .badge { display: inline-block; padding: .15rem .45rem; border-radius: 999px;
             background: #2980b933; font-size: .8rem; }
    code { overflow-wrap: anywhere; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: .8rem; }
    .muted { opacity: .72; }
    @media (max-width: 640px) { .row { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <h1>Biolab Evidence Workspace</h1>
  <p class="muted">Local-first, auditable scientific evidence retrieval.</p>
  <details>
    <summary>Remote API key (optional)</summary>
    <p class="muted">Stored only in this browser tab and sent as a Bearer header.</p>
    <input id="api-key" type="password" placeholder="blb_…" autocomplete="off">
  </details>
  <form id="search-form">
    <label>Scientific question
      <input id="query" required placeholder="KRAS pancreatic cancer">
    </label>
    <div class="row">
      <label>Sources
        <input id="sources" value="pubmed,europepmc,clinicaltrials,uniprot,opentargets">
      </label>
      <label>Results per source
        <input id="max-results" type="number" min="1" max="50" value="3">
      </label>
    </div>
    <button type="submit">Search and preserve evidence</button>
  </form>
  <div class="row" style="margin-top: 1rem">
    <section class="panel">
      <h2>Audit status</h2>
      <p id="audit">Checking…</p>
      <button id="verify" type="button">Verify hash chain</button>
    </section>
    <section class="panel">
      <h2>Projects</h2>
      <form id="project-form" style="border: 0; padding: 0">
        <input id="project-name" required placeholder="New project name">
        <button type="submit">Create project</button>
      </form>
      <ul id="projects"><li>Loading…</li></ul>
    </section>
  </div>
  <h2>Evidence</h2>
  <p id="summary" class="muted">Ready.</p>
  <div id="results" class="cards"></div>
  <script>
    const form = document.querySelector('#search-form');
    const summary = document.querySelector('#summary');
    const results = document.querySelector('#results');
    const apiFetch = (url, options = {}) => {
      const key = document.querySelector('#api-key').value.trim();
      const headers = {...(options.headers || {})};
      if (key) headers.Authorization = `Bearer ${key}`;
      return fetch(url, {...options, headers});
    };
    const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({
      '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
    }[c]));
    async function verify() {
      const response = await apiFetch('/v1/verify');
      const result = await response.json();
      document.querySelector('#audit').textContent = result.valid
        ? `Valid — ${result.record_count} preserved retrievals`
        : `Broken at ${result.first_broken_retrieval_id}`;
    }
    async function loadProjects() {
      const response = await apiFetch('/v1/projects');
      const result = await response.json();
      document.querySelector('#projects').innerHTML = result.projects.length
        ? result.projects.map(p => `<li><strong>${escapeHtml(p.name)}</strong><br><code>${p.project_id}</code></li>`).join('')
        : '<li>No projects yet.</li>';
    }
    document.querySelector('#verify').addEventListener('click', verify);
    document.querySelector('#project-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      await apiFetch('/v1/projects', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: document.querySelector('#project-name').value})});
      document.querySelector('#project-name').value = '';
      await loadProjects();
    });
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      summary.textContent = 'Searching…';
      results.innerHTML = '';
      const payload = {
        query: document.querySelector('#query').value,
        sources: document.querySelector('#sources').value.split(',').map(x => x.trim()).filter(Boolean),
        max_results: Number(document.querySelector('#max-results').value),
        agent_id: 'web:local'
      };
      try {
        const response = await apiFetch('/v1/evidence/search', {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
        });
        const result = await response.json();
        summary.textContent = `${result.retrieval_count} records preserved` +
          (Object.keys(result.errors).length ? `; errors: ${JSON.stringify(result.errors)}` : '');
        const records = Object.values(result.sources).flat();
        results.innerHTML = records.map(record => {
          const snap = record.snapshot || {};
          const title = snap.title || snap.name || snap.primary_accession || record.external_id;
          const candidateUrl = snap.url || snap.canonical_url || '';
          const url = /^https?:\/\//i.test(candidateUrl) ? candidateUrl : '';
          return `<article class="card"><span class="badge">${escapeHtml(record.source)}</span>
            <h3>${escapeHtml(title)}</h3><div>${escapeHtml(record.external_id)}</div>
            <div class="muted">retrieval <code>${escapeHtml(record.retrieval_id)}</code></div>
            ${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">Open source</a>` : ''}</article>`;
        }).join('') || '<p>No records returned.</p>';
      } catch (error) {
        summary.textContent = String(error);
      }
    });
    verify(); loadProjects();
  </script>
</body>
</html>
"""


def _record_payload(record) -> dict:
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


async def dashboard(_request: Request) -> HTMLResponse:
    return HTMLResponse(DASHBOARD_HTML)


async def health(request: Request) -> JSONResponse:
    request.app.state.conn.execute("SELECT 1").fetchone()
    return JSONResponse({"status": "ok", "service": "biolab"})


async def openapi(_request: Request) -> JSONResponse:
    return JSONResponse(OPENAPI_SCHEMA)


async def metrics(request: Request) -> JSONResponse:
    conn = request.app.state.conn
    valid, broken_id = retrieval_log.verify_chain(conn)
    tables = ("retrievals", "projects", "evidence_watches", "evidence_events", "claims")
    counts = {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables}
    return JSONResponse({
        "counts": counts,
        "audit_chain_valid": valid,
        "first_broken_retrieval_id": broken_id,
    })


async def search(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        authenticated_identity = auth.current_identity.get()
        requested_agent_id = str(payload.get("agent_id", "api:user"))
        agent_id = (
            authenticated_identity
            if authenticated_identity not in {None, "anonymous"}
            else requested_agent_id
        )
        result = evidence.search_evidence(
            request.app.state.conn,
            query=str(payload.get("query", "")),
            agent_id=agent_id,
            sources=payload.get("sources", list(evidence.SUPPORTED_SOURCES)),
            max_results=int(payload.get("max_results", 5)),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(result)


async def get_retrieval(request: Request) -> JSONResponse:
    retrieval_id = request.path_params["retrieval_id"]
    record = retrieval_log.get_retrieval(request.app.state.conn, retrieval_id)
    if record is None:
        return JSONResponse({"error": "retrieval not found"}, status_code=404)
    return JSONResponse(_record_payload(record))


async def verify(request: Request) -> JSONResponse:
    valid, broken_id = retrieval_log.verify_chain(request.app.state.conn)
    count = request.app.state.conn.execute("SELECT count(*) FROM retrievals").fetchone()[0]
    return JSONResponse(
        {"valid": valid, "record_count": count, "first_broken_retrieval_id": broken_id},
        status_code=200 if valid else 409,
    )


async def projects(request: Request) -> JSONResponse:
    if request.method == "GET":
        return JSONResponse({"projects": workspace.list_projects(request.app.state.conn)})
    try:
        payload = await request.json()
        project = workspace.create_project(
            request.app.state.conn, str(payload.get("name", "")), str(payload.get("description", ""))
        )
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(project, status_code=201)


async def project_report(request: Request):
    report = workspace.get_project_report(
        request.app.state.conn, request.path_params["project_id"]
    )
    if report is None:
        return JSONResponse({"error": "project not found"}, status_code=404)
    if request.query_params.get("format") == "markdown":
        return PlainTextResponse(workspace.render_project_markdown(report), media_type="text/markdown")
    return JSONResponse(report)


async def collections(request: Request) -> JSONResponse:
    if request.method == "GET":
        return JSONResponse({
            "collections": workspace.list_collections(
                request.app.state.conn, request.query_params.get("project_id")
            )
        })
    try:
        payload = await request.json()
        collection = workspace.create_collection(
            request.app.state.conn,
            str(payload.get("project_id", "")),
            str(payload.get("name", "")),
            str(payload.get("description", "")),
        )
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(collection, status_code=201)


async def collection_detail(request: Request) -> JSONResponse:
    collection = workspace.get_collection(
        request.app.state.conn, request.path_params["collection_id"]
    )
    if collection is None:
        return JSONResponse({"error": "collection not found"}, status_code=404)
    return JSONResponse(collection)


async def collection_items(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        item = workspace.add_collection_item(
            request.app.state.conn,
            request.path_params["collection_id"],
            str(payload.get("retrieval_id", "")),
            str(payload.get("note", "")),
            str(payload.get("relevance", "unreviewed")),
        )
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(item, status_code=201)


async def watches(request: Request) -> JSONResponse:
    if request.method == "GET":
        return JSONResponse({
            "watches": workspace.list_watches(
                request.app.state.conn, request.query_params.get("project_id")
            )
        })
    try:
        payload = await request.json()
        authenticated_identity = auth.current_identity.get()
        requested_agent_id = str(payload.get("agent_id", "api:watch"))
        agent_id = (
            authenticated_identity
            if authenticated_identity not in {None, "anonymous"}
            else requested_agent_id
        )
        watch = workspace.create_watch(
            request.app.state.conn,
            str(payload.get("project_id", "")),
            str(payload.get("source", "")),
            str(payload.get("query", "")),
            agent_id,
            int(payload.get("max_results", 10)),
        )
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(watch, status_code=201)


async def run_all_watches(request: Request) -> JSONResponse:
    result = workspace.run_active_watches(request.app.state.conn)
    return JSONResponse(result, status_code=200 if not result["errors"] else 207)


async def run_watch(request: Request) -> JSONResponse:
    try:
        result = workspace.run_watch(request.app.state.conn, request.path_params["watch_id"])
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse(result)


async def watch_events(request: Request) -> JSONResponse:
    events = workspace.list_watch_events(request.app.state.conn, request.path_params["watch_id"])
    return JSONResponse({"events": events})


async def claims(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        claim = workspace.create_claim(
            request.app.state.conn,
            str(payload.get("project_id", "")),
            str(payload.get("claim", "")),
        )
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(claim, status_code=201)


async def claim_evidence(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        link = workspace.link_claim_evidence(
            request.app.state.conn,
            request.path_params["claim_id"],
            str(payload.get("retrieval_id", "")),
            str(payload.get("stance", "")),
            str(payload.get("assessment", "unreviewed")),
            str(payload.get("note", "")),
        )
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(link, status_code=201)


async def claim_trace(request: Request) -> JSONResponse:
    result = workspace.trace_claim(request.app.state.conn, request.path_params["claim_id"])
    if result is None:
        return JSONResponse({"error": "claim not found"}, status_code=404)
    return JSONResponse(result)


class RestApiKeyAuthMiddleware:
    """Resolve API-key identity and optionally require it for every REST request."""

    def __init__(self, app, require_auth: bool = False):
        self.app = app
        self.require_auth = require_auth

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in {"/", "/health", "/openapi.json"}:
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        auth_header = headers.get(b"authorization", b"").decode("latin-1")
        identity = "anonymous"
        if auth_header.startswith("Bearer "):
            raw_key = auth_header.removeprefix("Bearer ").strip()
            identity = auth.verify_api_key(scope["app"].state.conn, raw_key) or ""
            if not identity:
                response = JSONResponse({"error": "invalid or revoked API key"}, status_code=401)
                await response(scope, receive, send)
                return
        elif self.require_auth:
            response = JSONResponse({"error": "API key required"}, status_code=401)
            await response(scope, receive, send)
            return

        token = auth.current_identity.set(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            auth.current_identity.reset(token)


def api_routes() -> list[Route]:
    """Return dashboard and REST routes for standalone or MCP-hosted use."""
    return [
        Route("/", dashboard),
        Route("/health", health),
        Route("/openapi.json", openapi),
        Route("/metrics", metrics),
        Route("/v1/evidence/search", search, methods=["POST"]),
        Route("/v1/retrievals/{retrieval_id}", get_retrieval),
        Route("/v1/verify", verify),
        Route("/v1/projects", projects, methods=["GET", "POST"]),
        Route("/v1/projects/{project_id}/report", project_report),
        Route("/v1/collections", collections, methods=["GET", "POST"]),
        Route("/v1/collections/{collection_id}", collection_detail),
        Route("/v1/collections/{collection_id}/items", collection_items, methods=["POST"]),
        Route("/v1/watches", watches, methods=["GET", "POST"]),
        Route("/v1/watches/run-all", run_all_watches, methods=["POST"]),
        Route("/v1/watches/{watch_id}/run", run_watch, methods=["POST"]),
        Route("/v1/watches/{watch_id}/events", watch_events),
        Route("/v1/claims", claims, methods=["POST"]),
        Route("/v1/claims/{claim_id}/evidence", claim_evidence, methods=["POST"]),
        Route("/v1/claims/{claim_id}", claim_trace),
    ]


def create_app(db_path: str | Path = "biolab.db", require_auth: bool = False) -> Starlette:
    """Create an isolated dashboard/API application for one audit database."""

    @asynccontextmanager
    async def lifespan(app: Starlette):
        app.state.conn = db.connect(str(db_path))
        try:
            yield
        finally:
            app.state.conn.close()

    app = Starlette(
        routes=api_routes(),
        lifespan=lifespan,
    )
    app.add_middleware(RestApiKeyAuthMiddleware, require_auth=require_auth)
    return app
