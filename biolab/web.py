"""Local scientist dashboard and REST API.

This surface deliberately defaults to loopback-only operation. It reuses the
same evidence and retrieval services as the CLI and MCP server.
"""

import base64
import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from biolab import (
    a2a,
    audit,
    auth,
    db,
    document_parser,
    evidence,
    exports,
    graph,
    hf_parser,
    inference,
    jobs,
    rag,
    ranking,
    rate_limit,
    retrieval_log,
    workspace,
    zotero,
)

MAX_ENCODED_DOCUMENT_CHARS = ((hf_parser.MAX_DOCUMENT_BYTES + 2) // 3) * 4
IDEMPOTENCY_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


async def _read_body(receive):
    """Read and replay an ASGI request body when idempotency is requested."""
    messages = []
    chunks = []
    while True:
        message = await receive()
        messages.append(message)
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    body = b"".join(chunks)
    replay_index = 0

    async def replay():
        nonlocal replay_index
        if replay_index < len(messages):
            message = messages[replay_index]
            replay_index += 1
            return message
        return {"type": "http.request", "body": b"", "more_body": False}

    return body, replay


def _decode_document_payload(encoded: object) -> bytes:
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("content_base64 is required")
    if len(encoded) > MAX_ENCODED_DOCUMENT_CHARS:
        raise ValueError("encoded document exceeds maximum size")
    return base64.b64decode(encoded, validate=True)

OPENAPI_SCHEMA = {
    "openapi": "3.1.0",
    "info": {
        "title": "Biolab Evidence API",
        "version": "0.5.0",
        "description": "Auditable scientific retrieval, projects, monitoring, and claim provenance.",
    },
    "paths": {
        "/health": {"get": {"summary": "Health check"}},
        "/metrics": {"get": {"summary": "Local service counters and chain status"}},
        "/v1/storage": {"get": {"summary": "Report metadata and vector storage durability"}},
        "/v1/evidence/search": {"post": {"summary": "Search and preserve evidence"}},
        "/v1/rankings": {"post": {"summary": "Rank preserved retrievals with explanations"}},
        "/v1/rankings/{ranking_run_id}": {"get": {"summary": "Get a ranking run"}},
        "/v1/rankings/{ranking_run_id}/candidates/{ranking_candidate_id}/review": {"post": {"summary": "Review a ranked candidate"}},
        "/v1/inferences": {"post": {"summary": "Create citation-safe grounded insights"}},
        "/v1/inferences/{inference_run_id}": {"get": {"summary": "Get grounded insights and citations"}},
        "/v1/inferences/{inference_run_id}/flush": {"post": {"summary": "Flush disposable parser chunks"}},
        "/v1/parser/parse": {"post": {"summary": "Parse a PDF through the configured Hugging Face parser"}},
        "/v1/projects/{project_id}/parser-runs": {"get": {"summary": "List parsed documents in a project folder"}},
        "/v1/projects/{project_id}/retrievals": {"get": {"summary": "List MCP papers selected for a project"}},
        "/v1/projects/{project_id}/retrievals/{retrieval_id}/parse": {"post": {"summary": "Parse a selected MCP paper into the project"}},
        "/v1/projects/{project_id}/rag/search": {"post": {"summary": "Search project-scoped parsed RAG chunks"}},
        "/v1/projects/{project_id}/rag/answer": {"post": {"summary": "Return an extractive answer grounded in project RAG chunks"}},
        "/v1/projects/{project_id}/rag/notes": {"get": {"summary": "List saved project RAG notes"}, "post": {"summary": "Save an important RAG note"}},
        "/v1/projects/{project_id}/context": {"get": {"summary": "Get project research context"}, "patch": {"summary": "Update project research context"}},
        "/v1/projects/{project_id}/query-history": {"get": {"summary": "List project-scoped RAG query history"}},
        "/v1/parser/runs/{parser_run_id}": {"get": {"summary": "Inspect parsed document blocks"}},
        "/v1/parser/runs/{parser_run_id}/rebuild-index": {"post": {"summary": "Rebuild project RAG vectors from accepted blocks"}},
        "/v1/parser/runs/{parser_run_id}/rebuild-index-job": {"post": {"summary": "Queue a bounded RAG index rebuild job"}},
        "/v1/jobs/{job_id}": {"get": {"summary": "Get a background job"}},
        "/v1/jobs/{job_id}/run": {"post": {"summary": "Run one queued background job attempt"}},
        "/v1/parser/runs/{parser_run_id}/blocks/{block_id}/review": {"post": {"summary": "Accept or exclude a parsed block for RAG"}},
        "/v1/parser/runs/{parser_run_id}/flush": {"post": {"summary": "Flush parsed document blocks"}},
        "/v1/parser/runs/{parser_run_id}/download": {"get": {"summary": "Download parsed document JSON"}},
        "/v1/retrievals/{retrieval_id}": {"get": {"summary": "Fetch a preserved retrieval"}},
        "/v1/verify": {"get": {"summary": "Verify the audit hash chain"}},
        "/v1/projects": {
            "get": {"summary": "List projects"},
            "post": {"summary": "Create a project"},
        },
        "/v1/projects/{project_id}/report": {"get": {"summary": "Export a project report as JSON, Markdown, or PDF"}},
        "/v1/projects/{project_id}/notebook": {"get": {"summary": "Export a cited Jupyter notebook for the project"}},
        "/v1/projects/{project_id}/imports/zotero": {"post": {"summary": "Import Zotero JSON metadata into a project"}},
        "/v1/projects/{project_id}/github-export": {"get": {"summary": "Export a GitHub-ready research artifact bundle"}},
        "/v1/projects/{project_id}/notes-export": {"get": {"summary": "Export cited Markdown notes for Notion or Obsidian"}},
        "/v1/projects/{project_id}/audit/export": {"post": {"summary": "Export the verified project audit ledger"}},
        "/v1/projects/{project_id}/graph/proposals": {"get": {"summary": "List graph proposals"}, "post": {"summary": "Create a provenance-backed graph proposal"}},
        "/v1/projects/{project_id}/graph/proposals/{proposal_id}/review": {"post": {"summary": "Review a graph proposal"}},
        "/v1/projects/{project_id}/graph/nodes/{node_id}/expand": {"get": {"summary": "Expand an accepted graph node"}},
        "/v1/projects/{project_id}/graph/entities/{entity_id}/trace": {"get": {"summary": "Trace graph provenance"}},
        "/v1/projects/{project_id}/graph/stats": {"get": {"summary": "Get graph counts and fingerprint"}},
        "/v1/projects/{project_id}/graph/extract": {"post": {"summary": "Extract cited pending graph proposals from an accepted parser run"}},
        "/v1/projects/{project_id}/save-reviewed": {"post": {"summary": "Save accepted ranking candidates to project evidence"}},
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
        "/.well-known/agent-card.json": {"get": {"summary": "A2A Agent Card"}},
        "/a2a": {"post": {"summary": "A2A JSON-RPC endpoint"}},
    },
}

PROJECT_PAGE_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Project workspace · Biolab</title>
  <style>
    :root{--ink:#10212b;--muted:#61747d;--line:#d9e4e5;--paper:#f7faf8;--card:#fff;--teal:#087f78;--dark:#075e5a}
    *{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 90% 0%,#dff7e7 0,transparent 30rem),var(--paper);color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,sans-serif}
    .shell{width:min(1120px,calc(100% - 40px));margin:auto;padding:28px 0 64px}.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:48px}.brand{font-weight:800}.back{color:var(--dark);font-size:.85rem;font-weight:800;text-decoration:none}.eyebrow{color:var(--teal);font-size:.72rem;font-weight:800;letter-spacing:.14em;text-transform:uppercase}h1{margin:8px 0;font-size:clamp(2.2rem,5vw,4.4rem);letter-spacing:-.06em;line-height:1}.lede{color:var(--muted);line-height:1.6}.grid{display:grid;grid-template-columns:1.15fr .85fr;gap:18px;margin-top:30px}.panel{padding:24px;border:1px solid var(--line);border-radius:20px;background:var(--card);box-shadow:0 18px 50px #174d3d0d}.panel h2{margin:0;font-size:1.1rem}.sub{color:var(--muted);font-size:.82rem;line-height:1.45}.list{display:grid;gap:10px;margin-top:16px}.item{padding:13px;border:1px solid var(--line);border-radius:13px;background:#fbfdfc;font-size:.8rem;line-height:1.45}.item strong{display:block;margin-bottom:4px}.meta{color:var(--muted);font-size:.72rem}.actions{display:flex;flex-wrap:wrap;gap:7px;margin-top:10px}button{padding:.55rem .7rem;border:1px solid var(--teal);border-radius:9px;color:#fff;background:var(--teal);font:inherit;font-size:.72rem;font-weight:800;cursor:pointer}button.secondary{color:var(--dark);background:#edf8f0;border-color:#cde9d6}button:disabled{opacity:.6;cursor:wait}.query{display:grid;gap:9px;margin-top:14px}input{width:100%;padding:.75rem;border:1px solid var(--line);border-radius:10px;font:inherit}.empty{padding:18px;border:1px dashed #bfd4cb;border-radius:12px;color:var(--muted);font-size:.8rem}.chunk{padding:11px;border-top:1px solid var(--line);font-size:.78rem;line-height:1.5}.chunk:first-child{border-top:0}.status{margin-top:12px;color:var(--dark);font-size:.78rem}.error{color:#9b4539}.wide{grid-column:1/-1}@media(max-width:800px){.shell{width:min(100% - 24px,1120px)}.grid{grid-template-columns:1fr}.top{margin-bottom:32px}}
  </style>
</head>
<body><main class="shell">
  <nav class="top"><span class="brand">biolab / project workspace</span><a class="back" href="/">← Evidence search</a></nav>
  <header><p class="eyebrow">Project workspace · Step 02</p><h1 id="title">Loading project…</h1><p class="lede" id="description">Loading the selected project and its preserved evidence.</p><div id="context" class="meta"></div></header>
  <div class="grid">
    <section class="panel"><h2>Selected MCP papers</h2><p class="sub">These are preserved retrievals selected from the evidence search page. Parsing uses the saved record; there is no upload step.</p><div id="papers" class="list"><div class="empty">Loading selected papers…</div></div></section>
  <section class="panel"><h2>Query the paper RAG</h2><p class="sub">Answers use only reviewed parser blocks and show their citations.</p><form id="query-form" class="query"><input id="question" required placeholder="What does this paper report about KRAS?"><button>Ask grounded question</button></form><div id="rag" class="list"><div class="empty">Ask a question after indexing reviewed blocks.</div></div><div id="notes" class="list"></div></section>
    <section class="panel wide"><h2>Parsed papers and review gate</h2><p class="sub">Inspect parser blocks, accept useful passages for the RAG, or exclude them.</p><div id="runs" class="list"><div class="empty">Loading parser runs…</div></div></section>
    <section class="panel wide"><h2>Knowledge graph review</h2><p class="sub">Graph facts are proposals until a human accepts them. Accepted relationships are the only ones visible to graph expansion.</p><div id="graph-stats" class="meta">Loading graph status…</div><div id="graph-proposals" class="list"><div class="empty">No graph proposals yet.</div></div></section>
    <section class="panel wide"><h2>Audit ledger</h2><p class="sub">Verify the project history, including retrievals, parsing, reviews, RAG queries, notes, and graph decisions.</p><div class="actions"><button id="audit-button">Verify project audit</button></div><div id="audit-status" class="status">Audit has not been verified in this session.</div><div id="audit-events" class="list"></div></section>
  </div>
</main>
<script>
const projectId=decodeURIComponent(location.pathname.split('/').filter(Boolean).pop());
const $=id=>document.getElementById(id);
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(path,options){const r=await fetch(path,options);const d=await r.json();if(!r.ok)throw Error(d.error||('Request failed '+r.status));return d}
async function load(){
  try{const report=await api('/v1/projects/'+encodeURIComponent(projectId)+'/report');const p=report.project||{};$('title').textContent=p.name||'Project workspace';$('description').textContent=p.description||'Selected scientific evidence, parser review, and paper-grounded retrieval.';const c=await api('/v1/projects/'+encodeURIComponent(projectId)+'/context');$('context').textContent=[c.objective&&'Objective: '+c.objective,c.research_question&&'Question: '+c.research_question,c.scope&&'Scope: '+c.scope].filter(Boolean).join(' · ')||'No research context saved yet.';await Promise.all([papers(),runs(),notes(),graphReview()]);}
  catch(e){$('description').innerHTML='<span class="error">'+esc(e.message)+'</span>'}
}
async function papers(){const d=await api('/v1/projects/'+encodeURIComponent(projectId)+'/retrievals');const rows=d.retrievals||[];$('papers').innerHTML=rows.length?rows.map(x=>{const s=x.snapshot||{};return '<article class="item"><strong>'+esc(s.title||s.name||x.external_id)+'</strong><span class="meta">'+esc(x.source)+' · '+esc(x.external_id)+'</span><div class="actions"><button data-parse="'+esc(x.retrieval_id)+'">Parse saved paper</button></div></article>'}).join(''):'<div class="empty">No papers selected yet. Return to evidence search and select a preserved result.</div>'}
async function runs(){const d=await api('/v1/projects/'+encodeURIComponent(projectId)+'/parser-runs');const rows=d.parser_runs||[];$('runs').innerHTML=rows.length?rows.map(x=>'<article class="item" data-run="'+esc(x.parser_run_id)+'"><strong>'+esc(x.title||x.source_name||'Parsed paper')+'</strong><span class="meta">'+esc(x.status||x.review_state||'parsed')+' · '+esc(x.parser_run_id)+'</span><div class="actions"><button class="secondary" data-inspect="'+esc(x.parser_run_id)+'">Inspect blocks</button><button class="secondary" data-extract="'+esc(x.parser_run_id)+'">Extract graph proposals</button></div><div class="blocks"></div></article>').join(''):'<div class="empty">No parsed papers yet. Parse one of the selected papers above.</div>'}
async function notes(){const d=await api('/v1/projects/'+encodeURIComponent(projectId)+'/rag/notes');const rows=d.notes||[];$('notes').innerHTML=rows.length?'<h3>Saved notes</h3>'+rows.map(x=>'<article class="item"><strong>'+esc(x.note_text)+'</strong><span class="meta">'+esc(x.question||'Saved from RAG')}</span></article>').join(''):''}
async function graphReview(){const [s,p]=await Promise.all([api('/v1/projects/'+encodeURIComponent(projectId)+'/graph/stats'),api('/v1/projects/'+encodeURIComponent(projectId)+'/graph/proposals?status=pending')]);$('graph-stats').textContent=s.graph_proposals+' proposals · '+s.graph_nodes+' nodes · '+s.graph_edges+' edges · '+s.pending_proposals+' awaiting review';$('graph-proposals').innerHTML=(p.proposals||[]).map(x=>'<article class="item"><strong>'+esc(x.proposal_type)+' proposal · '+esc(x.payload.canonical_name||x.payload.predicate||x.proposal_id)+'</strong><span class="meta">Pending human review · provenance attached</span><div class="actions"><button data-graph-review="accepted" data-proposal="'+esc(x.proposal_id)+'">Accept</button><button class="secondary" data-graph-review="rejected" data-proposal="'+esc(x.proposal_id)+'">Reject</button><button class="secondary" data-graph-review="deferred" data-proposal="'+esc(x.proposal_id)+'">Defer</button></div></article>').join('')||'<div class="empty">No pending graph proposals. Extraction will place source-backed facts here.</div>'}
document.addEventListener('click',async e=>{const parse=e.target.closest('[data-parse]'),inspect=e.target.closest('[data-inspect]'),review=e.target.closest('[data-review]'),extract=e.target.closest('[data-extract]'),graphReviewButton=e.target.closest('[data-graph-review]');try{
  if(parse){parse.disabled=true;await api('/v1/projects/'+encodeURIComponent(projectId)+'/retrievals/'+encodeURIComponent(parse.dataset.parse)+'/parse',{method:'POST'});await runs()}
  if(inspect){const run=inspect.closest('[data-run]'),d=await api('/v1/parser/runs/'+encodeURIComponent(inspect.dataset.inspect));run.querySelector('.blocks').innerHTML=(d.blocks||[]).map(b=>'<div class="chunk"><strong>Page '+esc(b.page_number)+' · '+esc(b.section_name)+' · '+esc(b.review_state)+'</strong><br>'+esc(b.text)+'<div class="actions"><button class="secondary" data-review="accepted" data-run="'+esc(b.parser_run_id)+'" data-block="'+esc(b.block_id)+'">Accept for RAG</button><button class="secondary" data-review="excluded" data-run="'+esc(b.parser_run_id)+'" data-block="'+esc(b.block_id)+'">Exclude</button></div></div>').join('')||'<div class="empty">No blocks.</div>'}
  if(review){review.disabled=true;await api('/v1/parser/runs/'+encodeURIComponent(review.dataset.run)+'/blocks/'+encodeURIComponent(review.dataset.block)+'/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({review_state:review.dataset.review})});review.textContent='Saved';}
  if(extract){extract.disabled=true;const result=await api('/v1/projects/'+encodeURIComponent(projectId)+'/graph/extract',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({parser_run_id:extract.dataset.extract})});extract.textContent='Extracted '+result.node_proposals+' nodes';await graphReview()}
  if(graphReviewButton){graphReviewButton.disabled=true;await api('/v1/projects/'+encodeURIComponent(projectId)+'/graph/proposals/'+encodeURIComponent(graphReviewButton.dataset.proposal)+'/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({decision:graphReviewButton.dataset.graphReview,reviewer_role:'owner'})});await graphReview()}
}catch(e){alert(e.message)}});
$('audit-button').addEventListener('click',async()=>{const button=$('audit-button'),status=$('audit-status'),events=$('audit-events');button.disabled=true;status.textContent='Verifying hash chain and project provenance…';events.innerHTML='';try{const d=await api('/v1/projects/'+encodeURIComponent(projectId)+'/audit');status.textContent=(d.valid?'Valid audit chain':'Broken audit chain')+' · '+esc(d.event_count)+' events';status.className='status '+(d.valid?'':'error');if(d.first_broken_event_id)status.textContent+=' · first broken event: '+esc(d.first_broken_event_id);events.innerHTML=(d.events||[]).slice().reverse().map(x=>'<article class="item"><strong>'+esc(x.event_type)+'</strong><span class="meta">'+esc(x.actor_id)+' · '+esc(x.created_at)+' · '+esc(x.event_id)+'</span></article>').join('')||'<div class="empty">No audit events recorded.</div>'}catch(e){status.textContent='Audit verification failed: '+e.message;status.className='status error'}finally{button.disabled=false}});
$('query-form').addEventListener('submit',async e=>{e.preventDefault();$('rag').innerHTML='<div class="empty">Searching reviewed evidence…</div>';try{const q=$('question').value.trim(),d=await api('/v1/projects/'+encodeURIComponent(projectId)+'/rag/answer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q,top_k:8})});if(d.status==='disabled'){$('rag').innerHTML='<div class="empty">RAG is not active: '+esc(d.reason)+'</div>';return}const answer=d.grounded?'<article class="item"><strong>Grounded answer</strong>'+esc(d.answer)+'<div class="meta">'+(d.citations||[]).map(c=>'Page '+esc(c.page_number)+' · '+esc(c.section_name)+' · '+esc(c.content_hash)).join(' | ')+'</div></article>':'<div class="empty">'+esc(d.answer||'No supported answer.')+'</div>';const facts=(d.graph_context||[]).map(x=>'<article class="chunk"><strong>Accepted graph fact</strong><br>'+esc(x.subject)+' '+esc(x.predicate)+' '+esc(x.object)+'</article>').join('');const passages=(d.chunks||[]).map(x=>'<article class="chunk"><strong>Supporting passage · Page '+esc(x.page_number)+' · '+esc(x.section_name)+'</strong><br>'+esc(x.chunk_text)+'</article>').join('');$('rag').innerHTML=answer+facts+passages}catch(e){$('rag').innerHTML='<div class="empty error">'+esc(e.message)+'</div>'}});
load();
</script></body></html>"""

DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Biolab Evidence Workspace</title>
  <style>
    :root { --ink: #10212b; --muted: #61747d; --line: #d9e4e5; --paper: #f7faf8;
      --card: #fff; --teal: #087f78; --teal-dark: #075e5a; --lime: #d8f36a;
      color: var(--ink); background: var(--paper); font-family: Inter, ui-sans-serif, system-ui, -apple-system, sans-serif; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; background: radial-gradient(circle at 90% 0%, #dff7e7 0, transparent 30rem), var(--paper); }
    .shell { width: min(1180px, calc(100% - 40px)); margin: 0 auto; padding: 28px 0 64px; }
    .topbar { display: flex; align-items: center; justify-content: space-between; gap: 1rem; margin-bottom: 70px; }
    .brand { display: flex; align-items: center; gap: .7rem; font-weight: 800; letter-spacing: -.02em; }
    .mark { display: grid; place-items: center; width: 34px; height: 34px; border-radius: 11px; color: #fff; background: var(--teal); font-size: .9rem; }
    .toplink { color: var(--muted); font-size: .85rem; }
    .eyebrow { margin: 0 0 12px; color: var(--teal); font-size: .74rem; font-weight: 800; letter-spacing: .14em; text-transform: uppercase; }
    h1 { max-width: 760px; margin: 0; font-size: clamp(2.6rem, 6vw, 5.3rem); line-height: .98; letter-spacing: -.065em; }
    .lede { max-width: 660px; margin: 22px 0 0; color: var(--muted); font-size: 1.08rem; line-height: 1.65; }
    .signals { display: flex; flex-wrap: wrap; gap: .55rem; margin: 24px 0 42px; }
    .signal { display: inline-flex; align-items: center; gap: .45rem; padding: .48rem .72rem; border: 1px solid var(--line); border-radius: 999px; background: #ffffffb8; color: #36515a; font-size: .78rem; font-weight: 700; }
    .dot { width: 7px; height: 7px; border-radius: 50%; background: #1aaf75; }
    .workspace { display: grid; grid-template-columns: minmax(0, 1.45fr) minmax(280px, .65fr); gap: 18px; align-items: start; }
    .panel { border: 1px solid var(--line); border-radius: 22px; background: var(--card); box-shadow: 0 18px 50px #174d3d0d; }
    .main-panel { padding: clamp(20px, 4vw, 36px); }
    .side-panel { padding: 22px; }
    .panel-head { display: flex; align-items: start; justify-content: space-between; gap: 1rem; margin-bottom: 22px; }
    h2 { margin: 0; font-size: 1.18rem; letter-spacing: -.025em; }
    h3 { margin: 0 0 .55rem; font-size: 1rem; letter-spacing: -.01em; }
    .kicker { margin: 6px 0 0; color: var(--muted); font-size: .83rem; line-height: 1.45; }
    form { display: grid; gap: 16px; }
    label { display: grid; gap: 8px; color: #38525b; font-size: .78rem; font-weight: 800; letter-spacing: .01em; }
    input, button { min-height: 46px; padding: .75rem .9rem; border: 1px solid var(--line); border-radius: 12px; font: inherit; }
    input { width: 100%; color: var(--ink); background: #fbfdfc; outline: none; }
    input:focus { border-color: var(--teal); box-shadow: 0 0 0 3px #087f7820; }
    button { cursor: pointer; color: #fff; background: var(--teal); border-color: var(--teal); font-weight: 800; transition: transform .15s ease, background .15s ease; }
    button:hover { transform: translateY(-1px); background: var(--teal-dark); }
    button.secondary { color: var(--teal-dark); background: #edf8f0; border-color: #cde9d6; }
    button:disabled { cursor: wait; opacity: .65; transform: none; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
    .search-button { width: 100%; margin-top: 3px; }
    .note { margin: 18px 0 0; padding: 12px 14px; border-left: 3px solid var(--lime); color: #536970; background: #f7faee; font-size: .78rem; line-height: 1.5; }
    .stack { display: grid; gap: 18px; }
    .status { display: flex; align-items: center; gap: 10px; padding: 13px 14px; border-radius: 14px; background: #f1faf3; color: #236a48; font-size: .83rem; line-height: 1.35; }
    .status.bad { background: #fff2ef; color: #9b4539; }
    .mini-form { gap: 10px; }
    .project-list { display: grid; gap: 9px; margin: 16px 0 0; padding: 0; list-style: none; }
    .project-item { width: 100%; padding: 11px 12px; border: 1px solid var(--line); border-radius: 12px; color: var(--ink); background: #fbfdfc; text-align: left; }
    button.project-item { min-height: 0; color: var(--ink); background: #fbfdfc; border-color: var(--line); font-size: .84rem; }
    button.project-item:hover { color: var(--teal-dark); background: #f1faf3; border-color: #b9ddc6; }
    .project-item code { display: block; margin-top: 4px; color: var(--muted); font-size: .68rem; }
    .project-detail { margin-top: 12px; padding: 12px; border-radius: 12px; color: #536970; background: #f7faf8; font-size: .76rem; line-height: 1.5; }
    .project-detail strong { color: var(--ink); }
    .project-detail a { color: var(--teal-dark); font-weight: 800; }
    .project-detail hr { margin: 14px 0; border: 0; border-top: 1px solid var(--line); }
    .paper-list, .rag-results, .saved-notes { display: grid; gap: 8px; margin-top: 10px; }
    .paper-card, .rag-chunk, .saved-note { padding: 9px; border: 1px solid var(--line); border-radius: 9px; background: #fff; font-size: .72rem; line-height: 1.4; }
    .paper-card button, .rag-query button { min-height: 28px; padding: .3rem .5rem; font-size: .66rem; }
    .rag-query { display: grid; gap: 7px; margin-top: 10px; }
    .rag-query input { min-height: 34px; padding: .5rem; font-size: .72rem; }
    .parser-run { margin-top: 10px; padding: 10px; border: 1px solid var(--line); border-radius: 10px; background: #fff; }
    .parser-run-head { display: flex; justify-content: space-between; gap: 8px; align-items: center; }
    .parser-run-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
    .parser-run-actions button { min-height: 30px; padding: .35rem .55rem; font-size: .68rem; }
    .parser-block { margin-top: 8px; padding: 8px; border-radius: 8px; background: #f7faf8; font-size: .72rem; line-height: 1.4; }
    .parser-block-actions { display: flex; gap: 5px; margin-top: 6px; }
    .parser-block-actions button { min-height: 26px; padding: .25rem .45rem; font-size: .65rem; }
    .results-section { margin-top: 44px; }
    .results-head { display: flex; align-items: end; justify-content: space-between; gap: 1rem; margin-bottom: 16px; }
    .summary { margin: 5px 0 0; color: var(--muted); font-size: .83rem; }
    .cards { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
    .evidence-card { padding: 19px; border: 1px solid var(--line); border-radius: 17px; background: #fff; }
    .card-meta { display: flex; align-items: center; justify-content: space-between; gap: 1rem; margin-bottom: 15px; }
    .badge { display: inline-block; padding: .3rem .52rem; border-radius: 999px; color: var(--teal-dark); background: #dff4e6; font-size: .68rem; font-weight: 850; letter-spacing: .04em; text-transform: uppercase; }
    .record-id { color: var(--muted); font-size: .69rem; }
    .evidence-card p { margin: 0 0 12px; color: #5b7077; font-size: .8rem; line-height: 1.5; }
    .evidence-card a { color: var(--teal-dark); font-size: .8rem; font-weight: 800; text-decoration: none; }
    .ranking-section { margin-top: 26px; padding-top: 26px; border-top: 1px solid var(--line); }
    .ranking-cards { display: grid; gap: 12px; }
    .ranking-card { padding: 17px; border: 1px solid #cfe2d7; border-radius: 16px; background: #fbfefc; }
    .ranking-card .card-meta { margin-bottom: 10px; }
    .rank-score { color: var(--teal-dark); font-size: .78rem; font-weight: 850; }
    .score-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 6px; margin: 12px 0; }
    .score-cell { padding: 7px; border-radius: 8px; color: #536970; background: #f0f7f2; font-size: .66rem; }
    .score-cell strong { display: block; color: var(--ink); font-size: .76rem; }
    .reason-line { margin: 9px 0; color: #536970; font-size: .76rem; line-height: 1.45; }
    .review-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; margin-top: 12px; }
    .review-actions button { min-height: 34px; padding: .45rem .65rem; font-size: .7rem; }
    .review-status { color: var(--teal-dark); font-size: .72rem; font-weight: 800; }
    .insight-section { margin-top: 26px; padding: 18px; border: 1px solid #d8e4eb; border-radius: 16px; background: #fbfdff; }
    .insight-card { padding: 13px 0; border-top: 1px solid #e5edf1; color: #405963; font-size: .82rem; line-height: 1.5; }
    .insight-card:first-child { border-top: 0; }
    .citation { display: inline-block; margin: 7px 5px 0 0; padding: 4px 7px; border-radius: 7px; color: var(--teal-dark); background: #e1f4e8; font-size: .69rem; font-weight: 800; }
    .empty { grid-column: 1 / -1; padding: 28px; border: 1px dashed #bfd4cb; border-radius: 17px; color: var(--muted); background: #fbfdfb; text-align: center; }
    code { overflow-wrap: anywhere; }
    .muted { color: var(--muted); }
    .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }
    @media (max-width: 800px) { .topbar { margin-bottom: 45px; } .workspace { grid-template-columns: 1fr; } .cards { grid-template-columns: 1fr; } }
    @media (max-width: 520px) { .shell { width: min(100% - 24px, 1180px); padding-top: 18px; } .row { grid-template-columns: 1fr; } h1 { font-size: 2.9rem; } .toplink { display: none; } }
  </style>
</head>
<body>
  <main class="shell">
    <nav class="topbar" aria-label="Workspace navigation">
      <div class="brand"><span class="mark" aria-hidden="true">B</span><span>biolab / evidence</span></div>
      <span class="toplink">A traceable workspace for scientific questions</span>
    </nav>
    <header>
      <p class="eyebrow">Evidence workspace · v0.4</p>
      <h1>Trace every scientific answer back to its source.</h1>
      <p class="lede">Search trusted life-science sources, preserve the returned evidence, and keep an audit trail that a reviewer can inspect later.</p>
      <div class="signals" aria-label="Workspace guarantees">
        <span class="signal"><span class="dot"></span>Local-first</span>
        <span class="signal"><span class="dot"></span>Hash-chained retrievals</span>
        <span class="signal"><span class="dot"></span>Human review required</span>
      </div>
    </header>
    <div class="workspace">
      <section class="panel main-panel" aria-labelledby="search-title">
        <div class="panel-head">
          <div><h2 id="search-title">Start an evidence pass</h2><p class="kicker">Ask one focused question. Biolab records what came back, from where, and when.</p></div>
          <span class="badge">Step 01 / retrieve</span>
        </div>
        <form id="search-form">
          <label for="query">Scientific question
            <input id="query" required placeholder="e.g. KRAS variants in pancreatic cancer" autocomplete="off">
          </label>
          <div class="row">
            <label for="sources">Sources
              <input id="sources" value="pubmed,europepmc,clinicaltrials,uniprot,opentargets">
            </label>
            <label for="max-results">Results per source
              <input id="max-results" type="number" min="1" max="50" value="3">
            </label>
          </div>
          <button class="search-button" type="submit"><span id="search-label">Search and preserve evidence</span></button>
        </form>
        <p class="note"><strong>Evidence boundary:</strong> retrieval is not validation. Use the preserved records to support a claim, then review the underlying source and context.</p>
      </section>
      <aside class="stack" aria-label="Workspace controls">
        <section class="panel side-panel">
          <div class="panel-head"><div><h2>Audit ledger</h2><p class="kicker">Integrity status for preserved retrievals.</p></div><span aria-hidden="true">◌</span></div>
          <div id="audit" class="status">Checking chain…</div>
          <button id="verify" class="secondary" type="button" style="width:100%; margin-top:12px">Verify hash chain</button>
        </section>
        <section class="panel side-panel">
          <div class="panel-head"><div><h2>Projects</h2><p class="kicker">Group evidence for a reviewable question.</p></div><span aria-hidden="true">＋</span></div>
          <form id="project-form" class="mini-form">
            <label class="sr-only" for="project-name">New project name</label>
            <input id="project-name" required placeholder="New project name">
            <button type="submit">Create project</button>
          </form>
          <ul id="projects" class="project-list"><li class="muted">Loading…</li></ul>
          <div id="project-detail" class="project-detail" hidden></div>
        </section>
        <details class="panel side-panel">
          <summary><strong>Remote API key</strong> <span class="muted">(required)</span></summary>
          <p class="kicker">This deployment requires a key. It is stored only in this browser tab and sent as a Bearer header.</p>
          <label class="sr-only" for="api-key">API key</label>
          <input id="api-key" type="password" placeholder="blb_…" autocomplete="off">
        </details>
      </aside>
    </div>
    <section class="results-section" aria-labelledby="evidence-title">
      <div class="results-head"><div><p class="eyebrow">Evidence ledger</p><h2 id="evidence-title">Preserved results</h2><p id="summary" class="summary">Ready for your first evidence pass.</p></div><span class="badge">Step 02 / inspect</span></div>
      <div id="results" class="cards"><div class="empty">Your evidence ledger is empty. Run a focused search to create the first preserved record.</div></div>
      <section id="ranking-section" class="ranking-section" hidden aria-labelledby="ranking-title">
        <div class="results-head"><div><p class="eyebrow">Review queue</p><h2 id="ranking-title">Top-ranked papers</h2><p id="ranking-summary" class="summary">Ranking preserved results…</p></div><span class="badge">Step 03 / review</span></div>
        <div id="ranking-results" class="ranking-cards"></div>
        <label class="project-picker-label" for="project-picker">Project for accepted papers
          <select id="project-picker"><option value="">Choose a project…</option></select>
        </label>
        <button id="generate-insights" class="secondary" type="button" style="margin-top:14px">Generate grounded insights</button>
        <button id="save-reviewed" class="secondary" type="button" style="margin:14px 0 0 8px" disabled>Save accepted papers to project</button>
      </section>
      <section id="insight-section" class="insight-section" hidden aria-labelledby="insight-title">
        <div class="results-head"><div><p class="eyebrow">Evidence-grounded inference</p><h2 id="insight-title">Extracted insights</h2><p id="insight-summary" class="summary"></p></div><span class="badge">Step 04 / cite</span></div>
        <div id="insights" class="insight-cards"></div>
        <button id="flush-parser" class="secondary" type="button" style="margin-top:12px">Flush temporary parser data</button>
      </section>
    </section>
  </main>
  <script>
    const form = document.querySelector('#search-form');
    const summary = document.querySelector('#summary');
    const results = document.querySelector('#results');
    const rankingSection = document.querySelector('#ranking-section');
    const rankingSummary = document.querySelector('#ranking-summary');
    const rankingResults = document.querySelector('#ranking-results');
    const generateInsights = document.querySelector('#generate-insights');
    const saveReviewed = document.querySelector('#save-reviewed');
    const projectPicker = document.querySelector('#project-picker');
    const insightSection = document.querySelector('#insight-section');
    const insightSummary = document.querySelector('#insight-summary');
    const insights = document.querySelector('#insights');
    const flushParser = document.querySelector('#flush-parser');
    let latestRecords = [];
    let latestRanking = null;
    let latestProjectId = null;
    let latestInferenceRunId = null;
    const apiBase = window.location.pathname.startsWith('/api') ? '/api' : '';
    const apiFetch = (url, options = {}) => {
      const key = document.querySelector('#api-key').value.trim();
      const headers = {...(options.headers || {})};
      if (key) headers.Authorization = `Bearer ${key}`;
      return fetch(`${apiBase}${url}`, {...options, headers});
    };
    const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({
      '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
    }[c]));
    const maskId = (value) => {
      const id = String(value ?? '');
      return id.length > 16 ? `${id.slice(0, 8)}…${id.slice(-6)}` : id;
    };
    const scoreLabel = (name) => name.replaceAll('_', ' ');
    function renderRanking(run, records) {
      const byId = Object.fromEntries(records.map(record => [record.retrieval_id, record]));
      rankingSummary.textContent = `${run.candidate_count} papers ranked with ${run.algorithm_version}`;
      rankingResults.innerHTML = run.candidates.map(candidate => {
        const record = byId[candidate.retrieval_id] || {};
        const snap = record.snapshot || {};
        const title = snap.title || snap.name || snap.primary_accession || candidate.external_id;
        const components = Object.entries(candidate.score_components).map(([name, value]) =>
          `<span class="score-cell">${escapeHtml(scoreLabel(name))}<strong>${(Number(value) * 100).toFixed(0)}%</strong></span>`).join('');
        const reasons = candidate.reason_codes.map(reason => escapeHtml(reason.replaceAll('_', ' '))).join(', ') || 'No explanation codes';
        const matched = candidate.matched_terms.length ? candidate.matched_terms.map(escapeHtml).join(', ') : 'None recorded';
        const missing = candidate.missing_facets.length ? candidate.missing_facets.map(escapeHtml).join(', ') : 'None';
        return `<article class="ranking-card" data-candidate-id="${escapeHtml(candidate.ranking_candidate_id)}">
          <div class="card-meta"><span class="badge">Rank ${candidate.rank}</span><span class="rank-score">${(Number(candidate.score_total) * 100).toFixed(1)}% fit</span></div>
          <h3>${escapeHtml(title)}</h3>
          <p class="reason-line"><strong>Why:</strong> ${reasons}</p>
          <div class="score-grid">${components}</div>
          <p class="reason-line"><strong>Matched:</strong> ${matched}<br><strong>Missing:</strong> ${missing}<br><strong>Source:</strong> ${escapeHtml(record.source || candidate.source)} · ${escapeHtml(candidate.external_id)}</p>
          <div class="review-actions" data-run-id="${escapeHtml(run.ranking_run_id)}">
            <button class="secondary" type="button" data-review-state="accepted">Accept</button>
            <button class="secondary" type="button" data-review-state="rejected">Reject</button>
            <button class="secondary" type="button" data-review-state="conflicting">Conflicting</button>
            <span class="review-status">Unreviewed</span>
          </div>
        </article>`;
      }).join('') || '<div class="empty">No candidates were available to rank.</div>';
    }
    rankingResults.addEventListener('click', async (event) => {
      const button = event.target.closest('[data-review-state]');
      if (!button) return;
      const card = button.closest('[data-candidate-id]');
      const actions = button.closest('[data-run-id]');
      const status = actions.querySelector('.review-status');
      button.disabled = true;
      try {
        const response = await apiFetch(`/v1/rankings/${encodeURIComponent(actions.dataset.runId)}/candidates/${encodeURIComponent(card.dataset.candidateId)}/review`, {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({review_state: button.dataset.reviewState, reviewed_by: 'web:local'})
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || `Review returned ${response.status}`);
        const reviewed = latestRanking?.candidates?.find(candidate => candidate.ranking_candidate_id === card.dataset.candidateId);
        if (reviewed) reviewed.review_state = result.review_state;
        saveReviewed.disabled = !(latestProjectId && latestRanking?.candidates?.some(candidate => candidate.review_state === 'accepted'));
        status.textContent = `Marked ${result.review_state}`;
      } catch (error) { status.textContent = String(error); }
      finally { button.disabled = false; }
    });
    saveReviewed.addEventListener('click', async () => {
      latestProjectId = projectPicker.value || latestProjectId;
      if (!latestProjectId || !latestRanking) { projectPicker.focus(); return; }
      const accepted = latestRanking.candidates.filter(candidate => candidate.review_state === 'accepted').map(candidate => candidate.ranking_candidate_id);
      if (!accepted.length) return;
      saveReviewed.disabled = true;
      try {
        const response = await apiFetch(`/v1/projects/${encodeURIComponent(latestProjectId)}/save-reviewed`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ranking_run_id: latestRanking.ranking_run_id, candidate_ids: accepted})});
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || `Save returned ${response.status}`);
        document.querySelector('#project-detail').hidden = false;
        document.querySelector('#project-detail').textContent = `Saved ${result.saved_count} reviewed paper(s) to the project collection.`;
      } catch (error) { document.querySelector('#project-detail').textContent = String(error); }
      finally { saveReviewed.disabled = false; }
    });
    projectPicker.addEventListener('change', () => {
      latestProjectId = projectPicker.value || null;
      saveReviewed.disabled = !(latestProjectId && latestRanking?.candidates?.some(candidate => candidate.review_state === 'accepted'));
    });
    function renderInsights(result) {
      insightSummary.textContent = `${result.insight_count ?? result.claims?.length ?? 0} grounded insights · ${result.parser_version}` + (result.durability === 'request-context' ? ' · session evidence only; configure Turso for durable citations' : '');
      insights.innerHTML = (result.insights || result.claims || []).map(claim => {
        const citations = (claim.citations || []).map(citation => `<span class="citation">${escapeHtml(citation.source)}:${escapeHtml(citation.external_id)} · ${escapeHtml(citation.field_name)} · ${escapeHtml(citation.excerpt || 'span preserved')}</span>`).join('');
        return `<article class="insight-card"><strong>${escapeHtml(claim.text)}</strong><div>${citations}</div></article>`;
      }).join('') || '<div class="empty">No grounded sentence matched the question terms.</div>';
    }
    generateInsights.addEventListener('click', async () => {
      if (!latestRanking || !latestRecords.length) return;
      generateInsights.disabled = true;
      insightSection.hidden = false;
      insightSummary.textContent = 'Parsing preserved evidence and validating citations…';
      try {
        const response = await apiFetch('/v1/inferences', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({question: document.querySelector('#query').value, retrieval_ids: latestRecords.map(record => record.retrieval_id), evidence_records: latestRecords, ranking_run_id: latestRanking.ranking_run_id, max_insights: 10, created_by: 'web:local'})
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || `Inference returned ${response.status}`);
        latestInferenceRunId = result.inference_run_id;
        renderInsights(result);
      } catch (error) { insightSummary.textContent = String(error); }
      finally { generateInsights.disabled = false; }
    });
    flushParser.addEventListener('click', async () => {
      if (!latestInferenceRunId) return;
      flushParser.disabled = true;
      try {
        const response = await apiFetch(`/v1/inferences/${encodeURIComponent(latestInferenceRunId)}/flush`, {method: 'POST'});
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || `Flush returned ${response.status}`);
        insightSummary.textContent += ` · ${result.flushed_parser_chunks} temporary chunks flushed; citations preserved`;
      } catch (error) { insightSummary.textContent = String(error); }
      finally { flushParser.disabled = false; }
    });
    async function verify() {
      const audit = document.querySelector('#audit');
      const button = document.querySelector('#verify');
      try {
        const response = await apiFetch('/v1/verify');
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || `Audit check returned ${response.status}`);
        audit.className = `status${result.valid ? '' : ' bad'}`;
        audit.textContent = result.valid
          ? `Chain valid · ${result.record_count} preserved retrievals`
          : `Chain broken at ${result.first_broken_retrieval_id}`;
        button.textContent = result.valid ? 'Verified just now' : 'Chain needs review';
      } catch (error) {
        audit.className = 'status bad';
        audit.textContent = error instanceof Error && error.message === 'API key required'
          ? 'Enter an API key to verify the audit ledger'
          : 'Audit status unavailable';
        button.textContent = 'Verify audit';
      }
    }
    async function loadProjects() {
      const response = await apiFetch('/v1/projects');
      const result = await response.json();
      projectPicker.innerHTML = '<option value="">Choose a project…</option>' + result.projects.map(p => `<option value="${escapeHtml(p.project_id)}">${escapeHtml(p.name)}</option>`).join('');
      document.querySelector('#projects').innerHTML = result.projects.length
        ? result.projects.map(p => `<li><button class="project-item" type="button" data-project-id="${escapeHtml(p.project_id)}"><strong>${escapeHtml(p.name)}</strong><code>${escapeHtml(maskId(p.project_id))}</code></button></li>`).join('')
        : '<li class="muted">No projects yet.</li>';
    }
    const bytesToBase64 = (bytes) => {
      let binary = '';
      for (let index = 0; index < bytes.length; index += 0x8000) binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
      return btoa(binary);
    };
    async function loadParserRuns(projectId) {
      const container = document.querySelector('[data-parser-runs]');
      if (!container) return;
      const response = await apiFetch(`/v1/projects/${encodeURIComponent(projectId)}/parser-runs`);
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `Parser list returned ${response.status}`);
      container.innerHTML = result.parser_runs.length ? result.parser_runs.map(run => `<article class="parser-run" data-run-id="${escapeHtml(run.parser_run_id)}">
        <div class="parser-run-head"><strong>${escapeHtml(run.filename)}</strong><span class="badge">${escapeHtml(run.status)}</span></div>
        <div class="muted">${run.block_count} blocks · ${escapeHtml(run.parser_model)} · ${run.retrieval_id ? `MCP paper ${escapeHtml(maskId(run.retrieval_id))}` : 'project document'}</div>
        <div class="parser-run-actions"><a href="${apiBase}/v1/parser/runs/${encodeURIComponent(run.parser_run_id)}/download" target="_blank" rel="noreferrer">Download parsed JSON ↗</a>
          <button class="secondary" type="button" data-parser-inspect="${escapeHtml(run.parser_run_id)}">Inspect blocks</button>
          <button class="secondary" type="button" data-parser-rebuild="${escapeHtml(run.parser_run_id)}">Rebuild RAG index</button></div>
        <div data-parser-output hidden></div></article>`).join('') : '<div class="muted">No papers yet. Upload a PDF or text file above.</div>';
    }
    async function loadProjectPapers(projectId) {
      const container = document.querySelector('[data-project-papers]');
      const notes = document.querySelector('[data-project-notes]');
      if (!container) return;
      const response = await apiFetch(`/v1/projects/${encodeURIComponent(projectId)}/retrievals`);
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `Paper list returned ${response.status}`);
      container.innerHTML = result.retrievals.length ? '<div class="paper-selection-actions"><button class="secondary" type="button" data-parse-selected="'+escapeHtml(projectId)+'">Parse selected papers</button><span class="muted">Select only the papers you want in this project RAG.</span></div>' + result.retrievals.map(paper => {
        const snap = paper.snapshot || {};
        const title = snap.title || snap.name || snap.primary_accession || paper.external_id;
        return `<article class="paper-card"><label><input type="checkbox" data-paper-select="${escapeHtml(paper.retrieval_id)}" ${paper.parser_run_id ? 'checked' : ''}> <strong>${escapeHtml(title)}</strong></label><br><span class="muted">${escapeHtml(paper.source)} · ${escapeHtml(paper.external_id)} · ${escapeHtml(paper.relevance)}</span><br>
          ${paper.parser_run_id ? `<span class="badge">Parsed into project</span>` : `<button class="secondary" type="button" data-paper-parse="${escapeHtml(paper.retrieval_id)}">Parse this MCP paper</button>`}</article>`;
      }).join('') : '<div class="muted">No MCP papers selected yet. Search above, accept a result, then select it for this project.</div>';
      if (notes) {
        const noteResponse = await apiFetch(`/v1/projects/${encodeURIComponent(projectId)}/rag/notes`);
        const noteResult = await noteResponse.json();
        notes.innerHTML = noteResult.notes?.length ? noteResult.notes.map(note => `<article class="saved-note"><strong>${escapeHtml(note.note_text)}</strong><br><span class="muted">${escapeHtml(note.question)} · ${escapeHtml(note.created_at)}</span></article>`).join('') : '<div class="muted">No saved RAG notes yet.</div>';
      }
    }
    async function loadProject(projectId) {
      latestProjectId = projectId;
      saveReviewed.disabled = !(latestRanking?.candidates?.some(candidate => candidate.review_state === 'accepted'));
      const detail = document.querySelector('#project-detail');
      detail.hidden = false;
      detail.textContent = 'Loading project report…';
      try {
        const response = await apiFetch(`/v1/projects/${encodeURIComponent(projectId)}/report`);
        if (!response.ok) {
          if (response.status === 404) {
            throw new Error('Project report unavailable. Configure TURSO_DATABASE_URL and TURSO_AUTH_TOKEN for durable Vercel storage, then create the project again.');
          }
          throw new Error(`Project report returned ${response.status}`);
        }
        const report = await response.json();
        const project = report.project || {};
        detail.innerHTML = `<strong>${escapeHtml(project.name || 'Project')}</strong><br>
          ${escapeHtml(project.description || 'No description')}<br>
          ${report.collections.length} collections · ${report.claims.length} claims · ${(report.parser_runs || []).length} parsed documents · ${report.watches.length} watches<br>
          <a href="${apiBase}/v1/projects/${encodeURIComponent(projectId)}/report" target="_blank" rel="noreferrer">Open JSON report ↗</a> ·
          <a href="${apiBase}/v1/projects/${encodeURIComponent(projectId)}/report?format=markdown" target="_blank" rel="noreferrer">Open Markdown report ↗</a>
          <button class="secondary" type="button" data-project-audit="${escapeHtml(projectId)}">Audit project history</button><div class="project-audit" data-project-audit-output></div>
          <hr><strong>Selected MCP papers</strong><div class="muted">Choose a preserved paper from the evidence results above. Biolab parses its saved title/abstract into this project; no re-upload is needed.</div>
          <div class="paper-list" data-project-papers>Loading selected papers…</div>
          <hr><strong>Query this paper RAG</strong>
          <form class="rag-query" data-rag-query="${escapeHtml(projectId)}"><input required placeholder="Ask about the selected paper…"><button type="submit">Search reviewed RAG</button></form>
          <div class="rag-results" data-rag-results></div>
          <div class="saved-notes" data-project-notes></div>
          <hr><strong>Parsed documents</strong><div data-parser-runs>Loading parsed papers…</div>`;
        await loadParserRuns(projectId);
        await loadProjectPapers(projectId);
      } catch (error) { detail.textContent = String(error); }
    }
    document.querySelector('#verify').addEventListener('click', verify);
    document.querySelector('#projects').addEventListener('click', (event) => {
      const button = event.target.closest('[data-project-id]');
      if (button) { latestProjectId = button.dataset.projectId; projectPicker.value = latestProjectId; window.location.href = `/projects/${encodeURIComponent(button.dataset.projectId)}`; }
    });
    document.querySelector('#project-detail').addEventListener('submit', async (event) => {
      const form = event.target.closest('[data-rag-query]');
      if (!form) return;
      event.preventDefault();
      const question = form.querySelector('input').value.trim();
      const output = form.parentElement.querySelector('[data-rag-results]');
      output.textContent = 'Searching reviewed paper passages…';
      try {
        const response = await apiFetch(`/v1/projects/${encodeURIComponent(form.dataset.ragQuery)}/rag/search`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({question, top_k: 8})});
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || `RAG returned ${response.status}`);
        if (result.status === 'disabled') { output.innerHTML = `<div class="muted">RAG is not active yet: ${escapeHtml(result.reason)}. Configure an embedding provider and pgvector.</div>`; return; }
        output.innerHTML = result.chunks?.length ? result.chunks.map(chunk => `<article class="rag-chunk"><strong>Page ${escapeHtml(chunk.page_number)} · ${escapeHtml(chunk.section_name)}</strong><br>${escapeHtml(chunk.chunk_text)}<br><span class="muted">${escapeHtml(chunk.content_hash || chunk.block_id)}</span></article>`).join('') + `<button class="secondary" type="button" data-save-rag-note data-project-id="${escapeHtml(form.dataset.ragQuery)}" data-question="${escapeHtml(question)}">Save these passages as a project note</button>` : '<div class="muted">No reviewed passages matched.</div>';
      } catch (error) { output.textContent = String(error); }
    });
    document.querySelector('#project-detail').addEventListener('click', async (event) => {
      const projectAudit = event.target.closest('[data-project-audit]');
      const inspect = event.target.closest('[data-parser-inspect]');
      const rebuild = event.target.closest('[data-parser-rebuild]');
      const review = event.target.closest('[data-parser-review]');
      const parseSelected = event.target.closest('[data-parse-selected]');
      try {
        if (parseSelected) {
          const projectId = parseSelected.dataset.parseSelected;
          const selected = [...document.querySelectorAll('[data-paper-select]:checked')].map(item => item.dataset.paperSelect);
          if (!selected.length) throw new Error('Select at least one paper to parse.');
          parseSelected.disabled = true;
          for (const retrievalId of selected) {
            await apiFetch(`/v1/projects/${encodeURIComponent(projectId)}/retrievals/${encodeURIComponent(retrievalId)}/parse`, {method: 'POST'});
          }
          await loadProject(projectId);
          return;
        }
        if (projectAudit) {
          projectAudit.disabled = true;
          const response = await apiFetch(`/v1/projects/${encodeURIComponent(projectAudit.dataset.projectAudit)}/audit`);
          const result = await response.json();
          if (!response.ok) throw new Error(result.error || `Project audit returned ${response.status}`);
          const output = document.querySelector('[data-project-audit-output]');
          output.innerHTML = `<strong>${result.valid ? 'Project history valid' : 'Project history broken'}</strong> · ${result.event_count} events` + (result.events.length ? '<div class="audit-events">' + result.events.map(item => `<div class="audit-event"><strong>${escapeHtml(item.event_type)}</strong><span>${escapeHtml(item.created_at)} · ${escapeHtml(item.entity_type)} ${escapeHtml(item.entity_id)}</span></div>`).join('') + '</div>' : '<div class="muted">No project events yet.</div>');
          projectAudit.textContent = 'Project history checked';
          return;
        }
        if (inspect) {
          const runId = inspect.dataset.parserInspect;
          const output = inspect.closest('[data-run-id]').querySelector('[data-parser-output]');
          const response = await apiFetch(`/v1/parser/runs/${encodeURIComponent(runId)}`);
          const result = await response.json();
          if (!response.ok) throw new Error(result.error || `Parser detail returned ${response.status}`);
          output.hidden = false;
          output.innerHTML = result.blocks.map(block => `<div class="parser-block"><strong>Page ${escapeHtml(block.page_number)} · ${escapeHtml(block.section_name)} · ${escapeHtml(block.review_state)}</strong><br>${escapeHtml(block.text)}<div class="parser-block-actions"><button class="secondary" type="button" data-parser-review="accepted" data-run-id="${escapeHtml(runId)}" data-block-id="${escapeHtml(block.block_id)}">Accept for RAG</button><button class="secondary" type="button" data-parser-review="excluded" data-run-id="${escapeHtml(runId)}" data-block-id="${escapeHtml(block.block_id)}">Exclude</button></div></div>`).join('') || '<div class="muted">No parsed blocks.</div>';
        }
        if (rebuild) {
          rebuild.disabled = true;
          const response = await apiFetch(`/v1/parser/runs/${encodeURIComponent(rebuild.dataset.parserRebuild)}/rebuild-index`, {method: 'POST'});
          const result = await response.json();
          if (!response.ok) throw new Error(result.error || `Rebuild returned ${response.status}`);
          rebuild.textContent = `${result.indexed_blocks ?? 0} indexed`;
        }
        if (review) {
          const response = await apiFetch(`/v1/parser/runs/${encodeURIComponent(review.dataset.runId)}/blocks/${encodeURIComponent(review.dataset.blockId)}/review`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({review_state: review.dataset.parserReview})});
          const result = await response.json();
          if (!response.ok) throw new Error(result.error || `Review returned ${response.status}`);
          review.parentElement.previousSibling.textContent = `${result.review_state} · ${result.vector_status?.status || 'not indexed'}`;
          review.disabled = true;
        }
        const paper = event.target.closest('[data-paper-parse]');
        if (paper) {
          paper.disabled = true;
          const projectId = latestProjectId;
          const response = await apiFetch(`/v1/projects/${encodeURIComponent(projectId)}/retrievals/${encodeURIComponent(paper.dataset.paperParse)}/parse`, {method: 'POST'});
          const result = await response.json();
          if (!response.ok) throw new Error(result.error || `Parse returned ${response.status}`);
          await loadProject(projectId);
        }
        const saveNote = event.target.closest('[data-save-rag-note]');
        if (saveNote) {
          const response = await apiFetch(`/v1/projects/${encodeURIComponent(saveNote.dataset.projectId)}/rag/notes`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({question: saveNote.dataset.question, note_text: 'Reviewed passages saved from the project RAG query.', citations: []})});
          if (!response.ok) throw new Error(`Save note returned ${response.status}`);
          await loadProjectPapers(saveNote.dataset.projectId);
        }
      } catch (error) { window.alert(String(error)); }
    });
    document.querySelector('#project-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const nameInput = document.querySelector('#project-name');
      const detail = document.querySelector('#project-detail');
      const name = nameInput.value.trim();
      detail.hidden = false;
      detail.textContent = 'Creating project…';
      try {
        const response = await apiFetch('/v1/projects', {method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name})});
        if (!response.ok) {
          const result = await response.json().catch(() => ({}));
          throw new Error(result.error === 'API key required'
            ? 'This deployment requires an API key. Add it under Remote API key.'
            : result.error || `Project creation returned ${response.status}`);
        }
        const created = await response.json();
        nameInput.value = '';
        await loadProjects();
        if (created.project_id) await loadProject(created.project_id);
      } catch (error) {
        detail.textContent = String(error);
      }
    });
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const button = form.querySelector('button');
      button.disabled = true;
      document.querySelector('#search-label').textContent = 'Searching sources…';
      summary.textContent = 'Searching sources and preserving returned records…';
      results.innerHTML = '';
      rankingSection.hidden = true;
      rankingResults.innerHTML = '';
      insightSection.hidden = true;
      insights.innerHTML = '';
      latestRecords = [];
      latestRanking = null;
      latestInferenceRunId = null;
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
        await verify();
        const records = Object.values(result.sources).flat();
        latestRecords = records;
        results.innerHTML = records.map(record => {
          const snap = record.snapshot || {};
          const title = snap.title || snap.name || snap.primary_accession || record.external_id;
          const candidateUrl = snap.url || snap.canonical_url || '';
          const url = /^https?:\/\//i.test(candidateUrl) ? candidateUrl : '';
          return `<article class="evidence-card" data-retrieval-id="${escapeHtml(record.retrieval_id)}"><div class="card-meta"><span class="badge">${escapeHtml(record.source)}</span><span class="record-id">${escapeHtml(record.external_id)}</span></div>
            <h3>${escapeHtml(title)}</h3><p>Preserved retrieval <code>${escapeHtml(record.retrieval_id)}</code></p>
            ${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">Open source ↗</a>` : ''}
            <button class="secondary" type="button" data-select-retrieval="${escapeHtml(record.retrieval_id)}" style="margin-top:10px">Select for project</button></article>`;
        }).join('') || '<div class="empty">No records returned. Check the source selection or broaden the question.</div>';
        if (records.length) {
          rankingSection.hidden = false;
          rankingSummary.textContent = 'Ranking preserved results…';
          const rankingResponse = await apiFetch('/v1/rankings', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({question: payload.query, retrieval_ids: records.map(record => record.retrieval_id), requested_limit: 5, created_by: 'web:local'})
          });
          const rankingResult = await rankingResponse.json();
          if (!rankingResponse.ok) throw new Error(rankingResult.error || `Ranking returned ${rankingResponse.status}`);
          latestRanking = rankingResult;
          renderRanking(rankingResult, records);
          saveReviewed.disabled = !(latestProjectId && rankingResult.candidates.some(candidate => candidate.review_state === 'accepted'));
        }
      } catch (error) {
        summary.textContent = 'Search failed. The source error is preserved in the API response when available.';
        results.innerHTML = `<div class="empty">${escapeHtml(error)}</div>`;
        rankingSection.hidden = true;
      } finally { button.disabled = false; document.querySelector('#search-label').textContent = 'Search and preserve evidence'; }
    });
    results.addEventListener('click', async (event) => {
      const button = event.target.closest('[data-select-retrieval]');
      if (!button) return;
      if (!latestProjectId) { window.alert('Select or create a project first.'); return; }
      button.disabled = true;
      try {
        const response = await apiFetch(`/v1/projects/${encodeURIComponent(latestProjectId)}/retrievals`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({retrieval_id: button.dataset.selectRetrieval})});
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || `Selection returned ${response.status}`);
        button.textContent = 'Selected for project';
        await loadProject(latestProjectId);
      } catch (error) { button.disabled = false; button.textContent = String(error); }
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


async def project_page(request: Request) -> HTMLResponse:
    project_id = request.path_params["project_id"]
    report = workspace.get_project_report(request.app.state.conn, project_id, auth.request_identity(request))
    if report is None:
        return HTMLResponse("Project not found", status_code=404)
    return HTMLResponse(PROJECT_PAGE_HTML)


async def health(request: Request) -> JSONResponse:
    request.app.state.conn.execute("SELECT 1").fetchone()
    return JSONResponse({"status": "ok", "service": "biolab"})


async def storage(request: Request) -> JSONResponse:
    return JSONResponse({"metadata": db.storage_status(request.app.state.conn), "vectors": rag.storage_status()})


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
        agent_id = auth.bind_agent_id(str(payload.get("agent_id", "api:user")), request)
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
    if auth.is_authenticated(request) and record.agent_id != auth.request_identity(request):
        return JSONResponse({"error": "retrieval not found"}, status_code=404)
    return JSONResponse(_record_payload(record))


async def rankings(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        retrieval_ids = payload.get("retrieval_ids", [])
        if not isinstance(retrieval_ids, list) or not all(isinstance(item, str) for item in retrieval_ids):
            return JSONResponse({"error": "retrieval_ids must be a list of strings"}, status_code=400)
        project_id = str(payload["project_id"]) if payload.get("project_id") else None
        if project_id and not workspace.has_project_access(request.app.state.conn, project_id, auth.request_identity(request)):
            return JSONResponse({"error": "project not found"}, status_code=404)
        if auth.is_authenticated(request):
            identity = auth.request_identity(request)
            inaccessible = [item for item in retrieval_ids if (record := retrieval_log.get_retrieval(request.app.state.conn, item)) is None or record.agent_id != identity]
            if inaccessible:
                return JSONResponse({"error": "retrieval access denied"}, status_code=404)
        created_by = auth.bind_agent_id(str(payload.get("created_by", "api:ranking")), request)
        result = ranking.rank_retrievals(
            request.app.state.conn,
            str(payload.get("question", "")),
            retrieval_ids,
            created_by=created_by,
            project_id=project_id,
            requested_limit=int(payload.get("requested_limit", 5)),
            as_of=str(payload["as_of"]) if payload.get("as_of") else None,
        )
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(result, status_code=201)


async def ranking_detail(request: Request) -> JSONResponse:
    result = ranking.get_ranking(
        request.app.state.conn, request.path_params["ranking_run_id"]
    )
    if result is None:
        return JSONResponse({"error": "ranking run not found"}, status_code=404)
    if result.get("project_id") and not workspace.has_project_access(request.app.state.conn, result["project_id"], auth.request_identity(request)):
        return JSONResponse({"error": "ranking run not found"}, status_code=404)
    return JSONResponse(result)


async def ranking_review(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        ranking_run = ranking.get_ranking(request.app.state.conn, request.path_params["ranking_run_id"])
        if ranking_run is None or (ranking_run.get("project_id") and not workspace.has_project_access(request.app.state.conn, ranking_run["project_id"], auth.request_identity(request))):
            return JSONResponse({"error": "ranking run not found"}, status_code=404)
        result = ranking.review_candidate(
            request.app.state.conn,
            request.path_params["ranking_run_id"],
            request.path_params["ranking_candidate_id"],
            review_state=str(payload.get("review_state", "")),
            reviewed_by=auth.bind_agent_id(str(payload.get("reviewed_by", "api:review")), request),
            review_note=str(payload.get("review_note", "")),
        )
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(result)


async def inferences(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        retrieval_ids = payload.get("retrieval_ids", [])
        if not isinstance(retrieval_ids, list) or not all(isinstance(item, str) for item in retrieval_ids):
            return JSONResponse({"error": "retrieval_ids must be a list of strings"}, status_code=400)
        project_id = str(payload["project_id"]) if payload.get("project_id") else None
        if project_id and not workspace.has_project_access(request.app.state.conn, project_id, auth.request_identity(request)):
            return JSONResponse({"error": "project not found"}, status_code=404)
        if auth.is_authenticated(request):
            identity = auth.request_identity(request)
            if any((record := retrieval_log.get_retrieval(request.app.state.conn, item)) is None or record.agent_id != identity for item in retrieval_ids):
                return JSONResponse({"error": "retrieval access denied"}, status_code=404)
        result = inference.create_inference(
            request.app.state.conn,
            str(payload.get("question", "")),
            retrieval_ids,
            created_by=auth.bind_agent_id(str(payload.get("created_by", "api:inference")), request),
            project_id=project_id,
            ranking_run_id=str(payload["ranking_run_id"]) if payload.get("ranking_run_id") else None,
            max_insights=int(payload.get("max_insights", 10)),
            evidence_records=payload.get("evidence_records") if isinstance(payload.get("evidence_records"), list) else None,
        )
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(result, status_code=201)


async def inference_detail(request: Request) -> JSONResponse:
    result = inference.get_inference(request.app.state.conn, request.path_params["inference_run_id"])
    if result is None:
        return JSONResponse({"error": "inference run not found"}, status_code=404)
    if result.get("project_id") and not workspace.has_project_access(request.app.state.conn, result["project_id"], auth.request_identity(request)):
        return JSONResponse({"error": "inference run not found"}, status_code=404)
    return JSONResponse(result)


async def inference_flush(request: Request) -> JSONResponse:
    try:
        existing = inference.get_inference(request.app.state.conn, request.path_params["inference_run_id"])
        if existing is None or (existing.get("project_id") and not workspace.has_project_access(request.app.state.conn, existing["project_id"], auth.request_identity(request))):
            return JSONResponse({"error": "inference run not found"}, status_code=404)
        result = inference.flush_parser_chunks(
            request.app.state.conn, request.path_params["inference_run_id"]
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse(result)


async def parser_parse(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        result = document_parser.create_parser_run(
            request.app.state.conn, _decode_document_payload(payload.get("content_base64", "")),
            str(payload.get("filename", "document.pdf")), auth.request_identity(request), str(payload.get("project_id")) if payload.get("project_id") else None,
        )
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse(result, status_code=201)


async def parser_detail(request: Request) -> JSONResponse:
    try:
        result = document_parser.assert_run_access(request.app.state.conn, request.path_params["parser_run_id"], auth.request_identity(request))
    except ValueError:
        return JSONResponse({"error": "parser run not found"}, status_code=404)
    return JSONResponse(result)


async def project_parser_runs(request: Request) -> JSONResponse:
    try:
        result = document_parser.list_parser_runs(request.app.state.conn, request.path_params["project_id"], auth.request_identity(request))
    except ValueError:
        return JSONResponse({"error": "project not found"}, status_code=404)
    return JSONResponse({"parser_runs": result})


async def project_retrievals(request: Request) -> JSONResponse:
    try:
        if request.method == "POST":
            payload = await request.json()
            saved = workspace.save_retrieval_to_project(request.app.state.conn, request.path_params["project_id"], str(payload.get("retrieval_id", "")), auth.request_identity(request))
            return JSONResponse(saved, status_code=201)
        result = workspace.list_project_retrievals(request.app.state.conn, request.path_params["project_id"], auth.request_identity(request))
    except ValueError:
        return JSONResponse({"error": "project not found"}, status_code=404)
    return JSONResponse({"retrievals": result})


async def parse_project_retrieval(request: Request) -> JSONResponse:
    try:
        result = document_parser.create_parser_run_from_retrieval(
            request.app.state.conn, request.path_params["project_id"], request.path_params["retrieval_id"], auth.request_identity(request)
        )
        audit.record(request.app.state.conn, request.path_params["project_id"], "paper_parsed", "parser_run", result["parser_run_id"], auth.request_identity(request), {"retrieval_id": request.path_params["retrieval_id"], "block_count": result.get("block_count", 0)})
        request.app.state.conn.commit()
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse(result, status_code=201)


async def project_rag_search(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    if not workspace.has_project_access(request.app.state.conn, project_id, auth.request_identity(request)):
        return JSONResponse({"error": "project not found"}, status_code=404)
    try:
        payload = await request.json()
        sections = payload.get("sections")
        if sections is not None and (not isinstance(sections, list) or not all(isinstance(section, str) for section in sections)):
            return JSONResponse({"error": "sections must be a list of strings"}, status_code=400)
        question = str(payload.get("question", "")).strip()
        top_k = int(payload.get("top_k", 8))
        corpus_fingerprint = document_parser.project_corpus_fingerprint(request.app.state.conn, project_id)
        context_fingerprint = workspace.project_context_fingerprint(request.app.state.conn, project_id) + ":" + graph.accepted_fingerprint(request.app.state.conn, project_id, owner_id=auth.request_identity(request))
        question_key = rag.query_key(question, top_k, sections, "search", corpus_fingerprint, context_fingerprint=context_fingerprint)
        cached = request.app.state.conn.execute("SELECT result_json FROM rag_query_cache WHERE project_id = ? AND question_key = ?", (project_id, question_key)).fetchone()
        if cached:
            result = json.loads(cached[0])
            result["cached"] = True
        else:
            result = rag.search_parser_context(project_id, question, top_k, sections)
            result["graph_context"] = graph.query_context(request.app.state.conn, project_id, question, owner_id=auth.request_identity(request), limit=top_k)
            result["cached"] = False
            if result.get("status") == "active":
                request.app.state.conn.execute("INSERT OR REPLACE INTO rag_query_cache (project_id, question_key, question, result_json, created_at, embedding_model) VALUES (?, ?, ?, ?, datetime('now'), ?)", (project_id, question_key, question, json.dumps(result), result.get("embedding_model", rag.embedding_client.model_name()),))
        audit.record(request.app.state.conn, project_id, "rag_queried", "project", project_id, auth.request_identity(request), {"question": question, "status": result.get("status", ""), "cached": bool(cached), "graph_edge_ids": [item.get("edge_id") for item in result.get("graph_context", [])]})
        workspace.record_query(request.app.state.conn, project_id, question, "search", bool(cached), result.get("status", ""), auth.request_identity(request))
        request.app.state.conn.commit()
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse(result)


async def project_rag_answer(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    owner_id = auth.request_identity(request)
    if not workspace.has_project_access(request.app.state.conn, project_id, owner_id):
        return JSONResponse({"error": "project not found"}, status_code=404)
    try:
        payload = await request.json()
        sections = payload.get("sections")
        if sections is not None and (not isinstance(sections, list) or not all(isinstance(section, str) for section in sections)):
            return JSONResponse({"error": "sections must be a list of strings"}, status_code=400)
        question = str(payload.get("question", "")).strip()
        top_k = int(payload.get("top_k", 8))
        corpus_fingerprint = document_parser.project_corpus_fingerprint(request.app.state.conn, project_id)
        context_fingerprint = workspace.project_context_fingerprint(request.app.state.conn, project_id) + ":" + graph.accepted_fingerprint(request.app.state.conn, project_id, owner_id=owner_id)
        question_key = rag.query_key(question, top_k, sections, "answer", corpus_fingerprint, context_fingerprint=context_fingerprint)
        cached = request.app.state.conn.execute("SELECT result_json FROM rag_query_cache WHERE project_id = ? AND question_key = ?", (project_id, question_key)).fetchone()
        if cached:
            result = json.loads(cached[0])
            try:
                document_parser.validate_rag_result(request.app.state.conn, project_id, result)
            except ValueError:
                request.app.state.conn.execute("DELETE FROM rag_query_cache WHERE project_id = ? AND question_key = ?", (project_id, question_key))
                cached = None
        if not cached:
            result = rag.answer_parser_context(project_id, question, top_k, sections)
            result["graph_context"] = graph.query_context(request.app.state.conn, project_id, question, owner_id=owner_id, limit=top_k)
            document_parser.validate_rag_result(request.app.state.conn, project_id, result)
            result["cached"] = False
            if result.get("status") == "active":
                request.app.state.conn.execute("INSERT OR REPLACE INTO rag_query_cache (project_id, question_key, question, result_json, created_at, embedding_model) VALUES (?, ?, ?, ?, datetime('now'), ?)", (project_id, question_key, question, json.dumps(result), rag.embedding_client.model_name()))
        else:
            result["cached"] = True
        audit.record(request.app.state.conn, project_id, "rag_answered", "project", project_id, owner_id, {"question": question, "grounded": result.get("grounded", False), "cached": bool(cached), "graph_edge_ids": [item.get("edge_id") for item in result.get("graph_context", [])]})
        workspace.record_query(request.app.state.conn, project_id, question, "answer", bool(cached), result.get("status", ""), owner_id)
        request.app.state.conn.commit()
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse(result)


async def project_rag_notes(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    owner_id = auth.request_identity(request)
    try:
        if request.method == "GET":
            return JSONResponse({"notes": workspace.list_project_notes(request.app.state.conn, project_id, owner_id)})
        payload = await request.json()
        citations = payload.get("citations", []) if isinstance(payload.get("citations", []), list) else []
        document_parser.validate_citations(request.app.state.conn, project_id, citations)
        note = workspace.save_project_note(request.app.state.conn, project_id, str(payload.get("question", "")), str(payload.get("note_text", "")), citations, owner_id)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(note, status_code=201)


async def parser_download(request: Request) -> Response:
    try:
        result = document_parser.assert_run_access(request.app.state.conn, request.path_params["parser_run_id"], auth.request_identity(request))
    except ValueError:
        return JSONResponse({"error": "parser run not found"}, status_code=404)
    if result.get("project_id"):
        audit.record(
            request.app.state.conn,
            result["project_id"],
            "parser_downloaded",
            "parser_run",
            request.path_params["parser_run_id"],
            auth.request_identity(request),
            {"filename": result.get("filename", "")},
        )
        request.app.state.conn.commit()
    payload = json.dumps(result, indent=2, sort_keys=True).encode("utf-8")
    filename = result["filename"].rsplit(".", 1)[0] + ".parsed.json"
    return Response(payload, media_type="application/json", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


async def parser_block_review(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        result = document_parser.review_block(
            request.app.state.conn, request.path_params["parser_run_id"], request.path_params["block_id"],
            str(payload.get("review_state", "")), auth.request_identity(request), str(payload.get("note", "")),
        )
        run = document_parser.get_parser_run(request.app.state.conn, request.path_params["parser_run_id"])
        if run and run.get("project_id"):
            audit.record(request.app.state.conn, run["project_id"], "parser_block_reviewed", "parsed_block", request.path_params["block_id"], auth.request_identity(request), {"review_state": result.get("review_state", "")})
            request.app.state.conn.commit()
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400 if "review_state" in str(exc) else 404)
    return JSONResponse(result)


async def parser_flush(request: Request) -> JSONResponse:
    try:
        result = document_parser.flush_parser_run(request.app.state.conn, document_parser.assert_run_access(request.app.state.conn, request.path_params["parser_run_id"], auth.request_identity(request))["parser_run_id"])
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse(result)


async def parser_rebuild_index(request: Request) -> JSONResponse:
    try:
        result = document_parser.rebuild_parser_index(
            request.app.state.conn, request.path_params["parser_run_id"], auth.request_identity(request)
        )
        run = document_parser.get_parser_run(request.app.state.conn, request.path_params["parser_run_id"])
        if run and run.get("project_id"):
            audit.record(request.app.state.conn, run["project_id"], "rag_index_rebuilt", "parser_run", request.path_params["parser_run_id"], auth.request_identity(request), {"indexed_blocks": result.get("indexed_blocks", 0)})
            request.app.state.conn.commit()
    except ValueError:
        return JSONResponse({"error": "parser run not found"}, status_code=404)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse(result)


async def parser_rebuild_index_job(request: Request) -> JSONResponse:
    owner_id = auth.request_identity(request)
    try:
        run = document_parser.assert_run_access(request.app.state.conn, request.path_params["parser_run_id"], owner_id)
        if not run.get("project_id"):
            return JSONResponse({"error": "project required for project-scoped RAG"}, status_code=422)
        payload = await request.json() if request.method == "POST" else {}
        job = jobs.enqueue(
            request.app.state.conn, project_id=run["project_id"], kind="parser_index_rebuild",
            payload={"parser_run_id": request.path_params["parser_run_id"]}, created_by=owner_id,
            idempotency_key=str(payload.get("idempotency_key")) if payload.get("idempotency_key") else None,
        )
    except ValueError:
        return JSONResponse({"error": "parser run not found"}, status_code=404)
    except (TypeError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(job, status_code=202)


async def job_detail(request: Request) -> JSONResponse:
    try:
        result = jobs.get(request.app.state.conn, request.path_params["job_id"], auth.request_identity(request))
    except ValueError:
        result = None
    return JSONResponse(result or {"error": "job not found"}, status_code=200 if result else 404)


async def job_run(request: Request) -> JSONResponse:
    try:
        result = jobs.run_once(request.app.state.conn, request.path_params["job_id"], auth.request_identity(request))
    except ValueError as exc:
        message = str(exc)
        return JSONResponse({"error": message}, status_code=409 if "already" in message else 404)
    return JSONResponse(result)


async def verify(request: Request) -> JSONResponse:
    valid, broken_id = retrieval_log.verify_chain(request.app.state.conn)
    count = request.app.state.conn.execute("SELECT count(*) FROM retrievals").fetchone()[0]
    return JSONResponse(
        {"valid": valid, "record_count": count, "first_broken_retrieval_id": broken_id},
        status_code=200 if valid else 409,
    )


async def project_audit(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    owner_id = auth.request_identity(request)
    if not workspace.has_project_access(request.app.state.conn, project_id, owner_id):
        return JSONResponse({"error": "project not found"}, status_code=404)
    valid, broken_id = audit.verify_project(request.app.state.conn, project_id)
    events = audit.list_events(request.app.state.conn, project_id)
    return JSONResponse({"project_id": project_id, "valid": valid, "event_count": len(events), "first_broken_event_id": broken_id, "events": events}, status_code=200 if valid else 409)


async def project_audit_export(request: Request) -> Response:
    project_id = request.path_params["project_id"]
    owner_id = auth.request_identity(request)
    if not workspace.has_project_access(request.app.state.conn, project_id, owner_id):
        return JSONResponse({"error": "project not found"}, status_code=404)
    valid, broken_id = audit.verify_project(request.app.state.conn, project_id)
    if not valid:
        return JSONResponse(
            {"project_id": project_id, "valid": False, "first_broken_event_id": broken_id},
            status_code=409,
        )
    event = audit.record(
        request.app.state.conn,
        project_id,
        "audit_exported",
        "project",
        project_id,
        owner_id,
        {"format": "json"},
    )
    request.app.state.conn.commit()
    valid, broken_id = audit.verify_project(request.app.state.conn, project_id)
    payload = json.dumps(
        {
            "project_id": project_id,
            "valid": valid,
            "event_count": len(audit.list_events(request.app.state.conn, project_id)),
            "first_broken_event_id": broken_id,
            "export_event_id": event["event_id"],
            "events": audit.list_events(request.app.state.conn, project_id),
        },
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    return Response(
        payload,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{project_id}.audit.json"'},
    )


async def project_graph_proposals(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    owner_id = auth.request_identity(request)
    try:
        if request.method == "GET":
            return JSONResponse({"proposals": graph.list_proposals(
                request.app.state.conn, project_id, owner_id=owner_id,
                status=request.query_params.get("status"),
                limit=int(request.query_params.get("limit", "100")),
            )})
        payload = await request.json()
        provenance = payload.get("provenance")
        if payload.get("proposal_type", "node") == "edge":
            proposal = graph.create_edge_proposal(
                request.app.state.conn, project_id,
                str(payload.get("subject_node_id", "")), str(payload.get("predicate", "")),
                str(payload.get("object_node_id", "")), owner_id=owner_id,
                properties=payload.get("properties") or {}, confidence=payload.get("confidence"),
                extraction_run_id=payload.get("extraction_run_id"), provenance=provenance,
                created_by=owner_id,
            )
        else:
            proposal = graph.create_node_proposal(
                request.app.state.conn, project_id, str(payload.get("node_type", "")),
                str(payload.get("canonical_name", "")), owner_id=owner_id,
                namespace=str(payload.get("namespace", "")), external_id=str(payload.get("external_id", "")),
                properties=payload.get("properties") or {}, extraction_run_id=payload.get("extraction_run_id"),
                provenance=provenance, created_by=owner_id,
            )
        return JSONResponse(proposal, status_code=201)
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def project_graph_review(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        result = graph.review_proposal(
            request.app.state.conn, request.path_params["proposal_id"],
            owner_id=auth.request_identity(request), decision=str(payload.get("decision", "")),
            reviewer_role=str(payload.get("reviewer_role", "owner")),
            reason=str(payload.get("reason", "")), edited_value=payload.get("edited_value") or {},
        )
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        status = 404 if "not found" in str(exc) else 400
        return JSONResponse({"error": str(exc)}, status_code=status)
    return JSONResponse(result)


async def project_graph_expand(request: Request) -> JSONResponse:
    try:
        result = graph.expand_node(
            request.app.state.conn, request.path_params["project_id"], request.path_params["node_id"],
            owner_id=auth.request_identity(request), limit=int(request.query_params.get("limit", "50")),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse(result)


async def project_graph_trace(request: Request) -> JSONResponse:
    try:
        result = graph.trace_entity(
            request.app.state.conn, request.path_params["project_id"], request.path_params["entity_id"],
            owner_id=auth.request_identity(request),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse({"provenance": result})


async def project_graph_stats(request: Request) -> JSONResponse:
    try:
        result = graph.stats(request.app.state.conn, request.path_params["project_id"], owner_id=auth.request_identity(request))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse(result)


async def project_graph_extract(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        result = graph.extract_parser_run(
            request.app.state.conn, str(payload.get("parser_run_id", "")),
            owner_id=auth.request_identity(request),
        )
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(result, status_code=201)


async def project_context(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    owner_id = auth.request_identity(request)
    try:
        if request.method == "GET":
            context = workspace.get_project_context(request.app.state.conn, project_id, owner_id)
            if context is None:
                return JSONResponse({"error": "project not found"}, status_code=404)
            return JSONResponse(context)
        payload = await request.json()
        context = workspace.update_project_context(request.app.state.conn, project_id, owner_id, str(payload.get("objective", "")), str(payload.get("research_question", "")), str(payload.get("scope", "")))
        return JSONResponse(context)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def project_query_history(request: Request) -> JSONResponse:
    try:
        history = workspace.list_query_history(request.app.state.conn, request.path_params["project_id"], auth.request_identity(request))
    except ValueError:
        return JSONResponse({"error": "project not found"}, status_code=404)
    return JSONResponse({"queries": history})


async def projects(request: Request) -> JSONResponse:
    owner_id = auth.bind_agent_id("anonymous", request)
    if request.method == "GET":
        return JSONResponse({"projects": workspace.list_projects(request.app.state.conn, owner_id)})
    try:
        payload = await request.json()
        project = workspace.create_project(
            request.app.state.conn, str(payload.get("name", "")), str(payload.get("description", "")), owner_id,
            str(payload.get("objective", "")), str(payload.get("research_question", "")), str(payload.get("scope", ""))
        )
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(project, status_code=201)


async def project_report(request: Request):
    report = workspace.get_project_report(
        request.app.state.conn, request.path_params["project_id"], auth.bind_agent_id("anonymous", request)
    )
    if report is None:
        return JSONResponse({"error": "project not found"}, status_code=404)
    format_name = request.query_params.get("format", "json").lower()
    if format_name == "markdown":
        markdown = workspace.render_project_markdown(report)
        exports.record_export(request.app.state.conn, request.path_params["project_id"], format_name, markdown.encode(), auth.bind_agent_id("api:export", request))
        return PlainTextResponse(markdown, media_type="text/markdown", headers={"Content-Disposition": "attachment; filename=biolab-report.md"})
    if format_name == "pdf":
        content = exports.markdown_to_pdf(workspace.render_project_markdown(report))
        exports.record_export(request.app.state.conn, request.path_params["project_id"], format_name, content, auth.bind_agent_id("api:export", request))
        return Response(content, media_type="application/pdf", headers={"Content-Disposition": "attachment; filename=biolab-report.pdf"})
    return JSONResponse(report)


async def project_notebook(request: Request) -> Response:
    project_id = request.path_params["project_id"]
    owner_id = auth.bind_agent_id("anonymous", request)
    report = workspace.get_project_report(request.app.state.conn, project_id, owner_id)
    if report is None:
        return JSONResponse({"error": "project not found"}, status_code=404)
    try:
        query_history = workspace.list_query_history(request.app.state.conn, project_id, owner_id)
        notes = workspace.list_project_notes(request.app.state.conn, project_id, owner_id)
    except ValueError:
        return JSONResponse({"error": "project not found"}, status_code=404)
    valid, broken_id = audit.verify_project(request.app.state.conn, project_id)
    audit_status = {"valid": valid, "event_count": len(audit.list_events(request.app.state.conn, project_id)), "first_broken_event_id": broken_id}
    content = exports.project_notebook(report, query_history, notes, audit_status)
    exports.record_export(request.app.state.conn, project_id, "ipynb", content, auth.bind_agent_id("api:notebook", request))
    audit.record(request.app.state.conn, project_id, "notebook_exported", "project", project_id, owner_id, {"format": "ipynb", "size_bytes": len(content)})
    request.app.state.conn.commit()
    return Response(content, media_type="application/x-ipynb+json", headers={"Content-Disposition": f'attachment; filename="{project_id}.ipynb"'})


async def project_zotero_import(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    owner_id = auth.request_identity(request)
    try:
        payload = await request.json()
        result = zotero.import_items(request.app.state.conn, project_id, payload.get("items") if isinstance(payload, dict) else None, owner_id)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    audit.record(request.app.state.conn, project_id, "zotero_imported", "project", project_id, owner_id, {"imported_count": result["imported_count"], "duplicate_count": result["duplicate_count"], "metadata_only": True})
    request.app.state.conn.commit()
    return JSONResponse(result, status_code=201)


async def project_github_export(request: Request) -> Response:
    project_id = request.path_params["project_id"]
    owner_id = auth.bind_agent_id("anonymous", request)
    report = workspace.get_project_report(request.app.state.conn, project_id, owner_id)
    if report is None:
        return JSONResponse({"error": "project not found"}, status_code=404)
    query_history = workspace.list_query_history(request.app.state.conn, project_id, owner_id)
    notes = workspace.list_project_notes(request.app.state.conn, project_id, owner_id)
    valid, broken_id = audit.verify_project(request.app.state.conn, project_id)
    audit_status = {"valid": valid, "event_count": len(audit.list_events(request.app.state.conn, project_id)), "first_broken_event_id": broken_id}
    notebook = exports.project_notebook(report, query_history, notes, audit_status)
    audit_json = json.dumps({"project_id": project_id, **audit_status, "events": audit.list_events(request.app.state.conn, project_id)}, indent=2, sort_keys=True).encode()
    files = {
        "README.md": f"# {report['project'].get('name', 'Biolab project')}\n\nGenerated by Biolab. This bundle contains provenance-preserving research artifacts; it is not a scientific validation certificate.\n".encode(),
        "biolab-project.json": json.dumps(report, indent=2, sort_keys=True).encode(),
        "project-notebook.ipynb": notebook,
        "audit.json": audit_json,
        "query-history.json": json.dumps(query_history, indent=2, sort_keys=True).encode(),
        "notes.json": json.dumps(notes, indent=2, sort_keys=True).encode(),
    }
    content = exports.github_bundle(files)
    exports.record_export(request.app.state.conn, project_id, "github-bundle", content, auth.bind_agent_id("api:github-export", request))
    audit.record(request.app.state.conn, project_id, "github_bundle_exported", "project", project_id, owner_id, {"format": "zip", "size_bytes": len(content), "file_count": len(files)})
    request.app.state.conn.commit()
    return Response(content, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{project_id}-research-bundle.zip"'})


async def project_notes_export(request: Request) -> Response:
    project_id = request.path_params["project_id"]
    owner_id = auth.bind_agent_id("anonymous", request)
    report = workspace.get_project_report(request.app.state.conn, project_id, owner_id)
    if report is None:
        return JSONResponse({"error": "project not found"}, status_code=404)
    target = request.query_params.get("target", "obsidian")
    query_history = workspace.list_query_history(request.app.state.conn, project_id, owner_id)
    notes = workspace.list_project_notes(request.app.state.conn, project_id, owner_id)
    valid, broken_id = audit.verify_project(request.app.state.conn, project_id)
    audit_status = {"valid": valid, "event_count": len(audit.list_events(request.app.state.conn, project_id)), "first_broken_event_id": broken_id}
    try:
        content = exports.cited_notes_markdown(report, notes, query_history, audit_status, target)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    exports.record_export(request.app.state.conn, project_id, f"{target}-notes", content, auth.bind_agent_id("api:notes-export", request))
    audit.record(request.app.state.conn, project_id, "cited_notes_exported", "project", project_id, owner_id, {"target": target, "format": "markdown", "size_bytes": len(content)})
    request.app.state.conn.commit()
    return Response(content, media_type="text/markdown", headers={"Content-Disposition": f'attachment; filename="{project_id}-{target}-notes.md"'})


async def save_reviewed_project_evidence(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        candidate_ids = payload.get("candidate_ids", [])
        if not isinstance(candidate_ids, list) or not all(isinstance(item, str) for item in candidate_ids):
            return JSONResponse({"error": "candidate_ids must be a list of strings"}, status_code=400)
        result = workspace.save_reviewed_candidates(
            request.app.state.conn, request.path_params["project_id"],
            str(payload.get("ranking_run_id", "")), candidate_ids,
            auth.bind_agent_id("anonymous", request), str(payload.get("override_note", "")),
        )
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(result, status_code=201)


async def collections(request: Request) -> JSONResponse:
    if request.method == "GET":
        project_id = request.query_params.get("project_id")
        if project_id and not workspace.has_project_access(request.app.state.conn, project_id, auth.request_identity(request)):
            return JSONResponse({"error": "project not found"}, status_code=404)
        return JSONResponse({
            "collections": workspace.list_collections(
                request.app.state.conn, project_id, auth.request_identity(request)
            )
        })
    try:
        payload = await request.json()
        collection = workspace.create_collection(
            request.app.state.conn,
            str(payload.get("project_id", "")),
            str(payload.get("name", "")),
            str(payload.get("description", "")),
            auth.request_identity(request),
        )
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(collection, status_code=201)


async def collection_detail(request: Request) -> JSONResponse:
    collection = workspace.get_collection(
        request.app.state.conn, request.path_params["collection_id"], auth.request_identity(request)
    )
    if collection is None:
        return JSONResponse({"error": "collection not found"}, status_code=404)
    return JSONResponse(collection)


async def collection_items(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        collection = workspace.get_collection(request.app.state.conn, request.path_params["collection_id"], auth.request_identity(request))
        if collection is None:
            return JSONResponse({"error": "collection not found"}, status_code=404)
        item = workspace.add_collection_item(
            request.app.state.conn,
            request.path_params["collection_id"],
            str(payload.get("retrieval_id", "")),
            str(payload.get("note", "")),
            str(payload.get("relevance", "unreviewed")),
            auth.request_identity(request),
        )
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(item, status_code=201)


async def watches(request: Request) -> JSONResponse:
    if request.method == "GET":
        project_id = request.query_params.get("project_id")
        if project_id and not workspace.has_project_access(request.app.state.conn, project_id, auth.request_identity(request)):
            return JSONResponse({"error": "project not found"}, status_code=404)
        return JSONResponse({
            "watches": workspace.list_watches(
                request.app.state.conn, project_id, auth.request_identity(request)
            )
        })
    try:
        payload = await request.json()
        agent_id = auth.bind_agent_id(str(payload.get("agent_id", "api:watch")), request)
        if not workspace.has_project_access(request.app.state.conn, str(payload.get("project_id", "")), auth.request_identity(request)):
            return JSONResponse({"error": "project not found"}, status_code=404)
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
    owner_id = auth.request_identity(request) if auth.is_authenticated(request) else None
    result = workspace.run_active_watches(request.app.state.conn, owner_id) if owner_id is not None else workspace.run_active_watches(request.app.state.conn)
    return JSONResponse(result, status_code=200 if not result["errors"] else 207)


async def run_watch(request: Request) -> JSONResponse:
    try:
        result = workspace.run_watch(request.app.state.conn, request.path_params["watch_id"], auth.request_identity(request))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse(result)


async def watch_events(request: Request) -> JSONResponse:
    events = workspace.list_watch_events(request.app.state.conn, request.path_params["watch_id"], auth.request_identity(request))
    return JSONResponse({"events": events})


async def claims(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        if not workspace.has_project_access(request.app.state.conn, str(payload.get("project_id", "")), auth.request_identity(request)):
            return JSONResponse({"error": "project not found"}, status_code=404)
        claim = workspace.create_claim(
            request.app.state.conn,
            str(payload.get("project_id", "")),
            str(payload.get("claim", "")), auth.request_identity(request),
        )
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(claim, status_code=201)


async def claim_evidence(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        claim = workspace.trace_claim(request.app.state.conn, request.path_params["claim_id"])
        if claim is None or not workspace.has_project_access(request.app.state.conn, claim["project_id"], auth.request_identity(request)):
            return JSONResponse({"error": "claim not found"}, status_code=404)
        link = workspace.link_claim_evidence(
            request.app.state.conn,
            request.path_params["claim_id"],
            str(payload.get("retrieval_id", "")),
            str(payload.get("stance", "")),
            str(payload.get("assessment", "unreviewed")),
            str(payload.get("note", "")), auth.request_identity(request),
        )
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(link, status_code=201)


async def claim_trace(request: Request) -> JSONResponse:
    result = workspace.trace_claim(request.app.state.conn, request.path_params["claim_id"])
    if result is None:
        return JSONResponse({"error": "claim not found"}, status_code=404)
    if not workspace.has_project_access(request.app.state.conn, result["project_id"], auth.request_identity(request)):
        return JSONResponse({"error": "claim not found"}, status_code=404)
    return JSONResponse(result)


class RestApiKeyAuthMiddleware:
    """Resolve API-key identity and optionally require it for every REST request."""

    def __init__(self, app, require_auth: bool = False):
        self.app = app
        self.require_auth = require_auth
        self.limiter = rate_limit.SlidingWindowRateLimiter.from_env()

    async def __call__(self, scope, receive, send):  # noqa: PLR0911, PLR0915 - auth and idempotency are one ASGI boundary
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        request_id = uuid.uuid4().hex
        scope["biolab_request_id"] = request_id

        async def send_with_request_id(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message.setdefault("headers", []))
                headers.append("x-request-id", request_id)
                decision = scope.get("biolab_rate_limit")
                if decision is not None and decision.limit:
                    headers.append("x-ratelimit-limit", str(decision.limit))
                    headers.append("x-ratelimit-remaining", str(decision.remaining))
                    if not decision.allowed:
                        headers.append("retry-after", str(decision.retry_after))
            await send(message)

        if path in {"/", "/health", "/openapi.json", "/.well-known/agent-card.json"}:
            await self.app(scope, receive, send_with_request_id)
            return

        headers = dict(scope.get("headers") or [])
        auth_header = headers.get(b"authorization", b"").decode("latin-1")
        identity = "anonymous"
        if auth_header.startswith("Bearer "):
            raw_key = auth_header.removeprefix("Bearer ").strip()
            identity = auth.verify_api_key(scope["app"].state.conn, raw_key) or ""
            if not identity:
                response = JSONResponse(
                    {"error": {"code": "invalid_api_key", "message": "invalid or revoked API key"}, "request_id": request_id},
                    status_code=401,
                )
                await response(scope, receive, send_with_request_id)
                return
        elif self.require_auth:
            response = JSONResponse(
                {"error": {"code": "authentication_required", "message": "API key required"}, "request_id": request_id},
                status_code=401,
            )
            await response(scope, receive, send_with_request_id)
            return

        scope["biolab_identity"] = identity
        decision = self.limiter.check(identity)
        scope["biolab_rate_limit"] = decision
        if not decision.allowed:
            response = JSONResponse(
                {"error": {"code": "rate_limit_exceeded", "message": "request quota exceeded", "retry_after_seconds": decision.retry_after}, "request_id": request_id},
                status_code=429,
            )
            await response(scope, receive, send_with_request_id)
            return
        idempotency_key = headers.get(b"idempotency-key", b"").decode("latin-1").strip()
        replay_receive = receive
        request_body = b""
        request_hash = ""
        captured_status = None
        captured_headers = []
        captured_body = bytearray()
        if scope.get("method", "GET").upper() in IDEMPOTENCY_METHODS and idempotency_key:
            if len(idempotency_key) > 200:
                response = JSONResponse(
                    {"error": {"code": "invalid_idempotency_key", "message": "Idempotency-Key is too long"}, "request_id": request_id},
                    status_code=400,
                )
                await response(scope, receive, send_with_request_id)
                return
            request_body, replay_receive = await _read_body(receive)
            request_hash = hashlib.sha256(request_body).hexdigest()
            existing = scope["app"].state.conn.execute(
                "SELECT method, path, request_hash, status_code, response_headers, response_body "
                "FROM idempotency_keys WHERE actor_id = ? AND idempotency_key = ?",
                (identity, idempotency_key),
            ).fetchone()
            if existing:
                if existing[0] != scope["method"] or existing[1] != scope.get("path", "") or existing[2] != request_hash:
                    response = JSONResponse(
                        {"error": {"code": "idempotency_conflict", "message": "Idempotency-Key was reused with a different request"}, "request_id": request_id},
                        status_code=409,
                    )
                    await response(scope, replay_receive, send_with_request_id)
                    return
                response_headers = json.loads(existing[4])
                response = Response(existing[5], status_code=existing[3], headers=dict(response_headers))
                await response(scope, replay_receive, send_with_request_id)
                return

        async def send_capture(message):
            nonlocal captured_status, captured_headers
            if message["type"] == "http.response.start":
                captured_status = message["status"]
                captured_headers = [
                    [name.decode("latin-1"), value.decode("latin-1")]
                    for name, value in message.get("headers", [])
                    if name.lower() != b"x-request-id"
                ]
            elif message["type"] == "http.response.body":
                captured_body.extend(message.get("body", b""))
            await send_with_request_id(message)

        token = auth.current_identity.set(identity)
        try:
            await self.app(scope, replay_receive, send_capture)
            if idempotency_key and request_hash and captured_status is not None:
                scope["app"].state.conn.execute(
                    "INSERT INTO idempotency_keys "
                    "(actor_id, idempotency_key, method, path, request_hash, status_code, response_headers, response_body, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        identity,
                        idempotency_key,
                        scope["method"],
                        scope.get("path", ""),
                        request_hash,
                        captured_status,
                        json.dumps(captured_headers),
                        bytes(captured_body),
                        datetime.now(UTC).isoformat(),
                    ),
                )
                scope["app"].state.conn.commit()
        finally:
            auth.current_identity.reset(token)


def api_routes() -> list[Route]:
    """Return dashboard and REST routes for standalone or MCP-hosted use."""
    return [
        *a2a.a2a_routes(),
        Route("/", dashboard),
        Route("/projects/{project_id}", project_page),
        Route("/health", health),
        Route("/v1/storage", storage),
        Route("/openapi.json", openapi),
        Route("/metrics", metrics),
        Route("/v1/evidence/search", search, methods=["POST"]),
        Route("/v1/rankings", rankings, methods=["POST"]),
        Route("/v1/rankings/{ranking_run_id}", ranking_detail),
        Route("/v1/rankings/{ranking_run_id}/candidates/{ranking_candidate_id}/review", ranking_review, methods=["POST"]),
        Route("/v1/inferences", inferences, methods=["POST"]),
        Route("/v1/inferences/{inference_run_id}", inference_detail),
        Route("/v1/inferences/{inference_run_id}/flush", inference_flush, methods=["POST"]),
        Route("/v1/parser/parse", parser_parse, methods=["POST"]),
        Route("/v1/projects/{project_id}/parser-runs", project_parser_runs),
        Route("/v1/projects/{project_id}/retrievals", project_retrievals, methods=["GET", "POST"]),
        Route("/v1/projects/{project_id}/retrievals/{retrieval_id}/parse", parse_project_retrieval, methods=["POST"]),
        Route("/v1/projects/{project_id}/rag/search", project_rag_search, methods=["POST"]),
        Route("/v1/projects/{project_id}/rag/answer", project_rag_answer, methods=["POST"]),
        Route("/v1/projects/{project_id}/rag/notes", project_rag_notes, methods=["GET", "POST"]),
        Route("/v1/parser/runs/{parser_run_id}", parser_detail),
        Route("/v1/parser/runs/{parser_run_id}/blocks/{block_id}/review", parser_block_review, methods=["POST"]),
        Route("/v1/parser/runs/{parser_run_id}/flush", parser_flush, methods=["POST"]),
        Route("/v1/parser/runs/{parser_run_id}/rebuild-index", parser_rebuild_index, methods=["POST"]),
        Route("/v1/parser/runs/{parser_run_id}/rebuild-index-job", parser_rebuild_index_job, methods=["POST"]),
        Route("/v1/jobs/{job_id}", job_detail),
        Route("/v1/jobs/{job_id}/run", job_run, methods=["POST"]),
        Route("/v1/parser/runs/{parser_run_id}/download", parser_download),
        Route("/v1/retrievals/{retrieval_id}", get_retrieval),
        Route("/v1/verify", verify),
        Route("/v1/projects", projects, methods=["GET", "POST"]),
        Route("/v1/projects/{project_id}/report", project_report),
        Route("/v1/projects/{project_id}/notebook", project_notebook),
        Route("/v1/projects/{project_id}/imports/zotero", project_zotero_import, methods=["POST"]),
        Route("/v1/projects/{project_id}/github-export", project_github_export),
        Route("/v1/projects/{project_id}/notes-export", project_notes_export),
        Route("/v1/projects/{project_id}/audit", project_audit),
        Route("/v1/projects/{project_id}/audit/export", project_audit_export, methods=["POST"]),
        Route("/v1/projects/{project_id}/graph/proposals", project_graph_proposals, methods=["GET", "POST"]),
        Route("/v1/projects/{project_id}/graph/proposals/{proposal_id}/review", project_graph_review, methods=["POST"]),
        Route("/v1/projects/{project_id}/graph/nodes/{node_id}/expand", project_graph_expand),
        Route("/v1/projects/{project_id}/graph/entities/{entity_id}/trace", project_graph_trace),
        Route("/v1/projects/{project_id}/graph/stats", project_graph_stats),
        Route("/v1/projects/{project_id}/graph/extract", project_graph_extract, methods=["POST"]),
        Route("/v1/projects/{project_id}/context", project_context, methods=["GET", "PATCH"]),
        Route("/v1/projects/{project_id}/query-history", project_query_history),
        Route("/v1/projects/{project_id}/save-reviewed", save_reviewed_project_evidence, methods=["POST"]),
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


def create_app(db_path: str | Path = "biolab.db", require_auth: bool = True) -> Starlette:
    """Create an isolated dashboard/API application for one audit database."""

    @asynccontextmanager
    async def lifespan(app: Starlette):
        app.state.conn = db.connect(str(db_path))
        app.state.a2a_tasks = a2a.A2ATaskStore()
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
