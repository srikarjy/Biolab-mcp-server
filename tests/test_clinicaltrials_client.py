"""Live tests against the real ClinicalTrials.gov API v2 — no fixtures, by design. See
test_pubmed_client.py for why this project hits the network on every run."""

import pytest
from biolab.clinicaltrials_client import paper_to_retrieval_input, search_and_fetch

pytestmark = pytest.mark.live


def test_search_and_fetch_returns_real_studies():
    studies = search_and_fetch("pancreatic cancer", 2)
    assert len(studies) == 2
    for study in studies:
        assert study.nct_id.startswith("NCT")
        assert study.brief_title
        assert study.full_json


def test_paper_to_retrieval_input_shapes_a_valid_record():
    studies = search_and_fetch("pancreatic cancer", 1)
    retrieval_input = paper_to_retrieval_input(studies[0])

    assert retrieval_input["source"] == "clinicaltrials"
    assert retrieval_input["external_id"] == studies[0].nct_id
    assert retrieval_input["snapshot"]["title"] == studies[0].brief_title
    assert retrieval_input["raw_response"] == studies[0].full_json
