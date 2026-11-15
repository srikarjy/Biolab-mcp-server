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
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib import request

PARSER_MODEL = os.environ.get("HF_PARSER_MODEL", "nvidia/Nemotron-3.5-Parse")
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024


def _now() -> str:
    return datetime.now(UTC).isoformat()


def configured() -> bool:
    return bool(os.environ.get("HF_PARSER_ENDPOINT") and os.environ.get("HF_TOKEN"))


def _normalize(raw: Any) -> list[dict[str, Any]]:
    """Normalize common HF parser responses into page-aware evidence blocks."""
    pages = raw.get("pages", []) if isinstance(raw, dict) else raw if isinstance(raw, list) else [{"text": str(raw)}]
    blocks: list[dict[str, Any]] = []
    for page_index, page_value in enumerate(pages if isinstance(pages, list) else [pages], 1):
        page = {"text": page_value} if isinstance(page_value, str) else page_value
        if not isinstance(page, dict):
            continue
        page_number = int(page.get("page", page.get("page_number", page_index)))
        page_blocks = page.get("blocks") or page.get("elements")
        if not isinstance(page_blocks, list):
            page_blocks = [{"text": page.get("markdown") or page.get("text", ""), "type": "text"}]
        for block_index, block_value in enumerate(page_blocks):
            block = {"text": block_value} if isinstance(block_value, str) else block_value
            if not isinstance(block, dict):
                continue
            text = str(block.get("text") or block.get("content") or "").strip()
            if not text:
                continue
            blocks.append({
                "block_id": str(uuid.uuid4()), "page_number": page_number,
                "block_index": block_index, "block_type": str(block.get("type") or block.get("label") or "text"),
                "text": text, "bbox": block.get("bbox") or block.get("box"),
            })
    return blocks


def parse_document(content: bytes, filename: str) -> dict[str, Any]:
    if len(content) > MAX_DOCUMENT_BYTES:
        raise ValueError(f"document exceeds {MAX_DOCUMENT_BYTES} byte limit")
    endpoint = os.environ.get("HF_PARSER_ENDPOINT")
    if not endpoint or not os.environ.get("HF_TOKEN"):
        raise RuntimeError("HF_PARSER_ENDPOINT and HF_TOKEN are required for Hugging Face parsing")
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
    """Create page-zero blocks for text-only API tests and non-PDF source records."""
    return [{"block_id": str(uuid.uuid4()), "page_number": 0, "block_index": index, "block_type": "text", "text": value.strip(), "bbox": None} for index, value in enumerate(re.split(r"\n{2,}|(?<=[.!?])\s+", text)) if value.strip()]
