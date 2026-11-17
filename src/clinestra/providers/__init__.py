"""
Pluggable providers. The core never needs any of them: about 80% of Clinestra
is deterministic. Plug these in to add the hosted or AI-native parts.

* :class:`EvidenceProvider` - evidence lookup for the AI harness.
* :class:`LLMProvider` - text generation used by the AI harness tools
  (config generation, extraction, summarisation, reasoning).
* :class:`BlobStore` - where rendered clinical documents are kept (S3 is one adapter).
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

__all__ = ["EvidenceProvider", "LLMProvider", "BlobStore", "MemoryBlobStore", "LocalBlobStore"]


@runtime_checkable
class EvidenceProvider(Protocol):
    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Return ``[{title, source, grade, summary, url}]``."""
        ...


@runtime_checkable
class LLMProvider(Protocol):
    def complete(self, system: str, prompt: str, json_schema: Optional[Dict[str, Any]] = None) -> Any:
        """Return text, or a parsed object matching ``json_schema`` when one is given."""
        ...


@runtime_checkable
class BlobStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> str:
        """Store ``data`` and return a reference (URL or path)."""
        ...

    def get(self, ref: str) -> bytes: ...


class MemoryBlobStore:
    def __init__(self):
        self._d: Dict[str, bytes] = {}

    def put(self, key, data, content_type):
        ref = f"mem://{key}"
        self._d[ref] = data
        return ref

    def get(self, ref):
        return self._d[ref]


class LocalBlobStore:
    """Files on local disk. For S3, implement ``put``/``get`` with boto3 and pass it to the Harness."""

    def __init__(self, root: str = "clinestra_blobs"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, key, data, content_type):
        safe = hashlib.sha256(key.encode()).hexdigest()[:24] + Path(key).suffix
        path = self.root / safe
        path.write_bytes(data)
        return str(path)

    def get(self, ref):
        return Path(ref).read_bytes()
