"""Settings from the environment and an optional ``.env`` file. Standard library only."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Optional

__all__ = ["load_env", "database_url"]

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def _value(raw: str) -> str:
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        inner = raw[1:-1]
        return inner.replace("\\n", "\n").replace('\\"', '"') if raw[0] == '"' else inner
    return re.sub(r"\s+#.*$", "", raw)


def load_env(path: str = ".env", override: bool = False) -> Dict[str, str]:
    """Load ``KEY=value`` lines into ``os.environ`` and return what the file defines.

    A variable already set in the real environment wins unless ``override`` is true. A missing file is not an error.
    """
    file = Path(path)
    if not file.is_file():
        return {}
    values: Dict[str, str] = {}
    for line in file.read_text(encoding="utf-8-sig").splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = _LINE.match(line)
        if match:
            values[match.group(1)] = _value(match.group(2))
    for key, value in values.items():
        if override or key not in os.environ:
            os.environ[key] = value
    return values


def database_url() -> Optional[str]:
    """``CLINESTRA_DATABASE_URL``, then ``DATABASE_URL``, then ``CLINESTRA_DB`` as a SQLite path, else ``None``."""
    return (os.environ.get("CLINESTRA_DATABASE_URL") or os.environ.get("DATABASE_URL")
            or os.environ.get("CLINESTRA_DB") or None)
