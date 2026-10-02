"""Live tests against the real bioRxiv details API — no fixtures, by design. See
test_pubmed_client.py for why this project hits the network on every run."""

import pytest
from biolab.biorxiv_client import list_and_fetch, paper_to_retrieval_input

pytestmark = pytest.mark.live


def test_list_and_fetch_returns_real_preprints():
    preprints = list_and_fetch("biorxiv", "neuroscience", 2)
    assert len(preprints) == 2
    for preprint in preprints:
        assert preprint.title
        assert preprint.category.lower() == "neuroscience"


def test_paper_to_retrieval_input_shapes_a_valid_record():
    preprints = list_and_fetch("biorxiv", "neuroscience", 1)
    retrieval_input = paper_to_retrieval_input(preprints[0], "biorxiv")

    assert retrieval_input["source"] == "biorxiv"
    assert retrieval_input["external_id"]
    assert retrieval_input["snapshot"]["title"] == preprints[0].title
    assert retrieval_input["source_metadata"]["pub_status"].startswith("biorxiv:")
