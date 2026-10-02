"""Offline contract tests for the Open Targets connector."""

import json

import pytest
from biolab import opentargets_client


class Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.payload


def test_search_filters_unsupported_entities_and_shapes_target(monkeypatch):
    payload = {
        "data": {
            "search": {
                "hits": [
                    {"entity": "study", "score": 10, "object": {}},
                    {
                        "entity": "target",
                        "score": 9.5,
                        "object": {
                            "id": "ENSG00000133703",
                            "approvedSymbol": "KRAS",
                            "approvedName": "KRas proto-oncogene",
                        },
                    },
                ]
            }
        }
    }
    monkeypatch.setattr(
        opentargets_client, "urlopen", lambda *_args, **_kwargs: Response(payload)
    )

    hits = opentargets_client.search("KRAS", 1)
    shaped = opentargets_client.hit_to_retrieval_input(hits[0])

    assert hits[0].object_id == "ENSG00000133703"
    assert shaped["source"] == "opentargets"
    assert shaped["snapshot"]["entity"] == "target"
    assert (
        json.loads(shaped["raw_response"])["data"]["search"]["hits"][1]["object"][
            "approvedSymbol"
        ]
        == "KRAS"
    )


def test_graphql_errors_fail_loudly(monkeypatch):
    monkeypatch.setattr(
        opentargets_client,
        "urlopen",
        lambda *_args, **_kwargs: Response({"errors": [{"message": "bad query"}]}),
    )
    with pytest.raises(RuntimeError, match="bad query"):
        opentargets_client.search("KRAS", 1)
