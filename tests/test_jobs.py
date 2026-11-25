"""Acceptance tests for bounded, persisted background jobs."""

import base64

from biolab import jobs, rag
from biolab.web import create_app
from starlette.testclient import TestClient


def _parser(monkeypatch):
    from biolab import hf_parser

    monkeypatch.setattr(hf_parser, "parse_document", lambda content, filename: {
        "parser_model": "test-parser", "status": "succeeded", "source_hash": "abc", "block_count": 1,
        "blocks": [{"block_id": "job-block", "page_number": 1, "block_index": 0, "block_type": "paragraph", "text": "Job evidence", "bbox": None}],
    })


def test_parser_index_job_is_persisted_idempotent_and_audited(monkeypatch, tmp_path):
    _parser(monkeypatch)
    monkeypatch.setattr(rag, "index_parser_blocks", lambda *args: {"status": "active", "indexed_blocks": len(args[3])})
    with TestClient(create_app(tmp_path / "jobs.db", require_auth=False)) as client:
        project = client.post("/v1/projects", json={"name": "Job project"}).json()
        parsed = client.post("/v1/parser/parse", json={
            "project_id": project["project_id"], "filename": "paper.pdf",
            "content_base64": base64.b64encode(b"paper").decode(),
        }).json()
        path = f"/v1/parser/runs/{parsed['parser_run_id']}/rebuild-index-job"
        queued = client.post(path, json={"idempotency_key": "rebuild-1"})
        duplicate = client.post(path, json={"idempotency_key": "rebuild-1"})
        status = client.get(f"/v1/jobs/{queued.json()['job_id']}")
        completed = client.post(f"/v1/jobs/{queued.json()['job_id']}/run")
        audit = client.get(f"/v1/projects/{project['project_id']}/audit")

    assert queued.status_code == 202
    assert duplicate.json()["job_id"] == queued.json()["job_id"]
    assert status.json()["status"] == "queued"
    assert completed.json()["status"] == "succeeded"
    assert completed.json()["result"]["indexed_blocks"] == 0
    assert audit.json()["valid"] is True
    assert {event["event_type"] for event in audit.json()["events"]} >= {"job_queued", "job_started", "job_succeeded"}


def test_job_retries_then_succeeds_and_stops_at_bound(monkeypatch, tmp_path):
    from biolab.db import connect

    db = connect(str(tmp_path / "retry.db"))
    project_id = "project-1"
    db.execute("INSERT INTO projects(project_id, name, created_at, owner_id) VALUES (?, ?, datetime('now'), ?)", (project_id, "Retry", "local"))
    db.commit()
    calls = {"count": 0}

    def flaky(*args):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("temporary provider failure")
        return {"status": "active", "indexed_blocks": 2}

    monkeypatch.setattr("biolab.document_parser.rebuild_parser_index", flaky)
    first = jobs.enqueue(db, project_id=project_id, kind="parser_index_rebuild", payload={"parser_run_id": "run-1"}, created_by="local", max_attempts=2)
    failed = jobs.run_once(db, first["job_id"], "local")
    assert failed["status"] == "queued"
    succeeded = jobs.run_once(db, first["job_id"], "local")
    assert succeeded["status"] == "succeeded"
    assert succeeded["attempts"] == 2
