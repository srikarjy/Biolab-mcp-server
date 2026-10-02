"""Thin UniProtKB REST adapter with no database or logging knowledge."""

import json
from dataclasses import dataclass
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

BASE_URL = "https://rest.uniprot.org/uniprotkb"
TIMEOUT_SECONDS = 20


@dataclass
class UniProtEntry:
    accession: str
    entry_id: str
    reviewed: bool
    protein_name: str
    gene_names: list[str]
    organism_name: str
    organism_id: int | None
    sequence_length: int | None
    full_json: str


def _request_json(url: str) -> tuple[dict, str]:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "biolab-mcp/0.3"})
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw), raw


def search(query: str, max_results: int) -> list[str]:
    params = urlencode({"query": query, "fields": "accession", "size": max_results, "format": "json"})
    payload, _ = _request_json(f"{BASE_URL}/search?{params}")
    return [row.get("primaryAccession", "") for row in payload.get("results", []) if row.get("primaryAccession")]


def fetch(accession: str) -> UniProtEntry:
    payload, raw = _request_json(f"{BASE_URL}/{quote(accession)}?format=json")
    protein_description = payload.get("proteinDescription", {})
    recommended = protein_description.get("recommendedName", {})
    protein_name = recommended.get("fullName", {}).get("value", "")
    if not protein_name:
        submitted = protein_description.get("submissionNames") or []
        if submitted:
            protein_name = submitted[0].get("fullName", {}).get("value", "")

    gene_names = []
    for gene in payload.get("genes") or []:
        primary = gene.get("geneName", {}).get("value")
        if primary:
            gene_names.append(primary)
        gene_names.extend(
            synonym["value"] for synonym in gene.get("synonyms") or [] if synonym.get("value")
        )

    organism = payload.get("organism") or {}
    sequence = payload.get("sequence") or {}
    return UniProtEntry(
        accession=payload.get("primaryAccession", accession),
        entry_id=payload.get("uniProtkbId", ""),
        reviewed=payload.get("entryType") == "UniProtKB reviewed (Swiss-Prot)",
        protein_name=protein_name,
        gene_names=gene_names,
        organism_name=organism.get("scientificName", ""),
        organism_id=organism.get("taxonId"),
        sequence_length=sequence.get("length"),
        full_json=raw,
    )


def search_and_fetch(query: str, max_results: int) -> list[UniProtEntry]:
    return [fetch(accession) for accession in search(query, max_results)]


def entry_to_retrieval_input(entry: UniProtEntry) -> dict:
    snapshot = {
        "title": entry.protein_name or entry.entry_id or entry.accession,
        "abstract": "",
        "authors": [],
        "journal": {"title": "UniProtKB"},
        "publication_types": ["reviewed"] if entry.reviewed else ["unreviewed"],
        "doi": "",
        "accession": entry.accession,
        "entry_id": entry.entry_id,
        "gene_names": entry.gene_names,
        "organism": {"name": entry.organism_name, "taxon_id": entry.organism_id},
        "sequence_length": entry.sequence_length,
        "canonical_url": f"https://www.uniprot.org/uniprotkb/{entry.accession}/entry",
    }
    return {
        "source": "uniprot",
        "external_id": entry.accession,
        "source_metadata": {"reviewed": entry.reviewed, "entry_id": entry.entry_id},
        "raw_response": entry.full_json,
        "snapshot": snapshot,
    }
