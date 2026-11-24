"""Offline acceptance tests for the local dashboard and REST API."""

import base64

from biolab import auth, evidence, rag, retrieval_log
from biolab.web import create_app
from starlette.testclient import TestClient


def test_dashboard_and_health(tmp_path):
    with TestClient(create_app(tmp_path / "web.db", require_auth=False)) as client:
        page = client.get("/")
        health = client.get("/health")
        schema = client.get("/openapi.json")
        metrics = client.get("/metrics")
        storage = client.get("/v1/storage")
        project = client.post("/v1/projects", json={"name": "Audit UI"}).json()
        project_page = client.get(f"/projects/{project['project_id']}")

    assert page.status_code == 200
    assert "Biolab Evidence Workspace" in page.text
    assert project_page.status_code == 200
    assert "Verify project audit" in project_page.text
    assert health.json() == {"status": "ok", "service": "biolab"}
    assert schema.json()["info"]["version"] == "0.5.0"
    assert metrics.json()["audit_chain_valid"] is True
    assert metrics.json()["counts"]["retrievals"] == 0
    assert storage.json()["metadata"]["backend"] == "sqlite"
    assert storage.json()["metadata"]["durable"] is False


def test_huggingface_parser_api_persists_and_flushes(monkeypatch, tmp_path):
    from biolab import hf_parser
    monkeypatch.setattr(hf_parser, "parse_document", lambda content, filename: {
        "parser_model": "nvidia/Nemotron-3.5-Parse", "status": "succeeded", "source_hash": "abc", "block_count": 1,
        "blocks": [{"block_id": "b1", "page_number": 1, "block_index": 0, "block_type": "paragraph", "text": "Parsed", "bbox": None}],
    })
    with TestClient(create_app(tmp_path / "parser-web.db", require_auth=False)) as client:
        created = client.post("/v1/parser/parse", json={"filename": "paper.pdf", "content_base64": base64.b64encode(b"pdf").decode()})
        run_id = created.json()["parser_run_id"]
        block_id = created.json()["blocks"][0]["block_id"]
        detail = client.get(f"/v1/parser/runs/{run_id}")
        reviewed = client.post(f"/v1/parser/runs/{run_id}/blocks/{block_id}/review", json={"review_state": "accepted", "note": "Relevant to the review"})
        flushed = client.post(f"/v1/parser/runs/{run_id}/flush")
    assert created.status_code == 201
    assert detail.json()["blocks"][0]["text"] == "Parsed"
    assert detail.json()["blocks"][0]["review_state"] == "pending"
    assert reviewed.status_code == 200
    assert reviewed.json()["review_state"] == "accepted"
    assert flushed.json()["preserved_parsed_blocks"] == 1


def test_parsed_documents_live_inside_project_folder_and_download(monkeypatch, tmp_path):
    from biolab import hf_parser

    monkeypatch.setattr(hf_parser, "parse_document", lambda content, filename: {
        "parser_model": "nvidia/Nemotron-3.5-Parse", "status": "succeeded", "source_hash": "abc", "block_count": 1,
        "blocks": [{"block_id": "b1", "page_number": 1, "block_index": 0, "block_type": "paragraph", "text": "Stored evidence", "bbox": None}],
    })
    with TestClient(create_app(tmp_path / "folder.db", require_auth=False)) as client:
        project = client.post("/v1/projects", json={"name": "Diabetes review"}).json()
        parsed = client.post("/v1/parser/parse", json={
            "project_id": project["project_id"], "filename": "paper.pdf",
            "content_base64": base64.b64encode(b"pdf").decode(),
        })
        run_id = parsed.json()["parser_run_id"]
        folder = client.get(f"/v1/projects/{project['project_id']}/parser-runs")
        rag_search = client.post(f"/v1/projects/{project['project_id']}/rag/search", json={"question": "stored evidence"})
        download = client.get(f"/v1/parser/runs/{run_id}/download")
        notebook = client.get(f"/v1/projects/{project['project_id']}/notebook")
    assert parsed.status_code == 201
    assert parsed.json()["project_id"] == project["project_id"]
    assert folder.status_code == 200
    assert folder.json()["parser_runs"][0]["parser_run_id"] == run_id
    assert rag_search.status_code == 200
    assert rag_search.json()["status"] == "disabled"
    assert download.status_code == 200
    assert download.headers["content-disposition"].endswith('"paper.parsed.json"')
    assert download.json()["blocks"][0]["text"] == "Stored evidence"
    assert notebook.status_code == 200
    assert notebook.headers["content-disposition"].endswith(f'"{project["project_id"]}.ipynb"')
    assert notebook.json()["nbformat"] == 4
    assert any(cell["cell_type"] == "code" for cell in notebook.json()["cells"])


def test_zotero_metadata_import_is_preserved_and_deduplicated(tmp_path):
    item = {
        "key": "ABC123",
        "itemType": "journalArticle",
        "title": "KRAS study",
        "abstractNote": "Metadata-only abstract",
        "DOI": "10.1000/example",
        "creators": [{"firstName": "Ada", "lastName": "Lovelace", "creatorType": "author"}],
        "tags": [{"tag": "KRAS"}],
    }
    with TestClient(create_app(tmp_path / "zotero.db", require_auth=False)) as client:
        project = client.post("/v1/projects", json={"name": "Zotero import"}).json()
        first = client.post(f"/v1/projects/{project['project_id']}/imports/zotero", json={"items": [item]})
        second = client.post(f"/v1/projects/{project['project_id']}/imports/zotero", json={"items": [item]})
        papers = client.get(f"/v1/projects/{project['project_id']}/retrievals")

    assert first.status_code == 201
    assert first.json()["imported_count"] == 1
    assert first.json()["metadata_only"] is True
    assert second.json()["duplicate_count"] == 1
    assert len(papers.json()["retrievals"]) == 1
    assert papers.json()["retrievals"][0]["source"] == "zotero"


def test_selected_mcp_paper_flows_into_project_parser_and_notes(monkeypatch, tmp_path):
    monkeypatch.setattr(rag, "index_parser_blocks", lambda *args: {"status": "active", "indexed_blocks": len(args[3])})
    app = create_app(tmp_path / "selected-paper.db", require_auth=False)
    with TestClient(app) as client:
        project = client.post("/v1/projects", json={"name": "Paper RAG"}).json()
        record = retrieval_log.write_retrieval(
            app.state.conn, "KRAS", "PMID-1", "api:user", "pubmed", {"url": "https://example.test/paper"},
            '{"title":"KRAS study","abstract":"Methods used a cohort. Results improved."}',
            {"title": "KRAS study", "abstract": "Methods used a cohort. Results improved."},
        )
        selected = client.post(f"/v1/projects/{project['project_id']}/retrievals", json={"retrieval_id": record.retrieval_id})
        papers = client.get(f"/v1/projects/{project['project_id']}/retrievals")
        parsed = client.post(f"/v1/projects/{project['project_id']}/retrievals/{record.retrieval_id}/parse")
        parsed_block = parsed.json()["blocks"][0]
        client.post(f"/v1/parser/runs/{parsed.json()['parser_run_id']}/blocks/{parsed_block['block_id']}/review", json={"review_state": "accepted"})
        notes = client.post(f"/v1/projects/{project['project_id']}/rag/notes", json={"question": "What changed?", "note_text": "The reviewed result improved.", "citations": [{"block_id": parsed_block["block_id"], "page_number": parsed_block["page_number"], "content_hash": parsed_block["content_hash"]}]})
        report = client.get(f"/v1/projects/{project['project_id']}/report")

    assert selected.status_code == 201
    assert papers.json()["retrievals"][0]["retrieval_id"] == record.retrieval_id
    assert parsed.status_code == 201
    assert parsed.json()["retrieval_id"] == record.retrieval_id
    assert notes.status_code == 201
    assert report.json()["papers"][0]["retrieval_id"] == record.retrieval_id


def test_three_paper_project_rag_download_and_audit_workflow(monkeypatch, tmp_path):
    from biolab import document_parser

    monkeypatch.setattr(rag, "index_parser_blocks", lambda *args: {"status": "active", "indexed_blocks": len(args[3])})
    monkeypatch.setattr(document_parser, "validate_rag_result", lambda *args: None)
    monkeypatch.setattr(rag, "answer_parser_context", lambda project_id, question, top_k, sections: {
        "status": "active", "grounded": True, "answer": "Reviewed evidence supports the query.",
        "citations": [], "answer_spans": [], "chunks": [],
    })
    app = create_app(tmp_path / "three-paper.db", require_auth=False)
    with TestClient(app) as client:
        project = client.post("/v1/projects", json={
            "name": "Three paper review", "objective": "Compare evidence",
            "research_question": "What changed?", "scope": "Selected papers only",
        }).json()
        records = []
        for index in range(3):
            record = retrieval_log.write_retrieval(
                app.state.conn, "Paper source", f"PAPER-{index}", "anonymous", "pubmed", {}, "raw",
                {"title": f"Paper {index}", "abstract": f"Results for paper {index} improved."},
            )
            records.append(record)
            assert client.post(f"/v1/projects/{project['project_id']}/retrievals", json={"retrieval_id": record.retrieval_id}).status_code == 201

        selected = client.get(f"/v1/projects/{project['project_id']}/retrievals").json()["retrievals"]
        assert len(selected) == 3
        runs = []
        for record in records:
            parsed = client.post(f"/v1/projects/{project['project_id']}/retrievals/{record.retrieval_id}/parse")
            assert parsed.status_code == 201
            runs.append(parsed.json())
            for block in parsed.json()["blocks"]:
                assert client.post(f"/v1/parser/runs/{parsed.json()['parser_run_id']}/blocks/{block['block_id']}/review", json={"review_state": "accepted"}).status_code == 200

        answer_path = f"/v1/projects/{project['project_id']}/rag/answer"
        first = client.post(answer_path, json={"question": "What changed?"})
        second = client.post(answer_path, json={"question": "What changed?"})
        history = client.get(f"/v1/projects/{project['project_id']}/query-history")
        audit_result = client.get(f"/v1/projects/{project['project_id']}/audit")
        audit_export = client.post(f"/v1/projects/{project['project_id']}/audit/export")
        report = client.get(f"/v1/projects/{project['project_id']}/report").json()
        downloads = [client.get(f"/v1/parser/runs/{run['parser_run_id']}/download") for run in runs]

    assert first.status_code == 200 and first.json()["grounded"] is True
    assert second.json()["cached"] is True
    assert len(history.json()["queries"]) == 2
    assert audit_result.json()["valid"] is True
    assert audit_export.status_code == 200
    assert audit_export.json()["valid"] is True
    assert audit_export.headers["content-disposition"].endswith(f'"{project["project_id"]}.audit.json"')
    assert len(report["papers"]) == 3 and len(report["parser_runs"]) == 3
    assert all(download.status_code == 200 for download in downloads)


def test_project_audit_records_history_and_detects_tampering(tmp_path):
    app = create_app(tmp_path / "project-audit.db", require_auth=False)
    with TestClient(app) as client:
        project = client.post("/v1/projects", json={"name": "Audit history"}).json()
        audit_result = client.get(f"/v1/projects/{project['project_id']}/audit")
        assert audit_result.status_code == 200
        assert audit_result.json()["valid"] is True
        assert audit_result.json()["events"][0]["event_type"] == "project_created"

        app.state.conn.execute("UPDATE audit_events SET payload = '{}' WHERE project_id = ?", (project["project_id"],))
        app.state.conn.commit()
        broken = client.get(f"/v1/projects/{project['project_id']}/audit")

    assert broken.status_code == 409
    assert broken.json()["valid"] is False


def test_project_rag_query_is_cached(monkeypatch, tmp_path):
    calls = []

    def fake_search(project_id, question, top_k, sections):
        calls.append((project_id, question, top_k, sections))
        return {"status": "active", "chunks": [{"chunk_text": "cached evidence"}]}

    monkeypatch.setattr(rag, "search_parser_context", fake_search)
    with TestClient(create_app(tmp_path / "rag-cache.db", require_auth=False)) as client:
        project = client.post("/v1/projects", json={"name": "Cached RAG"}).json()
        path = f"/v1/projects/{project['project_id']}/rag/search"
        first = client.post(path, json={"question": "What changed?", "top_k": 3})
        second = client.post(path, json={"question": "What changed?", "top_k": 3})

    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert len(calls) == 1


def test_project_rag_answer_endpoint_returns_grounding_and_citations(monkeypatch, tmp_path):
    monkeypatch.setattr("biolab.document_parser.validate_rag_result", lambda *args: None)
    monkeypatch.setattr(rag, "answer_parser_context", lambda *args: {
        "status": "active", "grounded": True, "answer": "The result improved.",
        "citations": [{"block_id": "b1", "page_number": 2}], "chunks": [],
    })
    with TestClient(create_app(tmp_path / "rag-answer.db", require_auth=False)) as client:
        project = client.post("/v1/projects", json={"name": "Grounded answer"}).json()
        path = f"/v1/projects/{project['project_id']}/rag/answer"
        response = client.post(path, json={"question": "What improved?"})
        cached = client.post(path, json={"question": "What improved?"})

    assert response.status_code == 200
    assert response.json()["grounded"] is True
    assert response.json()["citations"][0]["block_id"] == "b1"
    assert cached.json()["cached"] is True


def test_project_rag_end_to_end_selection_review_answer_cache_and_audit(monkeypatch, tmp_path):
    monkeypatch.setattr("biolab.document_parser.validate_rag_result", lambda *args: None)
    monkeypatch.setattr(rag, "index_parser_blocks", lambda *args: {"status": "active", "indexed_blocks": len(args[3])})
    monkeypatch.setattr(rag, "answer_parser_context", lambda project_id, question, top_k, sections: {
        "status": "active", "grounded": True, "answer": "The reviewed result improved.",
        "citations": [{"block_id": "stable-block"}], "chunks": [],
    })
    app = create_app(tmp_path / "rag-e2e.db", require_auth=False)
    with TestClient(app) as client:
        project = client.post("/v1/projects", json={"name": "End-to-end RAG"}).json()
        record = retrieval_log.write_retrieval(
            app.state.conn, "KRAS", "PMID-E2E", "api:user", "pubmed", {}, "raw",
            {"title": "KRAS study", "abstract": "Results improved in the reviewed cohort."},
        )
        selected = client.post(f"/v1/projects/{project['project_id']}/retrievals", json={"retrieval_id": record.retrieval_id})
        parsed = client.post(f"/v1/projects/{project['project_id']}/retrievals/{record.retrieval_id}/parse")
        block = parsed.json()["blocks"][0]
        reviewed = client.post(f"/v1/parser/runs/{parsed.json()['parser_run_id']}/blocks/{block['block_id']}/review", json={"review_state": "accepted"})
        answer_path = f"/v1/projects/{project['project_id']}/rag/answer"
        answer = client.post(answer_path, json={"question": "What improved?"})
        cached = client.post(answer_path, json={"question": "What improved?"})
        history = client.get(f"/v1/projects/{project['project_id']}/audit")

    assert selected.status_code == 201
    assert parsed.status_code == 201
    assert reviewed.json()["review_state"] == "accepted"
    assert answer.json()["grounded"] is True
    assert cached.json()["cached"] is True
    assert history.json()["valid"] is True
    assert {event["event_type"] for event in history.json()["events"]} >= {"paper_selected", "paper_parsed", "parser_block_reviewed", "rag_answered"}
def test_search_api_uses_shared_service(monkeypatch, tmp_path):
    captured = {}

    def fake_search(conn, query, agent_id, sources, max_results):
        captured.update(
            query=query, agent_id=agent_id, sources=sources, max_results=max_results
        )
        return {"query_echo": query, "sources": {}, "errors": {}, "retrieval_count": 0}

    monkeypatch.setattr(evidence, "search_evidence", fake_search)
    with TestClient(create_app(tmp_path / "web.db", require_auth=False)) as client:
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
    app = create_app(tmp_path / "web.db", require_auth=False)
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
    app = create_app(tmp_path / "ranking.db", require_auth=False)
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
    app = create_app(tmp_path / "ranking-review.db", require_auth=False)
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
    app = create_app(tmp_path / "save-reviewed.db", require_auth=False)
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
    app = create_app(tmp_path / "inference.db", require_auth=False)
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
    with TestClient(create_app(tmp_path / "web.db", require_auth=False)) as client:
        bad = client.post("/v1/evidence/search", json={"query": "", "max_results": 0})
        missing = client.get("/v1/retrievals/not-found")

    assert bad.status_code == 400
    assert missing.status_code == 404


def test_project_and_collection_api(tmp_path):
    app = create_app(tmp_path / "web.db", require_auth=False)
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
    app = create_app(tmp_path / "web.db", require_auth=False)
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
    app = create_app(tmp_path / "web.db", require_auth=False)
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
    app = create_app(tmp_path / "claims.db", require_auth=False)
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
    app = create_app(tmp_path / "report.db", require_auth=False)
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


def test_project_click_opens_dedicated_workspace_page(tmp_path):
    app = create_app(tmp_path / "workspace-page.db", require_auth=False)
    with TestClient(app) as client:
        project = client.post("/v1/projects", json={"name": "Workspace page"}).json()
        page = client.get(f"/projects/{project['project_id']}")
        missing = client.get("/projects/not-a-project")

    assert page.status_code == 200
    assert "Selected MCP papers" in page.text
    assert "Parsed papers and review gate" in page.text
    assert "Knowledge graph review" in page.text
    assert "Extract graph proposals" in page.text
    assert 'type="file"' not in page.text.lower()
    assert ">upload<" not in page.text.lower()
    assert missing.status_code == 404


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


def test_authenticated_caller_cannot_attach_another_callers_retrieval(tmp_path):
    app = create_app(tmp_path / "retrieval-ownership.db", require_auth=True)
    with TestClient(app) as client:
        auth.create_api_key(app.state.conn, "alice", "user:alice")
        bob_key = auth.create_api_key(app.state.conn, "bob", "user:bob")
        project = client.post("/v1/projects", headers={"Authorization": f"Bearer {bob_key}"}, json={"name": "Bob project"}).json()
        record = retrieval_log.write_retrieval(
            app.state.conn, "KRAS", "PMID-PRIVATE", "user:alice", "pubmed", {}, "raw", {"title": "Private paper"},
        )
        response = client.post(
            f"/v1/projects/{project['project_id']}/retrievals",
            headers={"Authorization": f"Bearer {bob_key}"},
            json={"retrieval_id": record.retrieval_id},
        )

    assert response.status_code == 404
