# Complex Problem Log

This file records non-trivial engineering problems solved while stitching the
Biolab project together. Each entry separates observed facts from decisions so
future work can resume without reconstructing the investigation from scratch.

## 2026-11-13 — Onboard and stitch the multi-surface repository

### Problem

The repository had several delivery surfaces and two implementations—Python and
Go—but the project guidance was distributed across `README.md`,
`PROJECT_STATUS.md`, source modules, CI, and recent commits. A new contributor
could run the project, but would not have one reliable map of request flow,
persistence boundaries, test commands, or unfinished work.

### Investigation

The repository was inspected at four levels:

1. Package and tooling configuration in `pyproject.toml`, requirements, Docker,
   CI, and Go module files.
2. Python entry points and service boundaries in `server.py`, `cli.py`,
   `evidence.py`, `retrieval_log.py`, `workspace.py`, `executions.py`, `db.py`,
   and `web.py`.
3. Test and implementation coverage in `tests/` and `go-biolab/`.
4. Git history to identify the existing feature and fix commit style.

### Solution

The project now has a linked documentation spine:

- `ONBOARDING.md` maps architecture, request lifecycle, directories, conventions,
  commands, and extension points.
- `CLAUDE.md` gives contributors and coding agents the short operational rules.
- `TODO.md` turns the next-version candidates and verification gaps into a
  prioritized backlog.
- This log preserves the reasoning and evidence for the stitching work.
- `README.md` links all four documents from the project’s main entry point.

### Verification

- Python deterministic suite: `101 passed, 28 deselected`.
- `ruff check .`: passed.
- `mypy biolab/`: passed.
- `git diff --check`: passed.
- Ruff format check reports 26 existing files that would be reformatted; this
  remains a separate mechanical cleanup item in `TODO.md`.
- Go tests initially hit a restricted global build cache; the follow-up command
  uses an explicit writable cache and is recorded below.

### Boundary preserved

The documentation does not promote integration plumbing into scientific or
clinical validation. It keeps the repository’s existing boundary: connectors
retrieve and normalize external evidence, shared services preserve provenance,
and scientist review is still required for claims and conclusions.

## 2026-11-13 — Add A2A interoperability without duplicating retrieval logic

### Problem

Biolab already supported MCP, REST, and CLI clients, but another agent using A2A
had no discovery document or task-based JSON-RPC interface. Adding a second
retrieval implementation would risk diverging provenance and error behavior.

### Solution

`biolab/a2a.py` adds an A2A 1.0 Agent Card, JSON-RPC `message/send` and
`tasks/get`, and a synchronous in-process task adapter. The adapter delegates
to `evidence.search_evidence`, so A2A returns the same retrieval IDs, source
errors, and scientific boundary as MCP and REST. `web.py` and `server.py` mount
the routes, while the Agent Card remains discoverable under authentication.

### Verification

- A2A acceptance tests cover discovery, search, task retrieval, invalid tasks,
  and unknown methods.
- Full offline suite: `103 passed, 28 deselected`.
- Ruff and MyPy pass.
- The built Space container responded successfully to `/health`,
  `/.well-known/agent-card.json`, and `/a2a`.

### Deployment boundary

The local image build succeeded, but the public Hugging Face Space was not
updated. Hugging Face MCP inspection confirmed the target Space and a
read-only credential; a live health check returned HTTP 503 because the Space
is paused. The public endpoint must not be described as A2A-enabled until the
Space is restarted, the verified build is uploaded, and its live Agent Card is
checked.

The Hugging Face path was abandoned for this release. A Vercel Python ASGI
deployment was created instead, with `/tmp` paths for serverless execution and
an `/api` prefix adapter. The final production health check, Agent Card check,
and A2A `message/send` smoke test all passed at `biolab-a2a.vercel.app`.

## 2026-11-14 — Turn the deployed dashboard into a reviewer-facing evidence cockpit

### Problem

The deployed service was healthy, but the dashboard still presented a generic
developer form. It also called `/v1/...` directly, which is correct locally but
skips the `/api` prefix used by the Vercel adapter in production.

### Investigation

The referenced `adjudicate` project makes its core reviewer state visible in a
single focused workspace: the user sees the task, the evidence, and the review
boundary together. Biolab already had the underlying search, project, and audit
routes, so the highest-value change was presentation and routing rather than a
new frontend framework.

### Solution

The embedded dashboard now uses a responsive evidence-cockpit layout with a
clear search step, audit-ledger status, project grouping, preserved-result
cards, explicit evidence-boundary copy, loading states, and accessible labels.
The browser API helper derives `/api` from the current path, so the same page
works at `/` locally and `/api` on Vercel without duplicating route logic.

### Verification

- Offline Python suite: `103 passed, 28 deselected`.
- The dashboard test still verifies the page title and route response.
- The existing Vercel health, Agent Card, and A2A smoke checks remain the
  release gate for redeployment.

### Boundary preserved

The visual redesign does not imply that retrieval is biological validation. The
UI explicitly states that preserved evidence must still be reviewed in source
context before a scientific claim is made.

## 2026-11-14 — Consolidate MCP, REST, dashboard, and A2A on one public platform

### Problem

The code could serve all surfaces from one ASGI process, but public instructions
still pointed MCP users at the paused Hugging Face Space. In addition, FastMCP's
localhost DNS-rebinding defaults rejected Vercel's forwarded public Host header
with HTTP 421 on `/api/mcp`.

### Solution

Vercel is now the canonical hosted platform. The adapter sets a serverless host
configuration so FastMCP accepts the platform-forwarded Host header, while the
existing API-key middleware remains in the application. README and the
end-to-end guide now use the single connection URL:
`https://biolab-a2a.vercel.app/api/mcp`.

### Verification

- Production deployment `dpl_9NMU6BPMMp4nEixqGN4cp4ddpxqc` reached READY.
- MCP `initialize` returned HTTP 200 and a session ID; `tools/list` returned the
  unified evidence/project/watch/claim tool catalog.
- `/api/health`, `/api/openapi.json`, and `/api/metrics` returned HTTP 200.
- The dashboard and A2A Agent Card remained available on the same origin.
- Offline suite: `103 passed, 28 deselected`; Ruff, MyPy, and diff checks pass.

### Boundary preserved

One platform means one connection surface, not one source of scientific truth.
Source responses, errors, retrieval IDs, and the requirement for human review
remain explicit across MCP, REST, dashboard, and A2A.

## 2026-11-14 — Reconcile the published package with the current workspace

### Problem

PyPI `0.4.0` was real, but it predated the current A2A/Vercel/frontend work.
The release metadata also failed a current setuptools build because the license
field used the old string form, and CI ran network-dependent live tests in its
normal Python job.

### Solution

The next package version is `0.4.1`. The license now uses a PEP 621-compatible
file declaration, the web and Agent Card versions are aligned, Vercel installs
the optional Turso extra, CI excludes live tests from the deterministic gate,
and the PyPI workflow refuses a tag that does not match `pyproject.toml`.

### Verification

- `python -m build --no-isolation` produced the `0.4.1` sdist and wheel.
- Wheel metadata includes the optional `libsql` Turso dependency.
- Offline suite: `103 passed, 28 deselected`.
- Ruff, MyPy, Go tests, release-version check, and diff checks pass.
- Vercel production reports API and Agent Card version `0.4.1`; MCP
  `initialize` returns HTTP 200.

### Boundary preserved

The package is build-ready but has not been uploaded to PyPI in this run.
Publishing remains an explicit release action. Turso is runtime-capable but no
database or credentials were created; the public demo remains ephemeral until
an operator supplies them.

## 2026-11-14 — Keep the audit status synchronized after a dashboard search

### Problem

The live dashboard correctly rendered preserved search results, but its audit
ledger was only verified on initial page load. After a successful search the
page could show `11 records preserved` beside `Chain valid · 0 preserved
retrievals`, which is confusing even though the underlying chain is valid.

### Solution

The search success path now re-runs the existing `/v1/verify` check immediately
after the result is preserved and before the result cards render. The UI keeps
its evidence boundary: cross-source retrieval is displayed as preserved source
material, not as a relevance ranking or scientific conclusion.

### Verification

- Offline suite: `103 passed, 28 deselected`.
- Ruff, MyPy, and diff checks pass.
- The change reuses the existing verification endpoint and does not create a
  second audit implementation.

### Boundary preserved

The dashboard still does not claim that every returned record is relevant to the
question. Relevance and scientific interpretation require source review.

## 2026-11-14 — Make audit verification and project navigation observable

### Problem

The dashboard could successfully call the verification and project APIs while
appearing inert: verification had no visible success state, and project rows
were rendered as non-interactive list items. This made a valid hash chain and a
created project look broken to a reviewer.

### Investigation

The deployed page was exercised in an isolated Chrome DevTools session. The
verification request returned HTTP 200, project creation returned HTTP 201,
and project listing refreshed with HTTP 200, but the UI did not expose those
state transitions. A project report endpoint also returned HTTP 200.

### Solution

Project rows are now keyboard-accessible buttons with project IDs, and clicking
one loads a project detail panel from `/v1/projects/{id}/report`, including
collection, claim, watch counts and JSON/Markdown report links. Verification
now changes its button label to `Verified just now` or `Chain needs review`.
The search success path re-verifies the chain before rendering preserved
results.

### Verification

- Live dashboard: project creation POST 201 and list refresh GET 200.
- Live dashboard: project click loads report GET 200 and report links render.
- Live dashboard: search POST 200, preserved results render, and the ledger
  shows `Chain valid · 4 preserved retrievals`.
- Live dashboard: verify click returns GET 200 and visibly shows `Verified just
  now`.
- Browser console: no errors or warnings.
- Offline suite: `103 passed, 28 deselected`; Ruff, MyPy, and tamper tests pass.

### Boundary preserved

The public Vercel demo still defaults to ephemeral `/tmp` storage when Turso
credentials are absent. The UI flow is fixed, but durable cross-instance
projects and evidence require `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` in
the deployment environment.

## 2026-11-14 — Add deterministic paper-ranking foundation

### Problem

The workspace preserved scientific retrievals but had no transparent way to
triage several returned papers or explain why one appeared above another.

### Investigation

Existing source adapters already normalize titles, abstracts, publication types,
journal dates, and source metadata into retrieval snapshots. The existing
retrieval IDs and hash chain provide stable provenance links for derived ranking
records.

### Solution

Added `ranking_runs` and `ranking_candidates` tables plus a deterministic v1
scorer. It combines query fit, entity/subject fit, requested study-design fit,
recency, and metadata completeness. Each candidate stores its component scores,
matched terms, missing facets, and reason codes. Added REST endpoints and MCP
tools so all delivery surfaces can use the same ranking service.

### Verification

- Full offline suite: `108 passed, 28 deselected`.
- Ruff and MyPy pass.
- Ranking tests cover exact/title matches, missing metadata, deterministic
  ordering, top-five persistence, and unknown retrieval rejection.
- REST test covers ranking creation and retrieval of stored explanations.

### Boundary preserved

The score means query-fit triage only. It does not establish paper quality,
scientific truth, clinical validity, or replace scientist review.

## 2026-11-15 — Connect ranking to the scientist review queue

### Problem

The ranking service returned explanations, but a scientist could not inspect
the top candidates or record a review decision from the hosted dashboard.

### Investigation

The live dashboard was exercised after a preserved UniProt search. The ranking
run and score components were returned correctly. A response-contract gap was
found because the create response did not include the persisted candidate rank
or candidate ID needed by the review action.

### Solution

The dashboard now automatically ranks the preserved search results, renders a
top-five review queue with score components, reason codes, matched terms, and
missing facets, and supports accepted, rejected, and conflicting decisions.
The ranking response now includes `rank` and `ranking_candidate_id`. REST and
MCP expose the same review operation.

### Verification

- Full offline suite: `111 passed, 28 deselected`.
- Ruff and MyPy pass.
- Live search returned a preserved retrieval.
- Live ranking returned `rank: 1` and a reviewable candidate ID.
- Live review recorded `review_state: rejected` with a reviewer note.

### Boundary preserved

The review queue records a scientist or agent decision; it does not convert a
ranking into a scientific conclusion or clinical recommendation.

## Entry template

Use this shape for future complex problems:

```markdown
## YYYY-MM-DD — Short problem title

### Problem
What was unclear, broken, or risky?

### Investigation
What evidence was inspected or reproduced?

### Solution
What changed, and why?

### Verification
What commands or independent checks passed or failed?

### Boundary preserved
What must not be inferred from the result?
```
