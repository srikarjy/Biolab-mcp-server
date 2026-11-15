"""Citation-safe deterministic inference over preserved retrievals.

This first implementation is intentionally provider-free. It extracts grounded
sentences from preserved title/abstract fields and stores exact citation spans.
It is a parser/inference contract that a model provider can consume later; it
does not pretend that extraction is scientific validation.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

PARSER_VERSION = "grounded-extraction-v1"
SENTENCE_RE = re.compile(r"[^.!?]+(?:[.!?]+|$)")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _tokens(value: str) -> set[str]:
    return {part.lower() for part in re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]*", value) if len(part) > 1}


def _sentences(value: str) -> list[tuple[int, int, str]]:
    return [(match.start(), match.end(), match.group(0).strip()) for match in SENTENCE_RE.finditer(value) if match.group(0).strip()]


def _citation_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def create_inference(  # noqa: PLR0912 - validation, fallback, parsing, and persistence are one atomic operation
    conn: Any,
    question: str,
    retrieval_ids: list[str],
    *,
    created_by: str,
    project_id: str | None = None,
    ranking_run_id: str | None = None,
    max_insights: int = 10,
    evidence_records: list[dict[str, Any]] | None = None,  # noqa: PLR0912
) -> dict[str, Any]:
    """Extract grounded insights and persist exact source spans."""
    if not question.strip():
        raise ValueError("question must not be empty")
    if not created_by.strip():
        raise ValueError("created_by must not be empty")
    if not retrieval_ids:
        raise ValueError("at least one retrieval_id is required")
    if not 1 <= max_insights <= 50:
        raise ValueError("max_insights must be between 1 and 50")
    ids = list(dict.fromkeys(retrieval_ids))
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT retrieval_id, source, external_id, snapshot FROM retrievals WHERE retrieval_id IN ({placeholders})",
        ids,
    ).fetchall()
    found = {row[0] for row in rows}
    missing = sorted(set(ids) - found)
    fallback = {record.get("retrieval_id"): record for record in (evidence_records or []) if isinstance(record, dict)}
    unresolved = [retrieval_id for retrieval_id in missing if retrieval_id not in fallback]
    if unresolved:
        raise ValueError(f"retrievals not found: {', '.join(unresolved)}")
    for retrieval_id in missing:
        record = fallback[retrieval_id]
        snapshot = record.get("snapshot")
        if not isinstance(snapshot, dict) or not record.get("source") or not record.get("external_id"):
            raise ValueError(f"fallback evidence record is incomplete: {retrieval_id}")
        rows.append((retrieval_id, str(record["source"]), str(record["external_id"]), json.dumps(snapshot, sort_keys=True)))
    if project_id and conn.execute("SELECT 1 FROM projects WHERE project_id = ?", (project_id,)).fetchone() is None:
        raise ValueError("project not found")
    if ranking_run_id and conn.execute("SELECT 1 FROM ranking_runs WHERE ranking_run_id = ?", (ranking_run_id,)).fetchone() is None:
        ranking_run_id = None

    run_id = str(uuid.uuid4())
    created_at = _now()
    query_tokens = _tokens(question)
    insights: list[dict[str, Any]] = []
    conn.execute(
        """INSERT INTO inference_runs
           (inference_run_id, project_id, ranking_run_id, question, parser_version,
            model_provider, model_name, status, created_by, created_at, completed_at)
           VALUES (?, ?, ?, ?, ?, 'local', 'deterministic-extractor', 'succeeded', ?, ?, ?)""",
        (run_id, project_id, ranking_run_id, question.strip(), PARSER_VERSION,
         created_by.strip(), created_at, _now()),
    )
    for retrieval_id, source, external_id, snapshot_json in rows:
        snapshot = json.loads(snapshot_json)
        for field_name in ("abstract", "title"):
            text = str(snapshot.get(field_name, ""))
            if not text.strip():
                continue
            for chunk_index, (start, end, sentence) in enumerate(_sentences(text)):
                conn.execute(
                    """INSERT INTO parser_chunks
                       (chunk_id, inference_run_id, retrieval_id, field_name, chunk_index, chunk_text, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (str(uuid.uuid4()), run_id, retrieval_id, field_name, chunk_index, sentence, created_at),
                )
                matched = sorted(query_tokens & _tokens(sentence))
                if not matched or len(insights) >= max_insights:
                    continue
                claim_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO inference_claims (claim_id, inference_run_id, claim_text, created_at) VALUES (?, ?, ?, ?)",
                    (claim_id, run_id, sentence, created_at),
                )
                citation_id = _citation_id(run_id, retrieval_id, field_name, str(start), str(end))
                conn.execute(
                    """INSERT INTO claim_citations
                       (citation_id, claim_id, retrieval_id, field_name, span_start, span_end, excerpt_hash, locator, source, external_id, excerpt_text)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (citation_id, claim_id, retrieval_id, field_name, start, end,
                     hashlib.sha256(sentence.encode("utf-8")).hexdigest(),
                     f"{source}:{external_id}", source, external_id, sentence),
                )
                insights.append({
                    "claim_id": claim_id,
                    "text": sentence,
                    "matched_terms": matched,
                    "citations": [{
                        "citation_id": citation_id, "retrieval_id": retrieval_id,
                        "source": source, "external_id": external_id,
                        "field_name": field_name, "span_start": start, "span_end": end,
                        "excerpt_hash": hashlib.sha256(sentence.encode("utf-8")).hexdigest(),
                        "locator": f"{source}:{external_id}",
                    }],
                })
            # Continue parsing all source fields so the disposable parser
            # workspace is complete even when the insight output is capped.
    conn.commit()
    return {
        "inference_run_id": run_id,
        "question": question.strip(),
        "parser_version": PARSER_VERSION,
        "model_provider": "local",
        "model_name": "deterministic-extractor",
        "status": "succeeded",
        "durability": "database" if not missing else "request-context",
        "non_durable_retrieval_ids": missing,
        "insight_count": len(insights),
        "insights": insights,
        "parser_chunk_count": conn.execute("SELECT count(*) FROM parser_chunks WHERE inference_run_id = ?", (run_id,)).fetchone()[0],
    }


def get_inference(conn: Any, inference_run_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT inference_run_id, project_id, ranking_run_id, question, parser_version, model_provider, model_name, status, created_by, created_at, completed_at FROM inference_runs WHERE inference_run_id = ?",
        (inference_run_id,),
    ).fetchone()
    if row is None:
        return None
    claims = conn.execute(
        """SELECT c.claim_id, c.claim_text, c.review_state, c.created_at,
                  x.citation_id, x.retrieval_id, r.source, r.external_id,
                  x.field_name, x.span_start, x.span_end, x.excerpt_hash, x.locator,
                  x.source, x.external_id, x.excerpt_text,
                  r.snapshot
           FROM inference_claims c
           LEFT JOIN claim_citations x ON x.claim_id = c.claim_id
           LEFT JOIN retrievals r ON r.retrieval_id = x.retrieval_id
           WHERE c.inference_run_id = ? ORDER BY c.rowid, x.citation_id""",
        (inference_run_id,),
    ).fetchall()
    by_claim: dict[str, dict[str, Any]] = {}
    for item in claims:
        claim = by_claim.setdefault(item[0], {
            "claim_id": item[0], "text": item[1], "review_state": item[2],
            "created_at": item[3], "citations": [],
        })
        if item[4]:
            snapshot = json.loads(item[16]) if item[16] else {}
            source_text = str(snapshot.get(item[8], "")) or item[15]
            claim["citations"].append({
                "citation_id": item[4], "retrieval_id": item[5], "source": item[6] or item[13],
                "external_id": item[7] or item[14], "field_name": item[8], "span_start": item[9],
                "span_end": item[10], "excerpt_hash": item[11], "locator": item[12],
                "excerpt": source_text[item[9]:item[10]],
            })
    return {
        "inference_run_id": row[0], "project_id": row[1], "ranking_run_id": row[2],
        "question": row[3], "parser_version": row[4], "model_provider": row[5],
        "model_name": row[6], "status": row[7], "created_by": row[8],
        "created_at": row[9], "completed_at": row[10], "claims": list(by_claim.values()),
        "parser_chunk_count": conn.execute("SELECT count(*) FROM parser_chunks WHERE inference_run_id = ?", (inference_run_id,)).fetchone()[0],
    }


def flush_parser_chunks(conn: Any, inference_run_id: str) -> dict[str, Any]:
    if conn.execute("SELECT 1 FROM inference_runs WHERE inference_run_id = ?", (inference_run_id,)).fetchone() is None:
        raise ValueError("inference run not found")
    count = conn.execute("SELECT count(*) FROM parser_chunks WHERE inference_run_id = ?", (inference_run_id,)).fetchone()[0]
    conn.execute("DELETE FROM parser_chunks WHERE inference_run_id = ?", (inference_run_id,))
    conn.commit()
    return {"inference_run_id": inference_run_id, "flushed_parser_chunks": count, "claims_preserved": True}
