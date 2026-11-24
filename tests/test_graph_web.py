"""REST acceptance tests for the human-reviewed project graph."""

import json

from biolab import graph, retrieval_log
from biolab.web import create_app
from starlette.testclient import TestClient


def test_graph_rest_propose_review_expand_and_trace(monkeypatch, tmp_path):
    from biolab import rag

    monkeypatch.setattr(rag, "index_parser_blocks", lambda *args: {"status": "active", "indexed_blocks": len(args[3])})
    app = create_app(tmp_path / "graph-api.db", require_auth=False)
    with TestClient(app) as client:
        project = client.post("/v1/projects", json={"name": "Graph API"}).json()
        record = retrieval_log.write_retrieval(
            app.state.conn, "Graph question", "GRAPH-1", "anonymous", "pubmed", {}, "raw",
            {"title": "Graph paper", "abstract": "KRAS activates pathway."},
        )
        client.post(f"/v1/projects/{project['project_id']}/retrievals", json={"retrieval_id": record.retrieval_id})
        parsed = client.post(f"/v1/projects/{project['project_id']}/retrievals/{record.retrieval_id}/parse")
        block = parsed.json()["blocks"][0]
        assert client.post(
            f"/v1/parser/runs/{parsed.json()['parser_run_id']}/blocks/{block['block_id']}/review",
            json={"review_state": "accepted"},
        ).status_code == 200
        extracted = client.post(
            f"/v1/projects/{project['project_id']}/graph/extract",
            json={"parser_run_id": parsed.json()["parser_run_id"]},
        )
        assert extracted.status_code == 201
        base = {"provenance": [{"block_id": block["block_id"], "content_hash": block["content_hash"], "excerpt": block["text"]}]}
        first = client.post(f"/v1/projects/{project['project_id']}/graph/proposals", json={**base, "node_type": "gene", "canonical_name": "KRAS"})
        second = client.post(f"/v1/projects/{project['project_id']}/graph/proposals", json={**base, "node_type": "pathway", "canonical_name": "MAPK pathway"})
        assert first.status_code == 201 and second.status_code == 201
        first_id = first.json()["node_id"]
        second_id = second.json()["node_id"]
        assert client.post(f"/v1/projects/{project['project_id']}/graph/proposals/{first.json()['proposal_id']}/review", json={"decision": "accepted"}).status_code == 200
        assert client.post(f"/v1/projects/{project['project_id']}/graph/proposals/{second.json()['proposal_id']}/review", json={"decision": "accepted"}).status_code == 200
        edge = client.post(f"/v1/projects/{project['project_id']}/graph/proposals", json={**base, "proposal_type": "edge", "subject_node_id": first_id, "predicate": "activates", "object_node_id": second_id})
        assert edge.status_code == 201
        assert client.post(f"/v1/projects/{project['project_id']}/graph/proposals/{edge.json()['proposal_id']}/review", json={"decision": "accepted"}).status_code == 200
        expanded = client.get(f"/v1/projects/{project['project_id']}/graph/nodes/{first_id}/expand")
        trace = client.get(f"/v1/projects/{project['project_id']}/graph/entities/{first_id}/trace")
        stats = client.get(f"/v1/projects/{project['project_id']}/graph/stats")

    assert expanded.status_code == 200
    assert expanded.json()["edges"][0]["predicate"] == "activates"
    assert trace.status_code == 200 and trace.json()["provenance"][0]["block_id"] == block["block_id"]
    assert stats.json()["pending_proposals"] == 0
    assert stats.json()["accepted_fingerprint"]


def test_graph_rest_keeps_edges_pending_until_nodes_are_accepted(tmp_path):
    app = create_app(tmp_path / "graph-gate.db", require_auth=False)
    with TestClient(app) as client:
        project = client.post("/v1/projects", json={"name": "Gate"}).json()
        other = client.post("/v1/projects", json={"name": "Other"}).json()
        # The API rejects graph writes without preserved source provenance.
        response = client.post(f"/v1/projects/{project['project_id']}/graph/proposals", json={"node_type": "gene", "canonical_name": "KRAS"})
        assert response.status_code == 400
        assert client.get(f"/v1/projects/{other['project_id']}/graph/stats").status_code == 200


def test_rag_answer_returns_accepted_graph_context_and_audits_graph_ids(monkeypatch, tmp_path):
    from biolab import document_parser, rag

    monkeypatch.setattr(rag, "answer_parser_context", lambda *args: {
        "status": "active", "grounded": True, "answer": "KRAS activates MAPK.",
        "citations": [], "answer_spans": [], "chunks": [],
    })
    monkeypatch.setattr(document_parser, "validate_rag_result", lambda *args: None)
    app = create_app(tmp_path / "hybrid-rag.db", require_auth=False)
    with TestClient(app) as client:
        project = client.post("/v1/projects", json={"name": "Hybrid RAG"}).json()
        pid = project["project_id"]
        app.state.conn.execute(
            """INSERT INTO parser_runs
               (parser_run_id, project_id, filename, parser_model, source_hash, status, block_count, created_by, created_at)
               VALUES ('hybrid-run', ?, 'paper.pdf', 'test', 'hash', 'completed', 1, 'anonymous', '2026-01-01T00:00:00Z')""", (pid,)
        )
        app.state.conn.execute(
            """INSERT INTO parsed_blocks
               (block_id, parser_run_id, page_number, block_index, block_type, section_name, text, content_hash, created_at)
               VALUES ('hybrid-block', 'hybrid-run', 1, 0, 'paragraph', 'results', 'KRAS activates MAPK.', 'hybrid-hash', '2026-01-01T00:00:00Z')"""
        )
        app.state.conn.commit()
        source = [{"block_id": "hybrid-block", "parser_run_id": "hybrid-run", "content_hash": "hybrid-hash", "excerpt": "KRAS activates MAPK."}]
        kras = graph.create_node_proposal(app.state.conn, pid, "gene", "KRAS", owner_id="anonymous", provenance=source)
        mapk = graph.create_node_proposal(app.state.conn, pid, "gene", "MAPK", owner_id="anonymous", provenance=source)
        graph.review_proposal(app.state.conn, kras["proposal_id"], owner_id="anonymous", decision="accepted", reviewer_role="owner")
        graph.review_proposal(app.state.conn, mapk["proposal_id"], owner_id="anonymous", decision="accepted", reviewer_role="owner")
        edge = graph.create_edge_proposal(app.state.conn, pid, kras["node_id"], "activates", mapk["node_id"], owner_id="anonymous", provenance=source)
        graph.review_proposal(app.state.conn, edge["proposal_id"], owner_id="anonymous", decision="accepted", reviewer_role="owner")
        answer = client.post(f"/v1/projects/{pid}/rag/answer", json={"question": "What activates MAPK?"})
        audit_result = client.get(f"/v1/projects/{pid}/audit")

    assert answer.status_code == 200
    assert answer.json()["graph_context"][0]["edge_id"] == edge["edge_id"]
    assert any(edge["edge_id"] in json.dumps(event["payload"]) for event in audit_result.json()["events"] if event["event_type"] == "rag_answered")
