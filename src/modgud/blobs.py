"""Content-addressed storage for durable source material."""

import hashlib
import os
import tempfile
from pathlib import Path


class BlobStore:
    """Store and retrieve bytes by their SHA-256 content hash."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def put(self, content: bytes) -> str:
        """Store content and return its SHA-256 hash."""
        blob_hash = hashlib.sha256(content).hexdigest()
        path = self._path(blob_hash)
        if path.exists():
            return blob_hash

        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{blob_hash}.",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            try:
                os.link(temporary_path, path)
            except FileExistsError:
                pass
        finally:
            temporary_path.unlink(missing_ok=True)

        return blob_hash

    def get(self, blob_hash: str) -> bytes:
        """Retrieve content previously stored under a SHA-256 hash."""
        return self._path(blob_hash).read_bytes()

    def _path(self, blob_hash: str) -> Path:
        if len(blob_hash) != 64 or any(
            character not in "0123456789abcdef" for character in blob_hash
        ):
            raise ValueError("blob hash must be a lowercase SHA-256 digest")
        return self._root / "sha256" / blob_hash[:2] / blob_hash
