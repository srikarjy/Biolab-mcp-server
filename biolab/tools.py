"""Built-in tools for the execution runtime.

Every tool is a pure-ish function of its canonical inputs returning {output name: bytes},
so the runtime can hash, store and replay the outputs. Heavy external tools (mmseqs2,
and later NIM/BioNeMo models) plug in as adapters that shell out or call an API and
report their own version.
"""

import json
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

from biolab.executions import ToolRegistry, ToolSpec

MAX_SEQUENCE_CHARS = 5_000_000
AMINO_ACIDS = set("ACDEFGHIKLMNPQRSTVWY")
NUCLEOTIDES = set("ACGTUN")


def _sequence_stats(inputs: dict) -> dict[str, bytes]:
    sequence = str(inputs.get("sequence", "")).upper().replace(" ", "").replace("\n", "")
    if not sequence or len(sequence) > MAX_SEQUENCE_CHARS:
        raise ValueError(f"sequence must be 1-{MAX_SEQUENCE_CHARS} characters")
    composition = Counter(sequence)
    alphabet = set(composition)
    if alphabet <= NUCLEOTIDES:
        kind = "nucleotide"
        gc = (composition["G"] + composition["C"]) / len(sequence)
        extra = {"gc_fraction": round(gc, 6)}
    elif alphabet <= AMINO_ACIDS | {"X", "*"}:
        kind = "protein"
        extra = {}
    else:
        raise ValueError(f"unrecognised residues: {sorted(alphabet - AMINO_ACIDS - NUCLEOTIDES)}")
    report = {"type": kind, "length": len(sequence), "composition": dict(sorted(composition.items()))}
    report.update(extra)
    return {"stats.json": json.dumps(report, sort_keys=True).encode()}


def _mmseqs_version(binary: str) -> str:
    out = subprocess.run([binary, "version"], capture_output=True, text=True, timeout=30, check=True)
    return out.stdout.strip()


def _mmseqs_search_factory(binary: str):
    def run(inputs: dict) -> dict[str, bytes]:
        query, target = str(inputs.get("query_fasta", "")), str(inputs.get("target_fasta", ""))
        if not query.startswith(">") or not target.startswith(">"):
            raise ValueError("query_fasta and target_fasta must be FASTA text")
        sensitivity = float(inputs.get("sensitivity", 5.7))
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "q.fasta").write_text(query)
            (work / "t.fasta").write_text(target)
            subprocess.run(
                [binary, "easy-search", "q.fasta", "t.fasta", "hits.m8", "tmp",
                 "-s", str(sensitivity), "--threads", "1"],
                cwd=work, capture_output=True, text=True, timeout=3600, check=True,
            )
            hits = sorted((work / "hits.m8").read_text().splitlines())  # stable order
        return {"hits.m8": ("\n".join(hits) + "\n").encode()}

    return run


def default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ToolSpec("sequence_stats", "1", _sequence_stats))
    binary = shutil.which("mmseqs")
    if binary:
        registry.register(
            ToolSpec("mmseqs2_search", _mmseqs_version(binary), _mmseqs_search_factory(binary))
        )
    return registry
