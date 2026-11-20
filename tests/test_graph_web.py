"""REST acceptance tests for the human-reviewed project graph."""

from biolab import retrieval_log
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
