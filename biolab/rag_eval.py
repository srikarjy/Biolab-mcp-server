"""Small, deterministic evaluation gate for project-scoped RAG retrieval.

The evaluator deliberately accepts a search function instead of reaching into
the vector backend. This keeps the gate usable with a local fixture corpus,
pgvector, or a future hybrid retriever while applying the same safety checks.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any


def evaluate_retrieval(
    cases: Iterable[dict[str, Any]],
    search: Callable[[dict[str, Any]], list[dict[str, Any]]],
    ks: tuple[int, ...] = (1, 3, 5),
) -> dict[str, Any]:
    """Evaluate ranked hits against known-answer fixture cases.

    A case contains ``project_id``, ``question``, ``expected_block_ids`` and
    optionally ``sections``. ``search`` receives the case and returns ranked
    block dictionaries. Safety violations are counted independently from
    relevance so a high recall score cannot hide a data-isolation failure.
    """
    cases = list(cases)
    if not cases:
        raise ValueError("at least one retrieval evaluation case is required")
    if not ks or any(k <= 0 for k in ks):
        raise ValueError("ks must contain positive cutoffs")

    recall_hits = dict.fromkeys(ks, 0)
    mrr_total = 0.0
    isolation_violations = 0
    review_violations = 0
    section_violations = 0
    case_reports: list[dict[str, Any]] = []

    for case in cases:
        expected = set(case.get("expected_block_ids", []))
        if not expected:
            raise ValueError("each case needs expected_block_ids")
        hits = search(case)
        project_id = case.get("project_id")
        sections = set(case.get("sections") or [])
        bad_project = [hit for hit in hits if project_id is not None and hit.get("project_id") != project_id]
        bad_review = [hit for hit in hits if hit.get("review_state", "accepted") != "accepted"]
        bad_section = [hit for hit in hits if sections and hit.get("section_name") not in sections]
        isolation_violations += len(bad_project)
        review_violations += len(bad_review)
        section_violations += len(bad_section)

        ranked_ids = [str(hit.get("block_id")) for hit in hits]
        first_relevant_rank = next((rank for rank, block_id in enumerate(ranked_ids, 1) if block_id in expected), None)
        if first_relevant_rank is not None:
            mrr_total += 1 / first_relevant_rank
        for k in ks:
            if expected.intersection(ranked_ids[:k]):
                recall_hits[k] += 1
        case_reports.append({
            "question": case.get("question", ""),
            "expected_block_ids": sorted(expected),
            "returned_block_ids": ranked_ids,
            "first_relevant_rank": first_relevant_rank,
            "project_isolation_ok": not bad_project,
            "review_isolation_ok": not bad_review,
            "section_filter_ok": not bad_section,
        })

    count = len(cases)
    recall = {f"recall_at_{k}": recall_hits[k] / count for k in ks}
    safety_ok = not (isolation_violations or review_violations or section_violations)
    return {
        "cases": count,
        **recall,
        "mrr": mrr_total / count,
        "project_isolation_violations": isolation_violations,
        "review_isolation_violations": review_violations,
        "section_filter_violations": section_violations,
        "safety_gate_passed": safety_ok,
        "case_reports": case_reports,
    }
