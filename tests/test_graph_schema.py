"""Schema and isolation checks for the first knowledge-graph storage slice."""

import sqlite3

import pytest

from biolab import db


def _project(conn, project_id: str, owner: str) -> None:
    conn.execute(
        "INSERT INTO projects (project_id, name, created_at, owner_id) VALUES (?, ?, ?, ?)",
        (project_id, project_id, "2026-01-01T00:00:00+00:00", owner),
    )


def _node(conn, node_id: str, project_id: str, name: str) -> None:
    conn.execute(
        """INSERT INTO graph_nodes
           (node_id, project_id, node_type, canonical_name, normalized_name, status, created_by, created_at)
           VALUES (?, ?, 'gene', ?, ?, 'pending', 'test', ?)""",
        (node_id, project_id, name, name.lower(), "2026-01-01T00:00:00+00:00"),
    )


def test_graph_schema_initializes_with_provenance_tables(tmp_path):
    conn = db.connect(str(tmp_path / "graph.db"))

    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'graph_%'"
        ).fetchall()
    }

    assert names == {
        "graph_extraction_runs",
        "graph_nodes",
        "graph_edges",
        "graph_proposals",
        "graph_provenance",
        "graph_reviews",
        "graph_index_outbox",
    }


def test_graph_edges_cannot_cross_project_endpoints(tmp_path):
    conn = db.connect(str(tmp_path / "graph-isolation.db"))
    _project(conn, "project-a", "alice")
    _project(conn, "project-b", "bob")
    _node(conn, "a-node", "project-a", "KRAS")
    _node(conn, "b-node", "project-b", "TP53")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO graph_edges
               (edge_id, project_id, subject_node_id, predicate, object_node_id, status, created_by, created_at)
               VALUES ('cross-project', 'project-a', 'a-node', 'ASSOCIATED_WITH', 'b-node', 'pending', 'test', ?)""",
            ("2026-01-01T00:00:00+00:00",),
        )


def test_graph_provenance_requires_source_locator(tmp_path):
    conn = db.connect(str(tmp_path / "graph-provenance.db"))
    _project(conn, "project-a", "alice")
    _node(conn, "a-node", "project-a", "KRAS")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO graph_provenance
               (provenance_id, project_id, node_id, content_hash, created_at)
               VALUES ('missing-source', 'project-a', 'a-node', 'hash', ?)""",
            ("2026-01-01T00:00:00+00:00",),
        )


def test_graph_proposal_requires_matching_project_entity(tmp_path):
    conn = db.connect(str(tmp_path / "graph-proposal.db"))
    _project(conn, "project-a", "alice")
    _project(conn, "project-b", "bob")
    _node(conn, "a-node", "project-a", "KRAS")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO graph_proposals
               (proposal_id, project_id, proposal_type, node_id, payload_json, payload_hash, created_by, created_at)
               VALUES ('cross-project-proposal', 'project-b', 'node', 'a-node', '{}', 'hash', 'test', ?)""",
            ("2026-01-01T00:00:00+00:00",),
        )
