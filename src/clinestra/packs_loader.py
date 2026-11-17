"""
Packs: versioned bundles of definitions (cohorts, protocols, assessments, note pages, journeys ...).

A pack is a folder with a ``pack.json`` manifest listing definition files. Each
file holds one definition or a list of them::

    {"def_kind": "assessment", "key": "phq9", "version": 1, "name": "PHQ-9", "config": {...}}

Pack definitions are library content: shared across sites, published and
read-only. An optional ``checksums`` map (file name to SHA-256) is verified on
load. That is an integrity check, not a publisher signature.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Union

from .errors import Invalid

__all__ = ["Pack", "load_pack", "builtin_packs", "BUILTIN_DIR"]

BUILTIN_DIR = Path(__file__).parent / "packs"


@dataclass
class Pack:
    pack_id: str
    name: str
    version: str
    manifest: dict
    definitions: List[dict] = field(default_factory=list)

    def summary(self) -> dict:
        kinds: dict = {}
        for d in self.definitions:
            kinds.setdefault(d["def_kind"], []).append(d["key"])
        return {"pack_id": self.pack_id, "name": self.name, "version": self.version,
                "licence": self.manifest.get("licence"), "definitions": {k: sorted(v) for k, v in sorted(kinds.items())}}


def builtin_packs() -> List[str]:
    return sorted(p.name for p in BUILTIN_DIR.iterdir() if (p / "pack.json").exists())


def load_pack(source: Union[str, Path]) -> Pack:
    """Load a pack from a folder path, or by name for a built-in pack (``"core"``)."""
    path = Path(source)
    if not (path / "pack.json").exists():
        path = BUILTIN_DIR / str(source)
    if not (path / "pack.json").exists():
        raise Invalid(f"No pack found at '{source}'. Built-in packs: {builtin_packs()}")
    manifest = json.loads((path / "pack.json").read_text(encoding="utf-8"))
    sums = manifest.get("checksums") or {}
    pack = Pack(manifest["pack_id"], manifest.get("name", manifest["pack_id"]), str(manifest.get("version", "0")), manifest)
    for name in manifest.get("definitions", []):
        raw = (path / name).read_bytes()
        if name in sums and hashlib.sha256(raw).hexdigest() != sums[name]:
            raise Invalid(f"Pack '{pack.pack_id}': checksum mismatch for {name}; refusing to load")
        data = json.loads(raw)
        for d in data if isinstance(data, list) else [data]:
            if not d.get("def_kind") or not d.get("key"):
                raise Invalid(f"Pack '{pack.pack_id}', {name}: every definition needs def_kind and key")
            pack.definitions.append(d)
    return pack
