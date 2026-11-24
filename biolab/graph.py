"""Project-scoped, provenance-first knowledge-graph services.

This module owns graph persistence and review invariants. HTTP and MCP layers
should call these functions instead of issuing graph SQL directly.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from biolab import audit, workspace

NODE_TYPES = {"paper", "author", "gene", "protein", "disease", "drug", "pathway", "method", "organization", "claim", "evidence_block"}
PROPOSAL_STATES = {"pending", "accepted", "edited", "rejected", "deferred", "superseded"}
REVIEW_DECISIONS = {"accepted", "edited", "rejected", "deferred"}
_EXTRACTOR_VERSION = "deterministic-graph-v1"
_STOP_SYMBOLS = {"THE", "AND", "WITH", "FROM", "THIS", "THAT", "WAS", "ARE", "FOR", "INTO", "PATHWAY"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _normalize_name(value: str) -> str:
    return " ".join(value.casefold().split())


def _require_access(conn: Any, project_id: str, owner_id: str) -> None:
    if not workspace.has_project_access(conn, project_id, owner_id):
        raise ValueError("project access denied")


def _require_provenance(provenance: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    records = provenance or []
    if not records:
        raise ValueError("graph proposals require provenance")
    for item in records:
        if not item.get("retrieval_id") and not item.get("block_id"):
            raise ValueError("provenance requires retrieval_id or block_id")
        if not item.get("content_hash"):
            raise ValueError("provenance requires content_hash")
    return records


def _insert_provenance(
    conn: Any,
    project_id: str,
    proposal_id: str,
    provenance: list[dict[str, Any]],
    *,
    node_id: str | None = None,
    edge_id: str | None = None,
) -> None:
    for item in provenance:
        if item.get("block_id"):
            row = conn.execute(
                """SELECT pr.project_id FROM parsed_blocks pb
                   JOIN parser_runs pr ON pr.parser_run_id = pb.parser_run_id
                   WHERE pb.block_id = ?""",
                (item["block_id"],),
            ).fetchone()
            if row is None or row[0] != project_id:
                raise ValueError("parser block provenance does not belong to the project")
        if item.get("retrieval_id"):
            row = conn.execute(
                """SELECT project_id FROM parser_runs WHERE retrieval_id = ? AND project_id = ?
                   UNION SELECT ec.project_id FROM collection_items ci
                   JOIN evidence_collections ec ON ec.collection_id = ci.collection_id
                   WHERE ci.retrieval_id = ? AND ec.project_id = ? LIMIT 1""",
                (item["retrieval_id"], project_id, item["retrieval_id"], project_id),
            ).fetchone()
            if row is None:
                raise ValueError("retrieval provenance does not belong to the project")
        conn.execute(
            """INSERT INTO graph_provenance
               (provenance_id, project_id, proposal_id, node_id, edge_id, retrieval_id,
                parser_run_id, block_id, page_number, locator_json, content_hash, excerpt,
                parser_version, extractor_version, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()), project_id, proposal_id, item.get("node_id"), item.get("edge_id"),
                item.get("retrieval_id"), item.get("parser_run_id"), item.get("block_id"),
                item.get("page_number"), _json(item.get("locator", {})), item["content_hash"],
                item.get("excerpt", ""), item.get("parser_version", ""),
                item.get("extractor_version", ""), _now(),
            ),
        )
        conn.execute(
            "UPDATE graph_provenance SET node_id = COALESCE(node_id, ?), edge_id = COALESCE(edge_id, ?) WHERE proposal_id = ? AND node_id IS NULL AND edge_id IS NULL",
            (node_id, edge_id, proposal_id),
        )


def _proposal_row(conn: Any, proposal_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT proposal_id, project_id, proposal_type, node_id, edge_id, extraction_run_id,
                  payload_json, payload_hash, proposal_version, status, created_by, created_at
           FROM graph_proposals WHERE proposal_id = ?""",
        (proposal_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "proposal_id": row[0], "project_id": row[1], "proposal_type": row[2],
        "node_id": row[3], "edge_id": row[4], "extraction_run_id": row[5],
        "payload": json.loads(row[6]), "payload_hash": row[7], "proposal_version": row[8],
        "status": row[9], "created_by": row[10], "created_at": row[11],
    }


def create_node_proposal(
    conn: Any,
    project_id: str,
    node_type: str,
    canonical_name: str,
    *,
    owner_id: str,
    namespace: str = "",
    external_id: str = "",
    properties: dict[str, Any] | None = None,
    extraction_run_id: str | None = None,
    provenance: list[dict[str, Any]] | None = None,
    created_by: str | None = None,
) -> dict[str, Any]:
    """Create an idempotent pending node proposal with required provenance."""
    _require_access(conn, project_id, owner_id)
    if node_type not in NODE_TYPES:
        raise ValueError(f"unsupported node_type: {node_type}")
    if not canonical_name.strip():
        raise ValueError("canonical_name must not be empty")
    sources = _require_provenance(provenance)
    normalized = _normalize_name(canonical_name)
    payload = {
        "node_type": node_type, "namespace": namespace.strip(), "canonical_name": canonical_name.strip(),
        "normalized_name": normalized, "external_id": external_id.strip(), "properties": properties or {},
    }
    payload_hash = _hash({**payload, "canonical_name": normalized})
    existing = conn.execute(
        """SELECT node_id FROM graph_nodes
           WHERE project_id = ? AND node_type = ? AND namespace = ? AND normalized_name = ? AND external_id = ?""",
        (project_id, node_type, namespace.strip(), normalized, external_id.strip()),
    ).fetchone()
    node_id = existing[0] if existing else str(uuid.uuid4())
    if existing is None:
        conn.execute(
            """INSERT INTO graph_nodes
               (node_id, project_id, node_type, namespace, canonical_name, normalized_name,
                external_id, properties_json, status, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (node_id, project_id, node_type, namespace.strip(), canonical_name.strip(), normalized,
             external_id.strip(), _json(properties or {}), created_by or owner_id, _now()),
        )
    duplicate = conn.execute(
        """SELECT proposal_id FROM graph_proposals
           WHERE project_id = ? AND proposal_type = 'node' AND node_id = ? AND payload_hash = ?
           AND status IN ('pending', 'accepted', 'edited')""",
        (project_id, node_id, payload_hash),
    ).fetchone()
    if duplicate:
        result = _proposal_row(conn, duplicate[0])
        assert result is not None
        return result
    proposal_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO graph_proposals
           (proposal_id, project_id, proposal_type, node_id, extraction_run_id, payload_json,
            payload_hash, status, created_by, created_at)
           VALUES (?, ?, 'node', ?, ?, ?, ?, 'pending', ?, ?)""",
        (proposal_id, project_id, node_id, extraction_run_id, _json(payload), payload_hash, created_by or owner_id, _now()),
    )
    _insert_provenance(conn, project_id, proposal_id, sources, node_id=node_id)
    audit.record(conn, project_id, "graph_node_proposed", "graph_proposal", proposal_id, created_by or owner_id, {"node_id": node_id})
    conn.commit()
    return _proposal_row(conn, proposal_id)  # type: ignore[return-value]


def create_edge_proposal(
    conn: Any,
    project_id: str,
    subject_node_id: str,
    predicate: str,
    object_node_id: str,
    *,
    owner_id: str,
    properties: dict[str, Any] | None = None,
    confidence: float | None = None,
    extraction_run_id: str | None = None,
    provenance: list[dict[str, Any]] | None = None,
    created_by: str | None = None,
) -> dict[str, Any]:
    """Create an idempotent pending relationship proposal."""
    _require_access(conn, project_id, owner_id)
    if not predicate.strip():
        raise ValueError("predicate must not be empty")
    if subject_node_id == object_node_id:
        raise ValueError("self-referential graph edges are not allowed")
    sources = _require_provenance(provenance)
    nodes = conn.execute(
        """SELECT node_id FROM graph_nodes
           WHERE project_id = ? AND node_id IN (?, ?)""",
        (project_id, subject_node_id, object_node_id),
    ).fetchall()
    if len(nodes) != 2:
        raise ValueError("both edge endpoints must belong to the project")
    payload = {
        "subject_node_id": subject_node_id, "predicate": predicate.strip(), "object_node_id": object_node_id,
        "properties": properties or {}, "confidence": confidence,
    }
    payload_hash = _hash(payload)
    existing = conn.execute(
        """SELECT edge_id FROM graph_edges
           WHERE project_id = ? AND subject_node_id = ? AND predicate = ? AND object_node_id = ?""",
        (project_id, subject_node_id, predicate.strip(), object_node_id),
    ).fetchone()
    edge_id = existing[0] if existing else str(uuid.uuid4())
    if existing is None:
        conn.execute(
            """INSERT INTO graph_edges
               (edge_id, project_id, subject_node_id, predicate, object_node_id, properties_json,
                confidence, status, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (edge_id, project_id, subject_node_id, predicate.strip(), object_node_id,
             _json(properties or {}), confidence, created_by or owner_id, _now()),
        )
    duplicate = conn.execute(
        """SELECT proposal_id FROM graph_proposals
           WHERE project_id = ? AND proposal_type = 'edge' AND edge_id = ? AND payload_hash = ?
           AND status IN ('pending', 'accepted', 'edited')""",
        (project_id, edge_id, payload_hash),
    ).fetchone()
    if duplicate:
        result = _proposal_row(conn, duplicate[0])
        assert result is not None
        return result
    proposal_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO graph_proposals
           (proposal_id, project_id, proposal_type, edge_id, extraction_run_id, payload_json,
            payload_hash, status, created_by, created_at)
           VALUES (?, ?, 'edge', ?, ?, ?, ?, 'pending', ?, ?)""",
        (proposal_id, project_id, edge_id, extraction_run_id, _json(payload), payload_hash, created_by or owner_id, _now()),
    )
    _insert_provenance(conn, project_id, proposal_id, sources, edge_id=edge_id)
    audit.record(conn, project_id, "graph_edge_proposed", "graph_proposal", proposal_id, created_by or owner_id, {"edge_id": edge_id})
    conn.commit()
    return _proposal_row(conn, proposal_id)  # type: ignore[return-value]


def list_proposals(conn: Any, project_id: str, *, owner_id: str, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    _require_access(conn, project_id, owner_id)
    if status is not None and status not in PROPOSAL_STATES:
        raise ValueError("invalid proposal status")
    where = "project_id = ?" + (" AND status = ?" if status else "")
    params: tuple[Any, ...] = (project_id,) + ((status,) if status else ())
    rows = conn.execute(
        f"SELECT proposal_id FROM graph_proposals WHERE {where} ORDER BY created_at DESC LIMIT ?",
        params + (min(max(limit, 1), 500),),
    ).fetchall()
    proposals = []
    for row in rows:
        proposal = _proposal_row(conn, row[0])
        if proposal is not None:
            proposals.append(proposal)
    return proposals


def review_proposal(conn: Any, proposal_id: str, *, owner_id: str, decision: str, reviewer_role: str, reason: str = "", edited_value: dict[str, Any] | None = None) -> dict[str, Any]:
    """Append a review and update the pending projection with enforced gates."""
    if decision not in REVIEW_DECISIONS:
        raise ValueError("invalid review decision")
    proposal = _proposal_row(conn, proposal_id)
    if proposal is None:
        raise ValueError("graph proposal not found")
    _require_access(conn, proposal["project_id"], owner_id)
    if proposal["status"] not in {"pending", "deferred", "edited"}:
        raise ValueError("proposal is not reviewable")
    if decision == "accepted" and proposal["proposal_type"] == "edge":
        row = conn.execute(
            """SELECT subject_node_id, object_node_id FROM graph_edges
               WHERE project_id = ? AND edge_id = ?""",
            (proposal["project_id"], proposal["edge_id"]),
        ).fetchone()
        if row is None or any(
            conn.execute("SELECT status FROM graph_nodes WHERE project_id = ? AND node_id = ?", (proposal["project_id"], node_id)).fetchone()[0] != "accepted"
            for node_id in row
        ):
            raise ValueError("edge endpoints must be accepted before accepting the edge")
    new_status = "edited" if decision == "edited" else decision
    decision_payload = {"proposal_id": proposal_id, "decision": decision, "edited_value": edited_value or {}, "reason": reason}
    created_at = _now()
    conn.execute(
        """INSERT INTO graph_reviews
           (review_id, project_id, proposal_id, decision, edited_value_json, reviewer_id,
            reviewer_role, reason, decision_hash, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (str(uuid.uuid4()), proposal["project_id"], proposal_id, decision, _json(edited_value or {}), owner_id,
         reviewer_role, reason.strip(), _hash(decision_payload), created_at),
    )
    conn.execute("UPDATE graph_proposals SET status = ? WHERE proposal_id = ?", (new_status, proposal_id))
    entity_table = "graph_nodes" if proposal["proposal_type"] == "node" else "graph_edges"
    entity_id = proposal["node_id"] or proposal["edge_id"]
    conn.execute(f"UPDATE {entity_table} SET status = ? WHERE project_id = ? AND {('node_id' if entity_table == 'graph_nodes' else 'edge_id')} = ?", (new_status, proposal["project_id"], entity_id))
    if decision in {"accepted", "edited"}:
        key = f"graph:{proposal['project_id']}:{proposal_id}:{proposal['payload_hash']}"
        conn.execute(
            """INSERT OR IGNORE INTO graph_index_outbox
               (outbox_id, project_id, proposal_id, entity_type, entity_id, target, state, idempotency_key, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'neon_pgvector', 'pending_index', ?, ?, ?)""",
            (str(uuid.uuid4()), proposal["project_id"], proposal_id, proposal["proposal_type"], entity_id, key, created_at, created_at),
        )
    audit.record(conn, proposal["project_id"], f"graph_proposal_{decision}", "graph_proposal", proposal_id, owner_id, decision_payload)
    conn.commit()
    return {"proposal_id": proposal_id, "status": new_status, "decision": decision, "entity_id": entity_id}


def expand_node(conn: Any, project_id: str, node_id: str, *, owner_id: str, limit: int = 50) -> dict[str, Any]:
    _require_access(conn, project_id, owner_id)
    node = conn.execute(
        """SELECT node_id, node_type, canonical_name, namespace, external_id, properties_json, status
           FROM graph_nodes WHERE project_id = ? AND node_id = ?""",
        (project_id, node_id),
    ).fetchone()
    if node is None:
        raise ValueError("graph node not found")
    edges = conn.execute(
        """SELECT edge_id, subject_node_id, predicate, object_node_id, properties_json, confidence
           FROM graph_edges WHERE project_id = ? AND status IN ('accepted', 'edited')
           AND (subject_node_id = ? OR object_node_id = ?) ORDER BY created_at LIMIT ?""",
        (project_id, node_id, node_id, min(max(limit, 1), 100)),
    ).fetchall()
    return {
        "node": {"node_id": node[0], "node_type": node[1], "canonical_name": node[2], "namespace": node[3], "external_id": node[4], "properties": json.loads(node[5]), "status": node[6]},
        "edges": [{"edge_id": row[0], "subject_node_id": row[1], "predicate": row[2], "object_node_id": row[3], "properties": json.loads(row[4]), "confidence": row[5]} for row in edges],
    }


def trace_entity(conn: Any, project_id: str, entity_id: str, *, owner_id: str) -> list[dict[str, Any]]:
    _require_access(conn, project_id, owner_id)
    rows = conn.execute(
        """SELECT provenance_id, proposal_id, node_id, edge_id, retrieval_id, parser_run_id, block_id,
                  page_number, locator_json, content_hash, excerpt, parser_version, extractor_version
           FROM graph_provenance WHERE project_id = ? AND (node_id = ? OR edge_id = ? OR proposal_id = ?)
           ORDER BY created_at""",
        (project_id, entity_id, entity_id, entity_id),
    ).fetchall()
    return [{"provenance_id": row[0], "proposal_id": row[1], "node_id": row[2], "edge_id": row[3], "retrieval_id": row[4], "parser_run_id": row[5], "block_id": row[6], "page_number": row[7], "locator": json.loads(row[8]), "content_hash": row[9], "excerpt": row[10], "parser_version": row[11], "extractor_version": row[12]} for row in rows]


def accepted_fingerprint(conn: Any, project_id: str, *, owner_id: str) -> str:
    _require_access(conn, project_id, owner_id)
    nodes = conn.execute(
        """SELECT 'node', node_id, node_type, canonical_name, status, properties_json
           FROM graph_nodes WHERE project_id = ? AND status IN ('accepted', 'edited')
           ORDER BY node_id""", (project_id,)
    ).fetchall()
    edges = conn.execute(
        """SELECT 'edge', edge_id, subject_node_id, predicate, object_node_id, status, properties_json
           FROM graph_edges WHERE project_id = ? AND status IN ('accepted', 'edited')
           ORDER BY edge_id""", (project_id,)
    ).fetchall()
    provenance = conn.execute(
        """SELECT COALESCE(node_id, edge_id), content_hash, parser_version, extractor_version
           FROM graph_provenance WHERE project_id = ? AND (node_id IS NOT NULL OR edge_id IS NOT NULL)
           ORDER BY COALESCE(node_id, edge_id), provenance_id""", (project_id,)
    ).fetchall()
    return _hash({
        "project_id": project_id, "graph_version": "v2", "ontology_version": "graph-v1",
        "records": [list(row) for row in nodes] + [list(row) for row in edges],
        "provenance": [list(row) for row in provenance],
    })


def stats(conn: Any, project_id: str, *, owner_id: str) -> dict[str, Any]:
    _require_access(conn, project_id, owner_id)
    counts = {}
    for table in ("graph_nodes", "graph_edges", "graph_proposals"):
        counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE project_id = ?", (project_id,)).fetchone()[0]
    counts["pending_proposals"] = conn.execute("SELECT COUNT(*) FROM graph_proposals WHERE project_id = ? AND status = 'pending'", (project_id,)).fetchone()[0]
    counts["accepted_fingerprint"] = accepted_fingerprint(conn, project_id, owner_id=owner_id)
    return counts


def extract_parser_run(conn: Any, parser_run_id: str, *, owner_id: str) -> dict[str, Any]:
    """Create cited, pending graph proposals from accepted parser blocks.

    This extractor is deliberately deterministic. It proposes entities and a
    small set of explicit relationships; it never changes a proposal to a
    trusted graph state.
    """
    run = conn.execute(
        "SELECT parser_run_id, project_id FROM parser_runs WHERE parser_run_id = ?", (parser_run_id,)
    ).fetchone()
    if run is None or not run[1]:
        raise ValueError("parser run not found")
    project_id = run[1]
    _require_access(conn, project_id, owner_id)
    blocks = conn.execute(
        """SELECT block_id, page_number, section_name, text, content_hash
           FROM parsed_blocks WHERE parser_run_id = ? AND review_state = 'accepted'
           ORDER BY page_number, block_index""",
        (parser_run_id,),
    ).fetchall()
    if not blocks:
        raise ValueError("no accepted parser blocks available for extraction")
    extraction_run_id = str(uuid.uuid4())
    created_at = _now()
    conn.execute(
        """INSERT INTO graph_extraction_runs
           (extraction_run_id, project_id, parser_run_id, extractor_type, extractor_version,
            input_block_hashes, output_schema_version, status, created_at)
           VALUES (?, ?, ?, 'deterministic', ?, ?, 'graph-v1', 'pending', ?)""",
        (extraction_run_id, project_id, parser_run_id, _EXTRACTOR_VERSION, _json([row[4] for row in blocks]), created_at),
    )
    source_by_token: dict[str, list[dict[str, Any]]] = {}
    node_ids: dict[tuple[str, str], str] = {}
    proposals = 0
    edges = 0
    try:
        for block_id, page_number, section_name, text, content_hash in blocks:
            provenance = [{
                "block_id": block_id, "parser_run_id": parser_run_id, "page_number": page_number,
                "content_hash": content_hash, "excerpt": text[:500], "locator": {"section": section_name},
                "extractor_version": _EXTRACTOR_VERSION,
            }]
            tokens = set(re.findall(r"\b[A-Z][A-Z0-9-]{1,9}\b", text))
            tokens -= _STOP_SYMBOLS
            for token in sorted(tokens):
                key = ("gene", _normalize_name(token))
                if key not in node_ids:
                    proposal = create_node_proposal(
                        conn, project_id, "gene", token, owner_id=owner_id,
                        namespace="symbol", properties={"extraction": "uppercase_symbol"},
                        extraction_run_id=extraction_run_id, provenance=provenance, created_by=owner_id,
                    )
                    node_ids[key] = proposal["node_id"]
                    proposals += 1
                source_by_token.setdefault(token, []).append({"provenance": provenance, "block": block_id})
            for pathway in re.findall(r"\b([A-Za-z0-9-]+(?:/[A-Za-z0-9-]+){1,2})\s+pathway\b", text, flags=re.IGNORECASE):
                key = ("pathway", _normalize_name(pathway + " pathway"))
                if key not in node_ids:
                    proposal = create_node_proposal(
                        conn, project_id, "pathway", pathway + " pathway", owner_id=owner_id,
                        namespace="text", properties={"extraction": "pathway_phrase"},
                        extraction_run_id=extraction_run_id, provenance=provenance, created_by=owner_id,
                    )
                    node_ids[key] = proposal["node_id"]
                    proposals += 1
            for subject, relation, object_name in re.findall(
                r"\b([A-Z][A-Z0-9-]{1,9})\s+(activates|inhibits|interacts with|associated with)\s+([A-Z][A-Z0-9-]{1,9})\b",
                text,
            ):
                if subject in _STOP_SYMBOLS or object_name in _STOP_SYMBOLS:
                    continue
                subject_id = node_ids.get(("gene", _normalize_name(subject)))
                object_id = node_ids.get(("gene", _normalize_name(object_name)))
                if not subject_id or not object_id or subject_id == object_id:
                    continue
                create_edge_proposal(
                    conn, project_id, subject_id, relation.replace(" ", "_"), object_id,
                    owner_id=owner_id, properties={"extraction": "explicit_relation"},
                    extraction_run_id=extraction_run_id, provenance=provenance, created_by=owner_id,
                )
                edges += 1
        conn.execute("UPDATE graph_extraction_runs SET status = 'completed' WHERE extraction_run_id = ?", (extraction_run_id,))
        audit.record(conn, project_id, "graph_extraction_completed", "graph_extraction_run", extraction_run_id, owner_id, {"proposals": proposals, "edges": edges})
        conn.commit()
    except Exception as exc:
        conn.execute("UPDATE graph_extraction_runs SET status = 'failed', failure_reason = ? WHERE extraction_run_id = ?", (str(exc), extraction_run_id))
        conn.commit()
        raise
    return {"extraction_run_id": extraction_run_id, "parser_run_id": parser_run_id, "status": "completed", "node_proposals": proposals, "edge_proposals": edges, "block_count": len(blocks)}


def query_context(conn: Any, project_id: str, question: str, *, owner_id: str, limit: int = 8) -> list[dict[str, Any]]:
    """Return accepted graph facts lexically relevant to a project question."""
    _require_access(conn, project_id, owner_id)
    terms = set(re.findall(r"[a-z0-9-]+", question.casefold()))
    nodes = conn.execute(
        """SELECT node_id, node_type, canonical_name, external_id, properties_json
           FROM graph_nodes WHERE project_id = ? AND status IN ('accepted', 'edited')""", (project_id,)
    ).fetchall()
    relevant = {row[0]: row for row in nodes if terms & set(re.findall(r"[a-z0-9-]+", row[2].casefold()))}
    if not relevant:
        return []
    edges = conn.execute(
        """SELECT edge_id, subject_node_id, predicate, object_node_id, properties_json, confidence
           FROM graph_edges WHERE project_id = ? AND status IN ('accepted', 'edited')
           AND (subject_node_id IN ({}) OR object_node_id IN ({})) LIMIT ?""".format(
               ",".join("?" for _ in relevant), ",".join("?" for _ in relevant)
           ),
        (project_id, *relevant, *relevant, min(max(limit, 1), 50)),
    ).fetchall()
    facts = []
    for edge_id, subject_id, predicate, object_id, properties_json, confidence in edges:
        subject = relevant.get(subject_id) or next((row for row in nodes if row[0] == subject_id), None)
        object_node = relevant.get(object_id) or next((row for row in nodes if row[0] == object_id), None)
        if subject is None or object_node is None:
            continue
        facts.append({"edge_id": edge_id, "subject": subject[2], "predicate": predicate, "object": object_node[2], "confidence": confidence, "properties": json.loads(properties_json)})
    return facts
