"""
Consent forms: build, send, sign, store - ported from Clinestra 2.0's consent management, on open standards.

* **Templates** are ``clinical_document`` definitions with ``doc_type: consent_form``. Their HTML comes from the
  console's Tiptap editor: record variables (``{{patient_name}}`` ...) and fillable fields are inline nodes,
  ``<span data-field="text|textarea|number|email|date|checkbox|radio|select|signature" data-signer="patient|clinician|witness"
  data-name="..." data-label="..." data-options="a,b" data-required="true">``. Templates from Clinestra 2.0 (TinyMCE
  ``span.mymention``) are upgraded on import.
* **Signing sessions** are API-layer state: a staff member sends a form to a patient; the patient signs from a
  private link (optionally after a one-time code) without an account; clinicians and witnesses sign in the console.
* **When every signer has signed** the form is rendered once (values escaped, signatures embedded), hashed (SHA-256),
  stored as a signed ``clinical_document`` and, if the template names a consent scope, recorded as a granted
  ``consent``. The harness's hash-chained timeline records both. ``GET .../fhir`` exports it as a FHIR R4 Consent.
* **An open template library** ships with the server: install a template into your site, edit it, share it back.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import os
import re
import secrets
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from ..base import STAFF, Actor
from ..errors import ClinestraError
from .agentic.state import SiteState, new_id

__all__ = ["mount_consents", "fields_of", "upgrade_legacy", "LIBRARY", "VARIABLES"]

FIELD_TYPES = ("text", "textarea", "number", "email", "date", "checkbox", "radio", "select", "signature")
SIGNERS = ("patient", "clinician", "witness")
VARIABLES = {
    "patient_name": "Patient name", "age_sex": "Age and sex", "date_of_birth": "Date of birth", "phone": "Mobile number",
    "medical_record_number": "Medical record number", "current_date": "Today's date", "current_datetime": "Date and time now",
    "clinician_name": "Clinician sending the form", "site_name": "Clinic / site", "encounter_type": "Visit type",
}
_SIG = re.compile(r"^data:image/(png|jpeg|svg\+xml);base64,[A-Za-z0-9+/=]+$")
_SPAN = re.compile(r"<span\b(?P<attrs>[^>]*\bdata-(?:field|variable)=[^>]*)>(?P<inner>.*?)</span>", re.S | re.I)
MAX_SIG = 400_000


class _Attrs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.attrs: Dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        if not self.attrs:
            self.attrs = {k: v or "" for k, v in attrs}


def _attrs(tag_attrs: str) -> Dict[str, str]:
    p = _Attrs()
    p.feed(f"<span {tag_attrs}>")
    return p.attrs


def fields_of(body: str) -> List[dict]:
    """The fillable fields of a template, in document order, with stable keys."""
    out, seen = [], {}
    for m in _SPAN.finditer(body or ""):
        a = _attrs(m.group("attrs"))
        if "data-field" not in a:
            continue
        typ = a["data-field"] if a["data-field"] in FIELD_TYPES else "text"
        signer = a.get("data-signer") if a.get("data-signer") in SIGNERS else "patient"
        base = re.sub(r"[^a-z0-9_]+", "_", (a.get("data-name") or f"{signer}_{typ}").lower()).strip("_") or typ
        seen[base] = seen.get(base, 0) + 1
        key = base if seen[base] == 1 else f"{base}_{seen[base]}"
        out.append({"key": key, "type": typ, "signer": signer, "label": a.get("data-label") or html.unescape(re.sub("<[^>]+>", "", m.group("inner"))).strip() or typ,
                    "options": [o.strip() for o in (a.get("data-options") or "").split(",") if o.strip()],
                    "required": a.get("data-required", "true" if typ == "signature" else "false") == "true"})
    return out


def _variables(h, actor: Actor, patient_id: str, encounter_id: Optional[str]) -> Dict[str, str]:
    p = h.get(actor, "patient", patient_id).model_dump(mode="json")
    enc = h.get(actor, "encounter", encounter_id).model_dump(mode="json") if encounter_id else {}
    now = datetime.now(timezone.utc).astimezone()
    age = p.get("age_years") or p.get("age")
    return {"patient_name": p.get("name") or "", "age_sex": " ".join(str(x) for x in (f"{age} y" if age else "", p.get("gender") or "") if x),
            "date_of_birth": str(p.get("birth_date") or p.get("date_of_birth") or ""), "phone": str((p.get("demographics") or {}).get("phone") or ""),
            "medical_record_number": str((p.get("custom_fields") or {}).get("employee_no") or p.get("mrn") or p["id"]),
            "current_date": now.strftime("%d %b %Y"), "current_datetime": now.strftime("%d %b %Y, %H:%M"),
            "clinician_name": actor.id.replace("_", " "), "site_name": actor.site, "encounter_type": str(enc.get("encounter_type") or "").replace("_", " ")}


def fill_variables(body: str, values: Dict[str, str]) -> str:
    """Replace variable nodes (and plain {{name}} text) with the record's values, escaped."""
    def node(m):
        a = _attrs(m.group("attrs"))
        if "data-variable" not in a:
            return m.group(0)
        return f'<span class="cf-var">{html.escape(values.get(a["data-variable"], ""))}</span>'
    body = _SPAN.sub(node, body or "")
    return re.sub(r"\{\{\s*([a-z_]+)\s*\}\}", lambda m: html.escape(values.get(m.group(1), m.group(0))), body)


def render_final(body: str, values: Dict[str, Any], signatures: Dict[str, str]) -> str:
    """Every field replaced by what was entered: text escaped, ticks as ☑/☐, signatures as images."""
    fields = iter(fields_of(body))

    def node(m):
        a = _attrs(m.group("attrs"))
        if "data-field" not in a:
            return m.group(0)
        f = next(fields)
        if f["type"] == "signature":
            sig = signatures.get(f["key"])
            return f'<img class="cf-signature" alt="Signature: {html.escape(f["label"])}" src="{sig}" style="height:56px"/>' if sig else '<span class="cf-empty">(not signed)</span>'
        v = values.get(f["key"])
        if f["type"] == "checkbox":
            return f'<span class="cf-check">{"☑" if v in (True, "true", "on", 1) else "☐"}</span>'
        return f'<span class="cf-value">{html.escape(str(v)) if v not in (None, "") else "—"}</span>'
    return _SPAN.sub(node, body)


_LEGACY = {"manual_text_input_field": "text", "number_input_field": "number", "checkbox_input_field": "checkbox", "datetime_input_field": "date",
           "external_signature_input_field": "signature", "inhouse_signature_input_field": "signature", "email_input_field": "email",
           "radio_input_field": "radio", "dropdown_input_field": "select", "textarea_input_field": "textarea"}
_LEGACY_VARS = {"beneficiary_name": "patient_name", "mobile_number": "phone", "date_of_birth": "date_of_birth",
                "medical_record_number": "medical_record_number", "current_datetime": "current_datetime", "current_date": "current_date"}


def upgrade_legacy(body: str) -> str:
    """Clinestra 2.0 TinyMCE mention spans (``span.mymention``) -> this format's field and variable nodes."""
    pat = re.compile(r"<span\b(?P<attrs>[^>]*\bclass=\"[^\"]*mymention[^\"]*\"[^>]*)>(?P<inner>.*?)</span>", re.S | re.I)

    def node(m):
        a = _attrs(m.group("attrs"))
        mid = a.get("data-mention-id", "")
        if mid in _LEGACY_VARS:
            return f'<span data-variable="{_LEGACY_VARS[mid]}">{{{{{_LEGACY_VARS[mid]}}}}}</span>'
        base = re.sub(r"_(internal|external)$", "", mid)
        typ = _LEGACY.get(base) or _LEGACY.get(mid)
        if not typ:
            return m.group(0)
        signer = "clinician" if mid.endswith("_internal") or mid.startswith("inhouse") else "patient"
        opts = html.escape(a.get("data-mention-value") or "") if typ in ("radio", "select") else ""
        label = html.escape(re.sub("<[^>]+>", "", m.group("inner")).strip("{} ") or typ)
        return (f'<span data-field="{typ}" data-signer="{signer}" data-label="{label}"' + (f' data-options="{opts}"' if opts else "")
                + (' data-required="true"' if typ == "signature" else "") + f">{label}</span>")
    return pat.sub(node, body or "")


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# ───────────────────────── the open template library ─────────────────────────
def _f(typ, signer, label, name=None, options=None, required=False):
    a = f'data-field="{typ}" data-signer="{signer}" data-label="{html.escape(label)}"'
    if name:
        a += f' data-name="{name}"'
    if options:
        a += f' data-options="{html.escape(",".join(options))}"'
    if required or typ == "signature":
        a += ' data-required="true"'
    return f"<span {a}>{html.escape(label)}</span>"


def _v(name):
    return f'<span data-variable="{name}">{{{{{name}}}}}</span>'


_SIGNOFF = (f"<h3>Signatures</h3><p>Patient (or guardian): {_f('signature', 'patient', 'Patient signature', 'patient_signature')}</p>"
            f"<p>Date: {_v('current_date')}</p><p>Clinician: {_v('clinician_name')} {_f('signature', 'clinician', 'Clinician signature', 'clinician_signature')}</p>")

LIBRARY = [
    {"id": "general_treatment", "name": "General consent for treatment", "language": "en", "consent_scope": "treatment", "signers": ["patient", "clinician"],
     "description": "Consent to examination and routine treatment at this clinic.",
     "body": f"<h1>Consent for examination and treatment</h1><p>I, {_v('patient_name')} ({_v('age_sex')}, record {_v('medical_record_number')}), agree to be examined and treated at {_v('site_name')}.</p>"
             "<p>The care team has explained what the examination and treatment involve, the expected benefits, the common risks and the alternatives, including not being treated. I could ask questions and they were answered.</p>"
             f"<p>{_f('checkbox', 'patient', 'I understand that I can withdraw this consent at any time', 'understands_withdrawal', required=True)} I understand that I can withdraw this consent at any time.</p>"
             f"<p>Anything I want the team to know: {_f('textarea', 'patient', 'Notes from the patient', 'patient_notes')}</p>" + _SIGNOFF},
    {"id": "ai_in_care", "name": "Use of AI assistants in my care", "language": "en", "consent_scope": "ai", "signers": ["patient"],
     "description": "Lets the clinic's AI agents read the record to prepare and propose; clinicians decide.",
     "body": f"<h1>Consent to the use of AI assistants</h1><p>{_v('site_name')} uses AI assistants that help the care team. With your consent they may read your health record to summarise it, prepare visits and suggest next steps.</p>"
             "<ul><li>The AI never makes decisions about your care. Every suggestion is checked and decided by a clinician.</li>"
             "<li>Your name, phone number and ID numbers are not sent to the AI model unless the clinic has switched that on and told you.</li>"
             "<li>Saying no does not change the care you receive.</li><li>You can withdraw this consent at any time.</li></ul>"
             f"<p>{_f('radio', 'patient', 'My choice', 'choice', ['I agree', 'I do not agree'], required=True)}</p>"
             f"<p>Patient: {_v('patient_name')} {_f('signature', 'patient', 'Patient signature', 'patient_signature')}</p><p>Date: {_v('current_date')}</p>"},
    {"id": "procedure", "name": "Consent for a procedure", "language": "en", "consent_scope": "procedure", "signers": ["patient", "clinician", "witness"],
     "description": "Names the procedure, its risks and alternatives; signed by patient, clinician and a witness.",
     "body": f"<h1>Consent for a procedure</h1><p>Patient: {_v('patient_name')} · {_v('age_sex')} · record {_v('medical_record_number')}</p>"
             f"<p>Procedure: {_f('text', 'clinician', 'Name of the procedure', 'procedure', required=True)}</p>"
             f"<p>Risks explained: {_f('textarea', 'clinician', 'Main risks explained', 'risks', required=True)}</p>"
             f"<p>Alternatives discussed: {_f('textarea', 'clinician', 'Alternatives', 'alternatives')}</p>"
             f"<p>{_f('checkbox', 'patient', 'The procedure, its risks and alternatives were explained to me in a language I understand', 'explained', required=True)} The procedure, its risks and alternatives were explained to me in a language I understand.</p>"
             + _SIGNOFF + f"<p>Witness name: {_f('text', 'witness', 'Witness name', 'witness_name', required=True)} {_f('signature', 'witness', 'Witness signature', 'witness_signature')}</p>"},
    {"id": "data_sharing", "name": "Sharing my records", "language": "en", "consent_scope": "data_sharing", "signers": ["patient"],
     "description": "Lets the clinic send the patient's documents to them and share records with named providers.",
     "body": f"<h1>Consent to share my health records</h1><p>I, {_v('patient_name')}, allow {_v('site_name')} to share my health records as below.</p>"
             f"<p>Share with: {_f('select', 'patient', 'Share with', 'share_with', ['Me only', 'Me and my family doctor', 'Me and referred specialists'], required=True)}</p>"
             f"<p>Send my documents to: {_f('email', 'patient', 'Email (optional)', 'email')}</p>"
             f"<p>Valid until: {_f('date', 'patient', 'Valid until', 'valid_until')}</p>"
             f"<p>{_f('signature', 'patient', 'Patient signature', 'patient_signature')}</p><p>Date: {_v('current_date')}</p>"},
    {"id": "general_treatment_hi", "name": "उपचार के लिए सामान्य सहमति", "language": "hi", "consent_scope": "treatment", "signers": ["patient", "clinician"],
     "description": "General consent for treatment, in Hindi.",
     "body": f"<h1>जाँच और उपचार के लिए सहमति</h1><p>मैं, {_v('patient_name')} ({_v('age_sex')}), {_v('site_name')} में जाँच और उपचार के लिए सहमति देता/देती हूँ।</p>"
             "<p>देखभाल टीम ने मुझे जाँच और उपचार, उनके लाभ, सामान्य जोखिम और विकल्प समझाए हैं। मेरे प्रश्नों के उत्तर दिए गए हैं।</p>"
             f"<p>{_f('checkbox', 'patient', 'मैं समझता/समझती हूँ कि मैं यह सहमति कभी भी वापस ले सकता/सकती हूँ', 'understands_withdrawal', required=True)} मैं समझता/समझती हूँ कि मैं यह सहमति कभी भी वापस ले सकता/सकती हूँ।</p>"
             f"<p>रोगी के हस्ताक्षर: {_f('signature', 'patient', 'रोगी के हस्ताक्षर', 'patient_signature')}</p><p>तारीख: {_v('current_date')}</p>"
             f"<p>चिकित्सक: {_v('clinician_name')} {_f('signature', 'clinician', 'चिकित्सक के हस्ताक्षर', 'clinician_signature')}</p>"},
]


# ───────────────────────── routes ─────────────────────────
class SendIn(BaseModel):
    definition_key: str
    patient_id: str
    encounter_id: Optional[str] = None


class SignIn(BaseModel):
    values: Dict[str, Any] = Field(default_factory=dict)
    signatures: Dict[str, str] = Field(default_factory=dict)
    signer: str = "clinician"
    signer_name: Optional[str] = None


class OtpIn(BaseModel):
    code: str


class ImportIn(BaseModel):
    html: str


def mount_consents(app: FastAPI, h, auth) -> None:
    from fastapi import Depends
    A = Depends(auth)

    def st_of(site: str) -> SiteState:
        return SiteState(h.store, site)

    def herr(e: ClinestraError):
        return HTTPException(e.status_code, str(e))

    def staff(actor: Actor) -> None:
        if actor.role not in STAFF and actor.role != "admin":
            raise HTTPException(403, "Only staff send and countersign consent forms")

    def template(actor: Actor, key: str) -> dict:
        defs = [d.model_dump(mode="json") for d in h.list(actor, "definition", None, "published", def_kind="clinical_document", key=key)]
        defs = [d for d in defs if (d.get("config") or {}).get("doc_type") == "consent_form"]
        if not defs:
            raise HTTPException(404, f"No published consent form '{key}'")
        return max(defs, key=lambda d: (d["site"] == actor.site, d.get("version") or 0))

    def public(sess: dict, signer: str) -> dict:
        mine = [f for f in sess["fields"] if f["signer"] == signer]
        return {"id": sess["id"], "patient_id": sess["patient_id"] if signer != "patient" else None, "title": sess["title"], "language": sess["language"], "status": sess["status"], "expires_at": sess["expires_at"],
                "html": sess["html"], "fields": sess["fields"], "editable": [f["key"] for f in mine], "values": sess["values"],
                "signatures": sess["signatures"], "signed": sess["signed"], "signers": sess["signers"], "signer": signer,
                "document_id": sess.get("document_id"), "consent_id": sess.get("consent_id"), "hash": sess.get("hash")}

    def live(st: SiteState, sess: dict) -> dict:
        if sess["status"] == "open" and time.time() > sess["expires_at"] and "patient" not in sess["signed"]:
            sess["status"] = "expired"
            st.put("_consent.session", sess["id"], sess)
        return sess

    def get_session(st: SiteState, sid: str) -> dict:
        s = st.get("_consent.session", sid)
        if not s or s.get("_deleted"):
            raise HTTPException(404, "No such consent form")
        return live(st, s)

    def by_token(token: str):
        site, _, _rest = token.partition("~")
        if not site or not _rest:
            raise HTTPException(404, "This signing link is not valid")
        st = st_of(site)
        link = st.get("_consent.link", _sha(token)[:40])
        if not link or link.get("_deleted"):
            raise HTTPException(404, "This signing link is not valid or has already been used")
        return st, get_session(st, link["session_id"])

    def new_link(st: SiteState, sess: dict) -> str:
        token = f"{st.site}~{secrets.token_urlsafe(24)}"  # the site travels in the link; the rest is the secret
        st.put("_consent.link", _sha(token)[:40], {"session_id": sess["id"]})
        sess["link_hash"] = _sha(token)[:40]
        return token

    def take(sess: dict, signer: str, body: SignIn) -> None:
        if sess["status"] != "open":
            raise HTTPException(409, f"This form is {sess['status']}")
        if signer not in sess["signers"]:
            raise HTTPException(400, f"This form is not signed by a {signer}")
        if signer in sess["signed"]:
            raise HTTPException(409, f"The {signer} has already signed")
        mine = {f["key"]: f for f in sess["fields"] if f["signer"] == signer}
        for k, v in body.values.items():
            if k in mine and mine[k]["type"] != "signature":
                if mine[k]["type"] in ("radio", "select") and v and v not in mine[k]["options"]:
                    raise HTTPException(400, f"{mine[k]['label']}: choose one of the options")
                sess["values"][k] = v if isinstance(v, bool) else str(v)[:4000]
        for k, v in body.signatures.items():
            if k in mine and mine[k]["type"] == "signature":
                if not isinstance(v, str) or len(v) > MAX_SIG or not _SIG.match(v):
                    raise HTTPException(400, "A signature must be a PNG, JPEG or SVG image under 400 KB")
                sess["signatures"][k] = v
        missing = [f["label"] for f in mine.values() if f["required"] and not (sess["signatures"] if f["type"] == "signature" else sess["values"]).get(f["key"])
                   and not (f["type"] == "checkbox" and sess["values"].get(f["key"]) is True)]
        if missing:
            raise HTTPException(400, "Still needed: " + ", ".join(missing))
        sess["signed"][signer] = {"at": time.time(), "name": body.signer_name}

    def finish(st: SiteState, sess: dict, by: Optional[Actor] = None) -> None:
        """All signed: store it. A clinician who signed last releases the document; otherwise the sender does (or it waits as a draft)."""
        if any(s not in sess["signed"] for s in sess["signers"]):
            return
        actor = by if by is not None and by.role == "clinician" else Actor(**sess["created_by"])
        body = render_final(sess["html"], sess["values"], sess["signatures"])
        digest = _sha(body)
        trail = "".join(f"<li>{html.escape(s.title())}{(' ' + html.escape(v['name'])) if v.get('name') else ''}: "
                        f"{datetime.fromtimestamp(v['at'], timezone.utc).isoformat(timespec='seconds')}</li>" for s, v in sess["signed"].items())
        footer = (f'<hr/><div class="cf-trail"><p>Signed electronically through Clinestra.</p><ul>{trail}</ul>'
                  f"<p>SHA-256 of the signed form: <code>{digest}</code></p></div>")
        try:
            doc = h.create(actor, "clinical_document", {"patient_id": sess["patient_id"], "doc_type": "consent_form", "title": sess["title"],
                                                        "definition_key": sess["definition_key"], "body_html": body, "footer_html": footer,
                                                        "shareable_with_patient": True})
            try:
                doc = h.transition(actor, "clinical_document", doc.id, "ready", "All signers have signed")
            except ClinestraError:
                pass  # e.g. the sender is not a clinician: the document stays a draft for one to release
            sess["document_id"] = doc.id
            if sess.get("consent_scope") and not _declined(sess):
                c = h.create(actor, "consent", {"patient_id": sess["patient_id"], "consent_scope": sess["consent_scope"], "document_id": doc.id,
                                                "details": f"{sess['title']} (signed form, SHA-256 {digest[:16]}…)"})
                c = h.transition(actor, "consent", c.id, "granted", "Signed consent form")
                sess["consent_id"] = c.id
        except ClinestraError as e:
            sess["error"] = str(e)
        sess.update(status="completed", hash=digest, completed_at=time.time())

    def _declined(sess: dict) -> bool:
        return any(str(v).lower().startswith(("i do not", "no", "मैं सहमत नहीं")) for k, v in sess["values"].items() if k == "choice")

    # ── templates ──
    @app.get("/v1/consent-forms/variables", tags=["Consent forms"])
    def variables(actor: Actor = A):
        return {"variables": [{"name": k, "label": v} for k, v in VARIABLES.items()], "field_types": list(FIELD_TYPES), "signers": list(SIGNERS)}

    @app.get("/v1/consent-forms/library", tags=["Consent forms"], summary="Open consent templates shipped with Clinestra")
    def library(actor: Actor = A):
        return [{k: v for k, v in t.items() if k != "body"} | {"fields": len(fields_of(t["body"]))} for t in LIBRARY]

    @app.post("/v1/consent-forms/library/{lib_id}/install", status_code=201, tags=["Consent forms"], summary="Copy a library template into this site as a draft")
    def install(lib_id: str, actor: Actor = A):
        staff(actor)
        t = next((x for x in LIBRARY if x["id"] == lib_id), None)
        if not t:
            raise HTTPException(404, "No such template")
        try:
            d = h.create(actor, "definition", {"def_kind": "clinical_document", "key": f"consent_{lib_id}", "name": t["name"], "description": t["description"],
                                               "config": {"doc_type": "consent_form", "body_template": t["body"], "consent_scope": t["consent_scope"],
                                                          "signers": t["signers"], "language": t["language"], "shareable_with_patient": True}})
        except ClinestraError as e:
            raise herr(e)
        return d

    @app.post("/v1/consent-forms/import", tags=["Consent forms"], summary="Upgrade a Clinestra 2.0 (TinyMCE) template's HTML to this format")
    def import_legacy(body: ImportIn, actor: Actor = A):
        out = upgrade_legacy(body.html)
        return {"html": out, "fields": fields_of(out)}

    @app.post("/v1/consent-forms/preview-fields", tags=["Consent forms"])
    def preview_fields(body: ImportIn, actor: Actor = A):
        return fields_of(body.html)

    # ── sessions (staff) ──
    @app.post("/v1/consent-forms/sessions", status_code=201, tags=["Consent forms"], summary="Send a consent form to a patient")
    def send(body: SendIn, actor: Actor = A):
        staff(actor)
        t = template(actor, body.definition_key)
        cfg = t["config"]
        try:
            html_body = fill_variables(cfg.get("body_template") or "", _variables(h, actor, body.patient_id, body.encounter_id))
        except ClinestraError as e:
            raise herr(e)
        signers = [s for s in (cfg.get("signers") or ["patient"]) if s in SIGNERS]
        st = st_of(actor.site)
        sess = {"id": new_id("cfs"), "definition_key": t["key"], "definition_version": t.get("version"), "title": t["name"], "language": cfg.get("language", "en"),
                "consent_scope": cfg.get("consent_scope"), "patient_id": body.patient_id, "encounter_id": body.encounter_id, "signers": signers,
                "require_otp": bool(cfg.get("require_otp")), "html": html_body, "fields": fields_of(html_body), "values": {}, "signatures": {}, "signed": {},
                "status": "open", "created_at": time.time(), "expires_at": time.time() + 60 * int(cfg.get("link_expiry_minutes") or 30),
                "created_by": actor.model_dump(mode="json")}
        token = new_link(st, sess) if "patient" in signers else None
        st.put("_consent.session", sess["id"], sess)
        h._audit(actor, body.patient_id, "consent_form.sent", "clinical_document", sess["id"], {"form": t["key"], "signers": signers})
        return {**public(sess, "clinician"), "patient_link": f"/sign/{token}" if token else None}

    @app.get("/v1/consent-forms/sessions", tags=["Consent forms"])
    def sessions(patient_id: Optional[str] = None, actor: Actor = A):
        staff(actor)
        st = st_of(actor.site)
        rows = [live(st, s) for s in st.all("_consent.session") if not s.get("_deleted") and (not patient_id or s["patient_id"] == patient_id)]
        return [{k: s.get(k) for k in ("id", "title", "patient_id", "status", "signers", "signed", "created_at", "expires_at", "document_id", "consent_id", "hash")}
                for s in sorted(rows, key=lambda r: -r["created_at"])]

    @app.get("/v1/consent-forms/sessions/{sid}", tags=["Consent forms"])
    def session(sid: str, actor: Actor = A):
        staff(actor)
        return public(get_session(st_of(actor.site), sid), "clinician")

    @app.post("/v1/consent-forms/sessions/{sid}/sign", tags=["Consent forms"], summary="Sign in the console as the clinician or a witness")
    def staff_sign(sid: str, body: SignIn, actor: Actor = A):
        staff(actor)
        if body.signer not in ("clinician", "witness"):
            raise HTTPException(400, "Staff sign as the clinician or a witness; the patient signs from their link")
        st = st_of(actor.site)
        sess = get_session(st, sid)
        take(sess, body.signer, body.model_copy(update={"signer_name": body.signer_name or actor.id.replace("_", " ")}))
        finish(st, sess, actor)
        st.put("_consent.session", sid, sess)
        h._audit(actor, sess["patient_id"], f"consent_form.signed_{body.signer}", "clinical_document", sid, {})
        return public(sess, body.signer)

    @app.post("/v1/consent-forms/sessions/{sid}/link", tags=["Consent forms"], summary="A fresh patient link (the old one stops working)")
    def relink(sid: str, actor: Actor = A):
        staff(actor)
        st = st_of(actor.site)
        sess = get_session(st, sid)
        if sess["status"] not in ("open", "expired") or "patient" not in sess["signers"]:
            raise HTTPException(409, "This form has no patient signature still to collect")
        if sess.get("link_hash"):
            st.delete("_consent.link", sess["link_hash"])
        token = new_link(st, sess)
        sess.update(status="open", expires_at=time.time() + 30 * 60, otp_ok=False)
        st.put("_consent.session", sid, sess)
        return {"patient_link": f"/sign/{token}", "expires_at": sess["expires_at"]}

    @app.post("/v1/consent-forms/sessions/{sid}/cancel", tags=["Consent forms"])
    def cancel(sid: str, actor: Actor = A):
        staff(actor)
        st = st_of(actor.site)
        sess = get_session(st, sid)
        if sess["status"] == "completed":
            raise HTTPException(409, "A completed form is withdrawn through its consent, not cancelled")
        sess["status"] = "cancelled"
        if sess.get("link_hash"):
            st.delete("_consent.link", sess["link_hash"])
        st.put("_consent.session", sid, sess)
        return {"ok": True}

    @app.get("/v1/consent-forms/sessions/{sid}/fhir", tags=["Consent forms"], summary="The signed form as a FHIR R4 Consent resource")
    def fhir(sid: str, actor: Actor = A):
        staff(actor)
        sess = get_session(st_of(actor.site), sid)
        if sess["status"] != "completed":
            raise HTTPException(409, "Only a completed form has a FHIR Consent")
        return fhir_consent(sess)

    # ── the patient's link (no account) ──
    @app.get("/v1/public/consent/{token}", tags=["Consent forms"], summary="The form behind a patient signing link")
    def open_link(token: str):
        st, sess = by_token(token)
        if sess["require_otp"] and not sess.get("otp_ok") and sess["status"] == "open":
            return {"otp_required": True, "title": sess["title"], "status": sess["status"]}
        return public(sess, "patient")

    @app.post("/v1/public/consent/{token}/otp", tags=["Consent forms"], summary="Send a one-time code to the patient's phone")
    def link_otp(token: str):
        st, sess = by_token(token)
        code = "".join(secrets.choice("0123456789") for _ in range(6))
        sess["otp"] = {"hash": _sha(code + sess["id"]), "expires": time.time() + 600, "attempts": 0}
        st.put("_consent.session", sess["id"], sess)
        out: Dict[str, Any] = {"sent": True}
        from .portal.sessions import settings as portal_settings
        ps = portal_settings(st)
        if ps.get("otp_provider") == "webhook" and ps.get("otp_webhook_url"):
            try:
                import httpx
                phone = (h.store.get("patient", sess["patient_id"], st.site) or {}).get("demographics", {}).get("phone")
                httpx.post(ps["otp_webhook_url"], json={"phone": phone, "otp": code, "app": "consent"}, timeout=10)
            except Exception:  # noqa: BLE001 - delivery failure is reported by the absent code, not a 500
                pass
        from .portal.sessions import returns_code_on_screen_allowed
        if returns_code_on_screen_allowed() and (ps.get("otp_provider") in ("dev_screen", "dev_fixed") or os.environ.get("CLINESTRA_ENV") == "development"):
            out["dev_otp"] = code
        return out

    @app.post("/v1/public/consent/{token}/otp/verify", tags=["Consent forms"])
    def link_otp_verify(token: str, body: OtpIn):
        st, sess = by_token(token)
        o = sess.get("otp") or {}
        if not o or o["expires"] < time.time() or o["attempts"] >= 5:
            raise HTTPException(400, "The code has expired. Ask for a new one.")
        if not hmac.compare_digest(o["hash"], _sha(body.code.strip() + sess["id"])):
            o["attempts"] += 1
            st.put("_consent.session", sess["id"], sess)
            raise HTTPException(400, "That code is not right")
        sess["otp_ok"] = True
        st.put("_consent.session", sess["id"], sess)
        return public(sess, "patient")

    @app.post("/v1/public/consent/{token}/sign", tags=["Consent forms"], summary="The patient signs")
    def patient_sign(token: str, body: SignIn, request: Request):
        st, sess = by_token(token)
        if sess["require_otp"] and not sess.get("otp_ok"):
            raise HTTPException(403, "Enter the code sent to your phone first")
        if sess["status"] == "open" and time.time() > sess["expires_at"]:
            raise HTTPException(410, "This link has expired. Ask the clinic for a new one.")
        take(sess, "patient", body.model_copy(update={"signer_name": body.signer_name}))
        finish(st, sess)
        st.put("_consent.session", sess["id"], sess)
        st.delete("_consent.link", sess.get("link_hash") or _sha(token)[:40])  # one signature per link
        h._audit(Actor(**sess["created_by"]), sess["patient_id"], "consent_form.signed_patient", "clinical_document", sess["id"],
                 {"ip_hash": _sha((request.client.host if request.client else "") + sess["id"])[:16]})
        return public(sess, "patient")


def fhir_consent(sess: dict) -> dict:
    """A FHIR R4 Consent for a completed form. The signed HTML is attached with its SHA-256 in an extension."""
    cats = {"treatment": ("59284-0", "Consent Document"), "procedure": ("59284-0", "Consent Document"), "research": ("57016-8", "Privacy policy acknowledgment"),
            "data_sharing": ("57016-8", "Privacy policy acknowledgment"), "ai": ("57016-8", "Privacy policy acknowledgment"),
            "communication": ("57016-8", "Privacy policy acknowledgment")}
    scope = sess.get("consent_scope") or "treatment"
    code, display = cats.get(scope, cats["treatment"])
    fhir_scope = {"research": "research", "data_sharing": "patient-privacy", "ai": "patient-privacy", "communication": "patient-privacy"}.get(scope, "treatment")
    when = datetime.fromtimestamp(sess.get("completed_at") or time.time(), timezone.utc).isoformat(timespec="seconds")
    return {
        "resourceType": "Consent", "id": sess["id"], "status": "active" if not sess.get("error") else "proposed",
        "scope": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/consentscope", "code": fhir_scope}]},
        "category": [{"coding": [{"system": "http://loinc.org", "code": code, "display": display}]}],
        "patient": {"reference": f"Patient/{sess['patient_id']}"}, "dateTime": when,
        "performer": [{"reference": f"Patient/{sess['patient_id']}"}] + [{"display": f"{s}: {v.get('name') or ''}".strip(": ")} for s, v in sess["signed"].items() if s != "patient"],
        "sourceReference": {"reference": f"DocumentReference/{sess.get('document_id')}"} if sess.get("document_id") else None,
        "policyRule": {"text": sess["title"]},
        "provision": {"type": "permit"},
        "extension": [{"url": "https://clinestra.org/fhir/StructureDefinition/signed-form-sha256", "valueString": sess.get("hash")},
                      {"url": "https://clinestra.org/fhir/StructureDefinition/clinestra-scope", "valueCode": scope}],
        "meta": {"tag": [{"system": "https://clinestra.org/fhir/form", "code": sess["definition_key"]}]},
        "text": {"status": "generated", "div": f"<div xmlns=\"http://www.w3.org/1999/xhtml\">{html.escape(sess['title'])}, signed {when}</div>"},
    }
