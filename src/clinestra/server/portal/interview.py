"""
Realtime AI symptom interview for the patient portal (the patient app's "Speak to ..." button, on the OpenAI
Realtime API with the tenant's own key).

  1. The portal asks for a session (patient token). The server checks the patient's AI consent, builds the
     interviewer instructions (tenant-configurable) and mints a short-lived OpenAI client secret with the stored key.
     The OpenAI key itself is encrypted at rest and never leaves the server.
  2. The browser talks to OpenAI directly over WebRTC (mic in, voice out, live transcripts).
  3. When the model has enough it calls ``finish_interview`` with structured findings; the browser posts those plus
     the transcript to ``/v1/portal/interview/finish``. The server records the case: a conversation with the
     transcript, an encounter + clinical record, an AI-drafted note page (for clinician review, never final), and a
     review task (urgent when red flags were reported).

A future managed voice service plugs in at :class:`clinestra.providers.remote.RemoteVoiceProvider`.
"""
from __future__ import annotations

import time
from typing import Optional

from ... import secrets as sec
from ..agentic.local import AGENT_ID, PORTAL_ID, call
from ..agentic.state import SiteState
from ...base import Actor

OPENAI = "https://api.openai.com/v1"

DEFAULT_REALTIME = {
    "enabled": True,
    "model": "gpt-realtime",
    "voice": "marin",
    "transcribe_model": "gpt-4o-mini-transcribe",
    "max_minutes": 8,
}

DEFAULT_INTERVIEW = {
    "enabled": True,
    "title": "Speak to {org}",
    "subtitle": "Tell us what's bothering you. Our AI assistant asks a few questions and records it for your doctor.",
    "encounter_key": "ai_interview",
    "page_key": "ai_symptom_interview",
    "greeting": "Hello {first_name}! I'm the {org} health assistant. What's bothering you today?",
    "instructions": (
        "You are a friendly health intake assistant for {org}. You are talking by voice with {name}, "
        "a {age}-year-old {gender}. Known problems: {conditions}.\n"
        "Speak in the language the patient uses (simple words). Ask ONE short question at a time and wait.\n"
        "Cover: main complaint; when it started and how it has changed; severity 0-10; associated symptoms; what makes it "
        "better or worse; relevant exposures; medicines and allergies.\n"
        "Screen for red flags and ask about them directly: chest pain, severe breathlessness, coughing blood, fainting, "
        "weakness or numbness of a limb, loss of bladder control, high fever, injury with heavy bleeding.\n"
        "Never diagnose, never suggest medicines or doses. If a red flag is present, tell them calmly to seek care "
        "or call for help now. When you have enough (usually 6-10 questions), thank them, say the doctor will review it, and "
        "call finish_interview with what you learned."),
}

# Before sign-in ("speak first"): no record is read, nothing is saved server-side. The summary stays on the device
# until the person signs in and chooses to send it. Limits keep an open endpoint from running up the OpenAI bill.
DEFAULT_GUEST = {
    "allow_guest": True,
    "guest_minutes": 5,
    "guest_per_ip_per_hour": 4,
    "guest_per_day": 200,
    "guest_instructions": (
        "You are a friendly health intake assistant for {org}. You are talking by voice with a worker who has not signed in yet; "
        "you know nothing about their record, so do not ask for their name, phone number or ID.\n"
        "Speak in the language they use (simple words). Ask ONE short question at a time and wait.\n"
        "Cover: main complaint; when it started and how it has changed; severity 0-10; associated symptoms; what makes it "
        "better or worse; relevant exposures; medicines and allergies.\n"
        "Screen for red flags and ask about them directly: chest pain, severe breathlessness, coughing blood, fainting, "
        "weakness or numbness of a limb, loss of bladder control, high fever, injury with heavy bleeding.\n"
        "Never diagnose, never suggest medicines or doses. If a red flag is present, tell them calmly to seek care "
        "or call for help now. When you have enough (usually 6-10 questions), thank them, tell them to sign in so the summary "
        "reaches their doctor, and call finish_interview with what you learned."),
    "guest_greeting": "Hello! I'm the {org} health assistant. What's bothering you today?",
}

FINISH_TOOL = {
    "type": "function",
    "name": "finish_interview",
    "description": "Call once the interview is complete, with the structured findings for the clinician.",
    "parameters": {
        "type": "object",
        "properties": {
            "chief_complaint": {"type": "string", "description": "Main complaint in the patient's words, translated to English"},
            "history": {"type": "string", "description": "Onset, duration, course, triggers, relieving factors"},
            "associated_symptoms": {"type": "string"},
            "red_flags": {"type": "array", "items": {"type": "string"}, "description": "Red flags reported (empty if none)"},
            "work_exposure": {"type": "string", "description": "Relevant exposures (work, environment)"},
            "medications_allergies": {"type": "string"},
            "severity": {"type": "number", "description": "0-10"},
            "duration_days": {"type": "number"},
            "urgency": {"type": "string", "enum": ["routine", "soon", "urgent"]},
            "summary": {"type": "string", "description": "Two-sentence clinician summary"},
        },
        "required": ["chief_complaint", "summary", "urgency"],
    },
}


class _Default(dict):
    def __missing__(self, k):
        return ""


# ───────────────────────── settings ─────────────────────────
def realtime_settings(st: SiteState, public: bool = True) -> dict:
    saved = st.kv_get("realtime") or {}
    s = {**DEFAULT_REALTIME, **saved}
    if public:
        s.pop("secret", None)
        s["key_configured"] = bool(saved.get("secret"))
    return s


def save_realtime(st: SiteState, body: dict, actor: str) -> dict:
    cur = st.kv_get("realtime") or {}
    new = {**cur, **{k: v for k, v in body.items() if k in DEFAULT_REALTIME}}
    key = (body.get("api_key") or "").strip()
    if key:
        new["secret"] = sec.encrypt({"api_key": key})
        new["secret_hint"] = sec.mask(key)
    st.kv_set("realtime", new)
    st.admin_log(actor, "realtime.saved", {k: v for k, v in new.items() if k != "secret"})
    return realtime_settings(st)


def _session_body(rt: dict, instructions: str, seconds: int = 600) -> dict:
    return {"expires_after": {"anchor": "created_at", "seconds": seconds},
            "session": {"type": "realtime", "model": rt["model"], "instructions": instructions,
                        "audio": {"input": {"transcription": {"model": rt["transcribe_model"]}, "turn_detection": {"type": "server_vad"}},
                                  "output": {"voice": rt["voice"]}},
                        "tools": [FINISH_TOOL], "tool_choice": "auto"}}


def _mint(st: SiteState, body: dict) -> dict:
    import httpx
    r = httpx.post(f"{OPENAI}/realtime/client_secrets", headers={"Authorization": f"Bearer {_api_key(st)}", "Content-Type": "application/json"},
                   json=body, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"OpenAI refused the session: {r.text[:300]}")
    return r.json()


def create_guest_session(st: SiteState, client_ip: str, org_name: str, interview_cfg: dict) -> dict:
    """A voice session before sign-in: generic instructions, no record access, rate limited per IP and per day."""
    import time
    rt = realtime_settings(st)
    cfg = {**DEFAULT_GUEST, **{k: v for k, v in interview_cfg.items() if k in DEFAULT_GUEST}}
    if not rt["enabled"] or not interview_cfg.get("enabled", True) or not cfg["allow_guest"]:
        raise PermissionError("Please sign in to use the voice assistant.")
    now = time.time()
    day, hour = time.strftime("%Y-%m-%d", time.gmtime(now)), int(now // 3600)
    counts = st.kv_get("interview_guest_counts", {}) or {}
    if counts.get("day") != day:
        counts = {"day": day, "total": 0, "ip": {}}
    ip_key = f"{client_ip}|{hour}"
    if counts["total"] >= int(cfg["guest_per_day"]) or counts["ip"].get(ip_key, 0) >= int(cfg["guest_per_ip_per_hour"]):
        raise OverflowError("The voice assistant is busy right now. Please sign in, or try again in a little while.")
    counts["total"] += 1
    counts["ip"] = {k: v for k, v in counts["ip"].items() if k.endswith(f"|{hour}")}
    counts["ip"][ip_key] = counts["ip"].get(ip_key, 0) + 1
    st.kv_set("interview_guest_counts", counts)
    fields = _Default(org=org_name)
    minutes = min(int(cfg["guest_minutes"]), int(rt["max_minutes"]))
    j = _mint(st, _session_body(rt, cfg["guest_instructions"].format_map(fields), seconds=minutes * 60 + 60))
    st.audit(actor="portal_guest", role="guest", feature="interview", model=rt["model"], provider="openai", status="ok", detail="realtime guest session")
    return {"client_secret": j.get("value") or j.get("client_secret", {}).get("value"), "expires_at": j.get("expires_at"), "model": rt["model"],
            "max_minutes": minutes, "greeting": cfg["guest_greeting"].format_map(fields), "guest": True}


def interview_settings(portal_settings: dict) -> dict:
    return {**DEFAULT_INTERVIEW, **(portal_settings.get("interview") or {})}


def _api_key(st: SiteState) -> str:
    secret = (st.kv_get("realtime") or {}).get("secret")
    if not secret:
        raise LookupError("The realtime voice assistant is not set up. An administrator adds the OpenAI key in Admin → Realtime voice.")
    return sec.decrypt(secret)["api_key"]


def test_key(st: SiteState) -> dict:
    import httpx
    try:
        r = httpx.get(f"{OPENAI}/models/{realtime_settings(st)['model']}", headers={"Authorization": f"Bearer {_api_key(st)}"}, timeout=15)
    except (LookupError, httpx.HTTPError) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": r.status_code == 200, "error": None if r.status_code == 200 else r.text[:300]}


# ───────────────────────── session ─────────────────────────
def _patient_context(h, patient: Actor, patient_id: str) -> dict:
    p = call(h, patient, "GET", f"/patients/{patient_id}")
    conds = [c.get("display") or c["code"] for c in call(h, patient, "GET", "/conditions", params={"patient_id": patient_id})
             if c["status"] in ("active", "remission")]
    consents = call(h, patient, "GET", "/consents", params={"patient_id": patient_id})
    return {"patient": p, "conditions": conds, "ai_ok": any(c["consent_scope"] == "ai" and c["status"] == "granted" for c in consents)}


def create_session(h, st: SiteState, patient: Actor, patient_id: Optional[str], org_name: str, interview_cfg: dict) -> dict:
    rt = realtime_settings(st)
    if not rt["enabled"] or not interview_cfg.get("enabled", True):
        raise PermissionError("The voice assistant is switched off.")
    pid = patient_id or patient.patient_id
    ctx = _patient_context(h, patient, pid)  # the harness enforces that this patient actor may read this record
    if not ctx["ai_ok"]:
        raise PermissionError("Please turn on 'Let the AI assistant read my record' in Me → My consents first.")
    p, cf = ctx["patient"], ctx["patient"].get("custom_fields") or {}
    policy = st.kv_get("policy") or {}
    if policy.get("send_patient_identifiers"):
        shown_name, first = p["name"], p["name"].split()[0]
        designation = cf.get("designation") or ""
        work_location = cf.get("work_location") or ""
    else:
        shown_name, first = "the patient", "the patient"
        designation, work_location = "", ""
    fields = _Default(org=org_name, name=shown_name, first_name=first, age=p.get("age") or "", gender=p.get("gender") or "",
                      designation=designation, work_location=work_location,
                      conditions=", ".join(ctx["conditions"]) or "none recorded")
    instructions = interview_cfg.get("instructions", DEFAULT_INTERVIEW["instructions"]).format_map(fields)
    if not policy.get("send_patient_identifiers"):
        from ..agentic.deid import redact, secrets_from_patient
        instructions = redact(instructions, secrets_from_patient(p))
    j = _mint(st, _session_body(rt, instructions))
    st.audit(actor=patient.id, role="patient", feature="interview", model=rt["model"], provider="openai", status="ok",
             detail="realtime session", patient_id=pid)
    return {"client_secret": j.get("value") or j.get("client_secret", {}).get("value"), "expires_at": j.get("expires_at"),
            "model": rt["model"], "patient_id": pid, "max_minutes": rt["max_minutes"],
            "greeting": interview_cfg.get("greeting", DEFAULT_INTERVIEW["greeting"]).format_map(fields)}


# ───────────────────────── finish: record the case ─────────────────────────
def finish(h, st: SiteState, patient: Actor, agent: Actor, svc: Actor, patient_id: Optional[str], transcript: list,
           findings: Optional[dict], interview_cfg: dict) -> dict:
    pid = patient_id or patient.patient_id
    call(h, patient, "GET", f"/patients/{pid}")  # access check as the patient
    lines = [t for t in transcript if (t.get("text") or "").strip()]
    if not lines:
        raise ValueError("Nothing was recorded.")
    f = findings or {}
    if not f.get("chief_complaint"):  # ended early: keep the patient's own words for the clinician
        said = [t["text"] for t in lines if t.get("role") == "user"]
        f = {"chief_complaint": said[0][:200] if said else "Not stated", "history": " ".join(said[1:])[:1500],
             "summary": "Interview ended before the assistant finished; see transcript.", "urgency": "soon", **f}
    red = [x for x in (f.get("red_flags") or []) if x]
    urgency = "urgent" if red else (f.get("urgency") or "routine")

    # 1. the transcript, as a conversation in the record (patient turns by the patient, assistant turns by the AI identity)
    conv = call(h, patient, "POST", "/conversations", {"patient_id": pid, "scope": "patient", "title": "AI symptom interview",
                                                       "structure": {"channel": "ai_interview"}})
    for t in lines:
        call(h, patient if t["role"] == "user" else agent, "POST", f"/conversations/{conv['id']}/turns", {"content": t["text"]})
    # 2. encounter + record + AI-drafted page (drafted, never final: a clinician reviews and finalizes)
    enc = call(h, svc, "POST", "/encounters", {"patient_id": pid, "definition_key": interview_cfg.get("encounter_key") or None,
                                               "encounter_type": "ai_interview", "reason": f"AI interview: {f['chief_complaint'][:80]}"})
    rec = next((r for r in call(h, svc, "GET", "/clinical-records", params={"patient_id": pid}) if r.get("encounter_id") == enc["id"]), None) \
        or call(h, svc, "POST", "/clinical-records", {"patient_id": pid, "encounter_id": enc["id"], "title": "AI interview record"})
    blocks = {"chief_complaint": f.get("chief_complaint"), "history": f.get("history"), "associated_symptoms": f.get("associated_symptoms"),
              "red_flags": "; ".join(red) or "None reported", "work_exposure": f.get("work_exposure"), "medications_allergies": f.get("medications_allergies"),
              "triage__severity": f.get("severity"), "triage__duration_days": f.get("duration_days"), "triage__urgency": urgency,
              "ai_summary": f.get("summary"), "transcript": "\n".join(f"{'Patient' if t['role'] == 'user' else 'AI'}: {t['text']}" for t in lines)}
    blocks = {k: v for k, v in blocks.items() if v not in (None, "")}
    page_key = interview_cfg.get("page_key")
    existing = [x for x in call(h, svc, "GET", "/note-pages", params={"patient_id": pid}) if x["record_id"] == rec["id"]]
    page = next((x for x in existing if x.get("definition_key") == page_key), None)
    if page:
        call(h, agent, "PATCH", f"/note-pages/{page['id']}", {"blocks": blocks})
    else:
        page = call(h, agent, "POST", "/note-pages", {"patient_id": pid, "record_id": rec["id"], "definition_key": page_key, "blocks": blocks})
    # 3. someone looks at it
    call(h, svc, "POST", "/tasks", {"patient_id": pid, "title": f"Review AI symptom interview: {f['chief_complaint'][:60]}" + (" — RED FLAG" if red else ""),
                                    "assignee_role": "clinician", "priority": "urgent" if red else ("high" if urgency == "soon" else "normal"),
                                    "encounter_id": enc["id"], "target_kind": "note_page", "target_id": page["id"]})
    advice = ("Some of what you described needs attention now. Please contact your care team today or call for help."
              if red else "Thank you. Your doctor will review this and contact you if needed.")
    return {"encounter_id": enc["id"], "page_id": page["id"], "conversation_id": conv["id"], "urgency": urgency, "red_flags": red,
            "summary": f.get("summary"), "advice": advice, "at": time.time()}


__all__ = ["DEFAULT_REALTIME", "DEFAULT_INTERVIEW", "FINISH_TOOL", "realtime_settings", "save_realtime", "interview_settings",
           "test_key", "create_session", "finish", "AGENT_ID", "PORTAL_ID"]
