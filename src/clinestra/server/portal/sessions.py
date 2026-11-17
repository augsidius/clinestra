"""Portal settings, OTP sign-in, patient sessions and the patient-app site config (see :mod:`clinestra.portal`)."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from typing import Optional

from ..agentic.local import HarnessError, call
from ..agentic.state import SiteState
from ...base import Actor

DEFAULT_PORTAL = {
    "enabled": True,
    "app_name": "My Health",
    "tagline": "Your care, every day",
    "primary_color": "#0d9488",
    "logo_url": "",
    "otp_provider": "dev_screen",       # dev_screen | dev_fixed | webhook
    "dev_fixed_otp": "123456",          # dev_fixed only: every sign-in uses this code and nothing is shown on screen (demos)
    "otp_webhook_url": "",
    "otp_length": 6,
    "otp_ttl_seconds": 300,
    "session_hours": 12,
    "country_code": "91",
    "tabs": {"home": True, "actions": True, "journal": True, "advice": True, "visits": True, "ask": True, "profile": True},
    "coach_agent_id": "patient_coach",
    "require_ai_consent_for_coach": True,
    "languages": ["en"],
    # Enterprise profile: the portal's starting screen shown before sign-in.
    "organization": {
        "name": "", "short_name": "", "assistant_name": "", "tagline": "", "logo_url": "", "about": "", "address": "", "phone": "", "email": "", "website": "",
        "languages": [], "specializes_in": [], "stats": [],
        "process": ["Sign in with your mobile number", "Tell the AI assistant what's bothering you", "Your doctor reviews it", "Follow your care plan here"],
    },
    "show_programs": True,
    "show_providers": True,
    "interview": {},
    "site_id": "",                      # URL key of the patient app: /portal/<site_id>
    "site_config": {},
    "features": {},                     # patient-app feature switches (booleans), e.g. booking, voiceIntake
    "pages": {},                        # e.g. {"home": {"sections": [{id, type, enabled}]}} (order + on/off)
    "image_hosts": [],                  # allowed remote image hostnames (stored only)
    # Content in other languages, per language code: {"hi": {"name", "short_name", "tagline", "about", "address",
    # "assistant_name", "specializes_in": [], "process": [], "stats": [{label, value}]}}. Sent as the patient app's
    # `localized.<lang>` overlay; UI labels come from the app's own translation files.
    "translations": {},                  # optional overrides deep-merged into the generated site config
}

SESSION_PREFIX = "pt-"
BOOT_ID = secrets.token_hex(8)


def settings(st: SiteState) -> dict:
    saved = st.kv_get("portal") or {}
    out = {**DEFAULT_PORTAL, **saved}
    out["organization"] = {**DEFAULT_PORTAL["organization"], **(saved.get("organization") or {})}
    out["tabs"] = {**DEFAULT_PORTAL["tabs"], **(saved.get("tabs") or {})}
    out["features"] = _merge(DEFAULT_PORTAL["features"], saved.get("features") or {})
    out["pages"] = _merge(DEFAULT_PORTAL["pages"], saved.get("pages") or {})
    return out


_public_cache: dict = {}


def public_directory(h, st: SiteState, svc: Actor) -> dict:
    """Programs (cohorts + their protocols) and providers for the enterprise profile, read with the portal service
    identity and reduced to what is safe to show before sign-in. Cached for a minute per site."""
    hit = _public_cache.get(st.site)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    defs = call(h, svc, "GET", "/definitions", params={"status": "published"})
    latest: dict = {}
    for d in defs:
        k = (d["def_kind"], d["key"])
        if k not in latest or (d["site"] != "*" and latest[k]["site"] == "*") or d["version"] > latest[k]["version"]:
            latest[k] = d
    protos = {d["key"]: d for (kind, _), d in latest.items() if kind == "protocol"}
    programs = []
    for (kind, key), c in sorted(latest.items(), key=lambda kv: kv[1]["name"]):
        if kind != "cohort" or c["config"].get("manual_only") and not c["config"].get("protocols"):
            continue
        pr = next((protos[p] for p in c["config"].get("protocols", []) if p in protos), None)
        programs.append({"key": key, "name": c["name"], "protocol": pr["name"] if pr else None,
                         "population": (pr or {}).get("config", {}).get("pico", {}).get("population"),
                         "intervention": (pr or {}).get("config", {}).get("pico", {}).get("intervention")})
    providers = [{"name": p["name"], "specialty": p.get("specialty"), "qualifications": p.get("qualifications") or []}
                 for p in call(h, svc, "GET", "/providers") if p["status"] == "active"]
    data = {"programs": programs, "providers": providers}
    _public_cache[st.site] = (time.time(), data)
    return data


def clear_cache(site: str) -> None:
    _public_cache.pop(site, None)


def normalize(phone: str, cc: str) -> str:
    d = re.sub(r"\D", "", phone or "")
    if len(d) == 10:
        return cc + d
    if len(d) == 11 and d.startswith("0"):
        return cc + d[1:]
    return d


def _hash(code: str, phone: str) -> str:
    return hashlib.sha256(f"{phone}:{code}".encode()).hexdigest()


def _token_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _patients_by_phone(h, st: SiteState, svc: Actor, phone: str) -> list:
    out = []
    for p in call(h, svc, "GET", "/patients", params={"status": "active"}):
        ph = normalize(str((p.get("demographics") or {}).get("phone", "")), settings(st)["country_code"])
        if ph and ph == phone:
            out.append({"id": p["id"], "name": p["name"], "gender": p.get("gender"), "age": p.get("age"),
                        "is_guardian": p.get("is_guardian", False), "employee_no": (p.get("custom_fields") or {}).get("employee_no")})
    return out


def otp_provider_allowed(s: dict) -> None:
    """dev_fixed is a local development provider. Anywhere else the portal must not accept it."""
    if s.get("otp_provider") == "dev_fixed" and os.environ.get("CLINESTRA_ENV") != "development":
        raise PermissionError(
            "dev_fixed OTP is refused unless CLINESTRA_ENV=development and the portal is bound to localhost")


def returns_code_on_screen_allowed() -> bool:
    """A code in the HTTP response lets anyone sign in as any patient, so production never does it."""
    return os.environ.get("CLINESTRA_MODE") != "production"


def _epoch(st: SiteState) -> str:
    epoch = st.kv_get("portal_session_epoch")
    if not epoch:
        st.kv_set("portal_session_epoch", BOOT_ID)
        return BOOT_ID
    return epoch


def invalidate_sessions(st: SiteState) -> None:
    """Drop every portal session. Called on deploy and when the operator asks."""
    st.kv_set("portal_session_epoch", secrets.token_hex(8))


def request_otp(h, st: SiteState, svc: Actor, phone_raw: str, ip: Optional[str] = None) -> dict:
    s = settings(st)
    if not s["enabled"]:
        raise PermissionError("The patient portal is switched off")
    otp_provider_allowed(s)
    phone = normalize(phone_raw, s["country_code"])
    if len(phone) < 10:
        raise ValueError("Enter a valid mobile number")
    if ip:
        bucket = st.get("_portal.otp_ip", ip) or {}
        recent = [t for t in (bucket.get("times") or []) if time.time() - t < 600]
        if len(recent) >= 8:
            raise ValueError("Too many codes requested. Please wait and try again.")
        st.put("_portal.otp_ip", ip, {"times": recent + [time.time()]})
    last = st.get("_portal.otp", phone)
    if last and not last.get("_deleted") and time.time() - last["sent"] < 30:
        raise ValueError("Please wait 30 seconds before asking for another code")
    found = _patients_by_phone(h, st, svc, phone)
    provider = s["otp_provider"]
    if provider != "webhook" and not returns_code_on_screen_allowed():
        raise ValueError("Production needs an OTP delivery webhook. Set otp_provider to webhook.")
    if provider == "dev_fixed":
        code = str(s.get("dev_fixed_otp") or "")
    else:
        length = int(s["otp_length"])
        code = "".join(secrets.choice("0123456789") for _ in range(length))
    st.put("_portal.otp", phone, {"code_hash": _hash(code, phone), "expires": time.time() + int(s["otp_ttl_seconds"]),
                                  "attempts": 0, "sent": time.time()})
    st.audit(actor="portal", role="system", feature="otp.request", status="ok", detail=_hash(phone, "otp-request"))
    # Known and unknown numbers get the same body. The code is only delivered to a registered phone.
    out = {"sent": True, "phone": phone, "expires_in": int(s["otp_ttl_seconds"])}
    if provider == "webhook" and s.get("otp_webhook_url") and found:
        try:
            import httpx
            httpx.post(s["otp_webhook_url"], json={"phone": phone, "otp": code, "app": s["app_name"]}, timeout=10)
        except Exception:
            st.audit(actor="portal", role="system", feature="otp.webhook", status="error", detail=_hash(phone, "otp-request"))
    if provider == "dev_screen" or (provider == "dev_fixed" and os.environ.get("CLINESTRA_ENV") == "development"):
        out["dev_otp"] = code
    return out


def verify_otp(h, st: SiteState, svc: Actor, phone_raw: str, code: str) -> dict:
    s = settings(st)
    phone = normalize(phone_raw, s["country_code"])
    r = st.get("_portal.otp", phone)
    if not r or r.get("_deleted") or r["expires"] < time.time():
        raise ValueError("The code has expired. Ask for a new one.")
    if r["attempts"] >= 5:
        raise ValueError("Too many attempts. Ask for a new code.")
    if not hmac.compare_digest(r["code_hash"], _hash(code.strip(), phone)):
        st.put("_portal.otp", phone, {**r, "attempts": r["attempts"] + 1})
        st.audit(actor="portal", role="system", feature="otp.verify", status="error", detail=_hash(phone, "otp-verify"))
        raise ValueError("That code is not right")
    st.delete("_portal.otp", phone)
    patients = _patients_by_phone(h, st, svc, phone)
    st.audit(actor="portal", role="system", feature="otp.verify", status="ok" if patients else "error",
             detail=_hash(phone, "otp-verify"))
    if not patients:
        raise ValueError("That code is not right")
    ticket = secrets.token_urlsafe(24)
    st.put("_portal.ticket", ticket, {"phone": phone, "patients": patients, "expires": time.time() + 900})
    return {"ticket": ticket, "profiles": patients}


def _represents(h, svc: Actor, patient_id: str) -> list:
    """Patients this person may act for: relationships where they are the guardian (the harness re-checks every access)."""
    out = []
    for p in call(h, svc, "GET", "/patients"):
        if p["id"] == patient_id:
            continue
        for r in call(h, svc, "GET", "/relationships", params={"patient_id": p["id"]}):
            if r["related_patient_id"] == patient_id and r["is_guardian"] and r["status"] == "active":
                out.append(p["id"])
    return out


def site_of_token(token: str) -> Optional[str]:
    """Session tokens carry their site: ``pt-<random>.<site>``."""
    if not token or not token.startswith(SESSION_PREFIX) or "." not in token:
        return None
    return token[len(SESSION_PREFIX):].split(".", 1)[1] or None


def _session(st: SiteState, token: str) -> Optional[dict]:
    doc = st.get("_portal.session", _token_id(token))
    return doc if doc and not doc.get("_deleted") else None


def start_session(h, st: SiteState, svc: Actor, ticket: Optional[str], patient_id: str, current_token: Optional[str] = None) -> dict:
    """Create a session for one of the verified profiles (also used to switch profile)."""
    s = settings(st)
    if ticket:
        t = st.get("_portal.ticket", ticket)
        if not t or t.get("_deleted") or t["expires"] < time.time():
            raise ValueError("Sign-in expired. Please start again.")
        phone, profiles = t["phone"], t["patients"]
    elif current_token:
        cur = _session(st, current_token)
        if not cur or cur["expires"] < time.time():
            raise ValueError("Session expired. Please sign in again.")
        phone, profiles = cur["phone"], cur["profiles"]
        end_session(st, current_token)
    else:
        raise ValueError("Sign in first")
    if patient_id not in {p["id"] for p in profiles}:
        raise PermissionError("That profile is not linked to this phone number")
    purge(st)
    token = f"{SESSION_PREFIX}{secrets.token_urlsafe(24)}.{st.site}"
    rep = _represents(h, svc, patient_id)
    exp = time.time() + int(s["session_hours"]) * 3600
    st.put("_portal.session", _token_id(token), {"patient_id": patient_id, "phone": phone, "profiles": profiles,
                                                 "represents": rep, "expires": exp, "created": time.time(),
                                                 "epoch": _epoch(st)})
    if ticket:
        st.delete("_portal.ticket", ticket)
    return {"token": token, "patient_id": patient_id, "profiles": profiles, "represents": rep, "expires": exp}


def session_actor(st: SiteState, token: str) -> Optional[Actor]:
    """The patient actor for a live session token, or None."""
    doc = _session(st, token)
    if not doc:
        return None
    if doc.get("epoch") != _epoch(st):
        return None
    if doc["expires"] < time.time():
        st.delete("_portal.session", doc["id"])
        return None
    return Actor(id=f"portal:{doc['patient_id']}", site=st.site, role="patient", patient_id=doc["patient_id"],
                 represents=doc.get("represents") or [])


def end_session(st: SiteState, token: str) -> None:
    st.delete("_portal.session", _token_id(token))


def purge(st: SiteState) -> int:
    now = time.time()
    expired = [d for d in st.records("_portal.session") if d["expires"] < now]
    for d in expired:
        st.delete("_portal.session", d["id"])
    for t in st.records("_portal.ticket"):
        if t["expires"] < now:
            st.delete("_portal.ticket", t["id"])
    return len(expired)


def active_sessions(st: SiteState) -> list:
    rows = sorted(st.records("_portal.session"), key=lambda r: r["created"], reverse=True)
    return [{"patient_id": r["patient_id"], "phone": "•" * (len(r["phone"]) - 4) + r["phone"][-4:],
             "expires": r["expires"], "created": r["created"]} for r in rows]


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _shade(hex_color: str, f: float) -> str:
    hx = hex_color.lstrip("#")
    if len(hx) != 6:
        return hex_color
    r, g, b = (int(hx[i:i + 2], 16) for i in (0, 2, 4))
    if f < 0:
        r, g, b = (int(c * (1 + f)) for c in (r, g, b))
    else:
        r, g, b = (int(c + (255 - c) * f) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def site_id(st: SiteState) -> str:
    s = settings(st)
    return (s.get("site_id") or s["organization"].get("short_name") or "app").lower().replace(" ", "-")


def site_config(h, st: SiteState, svc: Actor, interview: dict) -> dict:
    """The patient app's site config in the patient app's format, generated from the tenant settings."""
    s = settings(st)
    org = s["organization"]
    directory = {"programs": [], "providers": []}
    try:
        directory = public_directory(h, st, svc)
    except HarnessError:
        pass
    pc = s.get("primary_color") or "#3B82F6"
    sid = site_id(st)
    providers = [{"provider_id": f"prov_{i}", "id": f"prov_{i}", "name": p["name"], "specialty": p.get("specialty"),
                  "qualification": ", ".join(p.get("qualifications") or []), "specializes_in": [p["specialty"]] if p.get("specialty") else []}
                 for i, p in enumerate(directory["providers"])] if s.get("show_providers", True) else []
    services = [{"service_id": p["key"], "name": p["name"], "description": p.get("intervention") or p.get("population") or "",
                 "program_details": {"outcome_metrics": [], "milestones": []}}
                for p in directory["programs"]] if s.get("show_programs", True) else []
    feats = {"booking": False, **(s.get("features") or {})}
    stats = [{"label": x.get("label"), "value": x.get("value"), "enabled": True} for x in org.get("stats") or []]
    steps = [{"number": i + 1, "title": t, "description": ""} for i, t in enumerate(org.get("process") or [])]
    langs = list(dict.fromkeys([*(s.get("languages") or ["en"]), *[k for k, v in (s.get("translations") or {}).items() if v]]))
    cfg = {
        "site": sid, "site_name": sid, "siteId": sid, "config_type": "enterprise", "status": "active",
        "organization": {"name": org.get("name") or s["app_name"], "tagline": org.get("tagline") or s["tagline"], "logo": org.get("logo_url") or s.get("logo_url"),
                         "about": org.get("about") or org.get("description"), "cover": org.get("cover_url")},
        "clinic": {"name": org.get("name"), "address": org.get("address"), "phone": org.get("phone"), "email": org.get("email")},
        "languages": org.get("languages") or [], "specializes_in": org.get("specializes_in") or [],
        "supported_languages": langs,
        "theme_config": {"colors": {"action": {"main": pc, "light": _shade(pc, 0.85), "dark": _shade(pc, -0.25), "contrast": "#FFFFFF", "hover": _shade(pc, -0.12)}}},
        "features": feats,
        "bookConsultation": {"enabled": bool(feats.get("booking"))},
        "stats": stats,
        "assistant_name": org.get("assistant_name") or org.get("short_name") or s["app_name"],
        "i18n": {"enabled_languages": langs, "default_language": langs[0]},
        "voiceIntake": {"enabled": bool(interview.get("enabled")) and feats.get("voiceIntake", True) is not False, "label": interview.get("title"), "subtitle": interview.get("subtitle")},
        "services": services, "providers": providers,
        "service_provider_id": providers[0]["provider_id"] if providers else sid,
        "pages": {"home": {"enabled": True, "sections": _sections(s, org, services, providers, steps, stats)}},
    }
    cfg["localized"] = _localized(s.get("translations") or {})
    return _merge(cfg, s.get("site_config") or {})


def _localized(translations: dict) -> dict:
    """Admin translations -> the localized overlay (applyLocale): organization/clinic scalars, lists, step texts."""
    out = {}
    for lang, tr in translations.items():
        if not isinstance(tr, dict) or not tr:
            continue
        fork: dict = {}
        org = {k: tr[k] for k in ("name", "short_name", "tagline", "about") if tr.get(k)}
        if org:
            fork["organization"] = org
        if tr.get("address"):
            fork["clinic"] = {"address": tr["address"]}
        if tr.get("specializes_in"):
            fork["specializes_in"] = list(tr["specializes_in"])
        if tr.get("stats"):
            fork["stats"] = [{"label": x.get("label"), "value": x.get("value"), "enabled": True} for x in tr["stats"] if isinstance(x, dict)]
        if tr.get("process"):
            fork["pages"] = {"home": {"sections": [{"id": "processFlow", "config": {"steps": [
                {"number": i + 1, "title": t, "description": ""} for i, t in enumerate(tr["process"])]}}]}}
        if tr.get("assistant_name"):
            fork["assistant_name"] = tr["assistant_name"]
        out[lang] = fork
    return out


def _sections(s: dict, org: dict, services: list, providers: list, steps: list, stats: list) -> list:
    """Home sections with generated config. ``pages.home.sections`` (from Admin) sets order and on/off; sections that
    need data (programs, providers, steps) are dropped while empty."""
    hero = {"headline": org.get("name") or s["app_name"], "subheadline": org.get("about") or org.get("description") or ""}
    if org.get("cover_url"):
        hero["image"] = org["cover_url"]
    gen = {
        "enterpriseProfile": ("EnterpriseProfile", {"showShare": True}, True),
        "hero": ("HeroSection", hero, True),
        "quickStats": ("QuickStats", {}, bool(stats)),
        "specialties": ("Specialties", {}, bool(org.get("specializes_in"))),
        "programGrid": ("ProgramGrid", {"title": "Care programs"}, bool(services)),
        "providerShowcase": ("ProviderShowcase", {"title": "Our doctors"}, bool(providers)),
        "processFlow": ("ProcessFlow", {"steps": steps}, bool(steps)),
        "clinicInfo": ("ClinicInfo", {}, True),
    }
    chosen = ((s.get("pages") or {}).get("home") or {}).get("sections")
    if not chosen:
        chosen = [{"id": i, "enabled": True} for i in ("enterpriseProfile", "programGrid", "providerShowcase", "hero", "processFlow")]
    out = []
    for c in chosen:
        if not c.get("enabled", True) or c.get("id") not in gen:
            continue
        typ, cfg, has_data = gen[c["id"]]
        if has_data:
            out.append({"id": c["id"], "type": c.get("type") or typ, "config": {**cfg, **(c.get("config") or {})}})
    return out
