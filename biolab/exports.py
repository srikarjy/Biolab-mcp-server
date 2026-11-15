"""Small, deterministic report renderers and export bookkeeping."""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import UTC, datetime
from typing import Any


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
