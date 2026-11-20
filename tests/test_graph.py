"""Service-layer tests for project-scoped graph proposals and review gates."""

import pytest
from biolab import db, graph, workspace


def _setup(tmp_path):
    conn = db.connect(str(tmp_path / "graph-service.db"))
    project = workspace.create_project(conn, "Graph review", owner_id="alice")
    conn.execute(
        """INSERT INTO parser_runs
           (parser_run_id, project_id, filename, parser_model, source_hash, status, block_count, created_by, created_at)
           VALUES ('run-1', ?, 'paper.pdf', 'test-parser', 'source-hash', 'completed', 1, 'alice', '2026-01-01T00:00:00+00:00')""",
        (project["project_id"],),
    )
    conn.execute(
        """INSERT INTO parsed_blocks
           (block_id, parser_run_id, page_number, block_index, block_type, section_name, text, content_hash, created_at)
           VALUES ('block-1', 'run-1', 2, 0, 'paragraph', 'results', 'KRAS interacts with TP53.', 'block-hash', '2026-01-01T00:00:00+00:00')""",
    )
    conn.commit()
    source = [{"block_id": "block-1", "parser_run_id": "run-1", "page_number": 2, "content_hash": "block-hash", "excerpt": "KRAS interacts with TP53."}]
    return conn, project["project_id"], source


def test_node_proposal_is_idempotent_and_traceable(tmp_path):
    conn, project_id, source = _setup(tmp_path)

    first = graph.create_node_proposal(conn, project_id, "gene", " KRAS ", owner_id="alice", provenance=source)
    second = graph.create_node_proposal(conn, project_id, "gene", "kras", owner_id="alice", provenance=source)

    assert first["proposal_id"] == second["proposal_id"]
    assert graph.trace_entity(conn, project_id, first["proposal_id"], owner_id="alice")[0]["block_id"] == "block-1"
    assert graph.stats(conn, project_id, owner_id="alice")["pending_proposals"] == 1


def test_edge_requires_accepted_endpoints_and_expands_only_trusted_edges(tmp_path):
    conn, project_id, source = _setup(tmp_path)
    kras = graph.create_node_proposal(conn, project_id, "gene", "KRAS", owner_id="alice", provenance=source)
    tp53 = graph.create_node_proposal(conn, project_id, "gene", "TP53", owner_id="alice", provenance=source)
    graph.review_proposal(conn, kras["proposal_id"], owner_id="alice", decision="accepted", reviewer_role="owner")
    graph.review_proposal(conn, tp53["proposal_id"], owner_id="alice", decision="accepted", reviewer_role="owner")

    edge = graph.create_edge_proposal(conn, project_id, kras["node_id"], "INTERACTS_WITH", tp53["node_id"], owner_id="alice", provenance=source)
    reviewed = graph.review_proposal(conn, edge["proposal_id"], owner_id="alice", decision="accepted", reviewer_role="owner")
    expanded = graph.expand_node(conn, project_id, kras["node_id"], owner_id="alice")

    assert reviewed["status"] == "accepted"
    assert expanded["edges"][0]["predicate"] == "INTERACTS_WITH"


def test_edge_cannot_be_accepted_before_both_nodes(tmp_path):
    conn, project_id, source = _setup(tmp_path)
    kras = graph.create_node_proposal(conn, project_id, "gene", "KRAS", owner_id="alice", provenance=source)
    tp53 = graph.create_node_proposal(conn, project_id, "gene", "TP53", owner_id="alice", provenance=source)
    edge = graph.create_edge_proposal(conn, project_id, kras["node_id"], "INTERACTS_WITH", tp53["node_id"], owner_id="alice", provenance=source)

    with pytest.raises(ValueError, match="endpoints"):
        graph.review_proposal(conn, edge["proposal_id"], owner_id="alice", decision="accepted", reviewer_role="owner")


def test_graph_service_enforces_project_access(tmp_path):
    conn, project_id, source = _setup(tmp_path)

    with pytest.raises(ValueError, match="access denied"):
        graph.create_node_proposal(conn, project_id, "gene", "KRAS", owner_id="bob", provenance=source)
