"""
Encryption at rest for tenant secrets (model provider keys, the realtime voice key).

Secrets are Fernet-encrypted with a master key that never leaves the server:

* ``CLINESTRA_MASTER_KEY`` (a Fernet key, e.g. from a secrets manager) when set, otherwise
* a key file, ``CLINESTRA_MASTER_KEY_FILE`` or ``clinestra_master.key`` next to a SQLite database, created on first
  use. A server database such as Postgres has no such folder, so one of the two settings is required. Protect it like a password and back it up: losing it means re-entering every provider key.

Secrets are write-only through the API: responses carry :func:`mask` hints, never the value.
Requires ``pip install 'clinestra[ai]'`` (cryptography).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional, Tuple

__all__ = ["encrypt", "decrypt", "mask", "master_source", "SecretError", "reset"]


class SecretError(RuntimeError):
    pass


_fernet = None
_source = ""


def _key_file() -> Path:
    if os.environ.get("CLINESTRA_MASTER_KEY_FILE"):
        return Path(os.environ["CLINESTRA_MASTER_KEY_FILE"])
    url = os.environ.get("CLINESTRA_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if url and url.startswith("sqlite:///"):
        return Path(url[len("sqlite:///"):]).resolve().parent / "clinestra_master.key"
    if url and "://" in url:
        raise SecretError("With a server database there is no folder to keep a master key in. "
                          "Set CLINESTRA_MASTER_KEY, or CLINESTRA_MASTER_KEY_FILE to a file path.")
    db = os.environ.get("CLINESTRA_DB")
    return (Path(db).resolve().parent if db else Path.cwd()) / "clinestra_master.key"


def _load() -> Tuple[object, str]:
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover
        raise SecretError("Encrypted secrets need: pip install 'clinestra[ai]'") from exc
    env = os.environ.get("CLINESTRA_MASTER_KEY")
    if env:
        return Fernet(env.encode()), "environment variable CLINESTRA_MASTER_KEY"
    if os.environ.get("CLINESTRA_MODE") == "production":
        raise SecretError(
            "Production mode requires CLINESTRA_MASTER_KEY. A key file next to the database is refused.")
    path = _key_file()
    if not path.exists():
        path.write_bytes(Fernet.generate_key())
    return Fernet(path.read_bytes().strip()), f"key file {path.name}"


def _f():
    global _fernet, _source
    if _fernet is None:
        _fernet, _source = _load()
    return _fernet


def reset() -> None:
    """Forget the loaded master key (tests, key rotation)."""
    global _fernet, _source
    _fernet, _source = None, ""


def master_source() -> str:
    """Where the master key comes from, or, outside production, why there is none yet."""
    try:
        _f()
    except SecretError as exc:
        if os.environ.get("CLINESTRA_MODE") == "production":
            raise
        return f"not configured: {exc}"
    return _source


def encrypt(obj: dict) -> str:
    return _f().encrypt(json.dumps(obj).encode()).decode()


def decrypt(token: str) -> dict:
    from cryptography.fernet import InvalidToken
    try:
        return json.loads(_f().decrypt(token.encode()))
    except InvalidToken as exc:
        raise SecretError("Stored secret cannot be decrypted with the current master key") from exc


def reencrypt(token: str, old_key: str, new_key: str) -> str:
    """Decrypt ``token`` with ``old_key`` and encrypt it with ``new_key``. Does not touch a database."""
    from cryptography.fernet import Fernet, InvalidToken
    try:
        obj = json.loads(Fernet(old_key.encode()).decrypt(token.encode()))
    except InvalidToken as exc:
        raise SecretError("Stored secret cannot be decrypted with the old master key") from exc
    return Fernet(new_key.encode()).encrypt(json.dumps(obj).encode()).decode()


def mask(value: Optional[str]) -> str:
    if not value:
        return ""
    return f"{value[:7]}…•••{value[-4:]}" if len(value) > 14 else "•••" + value[-2:]
