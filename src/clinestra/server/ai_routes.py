"""
AI, administration and patient-portal routes, mounted on the Clinical API when ``clinestra[ai]`` is installed:

    /v1/ai/...       AI features for client applications: one-shot streaming features, console-driven tool steps, agents, config
    /v1/admin/...    model providers (bring your own keys, encrypted, write-only), models & routing, policy, prompts,
                     usage and audit, staff keys, agents, navigation, patient portal, realtime voice
    /v1/portal/...   patient portal: OTP sign-in, sessions, site config, realtime voice interview

All state is per site (the caller's site) in the harness store. Admin = role ``admin`` or listed in the site's admins.
Provider keys are accepted on write and never returned.
"""

import json
import os
import re
import secrets
import time
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import __version__
from .. import secrets as sec
from .agentic import agents as ag
from .agentic import runtime, toolbox
from .agentic import skills as sk
from .agentic.levels import public_levels
from .agentic.defaults import (CATALOG, DEFAULT_FEATURES, DEFAULT_NAV, DEFAULT_POLICY, FEATURES, PLATFORMS, PRICES,
                           SYSTEM_PROMPTS)
from .agentic.local import AGENT_ID, PORTAL_ID, HarnessError, call, identity
from .agentic.state import SiteState, new_id
from ..base import ROLES, Actor
from .portal import interview as iv
from .portal import sessions as portal

__all__ = ["mount_ai", "PortalSessionAuth", "default_site"]


class PortalSessionAuth:
    """Wraps an authenticator: ``pt-...`` bearer tokens are patient portal sessions; anything else goes to ``inner``."""

    def __init__(self, inner, store):
        self.inner, self.store = inner, store

    def __getattr__(self, name):  # keys, _path ... of the wrapped authenticator
        return getattr(self.inner, name)

    def __call__(self, request: Request) -> Actor:
        header = request.headers.get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else ""
        if token.startswith(portal.SESSION_PREFIX):
            site = portal.site_of_token(token)
            actor = portal.session_actor(SiteState(self.store, site), token) if site else None
            if not actor:
                raise HTTPException(401, "Your session has ended. Please sign in again.")
            return actor
        return self.inner(request)


def _keys(auth) -> Dict[str, Any]:
    return getattr(auth, "keys", None) or {}


def _any_admin_role(auth) -> bool:
    """True once any configured key has role admin. CLINESTRA_ADMINS is then ignored."""
    for value in _keys(auth).values():
        role = value.role if isinstance(value, Actor) else (value.get("role") if isinstance(value, dict) else None)
        if role == "admin":
            return True
    return False


def _keys_path(auth) -> Optional[str]:
    return getattr(auth, "_path", None)


def default_site(auth) -> str:
    """The install's main site: ``CLINESTRA_SITE``, else the portal service's site in the keys file, else the first
    staff key's site. Used where there is no signed-in actor (portal sign-in, ``/v1/health``)."""
    if os.environ.get("CLINESTRA_SITE"):
        return os.environ["CLINESTRA_SITE"]
    actors = [v if isinstance(v, Actor) else Actor(**v) for v in _keys(auth).values()]
    for a in actors:
        if a.id == PORTAL_ID:
            return a.site
    staff = [a.site for a in actors if a.role != "patient"]
    return staff[0] if staff else "dev_site"


def mount_ai(app: FastAPI, h, auth) -> None:
    store = h.store

    # ───────────────────────── identity ─────────────────────────
    def st_for(site: str) -> SiteState:
        st = SiteState(store, site)
        if st.kv_get("policy") is None:
            st.kv_set("policy", DEFAULT_POLICY)
        else:
            pol = dict(st.kv_get("policy") or {})
            if pol.get("send_patient_identifiers") is True and not pol.get("_identifiers_migrated"):
                pol["send_patient_identifiers"] = False
                pol["_identifiers_migrated"] = True
                st.kv_set("policy", pol)
                st.admin_log("system", "policy.send_patient_identifiers_disabled",
                             {"reason": "default is off; identifiers are not sent to models"})
        if st.kv_get("admins") is None and not _any_admin_role(auth):
            st.kv_set("admins", [a.strip() for a in os.environ.get("CLINESTRA_ADMINS", "").split(",") if a.strip()])
        if st.kv_get("routes") is None:
            st.kv_set("routes", {})
        return st

    def svc(actor_id: str, site: str) -> Actor:
        return identity(actor_id, site, _keys(auth))

    def who(request: Request) -> dict:
        actor = auth(request)
        st = st_for(actor.site)
        me = actor.model_dump()
        listed = actor.id in (st.kv_get("admins") or [])
        # Once a key has role admin, CLINESTRA_ADMINS and the site list cannot promote a clinician.
        if _any_admin_role(auth):
            me["is_admin"] = actor.actor_type != "agent" and actor.role == "admin"
        else:
            me["is_admin"] = actor.actor_type != "agent" and (listed or actor.role == "admin")
        me["_actor"] = actor
        return me

    def admin(me: dict = Depends(who)) -> dict:
        if not me.get("is_admin"):
            raise HTTPException(403, "Administrator access required")
        return me

    A, ADMIN = Depends(who), Depends(admin)

    def public(me: dict) -> dict:
        return {k: v for k, v in me.items() if k != "_actor"}

    def policy_of(st: SiteState) -> dict:
        return {**DEFAULT_POLICY, **(st.kv_get("policy") or {})}

    def portal_site(site: Optional[str] = None) -> str:
        """Site served by the unauthenticated portal endpoints (one portal per install unless ?site= is given)."""
        return site or default_site(auth)

    def herr(e: HarnessError):
        return HTTPException(e.status, e.detail)

    @app.get("/v1/admin/me", tags=["Administration"])
    def admin_me(me: dict = A):
        return public(me)

    # ───────────────────────── status ─────────────────────────
    @app.get("/v1/admin/status", tags=["Administration"])
    def status(me: dict = ADMIN):
        st = st_for(me["site"])
        routes = st.kv_get("routes", {}) or {}
        provs = [{k: p.get(k) for k in ("id", "name", "platform", "enabled", "last_test")} for p in st.records("_ai.provider")]
        warnings = []
        if not provs:
            warnings.append("No model provider configured: AI features run in preview mode.")
        if not routes.get("agent"):
            warnings.append("No model routed for agent chat.")
        src = sec.master_source()
        if src.startswith("key file"):
            warnings.append("Secrets are encrypted with a local key file. For production set CLINESTRA_MASTER_KEY from a secrets manager and back it up.")
        path = _keys_path(auth)
        if not path:
            warnings.append("Staff keys are not file-based; staff management is read-only here.")
        day = st.audit_since(time.time() - 86400)
        health = {"status": "ok", "version": __version__, "packs": sorted(h.packs),
                  "llm": bool(h.llm), "evidence": bool(h.evidence_provider)}
        return {"harness": health, "harness_url": "in-process (this server)", "master_key_source": src, "providers": provs,
                "routes": routes, "warnings": warnings,
                "last_24h": {"n": len(day), "i": sum(r["input_tokens"] for r in day), "o": sum(r["output_tokens"] for r in day)},
                "keys_file": path or "", "policy": st.kv_get("policy")}

    @app.get("/v1/admin/catalog", tags=["Administration"])
    def catalog(me: dict = ADMIN):
        return {"platforms": PLATFORMS, "models": CATALOG, "features": FEATURES}

    # ───────────────────────── providers (BYOK) ─────────────────────────
    class ProviderIn(BaseModel):
        name: str
        platform: str
        config: Dict[str, Any] = Field(default_factory=dict)
        secrets: Dict[str, str] = Field(default_factory=dict, description="Write-only. Omit on update to keep the stored secret.")
        enabled: bool = True

    def _public_provider(p: dict) -> dict:
        return {k: p.get(k) for k in ("id", "name", "platform", "config", "secret_hint", "enabled", "created_at", "updated_at", "last_test")}

    def _provider(st: SiteState, pid: str) -> dict:
        try:
            return runtime.provider_doc(st, pid)
        except LookupError as e:
            raise HTTPException(404, str(e))

    @app.get("/v1/admin/providers", tags=["Administration"])
    def list_providers(me: dict = ADMIN):
        st = st_for(me["site"])
        return [_public_provider(p) for p in sorted(st.records("_ai.provider"), key=lambda p: p["created_at"])]

    @app.post("/v1/admin/providers", status_code=201, tags=["Administration"])
    def create_provider(body: ProviderIn, me: dict = ADMIN):
        if body.platform not in PLATFORMS:
            raise HTTPException(400, f"Unknown platform {body.platform}")
        st = st_for(me["site"])
        pid, now = new_id("prov"), time.time()
        s = {k: v for k, v in body.secrets.items() if v}
        st.put("_ai.provider", pid, {"name": body.name, "platform": body.platform, "config": body.config,
                                     "secret": sec.encrypt(s) if s else None, "secret_hint": {k: sec.mask(v) for k, v in s.items()},
                                     "enabled": body.enabled, "created_at": now, "updated_at": now, "last_test": None})
        st.admin_log(me["id"], "provider.created", {"id": pid, "platform": body.platform, "name": body.name})
        return _public_provider(_provider(st, pid))

    @app.put("/v1/admin/providers/{pid}", tags=["Administration"])
    def update_provider(pid: str, body: ProviderIn, me: dict = ADMIN):
        st = st_for(me["site"])
        p = _provider(st, pid)
        s = {k: v for k, v in body.secrets.items() if v}
        if s:  # rotate: merge new values over the stored ones
            merged = {**(sec.decrypt(p["secret"]) if p.get("secret") else {}), **s}
            p.update(secret=sec.encrypt(merged), secret_hint={k: sec.mask(v) for k, v in merged.items()})
        p.update(name=body.name, config=body.config, enabled=body.enabled, updated_at=time.time())
        st.put("_ai.provider", pid, p)
        st.admin_log(me["id"], "provider.updated", {"id": pid, "rotated": sorted(s)})
        return _public_provider(_provider(st, pid))

    @app.delete("/v1/admin/providers/{pid}", tags=["Administration"])
    def delete_provider(pid: str, me: dict = ADMIN):
        st = st_for(me["site"])
        if any(m["provider_id"] == pid for m in st.records("_ai.model")):
            raise HTTPException(409, "Remove or move the models that use this provider first")
        st.delete("_ai.provider", pid)
        st.admin_log(me["id"], "provider.deleted", {"id": pid})
        return {"ok": True}

    class TestIn(BaseModel):
        model_id: Optional[str] = None

    @app.post("/v1/admin/providers/{pid}/test", tags=["Administration"])
    def test_provider(pid: str, body: TestIn, me: dict = ADMIN):
        st = st_for(me["site"])
        p = _provider(st, pid)
        res = runtime.test_provider(st, p, body.model_id)
        res["at"] = time.time()
        st.put("_ai.provider", pid, {**p, "last_test": res})
        st.admin_log(me["id"], "provider.tested", {"id": pid, "ok": res["ok"]})
        return res

    # ───────────────────────── models & routing ─────────────────────────
    class ModelIn(BaseModel):
        provider_id: str
        model_id: str
        effort: Optional[str] = "high"
        max_tokens: Optional[int] = None
        enabled: bool = True

    def _model_row(m: dict) -> dict:
        return {k: m.get(k) for k in ("alias", "provider_id", "model_id", "effort", "max_tokens", "enabled")}

    @app.get("/v1/admin/models", tags=["Administration"])
    def list_models(me: dict = ADMIN):
        st = st_for(me["site"])
        return sorted((_model_row(m) for m in st.records("_ai.model")), key=lambda m: m["alias"])

    @app.put("/v1/admin/models/{alias}", tags=["Administration"])
    def upsert_model(alias: str, body: ModelIn, me: dict = ADMIN):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{1,40}", alias):
            raise HTTPException(400, "Alias: lowercase letters, digits, . _ -")
        st = st_for(me["site"])
        _provider(st, body.provider_id)
        st.put("_ai.model", alias, {"alias": alias, **body.model_dump()})
        st.admin_log(me["id"], "model.saved", {"alias": alias, **body.model_dump()})
        return _model_row(st.get("_ai.model", alias))

    @app.delete("/v1/admin/models/{alias}", tags=["Administration"])
    def delete_model(alias: str, me: dict = ADMIN):
        st = st_for(me["site"])
        if alias in (st.kv_get("routes", {}) or {}).values():
            raise HTTPException(409, "This model is routed to a feature; change routing first")
        st.delete("_ai.model", alias)
        st.admin_log(me["id"], "model.deleted", {"alias": alias})
        return {"ok": True}

    @app.get("/v1/admin/routes", tags=["Administration"])
    def get_routes(me: dict = ADMIN):
        return st_for(me["site"]).kv_get("routes", {})

    @app.put("/v1/admin/routes", tags=["Administration"])
    def put_routes(body: Dict[str, str], me: dict = ADMIN):
        st = st_for(me["site"])
        aliases = {m["alias"] for m in st.records("_ai.model")}
        bad = [v for v in body.values() if v and v not in aliases]
        if bad:
            raise HTTPException(400, f"Unknown model aliases: {bad}")
        clean = {k: v for k, v in body.items() if k in FEATURES and v}
        st.kv_set("routes", clean)
        st.admin_log(me["id"], "routes.saved", clean)
        return clean

    # ───────────────────────── policy & prompts ─────────────────────────
    @app.get("/v1/admin/policy", tags=["Administration"])
    def get_policy(me: dict = ADMIN):
        return policy_of(st_for(me["site"]))

    @app.put("/v1/admin/policy", tags=["Administration"])
    def put_policy(body: Dict[str, Any], me: dict = ADMIN):
        st = st_for(me["site"])
        pol = {**policy_of(st), **{k: v for k, v in body.items() if k in DEFAULT_POLICY}}
        st.kv_set("policy", pol)
        st.admin_log(me["id"], "policy.saved", body)
        return pol

    def _prompt_history(st: SiteState, feature: str) -> list:
        rows = sorted((p for p in st.records("_ai.prompt") if p["feature"] == feature), key=lambda p: p["created_at"], reverse=True)
        return [{"id": r["id"], "author": r["author"], "created_at": r["created_at"], "text": r["text"]} for r in rows]

    def current_prompt(st: SiteState, feature: str) -> str:
        hist = _prompt_history(st, feature)
        return hist[0]["text"] if hist else SYSTEM_PROMPTS.get(feature, SYSTEM_PROMPTS["agent"])

    @app.get("/v1/admin/prompts", tags=["Administration"])
    def get_prompts(me: dict = ADMIN):
        st = st_for(me["site"])
        return {f: {"current": current_prompt(st, f), "default": SYSTEM_PROMPTS.get(f, ""), "history": _prompt_history(st, f)[:20]}
                for f in FEATURES}

    class PromptIn(BaseModel):
        text: str

    @app.put("/v1/admin/prompts/{feature}", tags=["Administration"])
    def put_prompt(feature: str, body: PromptIn, me: dict = ADMIN):
        if feature not in FEATURES:
            raise HTTPException(404, "Unknown feature")
        st = st_for(me["site"])
        st.put("_ai.prompt", new_id("pr"), {"feature": feature, "text": body.text, "author": me["id"], "created_at": time.time()})
        st.admin_log(me["id"], "prompt.saved", {"feature": feature, "chars": len(body.text)})
        return {"feature": feature, "current": body.text}

    # ───────────────────────── usage, audit ─────────────────────────
    @app.get("/v1/admin/usage", tags=["Administration"])
    def usage(days: int = 7, me: dict = ADMIN):
        st = st_for(me["site"])
        rows = st.audit_since(time.time() - days * 86400)
        groups: Dict[tuple, dict] = {}
        users: Dict[str, dict] = {}
        for r in rows:
            g = groups.setdefault((r["model"], r["feature"]), {"model": r["model"], "feature": r["feature"], "calls": 0, "i": 0, "o": 0,
                                                              "errors": 0, "_lat": 0})
            g["calls"] += 1
            g["i"] += r["input_tokens"]
            g["o"] += r["output_tokens"]
            g["errors"] += r["status"] != "ok"
            g["_lat"] += r["latency_ms"]
            u = users.setdefault(r["actor"], {"actor": r["actor"], "calls": 0, "tokens": 0})
            u["calls"] += 1
            u["tokens"] += r["input_tokens"] + r["output_tokens"]
        out = []
        for g in groups.values():
            g["latency"] = g.pop("_lat") / g["calls"]
            pin, pout = PRICES.get((g["model"] or "").replace("anthropic.", ""), (0, 0))
            g["est_cost_usd"] = round((g["i"] * pin + g["o"] * pout) / 1e6, 4)
            out.append(g)
        return {"days": days, "by_model_feature": out, "by_user": sorted(users.values(), key=lambda u: -u["tokens"])}

    @app.get("/v1/admin/audit", tags=["Administration"])
    def audit_log(limit: int = 200, me: dict = ADMIN):
        rows = sorted(st_for(me["site"]).records("_ai.audit"), key=lambda r: r["at"], reverse=True)[:min(limit, 1000)]
        return [{k: v for k, v in r.items() if k not in ("site", "created_at")} for r in rows]

    @app.get("/v1/admin/log", tags=["Administration"])
    def admin_actions(limit: int = 200, me: dict = ADMIN):
        rows = sorted(st_for(me["site"]).records("_ai.admin_log"), key=lambda r: r["at"], reverse=True)[:min(limit, 1000)]
        return [{"id": r["id"], "at": r["at"], "actor": r["actor"], "action": r["action"],
                 "detail": json.dumps(r["detail"]) if r.get("detail") is not None else None} for r in rows]

    # ───────────────────────── staff & access (API keys file) ─────────────────────────
    def _read_keys() -> dict:
        path = _keys_path(auth)
        if not path or not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def _write_keys(k: dict) -> None:
        path = _keys_path(auth)
        if not path:
            raise HTTPException(501, "Staff keys are managed by your identity provider, not a keys file")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(k, f, indent=2)
        os.replace(tmp, path)  # the authenticator reloads the file on its next request

    @app.get("/v1/admin/staff", tags=["Administration"])
    def staff(me: dict = ADMIN):
        admins = st_for(me["site"]).kv_get("admins", []) or []
        return [{**v, "key_hint": sec.mask(k), "is_admin": v["id"] in admins or v.get("role") == "admin"}
                for k, v in _read_keys().items() if v.get("site") == me["site"] and not k.startswith(portal.SESSION_PREFIX)]

    class StaffIn(BaseModel):
        id: str
        role: str
        site: Optional[str] = None
        actor_type: str = "human"
        patient_id: Optional[str] = None

    @app.post("/v1/admin/staff", status_code=201, tags=["Administration"])
    def add_staff(body: StaffIn, me: dict = ADMIN):
        if body.role not in ROLES:
            raise HTTPException(400, f"Role must be one of {list(ROLES)}")
        if not re.fullmatch(r"[a-z0-9_]{2,40}", body.id):
            raise HTTPException(400, "User id: lowercase letters, digits, underscore")
        keys = _read_keys()
        if any(v["id"] == body.id for v in keys.values()):
            raise HTTPException(409, "That user id already has a key; use Rotate")
        key = f"{me['site'][:8]}-{body.id}-{secrets.token_urlsafe(18)}"
        entry: Dict[str, Any] = {"id": body.id, "site": me["site"], "role": body.role}
        if body.actor_type == "agent":
            entry["actor_type"] = "agent"
        if body.patient_id:
            entry["patient_id"] = body.patient_id
        keys[key] = entry
        _write_keys(keys)
        st_for(me["site"]).admin_log(me["id"], "staff.added", entry)
        return {"user": entry, "api_key": key, "note": "Shown once. Hand it to the user securely."}

    @app.post("/v1/admin/staff/{uid}/rotate", tags=["Administration"])
    def rotate_staff(uid: str, me: dict = ADMIN):
        keys = _read_keys()
        old = next((k for k, v in keys.items() if v["id"] == uid and v.get("site") == me["site"]), None)
        if not old:
            raise HTTPException(404, "No such user")
        entry = keys.pop(old)
        key = f"{me['site'][:8]}-{uid}-{secrets.token_urlsafe(18)}"
        keys[key] = entry
        _write_keys(keys)
        st_for(me["site"]).admin_log(me["id"], "staff.rotated", {"id": uid})
        return {"user": entry, "api_key": key, "note": "Shown once. The old key stopped working."}

    @app.delete("/v1/admin/staff/{uid}", tags=["Administration"])
    def revoke_staff(uid: str, me: dict = ADMIN):
        if uid == me["id"]:
            raise HTTPException(400, "You cannot revoke your own key")
        keys = _read_keys()
        keep = {k: v for k, v in keys.items() if not (v["id"] == uid and v.get("site") == me["site"])}
        if len(keep) == len(keys):
            raise HTTPException(404, "No such user")
        _write_keys(keep)
        st_for(me["site"]).admin_log(me["id"], "staff.revoked", {"id": uid})
        return {"ok": True}

    @app.put("/v1/admin/admins", tags=["Administration"])
    def set_admins(body: List[str], me: dict = ADMIN):
        if me["id"] not in body and me["role"] != "admin":
            raise HTTPException(400, "Keep yourself in the admin list (another admin can remove you)")
        st = st_for(me["site"])
        st.kv_set("admins", sorted(set(body)))
        st.admin_log(me["id"], "admins.saved", body)
        return st.kv_get("admins")

    # ───────────────────────── AI features (used by client applications) ─────────────────────────
    class AiFeatureIn(BaseModel):
        mode: str = "stream"                    # "agent" | "stream"
        prompt: Optional[str] = None            # stream, or the user's message in agent mode
        tool_results: List[dict] = Field(default_factory=list)  # [{tool_call_id, name, content, is_error?}]
        conversation_id: Optional[str] = None
        tools: List[dict] = Field(default_factory=list)          # [{name, description, input_schema, x_kind}]
        context: Dict[str, Any] = Field(default_factory=dict)

    _phone = re.compile(r"(?<!\d)(\+?\d[\d\s-]{8,}\d)(?!\d)")
    _email = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
    _memory_conversations: Dict[str, list] = {}

    def _scrub(text: str, policy: dict) -> str:
        if policy.get("send_patient_identifiers", False):
            return text
        return _email.sub("[email]", _phone.sub("[phone]", text))

    def _check(st: SiteState, me: dict, policy: dict) -> None:
        if not policy.get("ai_enabled", True):
            raise HTTPException(403, "AI is switched off by the administrator")
        if me.get("actor_type") == "agent" or (me.get("role") not in policy.get("allowed_roles", []) and not me.get("is_admin")):
            raise HTTPException(403, f"AI is not enabled for role '{me.get('role')}'")
        day = time.time() - 86400
        if st.tokens_since(day) >= policy.get("daily_token_budget", 10 ** 12):
            raise HTTPException(429, "The site's daily AI budget is used up")
        if st.tokens_since(day, me["id"]) >= policy.get("per_user_daily_token_budget", 10 ** 12):
            raise HTTPException(429, "Your daily AI budget is used up")

    def _allowed_tools(me: dict, policy: dict, tools: List[dict]) -> List[dict]:
        kinds = set(policy.get("tool_access", {}).get(me["role"], []))
        off = set(policy.get("disabled_tools", []))
        return [{k: v for k, v in t.items() if k in ("name", "description", "input_schema")}
                for t in tools if t.get("x_kind", "read") in kinds and t["name"] not in off]

    @app.post("/v1/ai", tags=["AI"], summary="Run a streamed AI feature completion or one tool step")
    def run_ai_feature(body: AiFeatureIn, me: dict = A):
        import anthropic
        from .agentic.openai_compat import OpenAICompatError
        provider_errors = (anthropic.APIError, OpenAICompatError)
        st = st_for(me["site"])
        policy = policy_of(st)
        _check(st, me, policy)
        feature = "agent" if body.mode == "agent" else (body.context.get("feature") or "synopsis")
        if feature not in FEATURES:
            feature = "synopsis"
        try:
            m, p = runtime.resolve(st, feature)
        except LookupError as e:
            raise HTTPException(503, str(e))
        system = current_prompt(st, feature) + f"\n\nUser: {me['id']} ({me['role']}) at site {me['site']}. Screen context: {json.dumps(body.context)[:2000]}"
        t0 = time.time()

        if body.mode != "agent":
            prompt = _scrub(body.prompt or "", policy)

            def events():
                used = (0, 0)
                status_, err = "ok", None
                try:
                    for kind, val in runtime.stream_text(st, m, p, system, prompt):
                        if kind == "text":
                            yield f"data: {json.dumps({'type': 'text', 'data': val})}\n\n"
                        else:
                            used = (val.usage.input_tokens, val.usage.output_tokens)
                            if val.stop_reason == "refusal":
                                yield f"data: {json.dumps({'type': 'text', 'data': chr(10) + '_The model declined this request._'})}\n\n"
                except provider_errors as e:
                    status_, err = "error", str(e)[:300]
                    yield f"data: {json.dumps({'type': 'error', 'message': 'AI provider error. See Admin → Usage & audit.'})}\n\n"
                finally:
                    st.audit(actor=me["id"], role=me["role"], feature=feature, model=m["model_id"], provider=p["name"], status=status_,
                             input_tokens=used[0], output_tokens=used[1], latency_ms=int((time.time() - t0) * 1000), detail=err,
                             patient_id=body.context.get("patientId"))
            return StreamingResponse(events(), media_type="text/event-stream")

        # agent step: append-only conversation kept server-side (assistant turns stored verbatim)
        if policy.get("store_conversation_content", True):
            cid, messages = st.conversation(body.conversation_id, me["id"])
        else:
            cid = body.conversation_id or new_id("mem")
            messages = _memory_conversations.setdefault(cid, [])
        if body.tool_results:
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": r["tool_call_id"], "content": _scrub(str(r.get("content", "")), policy)[:20000],
                 **({"is_error": True} if r.get("is_error") else {})} for r in body.tool_results]})
        if body.prompt:
            messages.append({"role": "user", "content": _scrub(body.prompt, policy)})
        tools = _allowed_tools(me, policy, body.tools)
        try:
            resp = runtime.agent_step(st, m, p, system, messages, tools)
        except provider_errors as e:
            st.audit(actor=me["id"], role=me["role"], feature=feature, model=m["model_id"], provider=p["name"], status="error",
                     latency_ms=int((time.time() - t0) * 1000), detail=str(e)[:300], patient_id=body.context.get("patientId"))
            raise HTTPException(502, "AI provider error. An administrator can see details in Admin → Usage & audit.")
        content = resp.to_dict()["content"]
        messages.append({"role": "assistant", "content": content})
        if policy.get("store_conversation_content", True):
            st.save_conversation(cid, messages)
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        calls = [{"id": b["id"], "name": b["name"], "arguments": b.get("input") or {}} for b in content if b.get("type") == "tool_use"]
        if resp.stop_reason == "refusal":
            text = (text + "\n\n" if text else "") + "_The model declined this request._"
        st.audit(actor=me["id"], role=me["role"], feature=feature, model=resp.model, provider=p["name"], status="ok",
                 input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens, latency_ms=int((time.time() - t0) * 1000),
                 tool_calls=[{"name": c["name"], "arguments": c["arguments"]} for c in calls], patient_id=body.context.get("patientId"))
        return {"conversation_id": cid, "message": text, "tool_calls": calls, "stop_reason": resp.stop_reason}

    def _connected(st: SiteState) -> bool:
        return bool((st.kv_get("routes", {}) or {}).get("agent")) and policy_of(st).get("ai_enabled", True)

    @app.get("/v1/ai/status", tags=["AI"], summary="AI configuration status")
    def ai_status(me: dict = A):
        st = st_for(me["site"])
        policy = policy_of(st)
        return {"connected": _connected(st), "routes": sorted(st.kv_get("routes", {}) or {}), "is_admin": me.get("is_admin", False),
                "ai_enabled": policy.get("ai_enabled", True)}

    # ───────────────────────── console configuration (navigation, features, branding) ─────────────────────────
    def _nav(st: SiteState) -> List[dict]:
        saved = {n["id"]: n for n in (st.kv_get("navigation") or [])}
        return [{**d, **saved.get(d["id"], {})} for d in DEFAULT_NAV]

    def _features(st: SiteState) -> dict:
        return {**DEFAULT_FEATURES, **(st.kv_get("features") or {})}

    @app.get("/v1/ai/config", tags=["AI"], summary="AI features and navigation for this user")
    def ai_config(me: dict = A):
        """What the console needs at start: navigation for this role, feature switches, agents available to this user."""
        st = st_for(me["site"])
        role = me.get("role")
        nav = [n for n in _nav(st) if n["enabled"] and (role in n["roles"] or me.get("is_admin"))]
        policy = policy_of(st)
        agents = [{k: a.get(k) for k in ("id", "name", "description", "scope", "level", "kind", "color", "icon", "is_default", "skills",
                                         "owner_id", "care_team_key", "care_team_name")}
                  for a in ag.visible_to(h, st, me, svc(AGENT_ID, me["site"]))]
        return {"me": public(me), "navigation": nav, "features": _features(st), "agents": agents,
                "ai": {"connected": _connected(st), "enabled": policy.get("ai_enabled", True)}}

    @app.get("/v1/admin/navigation", tags=["Administration"])
    def get_navigation(me: dict = ADMIN):
        st = st_for(me["site"])
        return {"navigation": _nav(st), "features": _features(st)}

    @app.put("/v1/admin/navigation", tags=["Administration"])
    def put_navigation(body: Dict[str, Any], me: dict = ADMIN):
        st = st_for(me["site"])
        if "navigation" in body:
            st.kv_set("navigation", [{k: n[k] for k in ("id", "label", "group", "roles", "enabled") if k in n} for n in body["navigation"]])
        if "features" in body:
            st.kv_set("features", {k: v for k, v in body["features"].items() if k in DEFAULT_FEATURES})
        st.admin_log(me["id"], "navigation.saved", body)
        return {"navigation": _nav(st), "features": _features(st)}

    # ───────────────────────── agents, skills, tools (three levels) ─────────────────────────
    class AgentIn(BaseModel):
        id: str
        name: str
        description: str = ""
        level: Optional[str] = None
        scope: Optional[str] = None  # older clients; mapped to a level
        kind: str = "type"
        audience: List[str] = Field(default_factory=lambda: ["clinician"])
        color: str = "violet"
        icon: str = "sparkles"
        conversation_key: Optional[str] = None
        model_alias: Optional[str] = None
        system_prompt: str
        tools: Optional[List[str]] = None
        agentic_tools: List[str] = Field(default_factory=list)
        clinical_tools: List[str] = Field(default_factory=list)
        skills: List[str] = Field(default_factory=list)
        ui_tools: bool = True
        content_blocks: Optional[bool] = None
        owner_id: Optional[str] = None
        care_team_key: Optional[str] = None
        care_team_name: Optional[str] = None
        template_id: Optional[str] = None
        is_default: bool = False
        enabled: bool = True

    def agent_actor(site: str) -> Actor:
        return svc(AGENT_ID, site)

    def _agent_or_404(st: SiteState, agent_id: str) -> dict:
        try:
            return ag.get(st, agent_id)
        except LookupError as e:
            raise HTTPException(404, str(e))

    @app.get("/v1/admin/agents", tags=["Administration"])
    def list_agents(me: dict = ADMIN):
        st = st_for(me["site"])
        ag.team_agents(h, st, agent_actor(me["site"]))
        return ag.all_agents(st)

    @app.put("/v1/admin/agents/{agent_id}", tags=["Administration"])
    def save_agent(agent_id: str, body: AgentIn, me: dict = ADMIN):
        if not re.fullmatch(r"[a-z0-9_]{2,60}", agent_id) or body.id != agent_id:
            raise HTTPException(400, "Agent id: lowercase letters, digits, underscore")
        cfg = body.model_dump(exclude_none=True)
        if body.tools is None:
            cfg.pop("tools", None)
        else:
            cfg.pop("agentic_tools", None), cfg.pop("clinical_tools", None)
        st = st_for(me["site"])
        try:
            out = ag.save(st, cfg, me["id"])
        except ValueError as e:
            raise HTTPException(400, str(e))
        st.admin_log(me["id"], "agent.saved", {"id": agent_id, "level": out["level"], "tools": len(out["tools"]), "skills": out["skills"]})
        return out

    @app.delete("/v1/admin/agents/{agent_id}", tags=["Administration"])
    def delete_agent(agent_id: str, me: dict = ADMIN):
        st = st_for(me["site"])
        if agent_id in ("personal_assistant", "care_coordination"):
            raise HTTPException(409, "The personal and care-team templates can be edited or switched off, not deleted")
        ag.delete(st, agent_id)
        st.admin_log(me["id"], "agent.deleted", {"id": agent_id})
        return {"ok": True}

    @app.get("/v1/ai/content-blocks", tags=["AI"], summary="The UI component library agents render with (render_blocks)")
    def ui_component_catalog(me: dict = A):
        from .agentic import content_blocks as uic
        return {"tool": uic.TOOL["name"], "components": uic.catalog()}

    # skills
    class SkillIn(BaseModel):
        id: str
        name: str
        level: str
        description: str = ""
        instructions: str = ""
        tools: List[str] = Field(default_factory=list)
        starter: str = ""
        schedule: Optional[str] = None
        enabled: bool = True

    @app.get("/v1/admin/skills", tags=["Administration"])
    def list_skills(me: dict = ADMIN):
        return sk.all_skills(st_for(me["site"]))

    @app.put("/v1/admin/skills/{skill_id}", tags=["Administration"])
    def save_skill(skill_id: str, body: SkillIn, me: dict = ADMIN):
        if not re.fullmatch(r"[a-z0-9_]{2,40}", skill_id) or body.id != skill_id:
            raise HTTPException(400, "Skill id: lowercase letters, digits, underscore")
        st = st_for(me["site"])
        try:
            prev = sk.get(st, skill_id)
        except LookupError:
            prev = {}
        try:
            out = sk.save(st, {**body.model_dump(), "built_in": prev.get("built_in", False)}, me["id"])
        except ValueError as e:
            raise HTTPException(400, str(e))
        st.admin_log(me["id"], "skill.saved", {"id": skill_id, "level": body.level, "tools": body.tools})
        return out

    @app.delete("/v1/admin/skills/{skill_id}", tags=["Administration"])
    def delete_skill(skill_id: str, me: dict = ADMIN):
        st = st_for(me["site"])
        sk.delete(st, skill_id)
        st.admin_log(me["id"], "skill.deleted", {"id": skill_id})
        return {"ok": True}

    # tool administration
    class ToolSettingIn(BaseModel):
        enabled: bool = True
        levels: List[str] = Field(default_factory=list)
        roles: List[str] = Field(default_factory=list)
        note: str = ""

    @app.get("/v1/admin/tools", tags=["Administration"])
    def tool_catalog(me: dict = ADMIN):
        """Every tool agents can be given: kind, default levels, the site's settings and which agents and skills use it."""
        st = st_for(me["site"])
        rows = toolbox.catalog(h, st, agent_actor(me["site"]))
        agents, skills = ag.all_agents(st), sk.all_skills(st)
        for r in rows:
            r["used_by_agents"] = [a["id"] for a in agents if a.get("kind") in ("template", "type") and toolbox.match(r["name"], a.get("tools") or [])]
            r["used_by_skills"] = [s["id"] for s in skills if toolbox.match(r["name"], s.get("tools") or [])]
        return {"levels": public_levels(), "tools": rows}

    @app.put("/v1/admin/tools/{name}", tags=["Administration"])
    def save_tool_setting(name: str, body: ToolSettingIn, me: dict = ADMIN):
        st = st_for(me["site"])
        if name not in {r["name"] for r in toolbox.catalog(h, st, agent_actor(me["site"]))}:
            raise HTTPException(404, f"No tool '{name}'")
        try:
            out = toolbox.save_setting(st, name, body.model_dump())
        except ValueError as e:
            raise HTTPException(400, str(e))
        st.admin_log(me["id"], "tool.saved", {"name": name, **out})
        return out

    # the three levels, for everyone
    @app.get("/v1/ai/explorer", tags=["AI"], summary="Agent Explorer: the levels, the agents you work with, their skills and tools")
    def explorer(me: dict = A):
        st = st_for(me["site"])
        actor = agent_actor(me["site"])
        agents = [ag.explain(h, st, a, actor, me["role"]) for a in ag.visible_to(h, st, me, actor)]
        skills = [s for s in sk.all_skills(st) if s.get("enabled", True)]
        return {"levels": public_levels(), "agents": agents, "skills": skills}

    @app.get("/v1/ai/agents/personal", tags=["AI"], summary="Your personal agent (created on first use)")
    def my_agent(me: dict = A):
        st = st_for(me["site"])
        return ag.explain(h, st, ag.personal_for(st, me), agent_actor(me["site"]), me["role"])

    @app.get("/v1/ai/agents/teams", tags=["AI"], summary="The Care Coordination Agent of every care team")
    def team_agents(me: dict = A):
        st = st_for(me["site"])
        actor = agent_actor(me["site"])
        return [ag.explain(h, st, a, actor, me["role"]) for a in ag.team_agents(h, st, actor) if a.get("enabled", True)]

    class ChatIn(BaseModel):
        message: str = ""
        conversation_id: Optional[str] = None
        context: Dict[str, Any] = Field(default_factory=dict)
        skill_id: Optional[str] = None

    def _ready(st: SiteState, me: dict, policy: dict) -> None:
        if me["role"] != "patient":
            _check(st, me, policy)
        elif not policy.get("ai_enabled", True):
            raise HTTPException(403, "AI is switched off by the administrator")
        if (st.kv_get("routes") or {}).get("agent") and not str(policy.get("inference_geo") or "").strip():
            raise HTTPException(422, "inference_geo is required before AI can call an external provider")

    def _provider_error(st: SiteState, me: dict, agent_id: str, e: Exception):
        if type(e).__module__.startswith(("anthropic", "clinestra.server.agentic.openai_compat")):
            st.audit(actor=me["id"], role=me["role"], feature=f"agent:{agent_id}", status="error", detail=str(e)[:300])
            return HTTPException(502, "AI provider error. An administrator can see details in Admin, Usage and audit.")
        return None

    @app.post("/v1/ai/agents/{agent_id}/chat", tags=["AI"], summary="One message to an agent (runs as the AI identity)")
    def agent_chat(agent_id: str, body: ChatIn, me: dict = A):
        st = st_for(me["site"])
        policy = policy_of(st)
        if agent_id == "personal":
            agent_id = ag.personal_for(st, me)["id"]
        elif agent_id.startswith("team__"):
            ag.team_agents(h, st, agent_actor(me["site"]))  # a care team's agent exists as soon as the team does
        agent = _agent_or_404(st, agent_id)
        if agent.get("kind") == "template":
            raise HTTPException(400, "This is a template: talk to your personal agent or a care team's agent")
        if not agent.get("enabled", True):
            raise HTTPException(403, "This agent is switched off")
        if agent["level"] == "patient" and me["role"] not in agent.get("audience", []) and "any" not in agent.get("audience", []):
            raise HTTPException(403, f"This agent is not available to role {me['role']}")
        if not body.message.strip() and not body.skill_id:
            raise HTTPException(400, "Send a message or start a skill")
        _ready(st, me, policy)
        try:
            return ag.chat(h, st, agent, me, me["_actor"], agent_actor(me["site"]), body.message.strip(), body.conversation_id,
                           body.context, policy, body.skill_id)
        except ag.LevelError as e:
            raise HTTPException(403, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        except HarnessError as e:
            raise herr(e)
        except Exception as e:  # provider errors (anthropic.APIError) without importing the SDK at module load
            err = _provider_error(st, me, agent_id, e)
            if err:
                raise err
            raise

    class RunIn(BaseModel):
        skill_id: str

    @app.post("/v1/ai/agents/{agent_id}/runs", tags=["AI"], summary="Run a care-team skill on its own (read only), as its schedule would")
    def agent_run(agent_id: str, body: RunIn, me: dict = A):
        st = st_for(me["site"])
        policy = policy_of(st)
        if agent_id.startswith("team__"):
            ag.team_agents(h, st, agent_actor(me["site"]))
        agent = _agent_or_404(st, agent_id)
        _ready(st, me, policy)
        try:
            return ag.run(h, st, agent, me, agent_actor(me["site"]), body.skill_id, policy)
        except ag.LevelError as e:
            raise HTTPException(403, str(e))
        except HarnessError as e:
            raise herr(e)
        except Exception as e:
            err = _provider_error(st, me, agent_id, e)
            if err:
                raise err
            raise

    @app.get("/v1/ai/agents/{agent_id}/runs", tags=["AI"], summary="Reports from a care-team agent's own runs")
    def agent_runs(agent_id: str, me: dict = A):
        st = st_for(me["site"])
        _agent_or_404(st, agent_id)
        return ag.runs(st, agent_id)

    # ───────────────────────── patient portal ─────────────────────────
    class PhoneIn(BaseModel):
        phone: str
        site: Optional[str] = None

    class VerifyIn(BaseModel):
        phone: str
        otp: str
        site: Optional[str] = None

    class SessionIn(BaseModel):
        patient_id: str
        ticket: Optional[str] = None
        site: Optional[str] = None

    def _portal_ctx(site: Optional[str] = None):
        s = portal_site(site)
        return st_for(s), svc(PORTAL_ID, s)

    @app.get("/v1/portal/config", tags=["Patient portal"])
    def portal_config(site: Optional[str] = None):
        st, service = _portal_ctx(site)
        s = portal.settings(st)
        out = {k: s[k] for k in ("enabled", "app_name", "tagline", "primary_color", "logo_url", "tabs", "coach_agent_id", "languages", "otp_length")}
        out["dev_mode"] = s["otp_provider"] in ("dev_screen", "dev_fixed")
        org = s["organization"]
        out["organization"] = org
        try:
            directory = portal.public_directory(h, st, service)
        except HarnessError:
            directory = {"programs": [], "providers": []}
        out["programs"] = directory["programs"] if s.get("show_programs", True) else []
        out["providers"] = directory["providers"] if s.get("show_providers", True) else []
        ivs = iv.interview_settings(s)
        rt = iv.realtime_settings(st)
        name = org.get("short_name") or org.get("name") or s["app_name"]
        out["interview"] = {"enabled": bool(ivs["enabled"] and rt["enabled"] and rt["key_configured"]), "title": ivs["title"].replace("{org}", name),
                            "subtitle": ivs["subtitle"], "max_minutes": rt["max_minutes"]}
        return out

    @app.get("/v1/portal/site/{site_key}/config", tags=["Patient portal"])
    def portal_site_config(site_key: str, site: Optional[str] = None):
        st, service = _portal_ctx(site)
        if not portal.settings(st)["enabled"]:
            raise HTTPException(403, "Profile has been disabled")
        if site_key.lower() != portal.site_id(st):
            raise HTTPException(404, "Site not found")
        return portal.site_config(h, st, service, portal_config(site)["interview"])

    @app.get("/v1/portal/site", tags=["Patient portal"])
    def portal_default_site(site: Optional[str] = None):
        st, _ = _portal_ctx(site)
        return {"site_id": portal.site_id(st)}

    class InterviewIn(BaseModel):
        patient_id: Optional[str] = None

    class FinishIn(BaseModel):
        patient_id: Optional[str] = None
        transcript: List[dict] = Field(default_factory=list)   # [{role: user|assistant, text}]
        findings: Optional[dict] = None                         # finish_interview arguments from the model

    def _patient(me: dict) -> Actor:
        if me.get("role") != "patient":
            raise HTTPException(403, "The voice interview is for patients signed in to the portal")
        return me["_actor"]

    @app.post("/v1/portal/interview/session", tags=["Patient portal"])
    def interview_session(body: InterviewIn, me: dict = A):
        patient = _patient(me)
        st = st_for(me["site"])
        s = portal.settings(st)
        org = s["organization"].get("short_name") or s["organization"].get("name") or s["app_name"]
        try:
            return iv.create_session(h, st, patient, body.patient_id, org, iv.interview_settings(s))
        except LookupError as e:
            raise HTTPException(503, str(e))
        except PermissionError as e:
            raise HTTPException(403, str(e))
        except HarnessError as e:
            raise herr(e)
        except Exception as e:  # httpx errors, OpenAI refusals
            if isinstance(e, RuntimeError) or type(e).__module__.startswith("httpx"):
                raise HTTPException(502, str(e))
            raise

    @app.post("/v1/portal/site/{site_key}/interview/guest-session", tags=["Patient portal"],
              summary="Speak first: a voice session before sign-in (no record access; rate limited)")
    def interview_guest_session(site_key: str, request: Request, site: Optional[str] = None):
        st, _ = _portal_ctx(site)
        s = portal.settings(st)
        if not s["enabled"] or site_key.lower() != portal.site_id(st):
            raise HTTPException(404, "Site not found")
        org = s["organization"].get("short_name") or s["organization"].get("name") or s["app_name"]
        try:
            peer = request.client.host if request.client else "unknown"
            # Behind a local reverse proxy (dev server, nginx) every caller is loopback: use the forwarded client address.
            fwd = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()  # the hop our proxy appended
            ip = fwd if fwd and peer in ("127.0.0.1", "::1", "localhost") else peer
            return iv.create_guest_session(st, ip, org, iv.interview_settings(s))
        except LookupError as e:
            raise HTTPException(503, str(e))
        except PermissionError as e:
            raise HTTPException(403, str(e))
        except OverflowError as e:
            raise HTTPException(429, str(e))
        except Exception as e:  # httpx errors, OpenAI refusals
            if isinstance(e, RuntimeError) or type(e).__module__.startswith("httpx"):
                raise HTTPException(502, str(e))
            raise

    @app.post("/v1/portal/interview/finish", tags=["Patient portal"])
    def interview_finish(body: FinishIn, me: dict = A):
        patient = _patient(me)
        st = st_for(me["site"])
        try:
            return iv.finish(h, st, patient, svc(AGENT_ID, me["site"]), svc(PORTAL_ID, me["site"]), body.patient_id, body.transcript,
                             body.findings, iv.interview_settings(portal.settings(st)))
        except ValueError as e:
            raise HTTPException(400, str(e))
        except HarnessError as e:
            raise herr(e)

    @app.get("/v1/admin/realtime", tags=["Administration"])
    def get_realtime(me: dict = ADMIN):
        st = st_for(me["site"])
        return {"settings": iv.realtime_settings(st), "interview": iv.interview_settings(portal.settings(st))}

    @app.put("/v1/admin/realtime", tags=["Administration"])
    def put_realtime(body: Dict[str, Any], me: dict = ADMIN):
        st = st_for(me["site"])
        if "interview" in body:
            cur = st.kv_get("portal") or {}
            cur["interview"] = {**(cur.get("interview") or {}), **{k: v for k, v in body["interview"].items() if k in iv.DEFAULT_INTERVIEW}}
            st.kv_set("portal", cur)
        return {"settings": iv.save_realtime(st, body.get("settings") or {}, me["id"]), "interview": iv.interview_settings(portal.settings(st))}

    @app.post("/v1/admin/realtime/test", tags=["Administration"])
    def test_realtime(me: dict = ADMIN):
        return iv.test_key(st_for(me["site"]))

    @app.post("/v1/portal/otp/request", tags=["Patient portal"])
    def portal_otp_request(body: PhoneIn, request: Request):
        st, service = _portal_ctx(body.site)
        try:
            ip = request.client.host if request.client else None
            return portal.request_otp(h, st, service, body.phone, ip=ip)
        except (ValueError, PermissionError) as e:
            raise HTTPException(400, str(e))

    @app.post("/v1/portal/otp/verify", tags=["Patient portal"])
    def portal_otp_verify(body: VerifyIn):
        st, service = _portal_ctx(body.site)
        try:
            return portal.verify_otp(h, st, service, body.phone, body.otp)
        except ValueError as e:
            raise HTTPException(400, str(e))

    def _bearer(request: Request) -> Optional[str]:
        header = request.headers.get("authorization", "")
        return header[7:] if header.lower().startswith("bearer ") else None

    @app.post("/v1/portal/session", tags=["Patient portal"])
    def portal_session(body: SessionIn, request: Request):
        token = _bearer(request)
        site = body.site or (portal.site_of_token(token) if token and not body.ticket else None)
        st, service = _portal_ctx(site)
        try:
            return portal.start_session(h, st, service, body.ticket, body.patient_id, current_token=None if body.ticket else token)
        except (ValueError, PermissionError) as e:
            raise HTTPException(400, str(e))

    @app.post("/v1/portal/logout", tags=["Patient portal"])
    def portal_logout(request: Request):
        token = _bearer(request) or ""
        site = portal.site_of_token(token)
        if site:
            portal.end_session(st_for(site), token)
        return {"ok": True}

    @app.get("/v1/admin/portal", tags=["Administration"])
    def get_portal(me: dict = ADMIN):
        st = st_for(me["site"])
        return {"settings": portal.settings(st), "sessions": portal.active_sessions(st)}

    @app.put("/v1/admin/portal", tags=["Administration"])
    def put_portal(body: Dict[str, Any], me: dict = ADMIN):
        st = st_for(me["site"])
        clean = {k: v for k, v in body.items() if k in portal.DEFAULT_PORTAL}
        cur = st.kv_get("portal") or {}
        if "organization" in clean:
            clean["organization"] = {**(cur.get("organization") or {}), **clean["organization"]}
        st.kv_set("portal", {**cur, **clean})
        portal.clear_cache(st.site)
        st.admin_log(me["id"], "portal.saved", {k: v for k, v in clean.items() if k != "otp_webhook_url"})
        return {"settings": portal.settings(st), "sessions": portal.active_sessions(st)}

    @app.post("/v1/admin/portal/purge", tags=["Administration"])
    def purge_portal(me: dict = ADMIN):
        return {"removed": portal.purge(st_for(me["site"]))}
