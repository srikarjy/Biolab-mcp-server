"""Provenance-safe import of Zotero JSON export items."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from biolab import retrieval_log, workspace

MAX_ITEMS = 100


def _authors(item: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "first_name": str(author.get("firstName", "")),
            "last_name": str(author.get("lastName", "")),
            "name": str(author.get("name", "")),
        }
        for author in item.get("creators", [])
        if isinstance(author, dict)
    ]


def _external_id(item: dict[str, Any]) -> str:
    key = str(item.get("key", "")).strip()
    if key:
        return f"zotero:{key}"
    canonical = json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"zotero:sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def _retrieval_input(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise TypeError("each Zotero item must be an object")
    item_type = str(item.get("itemType", "")).strip()
    if not item_type:
        raise ValueError("Zotero itemType is required")
    title = str(item.get("title", "")).strip()
    if not title:
        raise ValueError("Zotero item title is required")
    snapshot = {
        "title": title,
        "abstract": str(item.get("abstractNote", "")).strip(),
        "authors": _authors(item),
        "date": str(item.get("date", "")).strip(),
        "doi": str(item.get("DOI", "")).strip(),
        "url": str(item.get("url", "")).strip(),
        "journal": str(item.get("publicationTitle", "")).strip(),
        "tags": [str(tag.get("tag", "")) for tag in item.get("tags", []) if isinstance(tag, dict)],
        "zotero_key": str(item.get("key", "")).strip(),
        "zotero_item_type": item_type,
        "metadata_only": True,
    }
    raw = json.dumps(item, sort_keys=True, ensure_ascii=False)
    return {
        "external_id": _external_id(item),
        "source_metadata": {"provider": "zotero", "item_type": item_type, "zotero_key": snapshot["zotero_key"]},
        "raw_response": raw,
        "snapshot": snapshot,
    }


def import_items(conn: Any, project_id: str, items: list[dict[str, Any]], owner_id: str) -> dict[str, Any]:
    """Import Zotero metadata, preserve it, and select it for a project."""
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    if len(items) > MAX_ITEMS:
        raise ValueError(f"items must contain at most {MAX_ITEMS} Zotero records")
    if not workspace.has_project_access(conn, project_id, owner_id):
        raise ValueError("project not found")

    imported = []
    duplicates = []
    for item in items:
        retrieval = _retrieval_input(item)
        row = conn.execute(
            "SELECT retrieval_id FROM retrievals WHERE source = 'zotero' AND external_id = ? AND agent_id = ? ORDER BY rowid DESC LIMIT 1",
            (retrieval["external_id"], owner_id),
        ).fetchone()
        if row:
            retrieval_id = row[0]
            duplicates.append(retrieval_id)
        else:
            record = retrieval_log.write_retrieval(
                conn,
                query_text="zotero_import",
                external_id=retrieval["external_id"],
                agent_id=owner_id,
                source="zotero",
                source_metadata=retrieval["source_metadata"],
                raw_response=retrieval["raw_response"],
                snapshot=retrieval["snapshot"],
            )
            retrieval_id = record.retrieval_id
            imported.append(retrieval_id)
        workspace.save_retrieval_to_project(conn, project_id, retrieval_id, owner_id)
    conn.commit()
    return {"project_id": project_id, "imported_count": len(imported), "duplicate_count": len(duplicates), "retrieval_ids": imported + duplicates, "metadata_only": True}
