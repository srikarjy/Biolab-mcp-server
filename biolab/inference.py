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

from biolab import deepseek_client, rag

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


def _deepseek_inference(
    conn: Any, question: str, rows: list[tuple[Any, ...]], *, created_by: str,
    project_id: str | None, ranking_run_id: str | None, max_insights: int,
    missing: list[str],
) -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    evidence = [
        {"retrieval_id": row[0], "source": row[1], "external_id": row[2], "snapshot": json.loads(row[3])}
        for row in rows
    ]
    context, rag_status = rag.retrieve_context(question, run_id, evidence, max_insights)
    generated = deepseek_client.generate_grounded_claims(question, context, max_insights)
    created_at = _now()
    conn.execute(
        """INSERT INTO inference_runs
           (inference_run_id, project_id, ranking_run_id, question, parser_version,
            model_provider, model_name, status, created_by, created_at, completed_at)
           VALUES (?, ?, ?, ?, 'deepseek-grounded-v1', ?, ?, 'succeeded', ?, ?, ?)""",
        (run_id, project_id, ranking_run_id, question.strip(), generated["model_provider"], generated["model_name"], created_by.strip(), created_at, _now()),
    )
    insights: list[dict[str, Any]] = []
    by_id = {item["retrieval_id"]: item for item in evidence}
    for generated_claim in generated["insights"]:
        if not isinstance(generated_claim, dict) or not isinstance(generated_claim.get("text"), str):
            continue
        valid: list[dict[str, Any]] = []
        for citation in generated_claim.get("citations", []):
            if not isinstance(citation, dict) or citation.get("retrieval_id") not in by_id:
                continue
            item = by_id[citation["retrieval_id"]]
            field_name = citation.get("field_name", "abstract")
            source_text = str(item["snapshot"].get(field_name, "")) if field_name in {"title", "abstract"} else ""
            excerpt = str(citation.get("excerpt") or citation.get("quote") or "")
            start = source_text.find(excerpt) if excerpt else -1
            if start < 0:
                try:
                    start = int(citation.get("span_start", -1))
                    end = int(citation.get("span_end", -1))
                except (TypeError, ValueError):
                    continue
                excerpt = source_text[start:end] if 0 <= start < end <= len(source_text) else ""
            else:
                end = start + len(excerpt)
            if not excerpt:
                continue
            valid.append({"item": item, "field_name": field_name, "start": start, "end": end, "excerpt": excerpt})
        if not valid:
            continue
        claim_id = str(uuid.uuid4())
        conn.execute("INSERT INTO inference_claims (claim_id, inference_run_id, claim_text, created_at) VALUES (?, ?, ?, ?)", (claim_id, run_id, generated_claim["text"].strip(), created_at))
        citations: list[dict[str, Any]] = []
        for citation in valid:
            item = citation["item"]
            citation_id = _citation_id(run_id, item["retrieval_id"], citation["field_name"], str(citation["start"]), str(citation["end"]))
            excerpt_hash = hashlib.sha256(citation["excerpt"].encode("utf-8")).hexdigest()
            conn.execute(
                """INSERT INTO claim_citations
                   (citation_id, claim_id, retrieval_id, field_name, span_start, span_end, excerpt_hash, locator, source, external_id, excerpt_text)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (citation_id, claim_id, item["retrieval_id"], citation["field_name"], citation["start"], citation["end"], excerpt_hash, f"{item['source']}:{item['external_id']}", item["source"], item["external_id"], citation["excerpt"]),
            )
            citations.append({"citation_id": citation_id, "retrieval_id": item["retrieval_id"], "source": item["source"], "external_id": item["external_id"], "field_name": citation["field_name"], "span_start": citation["start"], "span_end": citation["end"], "excerpt_hash": excerpt_hash, "excerpt": citation["excerpt"], "locator": f"{item['source']}:{item['external_id']}"})
        insights.append({"claim_id": claim_id, "text": generated_claim["text"].strip(), "citations": citations})
    conn.commit()
    return {"inference_run_id": run_id, "question": question.strip(), "parser_version": "deepseek-grounded-v1", "model_provider": generated["model_provider"], "model_name": generated["model_name"], "status": "succeeded", "durability": "database" if not missing else "request-context", "rag": rag_status, "insight_count": len(insights), "insights": insights, "parser_chunk_count": 0}


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

    if deepseek_client.configured():
        return _deepseek_inference(
            conn, question, rows, created_by=created_by, project_id=project_id,
            ranking_run_id=ranking_run_id, max_insights=max_insights, missing=missing,
        )

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
    vector_count = rag.flush_context(inference_run_id)
    return {"inference_run_id": inference_run_id, "flushed_parser_chunks": count, "flushed_vector_chunks": vector_count, "claims_preserved": True}
