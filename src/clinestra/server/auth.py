"""
Authentication: turn a request into an :class:`~clinestra.Actor`.

The actor is never read from a request body. Plug in your own identity
provider by passing any callable ``(request) -> Actor`` to ``create_app``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
import warnings
from typing import Callable, Dict, Optional

from fastapi import HTTPException, Request

from ..base import Actor

__all__ = ["Authenticator", "APIKeyAuth", "DevHeaderAuth", "from_env"]

Authenticator = Callable[[Request], Actor]


HASHED_FORMAT = "hashed-v1"
MIN_KEY_LENGTH = 32


def key_pepper() -> str:
    return os.environ.get("CLINESTRA_KEY_PEPPER") or "clinestra-dev-pepper"


def hash_api_key(raw: str, pepper: Optional[str] = None) -> str:
    return hmac.new((pepper or key_pepper()).encode(), raw.encode(), hashlib.sha256).hexdigest()


def accept_new_key(raw: str) -> str:
    """New keys are at least 32 characters. Migration may still hash older fixtures outside production."""
    if not raw or len(raw) < MIN_KEY_LENGTH:
        raise ValueError(f"API keys must be at least {MIN_KEY_LENGTH} characters")
    return raw


def _expired(value) -> bool:
    """Whether a key's ``expires_at`` has passed. A date alone lasts to the end of that day (UTC). Unreadable means expired."""
    if not value:
        return False
    text = str(value).replace("Z", "+00:00")
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return True
    if len(text) == 10:
        when += timedelta(days=1)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= when


class APIKeyAuth:
    """Static API keys, each bound to one actor. Send ``Authorization: Bearer <key>`` or ``X-API-Key``.

    Plaintext ``{rawkey: actor}`` files still load when ``CLINESTRA_MODE`` is not ``production``.
    The hashed file is ``{"format": "hashed-v1", "keys": [{key_id, prefix, hash, actor, ...}]}``.
    """

    def __init__(self, keys: Dict):
        self._load(keys)

    def _load(self, keys: Dict) -> None:
        self._hashed = isinstance(keys, dict) and keys.get("format") == HASHED_FORMAT
        if self._hashed:
            if os.environ.get("CLINESTRA_MODE") == "production" and not os.environ.get("CLINESTRA_KEY_PEPPER"):
                raise RuntimeError("Production mode needs CLINESTRA_KEY_PEPPER. Its default is public.")
            self.entries = list(keys.get("keys") or [])
            self.keys = {e["key_id"]: Actor(**e["actor"]) for e in self.entries if not e.get("revoked")}
            return
        if os.environ.get("CLINESTRA_MODE") == "production":
            raise RuntimeError("Production mode refuses plaintext API keys. Run: clinestra keys import")
        self.entries = []
        self.keys = {k: Actor(**v) for k, v in keys.items()}

    @classmethod
    def from_file(cls, path: str) -> "APIKeyAuth":
        with open(path, encoding="utf-8") as f:
            auth = cls(json.load(f))
        auth._path, auth._mtime = path, os.path.getmtime(path)
        return auth

    def _reload_if_changed(self) -> None:
        """Pick up edits to the keys file (e.g. from an admin panel) without a restart."""
        path = getattr(self, "_path", None)
        if not path:
            return
        try:
            mtime = os.path.getmtime(path)
            if mtime != self._mtime:
                with open(path, encoding="utf-8") as f:
                    self._load(json.load(f))
                self._mtime = mtime
        except (OSError, ValueError):
            pass  # keep the last good set of keys

    def __call__(self, request: Request) -> Actor:
        self._reload_if_changed()
        header = request.headers.get("authorization", "")
        key = header[7:] if header.lower().startswith("bearer ") else request.headers.get("x-api-key")
        actor = self._match(key or "")
        if not actor:
            raise HTTPException(401, "Missing or unknown API key")
        return actor

    def _match(self, raw: str) -> Optional[Actor]:
        if not self._hashed:
            return self.keys.get(raw)
        if len(raw) < 6:
            return None
        digest = hash_api_key(raw)
        prefix = raw[:6]
        found = None
        for entry in self.entries:
            if entry.get("revoked"):
                continue
            if entry.get("prefix") != prefix or _expired(entry.get("expires_at")):
                continue
            if hmac.compare_digest(entry.get("hash", ""), digest):
                found = Actor(**entry["actor"])
        if found is None:
            hmac.compare_digest(digest, digest)
        return found


class DevHeaderAuth:
    """DEVELOPMENT ONLY. Trusts ``X-Actor-Id``, ``X-Actor-Role``, ``X-Site``, ``X-Actor-Type``, ``X-Patient-Id``,
    ``X-Represents`` (comma-separated patient ids a guardian acts for).

    Anyone can claim any identity with this, so never expose it beyond localhost.
    """

    def __init__(self, default_site: str = "dev_site"):
        self.default_site = default_site
        warnings.warn("DevHeaderAuth trusts request headers for identity. Do not use in production.", stacklevel=2)

    def __call__(self, request: Request) -> Actor:
        h = request.headers
        if not h.get("x-actor-id") or not h.get("x-actor-role"):
            raise HTTPException(401, "Dev mode: send X-Actor-Id and X-Actor-Role headers")
        try:
            return Actor(id=h["x-actor-id"], role=h["x-actor-role"], site=h.get("x-site", self.default_site),
                         actor_type=h.get("x-actor-type", "human"), patient_id=h.get("x-patient-id"),
                         represents=[x for x in h.get("x-represents", "").split(",") if x])
        except ValueError as exc:
            raise HTTPException(401, f"Invalid dev identity: {exc}")


def from_env() -> Optional[Authenticator]:
    """``CLINESTRA_API_KEYS`` = path to a JSON file of ``{key: actor}``; ``CLINESTRA_DEV_AUTH=1`` enables dev headers."""
    path = os.environ.get("CLINESTRA_API_KEYS")
    if path:
        return APIKeyAuth.from_file(path)
    if os.environ.get("CLINESTRA_DEV_AUTH") == "1":
        return DevHeaderAuth()
    return None
