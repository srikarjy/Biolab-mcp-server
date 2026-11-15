"""Local scientist dashboard and REST API.

This surface deliberately defaults to loopback-only operation. It reuses the
same evidence and retrieval services as the CLI and MCP server.
"""

import base64
import json
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from biolab import (
    a2a,
    auth,
    db,
    document_parser,
    evidence,
    exports,
    inference,
    ranking,
    retrieval_log,
    workspace,
)

OPENAPI_SCHEMA = {
    "openapi": "3.1.0",
    "info": {
        "title": "Biolab Evidence API",
        "version": "0.4.1",
        "description": "Auditable scientific retrieval, projects, monitoring, and claim provenance.",
    },
    "paths": {
        "/health": {"get": {"summary": "Health check"}},
        "/metrics": {"get": {"summary": "Local service counters and chain status"}},
        "/v1/evidence/search": {"post": {"summary": "Search and preserve evidence"}},
        "/v1/rankings": {"post": {"summary": "Rank preserved retrievals with explanations"}},
        "/v1/rankings/{ranking_run_id}": {"get": {"summary": "Get a ranking run"}},
        "/v1/rankings/{ranking_run_id}/candidates/{ranking_candidate_id}/review": {"post": {"summary": "Review a ranked candidate"}},
        "/v1/inferences": {"post": {"summary": "Create citation-safe grounded insights"}},
        "/v1/inferences/{inference_run_id}": {"get": {"summary": "Get grounded insights and citations"}},
        "/v1/inferences/{inference_run_id}/flush": {"post": {"summary": "Flush disposable parser chunks"}},
        "/v1/parser/parse": {"post": {"summary": "Parse a PDF through the configured Hugging Face parser"}},
        "/v1/projects/{project_id}/parser-runs": {"get": {"summary": "List parsed documents in a project folder"}},
        "/v1/parser/runs/{parser_run_id}": {"get": {"summary": "Inspect parsed document blocks"}},
        "/v1/parser/runs/{parser_run_id}/flush": {"post": {"summary": "Flush parsed document blocks"}},
        "/v1/parser/runs/{parser_run_id}/download": {"get": {"summary": "Download parsed document JSON"}},
        "/v1/retrievals/{retrieval_id}": {"get": {"summary": "Fetch a preserved retrieval"}},
        "/v1/verify": {"get": {"summary": "Verify the audit hash chain"}},
        "/v1/projects": {
            "get": {"summary": "List projects"},
            "post": {"summary": "Create a project"},
        },
        "/v1/projects/{project_id}/report": {"get": {"summary": "Export a project report as JSON, Markdown, or PDF"}},
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
          <summary><strong>Remote API key</strong> <span class="muted">(optional)</span></summary>
          <p class="kicker">Stored only in this browser tab and sent as a Bearer header.</p>
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
        <button id="generate-insights" class="secondary" type="button" style="margin-top:14px">Generate grounded insights</button>
        <button id="save-reviewed" class="secondary" type="button" style="margin:14px 0 0 8px" disabled>Save accepted papers to selected project</button>
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
      if (!latestProjectId || !latestRanking) return;
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
        audit.className = `status${result.valid ? '' : ' bad'}`;
        audit.textContent = result.valid
          ? `Chain valid · ${result.record_count} preserved retrievals`
          : `Chain broken at ${result.first_broken_retrieval_id}`;
        button.textContent = result.valid ? 'Verified just now' : 'Chain needs review';
      } catch (error) { audit.className = 'status bad'; audit.textContent = 'Audit status unavailable'; }
    }
    async function loadProjects() {
      const response = await apiFetch('/v1/projects');
      const result = await response.json();
      document.querySelector('#projects').innerHTML = result.projects.length
        ? result.projects.map(p => `<li><button class="project-item" type="button" data-project-id="${escapeHtml(p.project_id)}"><strong>${escapeHtml(p.name)}</strong><code>${escapeHtml(maskId(p.project_id))}</code></button></li>`).join('')
        : '<li class="muted">No projects yet.</li>';
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
          <a href="${apiBase}/v1/projects/${encodeURIComponent(projectId)}/report?format=markdown" target="_blank" rel="noreferrer">Open Markdown report ↗</a>`;
      } catch (error) { detail.textContent = String(error); }
    }
    document.querySelector('#verify').addEventListener('click', verify);
    document.querySelector('#projects').addEventListener('click', (event) => {
      const button = event.target.closest('[data-project-id]');
      if (button) loadProject(button.dataset.projectId);
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
        if (!response.ok) throw new Error(`Project creation returned ${response.status}`);
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
          return `<article class="evidence-card"><div class="card-meta"><span class="badge">${escapeHtml(record.source)}</span><span class="record-id">${escapeHtml(record.external_id)}</span></div>
            <h3>${escapeHtml(title)}</h3><p>Preserved retrieval <code>${escapeHtml(record.retrieval_id)}</code></p>
            ${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">Open source ↗</a>` : ''}</article>`;
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
            project_id=str(payload["project_id"]) if payload.get("project_id") else None,
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
    return JSONResponse(result)


async def ranking_review(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
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
        if auth.is_authenticated(request):
            identity = auth.request_identity(request)
            if any((record := retrieval_log.get_retrieval(request.app.state.conn, item)) is None or record.agent_id != identity for item in retrieval_ids):
                return JSONResponse({"error": "retrieval access denied"}, status_code=404)
        result = inference.create_inference(
            request.app.state.conn,
            str(payload.get("question", "")),
            retrieval_ids,
            created_by=auth.bind_agent_id(str(payload.get("created_by", "api:inference")), request),
            project_id=str(payload["project_id"]) if payload.get("project_id") else None,
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
    return JSONResponse(result)


async def inference_flush(request: Request) -> JSONResponse:
    try:
        result = inference.flush_parser_chunks(
            request.app.state.conn, request.path_params["inference_run_id"]
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse(result)


async def parser_parse(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        encoded = payload.get("content_base64", "")
        if not isinstance(encoded, str) or not encoded:
            return JSONResponse({"error": "content_base64 is required"}, status_code=400)
        result = document_parser.create_parser_run(
            request.app.state.conn, base64.b64decode(encoded, validate=True),
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


async def parser_download(request: Request) -> Response:
    try:
        result = document_parser.assert_run_access(request.app.state.conn, request.path_params["parser_run_id"], auth.request_identity(request))
    except ValueError:
        return JSONResponse({"error": "parser run not found"}, status_code=404)
    payload = json.dumps(result, indent=2, sort_keys=True).encode("utf-8")
    filename = result["filename"].rsplit(".", 1)[0] + ".parsed.json"
    return Response(payload, media_type="application/json", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


async def parser_flush(request: Request) -> JSONResponse:
    try:
        result = document_parser.flush_parser_run(request.app.state.conn, document_parser.assert_run_access(request.app.state.conn, request.path_params["parser_run_id"], auth.request_identity(request))["parser_run_id"])
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse(result)


async def verify(request: Request) -> JSONResponse:
    valid, broken_id = retrieval_log.verify_chain(request.app.state.conn)
    count = request.app.state.conn.execute("SELECT count(*) FROM retrievals").fetchone()[0]
    return JSONResponse(
        {"valid": valid, "record_count": count, "first_broken_retrieval_id": broken_id},
        status_code=200 if valid else 409,
    )


async def projects(request: Request) -> JSONResponse:
    owner_id = auth.bind_agent_id("anonymous", request)
    if request.method == "GET":
        return JSONResponse({"projects": workspace.list_projects(request.app.state.conn, owner_id)})
    try:
        payload = await request.json()
        project = workspace.create_project(
            request.app.state.conn, str(payload.get("name", "")), str(payload.get("description", "")), owner_id
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
        if project_id and auth.is_authenticated(request) and not workspace.has_project_access(request.app.state.conn, project_id, auth.request_identity(request)):
            return JSONResponse({"error": "project not found"}, status_code=404)
        return JSONResponse({
            "collections": workspace.list_collections(
                request.app.state.conn, project_id
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
        request.app.state.conn, request.path_params["collection_id"]
    )
    if collection is None:
        return JSONResponse({"error": "collection not found"}, status_code=404)
    if auth.is_authenticated(request) and not workspace.has_project_access(request.app.state.conn, collection["project_id"], auth.request_identity(request)):
        return JSONResponse({"error": "collection not found"}, status_code=404)
    return JSONResponse(collection)


async def collection_items(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        collection = workspace.get_collection(request.app.state.conn, request.path_params["collection_id"])
        if collection is None or (auth.is_authenticated(request) and not workspace.has_project_access(request.app.state.conn, collection["project_id"], auth.request_identity(request))):
            return JSONResponse({"error": "collection not found"}, status_code=404)
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
        project_id = request.query_params.get("project_id")
        if project_id and auth.is_authenticated(request) and not workspace.has_project_access(request.app.state.conn, project_id, auth.request_identity(request)):
            return JSONResponse({"error": "project not found"}, status_code=404)
        return JSONResponse({
            "watches": workspace.list_watches(
                request.app.state.conn, project_id
            )
        })
    try:
        payload = await request.json()
        agent_id = auth.bind_agent_id(str(payload.get("agent_id", "api:watch")), request)
        if auth.is_authenticated(request) and not workspace.has_project_access(request.app.state.conn, str(payload.get("project_id", "")), auth.request_identity(request)):
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
        if auth.is_authenticated(request) and not workspace.has_project_access(request.app.state.conn, str(payload.get("project_id", "")), auth.request_identity(request)):
            return JSONResponse({"error": "project not found"}, status_code=404)
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
        claim = workspace.trace_claim(request.app.state.conn, request.path_params["claim_id"])
        if claim is None or (auth.is_authenticated(request) and not workspace.has_project_access(request.app.state.conn, claim["project_id"], auth.request_identity(request))):
            return JSONResponse({"error": "claim not found"}, status_code=404)
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
    if auth.is_authenticated(request) and not workspace.has_project_access(request.app.state.conn, result["project_id"], auth.request_identity(request)):
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
        if path in {"/", "/health", "/openapi.json", "/.well-known/agent-card.json"}:
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

        scope["biolab_identity"] = identity
        token = auth.current_identity.set(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            auth.current_identity.reset(token)


def api_routes() -> list[Route]:
    """Return dashboard and REST routes for standalone or MCP-hosted use."""
    return [
        *a2a.a2a_routes(),
        Route("/", dashboard),
        Route("/health", health),
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
        Route("/v1/parser/runs/{parser_run_id}", parser_detail),
        Route("/v1/parser/runs/{parser_run_id}/flush", parser_flush, methods=["POST"]),
        Route("/v1/parser/runs/{parser_run_id}/download", parser_download),
        Route("/v1/retrievals/{retrieval_id}", get_retrieval),
        Route("/v1/verify", verify),
        Route("/v1/projects", projects, methods=["GET", "POST"]),
        Route("/v1/projects/{project_id}/report", project_report),
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


def create_app(db_path: str | Path = "biolab.db", require_auth: bool = False) -> Starlette:
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
