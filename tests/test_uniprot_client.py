"""Offline contract tests for the UniProt connector."""

import json

from biolab import uniprot_client


class Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.payload


def test_search_and_fetch_preserves_full_entry(monkeypatch):
    search_payload = {"results": [{"primaryAccession": "P01116"}]}
    entry_payload = {
        "entryType": "UniProtKB reviewed (Swiss-Prot)",
        "primaryAccession": "P01116",
        "uniProtkbId": "RASK_HUMAN",
        "organism": {"scientificName": "Homo sapiens", "taxonId": 9606},
        "proteinDescription": {"recommendedName": {"fullName": {"value": "GTPase KRas"}}},
        "genes": [{"geneName": {"value": "KRAS"}, "synonyms": [{"value": "KRAS2"}]}],
        "sequence": {"length": 189},
    }
    responses = iter([Response(search_payload), Response(entry_payload)])
    monkeypatch.setattr(uniprot_client, "urlopen", lambda *_args, **_kwargs: next(responses))

    entries = uniprot_client.search_and_fetch("gene:KRAS AND organism_id:9606", 1)
    shaped = uniprot_client.entry_to_retrieval_input(entries[0])

    assert entries[0].accession == "P01116"
    assert entries[0].gene_names == ["KRAS", "KRAS2"]
    assert shaped["source"] == "uniprot"
    assert shaped["snapshot"]["sequence_length"] == 189
    assert json.loads(shaped["raw_response"])["primaryAccession"] == "P01116"
