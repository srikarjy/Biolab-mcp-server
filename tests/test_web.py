"""Offline acceptance tests for the local dashboard and REST API."""

import base64

from biolab import auth, evidence, retrieval_log
from biolab.web import create_app
from starlette.testclient import TestClient


def test_dashboard_and_health(tmp_path):
    with TestClient(create_app(tmp_path / "web.db")) as client:
        page = client.get("/")
        health = client.get("/health")
        schema = client.get("/openapi.json")
        metrics = client.get("/metrics")

    assert page.status_code == 200
    assert "Biolab Evidence Workspace" in page.text
    assert health.json() == {"status": "ok", "service": "biolab"}
    assert schema.json()["info"]["version"] == "0.4.1"
    assert metrics.json()["audit_chain_valid"] is True
    assert metrics.json()["counts"]["retrievals"] == 0


def test_huggingface_parser_api_persists_and_flushes(monkeypatch, tmp_path):
    from biolab import hf_parser
    monkeypatch.setattr(hf_parser, "parse_document", lambda content, filename: {
        "parser_model": "nvidia/Nemotron-3.5-Parse", "status": "succeeded", "source_hash": "abc", "block_count": 1,
        "blocks": [{"block_id": "b1", "page_number": 1, "block_index": 0, "block_type": "paragraph", "text": "Parsed", "bbox": None}],
    })
    with TestClient(create_app(tmp_path / "parser-web.db")) as client:
        created = client.post("/v1/parser/parse", json={"filename": "paper.pdf", "content_base64": base64.b64encode(b"pdf").decode()})
        run_id = created.json()["parser_run_id"]
        detail = client.get(f"/v1/parser/runs/{run_id}")
        flushed = client.post(f"/v1/parser/runs/{run_id}/flush")
    assert created.status_code == 201
    assert detail.json()["blocks"][0]["text"] == "Parsed"
    assert flushed.json()["flushed_parsed_blocks"] == 1
def test_search_api_uses_shared_service(monkeypatch, tmp_path):
    captured = {}

    def fake_search(conn, query, agent_id, sources, max_results):
        captured.update(
            query=query, agent_id=agent_id, sources=sources, max_results=max_results
        )
        return {"query_echo": query, "sources": {}, "errors": {}, "retrieval_count": 0}

    monkeypatch.setattr(evidence, "search_evidence", fake_search)
    with TestClient(create_app(tmp_path / "web.db")) as client:
        response = client.post(
            "/v1/evidence/search",
            json={"query": "KRAS", "sources": ["pubmed"], "max_results": 2},
        )

    assert response.status_code == 200
    assert response.json()["query_echo"] == "KRAS"
    assert captured == {
        "query": "KRAS",
        "agent_id": "api:user",
        "sources": ["pubmed"],
        "max_results": 2,
    }


def test_retrieval_and_verification_endpoints(tmp_path):
    app = create_app(tmp_path / "web.db")
    with TestClient(app) as client:
        record = retrieval_log.write_retrieval(
            app.state.conn, "q", "1", "test", "pubmed", {}, "raw", {"title": "Test"}
        )
        fetched = client.get(f"/v1/retrievals/{record.retrieval_id}")
        verified = client.get("/v1/verify")

    assert fetched.status_code == 200
    assert fetched.json()["raw_response"] == "raw"
    assert fetched.json()["prev_hash"] == ""
    assert verified.json() == {
        "valid": True,
        "record_count": 1,
        "first_broken_retrieval_id": None,
    }


def test_ranking_api_returns_explanations_and_persists_run(tmp_path):
    app = create_app(tmp_path / "ranking.db")
    with TestClient(app) as client:
        records = [
            retrieval_log.write_retrieval(
                app.state.conn, "KRAS trial", "1", "test", "pubmed", {}, "raw-1",
                {"title": "KRAS randomized trial", "abstract": "KRAS study", "publication_types": ["Randomized Controlled Trial"]},
            ),
            retrieval_log.write_retrieval(
                app.state.conn, "KRAS trial", "2", "test", "pubmed", {}, "raw-2",
                {"title": "Unrelated paper", "abstract": "biology"},
            ),
        ]
        response = client.post(
            "/v1/rankings",
            json={"question": "KRAS randomized trial", "retrieval_ids": [r.retrieval_id for r in records]},
        )
        fetched = client.get(f"/v1/rankings/{response.json()['ranking_run_id']}")

    assert response.status_code == 201
    assert response.json()["candidates"][0]["external_id"] == "1"
    assert "score_components" in response.json()["candidates"][0]
    assert fetched.status_code == 200
    assert fetched.json()["candidates"][0]["review_state"] == "unreviewed"


def test_ranking_review_api_updates_candidate(tmp_path):
    app = create_app(tmp_path / "ranking-review.db")
    with TestClient(app) as client:
        record = retrieval_log.write_retrieval(
            app.state.conn, "KRAS", "1", "test", "pubmed", {}, "raw",
            {"title": "KRAS paper", "abstract": "KRAS"},
        )
        run = client.post(
            "/v1/rankings", json={"question": "KRAS", "retrieval_ids": [record.retrieval_id]}
        ).json()
        candidate = run["candidates"][0]
        response = client.post(
            f"/v1/rankings/{run['ranking_run_id']}/candidates/{candidate['ranking_candidate_id']}/review",
            json={"review_state": "accepted", "reviewed_by": "scientist", "review_note": "Relevant"},
        )
    assert response.status_code == 200
    assert response.json()["review_state"] == "accepted"


def test_save_reviewed_candidates_requires_review_and_persists_collection_item(tmp_path):
    app = create_app(tmp_path / "save-reviewed.db")
    with TestClient(app) as client:
        project = client.post("/v1/projects", json={"name": "Reviewed"}).json()
        record = retrieval_log.write_retrieval(
            app.state.conn, "KRAS", "1", "test", "pubmed", {}, "raw",
            {"title": "KRAS paper", "abstract": "KRAS"},
        )
        run = client.post("/v1/rankings", json={"question": "KRAS", "retrieval_ids": [record.retrieval_id]}).json()
        candidate = run["candidates"][0]
        denied = client.post(f"/v1/projects/{project['project_id']}/save-reviewed", json={"ranking_run_id": run["ranking_run_id"], "candidate_ids": [candidate["ranking_candidate_id"]]})
        client.post(f"/v1/rankings/{run['ranking_run_id']}/candidates/{candidate['ranking_candidate_id']}/review", json={"review_state": "accepted"})
        saved = client.post(f"/v1/projects/{project['project_id']}/save-reviewed", json={"ranking_run_id": run["ranking_run_id"], "candidate_ids": [candidate["ranking_candidate_id"]]})
    assert denied.status_code == 400
    assert saved.status_code == 201
    assert saved.json()["saved_retrieval_ids"] == [record.retrieval_id]


def test_inference_api_returns_citations_and_flushes_parser_data(tmp_path):
    app = create_app(tmp_path / "inference.db")
    with TestClient(app) as client:
        record = retrieval_log.write_retrieval(
            app.state.conn, "KRAS cancer", "1", "test", "pubmed", {}, "raw",
            {"title": "KRAS signal", "abstract": "KRAS is associated with pancreatic cancer."},
        )
        created = client.post(
            "/v1/inferences",
            json={"question": "KRAS cancer", "retrieval_ids": [record.retrieval_id]},
        )
        inference_run_id = created.json()["inference_run_id"]
        detail = client.get(f"/v1/inferences/{inference_run_id}")
        flushed = client.post(f"/v1/inferences/{inference_run_id}/flush")
        after = client.get(f"/v1/inferences/{inference_run_id}")

    assert created.status_code == 201
    assert detail.json()["claims"][0]["citations"][0]["retrieval_id"] == record.retrieval_id
    assert flushed.json()["claims_preserved"] is True
    assert after.json()["parser_chunk_count"] == 0


def test_bad_request_and_missing_retrieval(tmp_path):
    with TestClient(create_app(tmp_path / "web.db")) as client:
        bad = client.post("/v1/evidence/search", json={"query": "", "max_results": 0})
        missing = client.get("/v1/retrievals/not-found")

    assert bad.status_code == 400
    assert missing.status_code == 404


def test_project_and_collection_api(tmp_path):
    app = create_app(tmp_path / "web.db")
    with TestClient(app) as client:
        project = client.post("/v1/projects", json={"name": "KRAS project"})
        collection = client.post(
            "/v1/collections",
            json={"project_id": project.json()["project_id"], "name": "Reviewed evidence"},
        )
        record = retrieval_log.write_retrieval(
            app.state.conn, "KRAS", "P01116", "test", "uniprot", {}, "raw", {"title": "KRAS"}
        )
        item = client.post(
            f"/v1/collections/{collection.json()['collection_id']}/items",
            json={"retrieval_id": record.retrieval_id, "relevance": "relevant"},
        )
        listed = client.get(f"/v1/collections?project_id={project.json()['project_id']}")
        fetched = client.get(f"/v1/collections/{collection.json()['collection_id']}")

    assert project.status_code == 201
    assert collection.status_code == 201
    assert item.status_code == 201
    assert listed.json()["collections"][0]["collection_id"] == collection.json()["collection_id"]
    assert fetched.json()["items"][0]["external_id"] == "P01116"


def test_watch_api(monkeypatch, tmp_path):
    app = create_app(tmp_path / "web.db")
    monkeypatch.setattr(
        "biolab.workspace.evidence.search_source",
        lambda *_args: [
            {"external_id": "PMID1", "retrieval_id": "r1", "response_hash": "hash1"}
        ],
    )
    with TestClient(app) as client:
        project = client.post("/v1/projects", json={"name": "Monitoring"}).json()
        watch = client.post(
            "/v1/watches",
            json={
                "project_id": project["project_id"], "source": "pubmed", "query": "KRAS"
            },
        )
        run = client.post(f"/v1/watches/{watch.json()['watch_id']}/run")
        listed = client.get(f"/v1/watches?project_id={project['project_id']}")
        events = client.get(f"/v1/watches/{watch.json()['watch_id']}/events")

    assert watch.status_code == 201
    assert run.status_code == 200
    assert run.json()["events"][0]["event_type"] == "added"
    assert listed.json()["watches"][0]["watch_id"] == watch.json()["watch_id"]
    assert len(events.json()["events"]) == 1


def test_run_all_watches_api(monkeypatch, tmp_path):
    app = create_app(tmp_path / "web.db")
    monkeypatch.setattr(
        "biolab.workspace.run_active_watches",
        lambda _conn: {"completed": [{"watch_id": "w1"}], "errors": []},
    )
    with TestClient(app) as client:
        response = client.post("/v1/watches/run-all")

    assert response.status_code == 200
    assert response.json()["completed"][0]["watch_id"] == "w1"


def test_required_api_key_and_identity_binding(monkeypatch, tmp_path):
    app = create_app(tmp_path / "auth.db", require_auth=True)
    captured = {}

    def fake_search(conn, query, agent_id, sources, max_results):
        captured["agent_id"] = agent_id
        return {"query_echo": query, "sources": {}, "errors": {}, "retrieval_count": 0}

    monkeypatch.setattr(evidence, "search_evidence", fake_search)
    with TestClient(app) as client:
        raw_key = auth.create_api_key(app.state.conn, "scientist", "scientist:alice")
        dashboard = client.get("/")
        schema = client.get("/openapi.json")
        missing = client.post("/v1/evidence/search", json={"query": "KRAS"})
        invalid = client.post(
            "/v1/evidence/search",
            json={"query": "KRAS"},
            headers={"Authorization": "Bearer bad-key"},
        )
        valid = client.post(
            "/v1/evidence/search",
            json={"query": "KRAS", "agent_id": "spoofed"},
            headers={"Authorization": f"Bearer {raw_key}"},
        )

    assert dashboard.status_code == 200
    assert schema.status_code == 200
    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert valid.status_code == 200
    assert captured["agent_id"] == "scientist:alice"


def test_claim_trace_api(tmp_path):
    app = create_app(tmp_path / "claims.db")
    with TestClient(app) as client:
        project = client.post("/v1/projects", json={"name": "Claims"}).json()
        claim = client.post(
            "/v1/claims", json={"project_id": project["project_id"], "claim": "KRAS claim"}
        )
        record = retrieval_log.write_retrieval(
            app.state.conn, "KRAS", "1", "test", "pubmed", {}, "raw", {"title": "Paper"}
        )
        linked = client.post(
            f"/v1/claims/{claim.json()['claim_id']}/evidence",
            json={
                "retrieval_id": record.retrieval_id,
                "stance": "supports",
                "assessment": "human_reviewed",
            },
        )
        trace = client.get(f"/v1/claims/{claim.json()['claim_id']}")

    assert claim.status_code == 201
    assert linked.status_code == 201
    assert trace.json()["evidence"][0]["stance"] == "supports"


def test_project_report_api_supports_json_and_markdown(tmp_path):
    app = create_app(tmp_path / "report.db")
    with TestClient(app) as client:
        project = client.post("/v1/projects", json={"name": "Report"}).json()
        json_report = client.get(f"/v1/projects/{project['project_id']}/report")
        markdown_report = client.get(
            f"/v1/projects/{project['project_id']}/report?format=markdown"
        )
        pdf_report = client.get(
            f"/v1/projects/{project['project_id']}/report?format=pdf"
        )

    assert json_report.status_code == 200
    assert json_report.json()["project"]["name"] == "Report"
    assert markdown_report.status_code == 200
    assert markdown_report.text.startswith("# Report")
    assert pdf_report.status_code == 200
    assert pdf_report.content.startswith(b"%PDF-1.4")


def test_authenticated_projects_are_isolated_by_owner(tmp_path):
    app = create_app(tmp_path / "tenant.db", require_auth=True)
    with TestClient(app) as client:
        alice_key = auth.create_api_key(app.state.conn, "alice", "user:alice")
        bob_key = auth.create_api_key(app.state.conn, "bob", "user:bob")
        alice = client.post("/v1/projects", headers={"Authorization": f"Bearer {alice_key}"}, json={"name": "Alice private"})
        alice_id = alice.json()["project_id"]
        bob_list = client.get("/v1/projects", headers={"Authorization": f"Bearer {bob_key}"})
        bob_report = client.get(f"/v1/projects/{alice_id}/report", headers={"Authorization": f"Bearer {bob_key}"})
    assert alice.status_code == 201
    assert bob_list.json()["projects"] == []
    assert bob_report.status_code == 404
