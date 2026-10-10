"""Content-addressed artifact storage for tool outputs.

An artifact's id IS its SHA-256, so the same bytes are stored once and a stored file
can always be re-verified against the id an ExecutionRecord cites. Local filesystem
only for now; the interface (put/get/verify) is what a remote bucket would implement.
"""

import hashlib
import os
import tempfile
from pathlib import Path


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ArtifactStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, digest: str) -> Path:
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("artifact id must be a lowercase sha256 hex digest")
        return self.root / digest[:2] / digest

    def put(self, data: bytes) -> str:
        """Store bytes durably and return their sha256. Idempotent."""
        digest = sha256_hex(data)
        path = self._path(digest)
        if path.exists() and self.verify(digest):
            return digest
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            Path(tmp).replace(path)  # atomic: readers never see a partial artifact
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return digest

    def get(self, digest: str) -> bytes:
        data = self._path(digest).read_bytes()
        if sha256_hex(data) != digest:
            raise ValueError(f"artifact {digest} is corrupted on disk")
        return data

    def verify(self, digest: str) -> bool:
        try:
            self.get(digest)
        except (OSError, ValueError):
            return False
        return True
