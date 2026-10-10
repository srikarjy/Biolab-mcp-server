"""ExecutionRecord: the provenance record for running a tool, as retrievals are for reading a source.

Design rules
- Failure-closed. Output artifacts are stored first, then the record is committed. If either
  step fails the caller gets an exception, never an unaudited result.
- Immutable and chained. One row per finished run (succeeded or failed), hash-chained with
  its own chain so slow compute never contends with the retrieval writer.
- Replayable. The record keeps canonical inputs, tool version, container and device plus the
  sha256 of every output, so `replay` can re-run the tool and report whether it reproduced.
"""

import hashlib
import hmac
import json
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from biolab.artifacts import ArtifactStore, sha256_hex

HASH_VERSION = 1
_write_lock = threading.Lock()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


@dataclass(frozen=True)
class ToolSpec:
    """A registered tool. `run` maps canonical inputs to {output name: bytes}."""

    name: str
    version: str
    run: Callable[[dict], dict[str, bytes]]
    container: str = "host"
    device: str = "cpu"
    deterministic: bool = True  # False: replay reports output drift instead of failing


@dataclass
class ToolRegistry:
    tools: dict[str, ToolSpec] = field(default_factory=dict)

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self.tools:
            raise ValueError(f"tool {spec.name!r} is already registered")
        self.tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self.tools[name]
        except KeyError:
            raise ValueError(f"unknown tool {name!r}; registered: {sorted(self.tools)}") from None


def _record_hash(version: int, row: dict) -> str:
    if version != 1:
        raise ValueError(f"unknown execution hash_version {version}")
    material = canonical_json(
        [
            "biolab-execution-v1", row["prev_hash"], row["execution_id"], row["tool"],
            row["tool_version"], row["agent_id"], row["status"], row["input_hash"],
            row["container"], row["device"], row["retrieval_ids"], row["outputs"],
            row["error"], row["started_at"], row["finished_at"],
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


_COLUMNS = (
    "execution_id", "tool", "tool_version", "agent_id", "status", "inputs", "input_hash",
    "container", "device", "retrieval_ids", "outputs", "error", "started_at", "finished_at",
    "prev_hash", "record_hash",
)


def _insert_chained(conn: Any, row: dict) -> None:
    """Read the head and append atomically; BEGIN IMMEDIATE also serializes across processes."""
    with _write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            head = conn.execute(
                "SELECT record_hash FROM executions ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            row["prev_hash"] = head[0] if head else ""
            row["record_hash"] = _record_hash(HASH_VERSION, row)
            conn.execute(
                f"INSERT INTO executions ({', '.join(_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _COLUMNS)})",
                tuple(row[c] for c in _COLUMNS),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def execute(
    conn: Any,
    store: ArtifactStore,
    registry: ToolRegistry,
    tool: str,
    inputs: dict,
    agent_id: str,
    retrieval_ids: list[str] | None = None,
) -> dict:
    """Run a registered tool and seal an ExecutionRecord. Raises if it cannot be sealed.

    A tool that raises is itself recorded (status "failed") and then re-raised, so failed
    attempts are as auditable as successes.
    """
    if not agent_id.strip():
        raise ValueError("agent_id must not be empty")
    spec = registry.get(tool)
    canonical_inputs = canonical_json(inputs)
    started_at = datetime.now(UTC).isoformat()

    outputs: list[dict] = []
    error = ""
    failure: Exception | None = None
    try:
        produced = spec.run(json.loads(canonical_inputs))
        for name in sorted(produced):
            data = produced[name]
            outputs.append({"name": name, "sha256": store.put(data), "size": len(data)})
    except Exception as exc:
        failure = exc
        error = f"{type(exc).__name__}: {exc}"
        outputs = []

    row = {
        "execution_id": str(uuid.uuid4()),
        "tool": spec.name,
        "tool_version": spec.version,
        "agent_id": agent_id,
        "status": "failed" if failure else "succeeded",
        "inputs": canonical_inputs,
        "input_hash": sha256_hex(canonical_inputs.encode("utf-8")),
        "container": spec.container,
        "device": spec.device,
        "retrieval_ids": canonical_json(sorted(retrieval_ids or [])),
        "outputs": canonical_json(outputs),
        "error": error,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
    }
    _insert_chained(conn, row)  # raises -> caller never sees an unrecorded result
    if failure:
        raise failure
    return to_dict(row)


def to_dict(row: dict) -> dict:
    return {
        **{k: row[k] for k in _COLUMNS if k not in {"inputs", "retrieval_ids", "outputs"}},
        "inputs": json.loads(row["inputs"]),
        "retrieval_ids": json.loads(row["retrieval_ids"]),
        "outputs": json.loads(row["outputs"]),
    }


def get_execution(conn: Any, execution_id: str) -> dict | None:
    cur = conn.execute(
        f"SELECT {', '.join(_COLUMNS)} FROM executions WHERE execution_id = ?", (execution_id,)
    )
    found = cur.fetchone()
    return to_dict(dict(zip(_COLUMNS, found, strict=True))) if found else None


def list_executions(conn: Any, tool: str | None = None, limit: int = 50) -> list[dict]:
    where, params = ("WHERE tool = ?", (tool,)) if tool else ("", ())
    rows = conn.execute(
        f"SELECT {', '.join(_COLUMNS)} FROM executions {where} ORDER BY rowid DESC LIMIT ?",
        (*params, max(1, min(limit, 500))),
    ).fetchall()
    return [to_dict(dict(zip(_COLUMNS, r, strict=True))) for r in rows]


def verify_executions(conn: Any, store: ArtifactStore | None = None) -> tuple[bool, str | None]:
    """Verify the execution chain; with a store, also that every output artifact is intact."""
    rows = conn.execute(
        f"SELECT {', '.join(_COLUMNS)} FROM executions ORDER BY rowid ASC"
    ).fetchall()
    expected_prev = ""
    for values in rows:
        row = dict(zip(_COLUMNS, values, strict=True))
        if row["prev_hash"] != expected_prev:
            return False, row["execution_id"]
        if _record_hash(HASH_VERSION, row) != row["record_hash"]:
            return False, row["execution_id"]
        if row["input_hash"] != sha256_hex(row["inputs"].encode("utf-8")):
            return False, row["execution_id"]
        if store is not None:
            for output in json.loads(row["outputs"]):
                if not store.verify(output["sha256"]):
                    return False, row["execution_id"]
        expected_prev = row["record_hash"]
    return True, None


def replay(
    conn: Any, store: ArtifactStore, registry: ToolRegistry, execution_id: str
) -> dict:
    """Re-run a recorded execution with its stored inputs and compare output hashes.

    Replay never writes a record: it is a check on the original, not new evidence. The
    result names exactly which outputs matched, drifted, appeared or vanished.
    """
    original = get_execution(conn, execution_id)
    if original is None:
        raise ValueError("execution not found")
    spec = registry.get(original["tool"])
    version_matches = spec.version == original["tool_version"]
    try:
        produced = spec.run(original["inputs"])
        replayed = {name: sha256_hex(data) for name, data in produced.items()}
        replay_error = ""
    except Exception as exc:
        replayed, replay_error = {}, f"{type(exc).__name__}: {exc}"

    recorded = {o["name"]: o["sha256"] for o in original["outputs"]}
    diff = {
        "matched": sorted(n for n in recorded if replayed.get(n) == recorded[n]),
        "drifted": sorted(n for n in recorded if n in replayed and replayed[n] != recorded[n]),
        "missing": sorted(n for n in recorded if n not in replayed),
        "unexpected": sorted(n for n in replayed if n not in recorded),
    }
    reproduced = (
        original["status"] == "succeeded"
        and not replay_error
        and not (diff["drifted"] or diff["missing"] or diff["unexpected"])
    )
    return {
        "execution_id": execution_id,
        "reproduced": reproduced,
        "tool_version_matches": version_matches,
        "deterministic_tool": spec.deterministic,
        "replay_error": replay_error,
        "diff": diff,
    }


def _anchor_mac(anchor: dict, key: bytes) -> str:
    body = canonical_json(
        ["biolab-execution-anchor", anchor["version"], anchor["count"], anchor["head_hash"],
         anchor["created_at"]]
    )
    return hmac.new(key, body.encode("utf-8"), hashlib.sha256).hexdigest()


def make_anchor(conn: Any, key: bytes) -> dict:
    """Signed checkpoint of the execution chain head, stored outside the database.

    Same purpose as retrieval_log.make_anchor: verify_executions cannot see tail
    truncation or a rewrite that recomputes every hash.
    """
    count = conn.execute("SELECT count(*) FROM executions").fetchone()[0]
    head = conn.execute("SELECT record_hash FROM executions ORDER BY rowid DESC LIMIT 1").fetchone()
    anchor = {
        "chain": "executions",
        "version": 1,
        "count": count,
        "head_hash": head[0] if head else "",
        "created_at": datetime.now(UTC).isoformat(),
    }
    anchor["hmac"] = _anchor_mac(anchor, key)
    return anchor


def verify_anchor(conn: Any, anchor: dict, key: bytes) -> tuple[bool, str]:
    """Check the live execution chain against a checkpoint. Returns (ok, reason)."""
    try:
        is_executions = anchor["chain"] == "executions"
        expected = _anchor_mac(anchor, key)
    except (KeyError, TypeError):
        return False, "malformed anchor"
    if not is_executions or not hmac.compare_digest(expected, str(anchor.get("hmac", ""))):
        return False, "anchor signature invalid (wrong key, edited anchor, or not an executions anchor)"
    count = anchor["count"]
    if count == 0:
        return True, "anchor predates any records"
    rows = conn.execute(
        "SELECT record_hash FROM executions ORDER BY rowid ASC LIMIT 1 OFFSET ?", (count - 1,)
    ).fetchall()
    if not rows:
        total = conn.execute("SELECT count(*) FROM executions").fetchone()[0]
        return False, f"log truncated: anchor covers {count} executions, log has {total}"
    if rows[0][0] != anchor["head_hash"]:
        return False, f"execution {count} no longer matches the anchored hash (log rewritten)"
    return True, f"executions extend the anchored head at record {count}"
