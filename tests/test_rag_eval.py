from biolab.rag_eval import evaluate_retrieval


def test_retrieval_eval_reports_recall_mrr_and_safety_gate():
    cases = [
        {
            "project_id": "p1",
            "question": "What is the assay?",
            "expected_block_ids": ["b1"],
            "sections": ["methods"],
        },
        {
            "project_id": "p1",
            "question": "What was measured?",
            "expected_block_ids": ["b2"],
        },
    ]
    fixture = {
        "What is the assay?": [
            {"block_id": "b1", "project_id": "p1", "review_state": "accepted", "section_name": "methods"},
        ],
        "What was measured?": [
            {"block_id": "b3", "project_id": "p1", "review_state": "accepted", "section_name": "results"},
            {"block_id": "b2", "project_id": "p1", "review_state": "accepted", "section_name": "results"},
        ],
    }

    report = evaluate_retrieval(cases, lambda case: fixture[case["question"]])

    assert report["recall_at_1"] == 0.5
    assert report["recall_at_3"] == 1.0
    assert report["mrr"] == 0.75
    assert report["safety_gate_passed"] is True


def test_retrieval_eval_fails_safety_gate_for_leaks():
    case = {
        "project_id": "p1",
        "question": "known answer",
        "expected_block_ids": ["b1"],
        "sections": ["results"],
    }

    report = evaluate_retrieval(
        [case],
        lambda _case: [
            {"block_id": "b-other", "project_id": "p2", "review_state": "pending", "section_name": "methods"},
            {"block_id": "b1", "project_id": "p1", "review_state": "accepted", "section_name": "results"},
        ],
    )

    assert report["safety_gate_passed"] is False
    assert report["project_isolation_violations"] == 1
    assert report["review_isolation_violations"] == 1
    assert report["section_filter_violations"] == 1
