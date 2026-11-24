"""Small, deterministic report renderers and export bookkeeping."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any


def project_notebook(report: dict[str, Any], query_history: list[dict[str, Any]], notes: list[dict[str, Any]], audit_status: dict[str, Any]) -> bytes:
    """Build a portable, citation-preserving Jupyter notebook for a project."""
    project = report.get("project", {})
    context_fields = (
        ("Objective", project.get("objective", "")),
        ("Research question", project.get("research_question", "")),
        ("Scope", project.get("scope", "")),
    )
    context = "\n".join(f"**{label}:** {value}" for label, value in context_fields if str(value).strip())
    context = context or "No research context saved."
    papers = "\n".join(
        f"- **{paper.get('snapshot', {}).get('title') or paper.get('external_id', '')}** "
        f"({paper.get('source', '')}:{paper.get('external_id', '')})"
        for paper in report.get("papers", [])
    ) or "- No selected papers."
    note_lines = []
    for note in notes:
        citations = ", ".join(
            f"block {item.get('block_id', '')} p.{item.get('page_number', '?')}"
            for item in note.get("citations", [])
        ) or "no citations"
        note_lines.append(f"- **{note.get('note_text', '')}** ({citations})")
    history = "\n".join(
        f"- `{item.get('question', '')}` — {item.get('mode', '')}, "
        f"status `{item.get('result_status', '')}`, cached `{item.get('cache_hit', False)}`"
        for item in query_history
    ) or "- No recorded queries."
    cells = [
        {"cell_type": "markdown", "metadata": {}, "source": [f"# {project.get('name', 'Biolab project')}\n\n{context}"]},
        {"cell_type": "markdown", "metadata": {}, "source": [f"## Selected papers\n\n{papers}"]},
        {"cell_type": "markdown", "metadata": {}, "source": [f"## Saved cited notes\n\n{'\n'.join(note_lines) or '- No saved notes.'}"]},
        {"cell_type": "markdown", "metadata": {}, "source": [f"## Query history\n\n{history}"]},
        {"cell_type": "markdown", "metadata": {}, "source": [
            "## Audit status\n\n"
            f"Chain valid: `{audit_status.get('valid', False)}`  \n"
            f"Events: `{audit_status.get('event_count', 0)}`  \n"
            f"First broken event: `{audit_status.get('first_broken_event_id')}`"
        ]},
        {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": [
            "# Re-query the governed project API from a notebook.\n"
            "# Supply credentials through your notebook environment, never in this file.\n"
            "import os\n"
            "import requests\n\n"
            f"PROJECT_ID = {project.get('project_id', '')!r}\n"
            "API_BASE = os.environ.get('BIOLAB_API_BASE', 'https://biolab-a2a.vercel.app/v1')\n"
            "headers = {'Authorization': f\"Bearer {os.environ['BIOLAB_API_KEY']}\"}\n"
            "response = requests.get(f'{API_BASE}/projects/{PROJECT_ID}/audit', headers=headers)\n"
            "response.raise_for_status()\n"
            "audit = response.json()\n"
            "audit['valid'], audit['event_count']\n"
        ]},
    ]
    notebook = {
        "cells": cells,
        "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}, "language_info": {"name": "python"}},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    return json.dumps(notebook, indent=2, ensure_ascii=False).encode("utf-8")


def _pdf_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def markdown_to_pdf(markdown: str) -> bytes:
    """Render bounded plain report text into a valid dependency-free PDF."""
    raw_lines = re.sub(r"[`*_]", "", markdown).splitlines()
    lines: list[str] = []
    for raw in raw_lines:
        text = raw.strip()
        lines.extend(text[i : i + 105] for i in range(0, len(text), 105)) if text else lines.append("")
    lines = lines[:180]
    commands = ["BT", "/F1 9 Tf", "50 760 Td", "12 TL"]
    commands.extend(f"({_pdf_escape(line)}) Tj T*" if line else "T*" for line in lines)
    commands.append("ET")
    stream = "\n".join(commands).encode("latin-1", "replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode())
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    output.extend("".join(f"{offset:010d} 00000 n \n" for offset in offsets[1:]).encode())
    output.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(output)


def record_export(conn: Any, project_id: str, format_name: str, content: bytes, created_by: str) -> dict:
    export = {
        "export_id": str(uuid.uuid4()), "project_id": project_id, "format": format_name,
        "status": "succeeded", "checksum": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content), "created_by": created_by,
        "created_at": datetime.now(UTC).isoformat(),
    }
    conn.execute(
        """INSERT INTO exports
           (export_id, project_id, format, status, checksum, size_bytes, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        tuple(export.values()),
    )
    conn.commit()
    return export
