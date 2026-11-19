"""Hugging Face document-parser adapter with provenance-preserving blocks.

The production endpoint should be a Hugging Face Inference Endpoint or Space
wrapper that renders PDF pages and calls the selected parser model (for example
``nvidia/Nemotron-3.5-Parse``). The adapter deliberately treats the returned
layout/text JSON as untrusted model output and normalizes it before storage.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from typing import Any
from urllib import request

PARSER_MODEL = os.environ.get("HF_PARSER_MODEL", "nvidia/Nemotron-3.5-Parse")
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
MAX_CHUNK_CHARACTERS = 1800


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _clean_text(value: Any) -> str:
    """Collapse layout whitespace while preserving readable paragraph text."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _stable_block_id(page_number: int, block_index: int, block_type: str, text: str) -> str:
    """Create an id that is stable for the same normalized parser output."""
    digest = hashlib.sha256(
        f"{page_number}:{block_index}:{block_type}:{text}".encode()
    ).hexdigest()[:24]
    return f"chunk-{digest}"


def _structured_block_text(block: dict[str, Any], block_type: str, text: str, metadata: dict[str, Any]) -> str:
    """Make table/figure meaning searchable without discarding layout fields."""
    if "table" in block_type.lower():
        parts = [text]
        if metadata.get("caption") and str(metadata["caption"]) not in text:
            parts.append(f"Caption: {metadata['caption']}")
        if metadata.get("columns"):
            parts.append("Headers: " + " | ".join(map(str, metadata["columns"])))
        if metadata.get("rows"):
            parts.append("Rows: " + " ; ".join(" | ".join(map(str, row)) if isinstance(row, list | tuple) else str(row) for row in metadata["rows"]))
        return _clean_text(" ".join(parts))
    if "figure" in block_type.lower() and metadata.get("caption") and str(metadata["caption"]) not in text:
        return _clean_text(f"{text} Caption: {metadata['caption']}")
    return text


def configured() -> bool:
    return bool(os.environ.get("HF_PARSER_ENDPOINT") and os.environ.get("HF_TOKEN"))


def _normalize(raw: Any) -> list[dict[str, Any]]:  # noqa: PLR0912
    """Normalize common HF parser responses into page-aware evidence blocks."""
    pages = raw.get("pages", []) if isinstance(raw, dict) else raw if isinstance(raw, list) else [{"text": str(raw)}]
    blocks: list[dict[str, Any]] = []
    for page_index, page_value in enumerate(pages if isinstance(pages, list) else [pages], 1):
        page = {"text": page_value} if isinstance(page_value, str) else page_value
        if not isinstance(page, dict):
            continue
        page_number = int(page.get("page") or page.get("page_number") or page_index)
        page_blocks = page.get("blocks") or page.get("elements")
        if not isinstance(page_blocks, list):
            page_blocks = [{"text": page.get("markdown") or page.get("text", ""), "type": "text"}]
        for block_index, block_value in enumerate(page_blocks):
            block = {"text": block_value} if isinstance(block_value, str) else block_value
            if not isinstance(block, dict):
                continue
            text = _clean_text(block.get("text") or block.get("content") or "")
            if not text:
                continue
            block_type = str(block.get("type") or block.get("label") or "text")
            metadata = dict(block.get("metadata") if isinstance(block.get("metadata"), dict) else {})
            for key in ("caption", "table_id", "figure_id", "rows", "columns"):
                if key in block and key not in metadata:
                    metadata[key] = block[key]
            block_type = str(block.get("type") or block.get("label") or "text")
            text = _structured_block_text(block, block_type, text, metadata)
            metadata["serialization_version"] = "layout-block-v1"
            if "table" in block_type.lower():
                metadata["table_serialization_hash"] = hashlib.sha256(json.dumps({"caption": metadata.get("caption"), "columns": metadata.get("columns"), "rows": metadata.get("rows"), "text": text}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            blocks.append({
                "block_id": _stable_block_id(page_number, block_index, block_type, text), "page_number": page_number,
                "block_index": block_index, "block_type": str(block.get("type") or block.get("label") or "text"),
                "text": text, "bbox": block.get("bbox") or block.get("box"), "metadata": metadata,
            })
    for index, block in enumerate(blocks):
        if "figure" not in block["block_type"].lower():
            continue
        neighbors = []
        for neighbor_index in (index - 1, index + 1):
            if 0 <= neighbor_index < len(blocks):
                neighbor = blocks[neighbor_index]
                if neighbor["page_number"] == block["page_number"] and "figure" not in neighbor["block_type"].lower() and "table" not in neighbor["block_type"].lower():
                    neighbors.append(neighbor["block_id"])
        block["metadata"]["associated_block_ids"] = neighbors
    return blocks


def parse_document(content: bytes, filename: str) -> dict[str, Any]:
    if len(content) > MAX_DOCUMENT_BYTES:
        raise ValueError(f"document exceeds {MAX_DOCUMENT_BYTES} byte limit")
    endpoint = os.environ.get("HF_PARSER_ENDPOINT")
    if not endpoint or not os.environ.get("HF_TOKEN"):
        text: str
        if filename.lower().endswith(".pdf") or content.startswith(b"%PDF"):
            if shutil.which("pdftotext") is None:
                raise RuntimeError("local PDF parsing requires pdftotext or configured HF_PARSER_ENDPOINT and HF_TOKEN")
            try:
                completed = subprocess.run(
                    ["pdftotext", "-layout", "-", "-"],
                    input=content, capture_output=True, check=True,
                    timeout=float(os.environ.get("LOCAL_PARSER_TIMEOUT_SECONDS", "30")),
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise RuntimeError(f"local PDF parsing failed: {exc}") from exc
            text = completed.stdout.decode("utf-8", "replace")
        else:
            text = content.decode("utf-8", "replace")
        blocks = fallback_text_blocks(text)
        return {
            "parser_model": "pdftotext-local",
            "status": "succeeded-local",
            "source_hash": hashlib.sha256(content).hexdigest(),
            "block_count": len(blocks),
            "blocks": blocks,
        }
    prompt = os.environ.get("HF_PARSER_PROMPT", "</s><s><predict_bbox><predict_classes><output_markdown><predict_text_in_pic>")
    req = request.Request(
        endpoint, data=content,
        headers={"Authorization": f"Bearer {os.environ['HF_TOKEN']}", "Content-Type": "application/pdf", "X-Biolab-Filename": filename, "X-Biolab-Prompt": prompt},
        method="POST",
    )
    with request.urlopen(req, timeout=float(os.environ.get("HF_PARSER_TIMEOUT_SECONDS", "120"))) as response:
        raw_bytes = response.read()
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raw = {"text": raw_bytes.decode("utf-8", "replace")}
    blocks = _normalize(raw)
    return {"parser_model": PARSER_MODEL, "status": "succeeded", "source_hash": hashlib.sha256(content).hexdigest(), "block_count": len(blocks), "blocks": blocks}


def fallback_text_blocks(text: str) -> list[dict[str, Any]]:
    """Create deterministic, page-aware chunks for local parsing.

    Form-feed characters are emitted by ``pdftotext`` at page boundaries. A
    text-only source has page zero because it has no trustworthy page map.
    Chunks are intentionally bounded so the later embedding step never has to
    guess how to split an oversized parser block.
    """
    pages = text.split("\f") if "\f" in text else [text]
    page_numbers = range(1, len(pages) + 1) if len(pages) > 1 else (0,)
    chunks: list[dict[str, Any]] = []
    for page_number, page_text in zip(page_numbers, pages, strict=True):
        candidates = re.split(r"\n{2,}|(?<=[.!?])\s+", page_text)
        page_index = 0
        for candidate in candidates:
            normalized = _clean_text(candidate)
            if not normalized:
                continue
            words = normalized.split()
            current: list[str] = []
            current_length = 0
            pieces = []
            for word in words:
                added = len(word) + (1 if current else 0)
                if current and current_length + added > MAX_CHUNK_CHARACTERS:
                    pieces.append(" ".join(current))
                    current = []
                    current_length = 0
                current.append(word)
                current_length += len(word) + (1 if len(current) > 1 else 0)
            if current:
                pieces.append(" ".join(current))
            for piece in pieces:
                chunks.append({
                    "block_id": _stable_block_id(page_number, page_index, "text", piece),
                    "page_number": page_number,
                    "block_index": page_index,
                    "block_type": "text",
                    "text": piece,
                    "bbox": None, "metadata": {},
                })
                page_index += 1
    return chunks
