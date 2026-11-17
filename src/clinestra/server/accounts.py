"""Staff accounts. People sign in with a username and a password, not an API key.

The first account is created with no password. That person chooses one the first
time they sign in. Accounts a superadmin adds carry the temporary password the
superadmin set, and the API refuses every other call until that password is replaced.
Device and agent keys still authenticate when a keys file is configured.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from datetime import timedelta
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..base import ROLES, Actor, utcnow

AUTH_SITE = "_auth"
KIND_ACCOUNT = "account"
KIND_SESSION = "staff_session"
SESSION_PREFIX = "st-"
MIN_PASSWORD = 8
PBKDF2_ROUNDS = 120_000
SESSION_DAYS = 7
SETUP_MINUTES = 15
STAFF_ROLES = tuple(r for r in ROLES if r not in ("patient", "system"))

_failures: dict[str, list[float]] = {}


def account_site(auth) -> str:
    """Site for the bootstrap superadmin: ``CLINESTRA_SITE``, else the first staff key's site."""
    chosen = os.environ.get("CLINESTRA_SITE")
    if chosen:
        return chosen
    keys = getattr(auth, "keys", None) or {}
    for value in keys.values():
        if isinstance(value, Actor):
            role, site = value.role, value.site
        elif isinstance(value, dict):
            role, site = value.get("role"), value.get("site")
        else:
            continue
        if role and role != "patient" and site:
            return site
    return "dev_site"


def bootstrap_username() -> str:
    return os.environ.get("CLINESTRA_BOOTSTRAP_USER") or "admin"


def _now():
    return utcnow()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ROUNDS).hex()
    return f"pbkdf2${salt}${digest}"


def verify_password(password: str, stored: Optional[str]) -> bool:
    if not stored:
        return False
    try:
        _, salt, digest = stored.split("$", 2)
        check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ROUNDS).hex()
        return hmac.compare_digest(check, digest)
    except (ValueError, TypeError):
        return False


def _check_password(password: str) -> None:
    if not password or len(password) < MIN_PASSWORD:
        raise HTTPException(422, f"Password must be at least {MIN_PASSWORD} characters")


def _username_ok(username: str) -> bool:
    import re
    return bool(re.fullmatch(r"[a-z][a-z0-9_]{1,39}", username or ""))


def _doc(store, kind: str, id: str) -> Optional[dict]:
    return store.get(kind, id, AUTH_SITE)


def _put(store, kind: str, doc: dict) -> None:
    store.put(kind, doc)


def list_accounts(store) -> list[dict]:
    return store.list(KIND_ACCOUNT, AUTH_SITE)


def ensure_bootstrap(store, site: str) -> dict:
    """Create the first superadmin when the account list is empty. No password yet."""
    existing = list_accounts(store)
    if existing:
        return existing[0]
    username = bootstrap_username()
    doc = {
        "id": username,
        "site": AUTH_SITE,
        "home_site": site,
        "role": "admin",
        "actor_type": "human",
        "password_hash": None,
        "must_change_password": True,
        "created_at": _now().isoformat(),
        "created_by": "bootstrap",
    }
    _put(store, KIND_ACCOUNT, doc)
    return doc


def _actor_of(account: dict) -> Actor:
    return Actor(id=account["id"], site=account["home_site"], role=account["role"], actor_type=account.get("actor_type") or "human")


def _session(store, token: str) -> Optional[dict]:
    if not token.startswith(SESSION_PREFIX):
        return None
    return _doc(store, KIND_SESSION, hashlib.sha256(token.encode()).hexdigest())


def _new_token(store, account: dict, purpose: str, minutes: Optional[int] = None) -> str:
    token = SESSION_PREFIX + secrets.token_urlsafe(32)
    expires = _now() + (timedelta(minutes=minutes) if minutes else timedelta(days=SESSION_DAYS))
    _put(store, KIND_SESSION, {
        "id": hashlib.sha256(token.encode()).hexdigest(),
        "site": AUTH_SITE,
        "username": account["id"],
        "purpose": purpose,
        "actor": _actor_of(account).model_dump(),
        "expires_at": expires.isoformat(),
        "created_at": _now().isoformat(),
    })
    return token


def _drop_token(store, token: str) -> None:
    doc = _session(store, token)
    if doc:
        store.delete(KIND_SESSION, doc["id"], AUTH_SITE)


def _live(doc: Optional[dict]) -> Optional[dict]:
    if not doc:
        return None
    if str(doc.get("expires_at", "")) < _now().isoformat():
        return None
    return doc


def _too_many(username: str) -> bool:
    now = time.time()
    recent = [t for t in _failures.get(username, []) if now - t < 600]
    _failures[username] = recent
    return len(recent) >= 8


def _fail(username: str) -> None:
    _failures.setdefault(username, []).append(time.time())


def _clear_failures(username: str) -> None:
    _failures.pop(username, None)


def _public(account: dict) -> dict:
    return {
        "username": account["id"],
        "role": account["role"],
        "actor_type": account.get("actor_type") or "human",
        "site": account["home_site"],
        "must_change_password": bool(account.get("must_change_password")),
        "password_unset": not account.get("password_hash"),
    }


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:] if header.lower().startswith("bearer ") else ""


class UserSessionAuth:
    """Accept ``st-`` staff sessions. Anything else is passed to the inner authenticator (keys or dev headers)."""

    def __init__(self, inner, store):
        self.inner, self.store = inner, store

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def __call__(self, request: Request) -> Actor:
        token = _bearer(request)
        if token.startswith(SESSION_PREFIX):
            doc = _live(_session(self.store, token))
            if not doc or doc.get("purpose") != "session":
                raise HTTPException(401, "Choose a new password before continuing." if doc else "Your session has ended. Sign in again.")
            return Actor(**doc["actor"])
        return self.inner(request)


class LoginIn(BaseModel):
    username: str
    password: str = ""


class PasswordIn(BaseModel):
    username: Optional[str] = None
    new_password: str


class AccountIn(BaseModel):
    username: str
    role: str
    password: str
    actor_type: str = "human"


def mount_accounts(app: FastAPI, store, actor_dep) -> None:
    def superadmin(actor: Actor = actor_dep) -> Actor:
        if actor.role != "admin" or actor.actor_type != "human":
            raise HTTPException(403, "Only a superadmin can add people")
        return actor

    @app.get("/v1/auth/setup", tags=["system"])
    def setup():
        account = next((a for a in list_accounts(store) if a["id"] == bootstrap_username()), None)
        return {"username": bootstrap_username(), "password_unset": bool(account and not account.get("password_hash"))}

    @app.post("/v1/auth/login", tags=["system"])
    def login(body: LoginIn):
        username = body.username.strip().lower()
        if _too_many(username):
            return JSONResponse({"error": "TooManyAttempts", "detail": "Too many attempts. Wait a few minutes and try again."}, status_code=429)
        account = _doc(store, KIND_ACCOUNT, username)
        if account and not account.get("password_hash"):
            return JSONResponse({"error": "PasswordUnset", "detail": "Set a password before signing in."}, status_code=409)
        if not account or not verify_password(body.password, account.get("password_hash")):
            _fail(username)
            return JSONResponse({"error": "Unauthorized", "detail": "That username or password is not right."}, status_code=401)
        _clear_failures(username)
        if account.get("must_change_password"):
            return {"must_change_password": True, "setup_token": _new_token(store, account, "setup", SETUP_MINUTES)}
        token = _new_token(store, account, "session")
        return {"must_change_password": False, "token": token, "actor": _actor_of(account).model_dump()}

    @app.post("/v1/auth/password", tags=["system"])
    def set_password(body: PasswordIn, request: Request):
        _check_password(body.new_password)
        token = _bearer(request)
        setup = _live(_session(store, token)) if token.startswith(SESSION_PREFIX) else None
        if setup and setup.get("purpose") == "setup":
            account = _doc(store, KIND_ACCOUNT, setup["username"])
            if not account:
                raise HTTPException(401, "Your session has ended. Sign in again.")
            if verify_password(body.new_password, account.get("password_hash")):
                raise HTTPException(422, "Choose a password that is different from the temporary one")
            account["password_hash"] = hash_password(body.new_password)
            account["must_change_password"] = False
            _put(store, KIND_ACCOUNT, account)
            _drop_token(store, token)
            issued = _new_token(store, account, "session")
            return {"must_change_password": False, "token": issued, "actor": _actor_of(account).model_dump()}
        username = (body.username or "").strip().lower()
        account = _doc(store, KIND_ACCOUNT, username) if username else None
        if not account or account.get("password_hash"):
            return JSONResponse({"error": "Unauthorized", "detail": "That username or password is not right."}, status_code=401)
        account["password_hash"] = hash_password(body.new_password)
        account["must_change_password"] = False
        _put(store, KIND_ACCOUNT, account)
        issued = _new_token(store, account, "session")
        return {"must_change_password": False, "token": issued, "actor": _actor_of(account).model_dump()}

    @app.post("/v1/auth/logout", tags=["system"])
    def logout(request: Request):
        _drop_token(store, _bearer(request))
        return {"ok": True}

    @app.get("/v1/admin/accounts", tags=["Administration"])
    def accounts(actor: Actor = Depends(superadmin)):
        return [_public(a) for a in list_accounts(store) if a.get("home_site") == actor.site]

    @app.post("/v1/admin/accounts", status_code=201, tags=["Administration"])
    def add_account(body: AccountIn, actor: Actor = Depends(superadmin)):
        username = body.username.strip().lower()
        if not _username_ok(username):
            raise HTTPException(422, "Username: lowercase letters, digits, underscore, starting with a letter")
        if body.role not in STAFF_ROLES:
            raise HTTPException(422, f"Role must be one of {list(STAFF_ROLES)}")
        if body.actor_type not in ("human", "agent"):
            raise HTTPException(422, "Type must be human or agent")
        if body.actor_type == "agent" and body.role == "admin":
            raise HTTPException(422, "An agent cannot be a superadmin")
        _check_password(body.password)
        if _doc(store, KIND_ACCOUNT, username):
            raise HTTPException(409, "That username already exists")
        doc = {
            "id": username,
            "site": AUTH_SITE,
            "home_site": actor.site,
            "role": body.role,
            "actor_type": body.actor_type,
            "password_hash": hash_password(body.password),
            "must_change_password": True,
            "created_at": _now().isoformat(),
            "created_by": actor.id,
        }
        _put(store, KIND_ACCOUNT, doc)
        return _public(doc)
