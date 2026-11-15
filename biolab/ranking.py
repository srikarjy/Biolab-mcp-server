"""Deterministic, explainable ranking over preserved retrieval snapshots."""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

ALGORITHM_VERSION = "ranking-v1"
SCORE_WEIGHTS = {
    "query_fit": 0.45,
    "entity_fit": 0.20,
    "study_design_fit": 0.15,
    "recency_fit": 0.10,
    "completeness": 0.10,
}
REVIEW_STATES = {"unreviewed", "accepted", "rejected", "conflicting"}
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_'-]*")
DESIGN_TERMS = {
    "randomized": {"randomized", "randomised", "controlled trial", "rct"},
    "systematic_review": {"systematic review", "meta-analysis", "metaanalysis"},
    "cohort": {"cohort", "longitudinal"},
    "case_control": {"case-control", "case control"},
    "review": {"review"},
    "preprint": {"preprint", "biorxiv", "medrxiv"},
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _tokens(value: str) -> set[str]:
    return {token.lower() for token in TOKEN_RE.findall(value) if len(token) > 1}


def _text(snapshot: dict[str, Any], *keys: str) -> str:
    values: list[str] = []
    for key in keys:
        value = snapshot.get(key, "")
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            values.extend(str(item) for item in value)
        elif isinstance(value, dict):
            values.extend(str(item) for item in value.values())
    return " ".join(values)


def extract_query_facets(question: str) -> dict[str, Any]:
    """Extract deterministic facets used for explanation, not truth assessment."""
    normalized = question.lower()
    requested_designs = [
        name for name, terms in DESIGN_TERMS.items() if any(term in normalized for term in terms)
    ]
    return {
        "terms": sorted(_tokens(question)),
        "requested_designs": requested_designs,
    }


def _query_fit(question: str, snapshot: dict[str, Any]) -> tuple[float, list[str], list[str]]:
    query_terms = _tokens(question)
    title_terms = _tokens(str(snapshot.get("title", "")))
    abstract_terms = _tokens(str(snapshot.get("abstract", "")))
    subject_terms = _tokens(_text(snapshot, "keywords", "mesh_terms", "conditions", "interventions"))
    if not query_terms:
        return 0.0, [], []
    matched = query_terms & (title_terms | abstract_terms | subject_terms)
    coverage = len(matched) / len(query_terms)
    title_coverage = len(query_terms & title_terms) / len(query_terms)
    phrase = question.strip().lower() in str(snapshot.get("title", "")).lower()
    score = min(1.0, 0.55 * coverage + 0.35 * title_coverage + (0.10 if phrase else 0.0))
    reasons = []
    if matched:
        reasons.append("query_terms_match")
    if title_coverage:
        reasons.append("title_match")
    if phrase:
        reasons.append("exact_title_phrase")
    return score, sorted(matched), reasons


def _entity_fit(question: str, snapshot: dict[str, Any]) -> tuple[float, list[str], list[str]]:
    query_terms = _tokens(question)
    entity_fields = (
        "entities", "gene_symbols", "genes", "diseases", "drugs", "organisms",
        "gene_names", "accession", "entry_id", "conditions", "interventions",
        "mesh_terms", "keywords",
    )
    available = _tokens(_text(snapshot, *entity_fields))
    if not available:
        return 0.5, [], ["entity_metadata_unavailable"]
    matched = query_terms & available
    score = len(matched) / len(query_terms) if query_terms else 0.0
    reasons = ["entity_match"] if matched else ["no_entity_match"]
    return score, sorted(matched), reasons


def _study_design_fit(question: str, snapshot: dict[str, Any]) -> tuple[float, list[str], list[str]]:
    facets = extract_query_facets(question)
    requested = facets["requested_designs"]
    if not requested:
        return 0.5, [], ["study_design_not_requested"]
    text = _text(snapshot, "title", "abstract", "publication_types", "study_type", "phase").lower()
    matched = [design for design in requested if any(term in text for term in DESIGN_TERMS[design])]
    score = len(matched) / len(requested)
    reasons = [f"study_design_{design}" for design in matched]
    return score, matched, reasons or ["study_design_not_matched"]


def _date_from_snapshot(snapshot: dict[str, Any]) -> datetime | None:
    raw = _text(snapshot, "publication_date", "published_at", "date")
    journal = snapshot.get("journal")
    if isinstance(journal, dict):
        raw = f"{raw} {journal.get('pub_date', '')}"
    match = re.search(r"\b(19|20)\d{2}(?:[-/]\d{1,2})?(?:[-/]\d{1,2})?\b", raw)
    if not match:
        return None
    parts = re.split(r"[-/]", match.group(0))
    year, month, day = int(parts[0]), int(parts[1]) if len(parts) > 1 else 6, int(parts[2]) if len(parts) > 2 else 15
    try:
        return datetime(year, month, day, tzinfo=UTC)
    except ValueError:
        return None


def _recency_fit(snapshot: dict[str, Any], as_of: datetime) -> tuple[float, list[str]]:
    published = _date_from_snapshot(snapshot)
    if published is None or published > as_of:
        return 0.5, ["publication_date_unavailable"]
    age_years = max(0.0, (as_of - published).days / 365.25)
    score = max(0.0, min(1.0, 1.0 - age_years / 10.0))
    return score, ["recent_publication"] if score >= 0.5 else ["older_publication"]


def _completeness(snapshot: dict[str, Any], external_id: str) -> tuple[float, list[str]]:
    fields = {
        "title": bool(str(snapshot.get("title", "")).strip()),
        "abstract": bool(str(snapshot.get("abstract", "")).strip()),
        "identifier": bool(external_id.strip()),
        "source_metadata": bool(snapshot.get("journal") or snapshot.get("publication_types")),
    }
    return sum(fields.values()) / len(fields), [f"missing_{name}" for name, present in fields.items() if not present]


def score_snapshot(
    question: str,
    snapshot: dict[str, Any],
    *,
    external_id: str,
    as_of: datetime,
) -> dict[str, Any]:
    """Score one snapshot and return reconstructable explanation data."""
    query_score, query_matches, query_reasons = _query_fit(question, snapshot)
    entity_score, entity_matches, entity_reasons = _entity_fit(question, snapshot)
    design_score, design_matches, design_reasons = _study_design_fit(question, snapshot)
    recency_score, recency_reasons = _recency_fit(snapshot, as_of)
    completeness_score, completeness_missing = _completeness(snapshot, external_id)
    components = {
        "query_fit": round(query_score, 6),
        "entity_fit": round(entity_score, 6),
        "study_design_fit": round(design_score, 6),
        "recency_fit": round(recency_score, 6),
        "completeness": round(completeness_score, 6),
    }
    total = sum(SCORE_WEIGHTS[name] * value for name, value in components.items())
    reasons = sorted(set(query_reasons + entity_reasons + design_reasons + recency_reasons))
    return {
        "score_total": round(total, 6),
        "score_components": components,
        "reason_codes": reasons,
        "matched_terms": sorted(set(query_matches + entity_matches + design_matches)),
        "missing_facets": sorted(set(completeness_missing)),
    }


def rank_retrievals(
    conn: Any,
    question: str,
    retrieval_ids: list[str],
    *,
    created_by: str,
    project_id: str | None = None,
    requested_limit: int = 5,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Persist a deterministic ranking run over existing retrievals."""
    if not question.strip():
        raise ValueError("question must not be empty")
    if not created_by.strip():
        raise ValueError("created_by must not be empty")
    if not retrieval_ids:
        raise ValueError("at least one retrieval_id is required")
    if not 1 <= requested_limit <= 50:
        raise ValueError("requested_limit must be between 1 and 50")
    unique_ids = list(dict.fromkeys(retrieval_ids))
    placeholders = ",".join("?" for _ in unique_ids)
    rows = conn.execute(
        f"SELECT retrieval_id, source, external_id, snapshot FROM retrievals WHERE retrieval_id IN ({placeholders})",
        unique_ids,
    ).fetchall()
    found = {row[0] for row in rows}
    missing = sorted(set(unique_ids) - found)
    if missing:
        raise ValueError(f"retrievals not found: {', '.join(missing)}")
    if project_id and conn.execute("SELECT 1 FROM projects WHERE project_id = ?", (project_id,)).fetchone() is None:
        raise ValueError("project not found")
    as_of_dt = datetime.fromisoformat(as_of) if as_of else datetime.now(UTC)
    if as_of_dt.tzinfo is None:
        as_of_dt = as_of_dt.replace(tzinfo=UTC)
    run_id = str(uuid.uuid4())
    created_at = _now()
    candidates = []
    for retrieval_id, source, external_id, snapshot_json in rows:
        snapshot = json.loads(snapshot_json)
        score = score_snapshot(question, snapshot, external_id=external_id, as_of=as_of_dt)
        candidates.append({"retrieval_id": retrieval_id, "source": source, "external_id": external_id, **score})
    candidates.sort(key=lambda item: (-item["score_total"], item["source"], item["external_id"], item["retrieval_id"]))
    selected = candidates[:requested_limit]
    conn.execute(
        """INSERT INTO ranking_runs
           (ranking_run_id, project_id, question, query_facets, algorithm_version,
            requested_limit, as_of, status, created_by, created_at, completed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'succeeded', ?, ?, ?)""",
        (run_id, project_id, question.strip(), json.dumps(extract_query_facets(question), sort_keys=True),
         ALGORITHM_VERSION, requested_limit, as_of_dt.isoformat(), created_by.strip(), created_at, _now()),
    )
    for rank, candidate in enumerate(selected, start=1):
        conn.execute(
            """INSERT INTO ranking_candidates
               (ranking_candidate_id, ranking_run_id, retrieval_id, rank, score_total,
                score_components, reason_codes, matched_terms, missing_facets)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), run_id, candidate["retrieval_id"], rank, candidate["score_total"],
             json.dumps(candidate["score_components"], sort_keys=True), json.dumps(candidate["reason_codes"]),
             json.dumps(candidate["matched_terms"]), json.dumps(candidate["missing_facets"])),
        )
    conn.commit()
    return {"ranking_run_id": run_id, "algorithm_version": ALGORITHM_VERSION,
            "question": question.strip(), "requested_limit": requested_limit,
            "candidate_count": len(selected), "candidates": selected}


def get_ranking(conn: Any, ranking_run_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT ranking_run_id, project_id, question, query_facets, algorithm_version, requested_limit, as_of, status, created_by, created_at, completed_at FROM ranking_runs WHERE ranking_run_id = ?",
        (ranking_run_id,),
    ).fetchone()
    if row is None:
        return None
    candidates = conn.execute(
        "SELECT ranking_candidate_id, retrieval_id, rank, score_total, score_components, reason_codes, matched_terms, missing_facets, review_state, review_note, reviewed_by, reviewed_at FROM ranking_candidates WHERE ranking_run_id = ? ORDER BY rank",
        (ranking_run_id,),
    ).fetchall()
    return {
        "ranking_run_id": row[0], "project_id": row[1], "question": row[2],
        "query_facets": json.loads(row[3]), "algorithm_version": row[4],
        "requested_limit": row[5], "as_of": row[6], "status": row[7],
        "created_by": row[8], "created_at": row[9], "completed_at": row[10],
        "candidates": [{
            "ranking_candidate_id": item[0], "retrieval_id": item[1], "rank": item[2],
            "score_total": item[3], "score_components": json.loads(item[4]),
            "reason_codes": json.loads(item[5]), "matched_terms": json.loads(item[6]),
            "missing_facets": json.loads(item[7]), "review_state": item[8],
            "review_note": item[9], "reviewed_by": item[10], "reviewed_at": item[11],
        } for item in candidates],
    }
