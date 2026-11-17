"""Every flow in docs/flows.md and docs/guides.md, run over HTTP. If this fails, the docs are wrong."""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from clinestra import Harness  # noqa: E402
from clinestra.server.app import create_app  # noqa: E402
from clinestra.server.auth import DevHeaderAuth  # noqa: E402


@pytest.fixture()
def api():
    client = TestClient(create_app(Harness(packs=["core"]), auth=DevHeaderAuth()))

    def call(who, method, path, body=None, expect=None):
        uid, role = who
        headers = {"X-Actor-Id": uid, "X-Actor-Role": role, "X-Site": "clinic_1"}
        if uid == "copilot":
            headers["X-Actor-Type"] = "agent"
        if uid == "asha":
            headers["X-Patient-Id"] = PATIENT["id"]
        r = client.request(method, "/v1" + path, json=body, headers=headers)
        assert r.status_code < 400 if expect is None else r.status_code == expect, f"{method} {path}: {r.status_code} {r.text[:400]}"
        return r.json()
    return call


DR, DR2, NURSE, ADMIN, LEAD = ("dr_rao", "clinician"), ("dr_iyer", "clinician"), ("nurse_meena", "nurse"), ("admin", "admin"), ("lead", "clinical_lead")
AGENT = ("copilot", "clinician")
PATIENT = {}


def patient(api):
    p = api(DR, "POST", "/patients", {"name": "Asha K", "country": "IN", "gender": "female", "age_years": 54})
    PATIENT.update(p)
    return p["id"]


def consent(api, pid, scope):
    c = api(DR, "POST", "/consents", {"patient_id": pid, "consent_scope": scope})
    return api(DR, "POST", f"/consents/{c['id']}/transition", {"to": "granted", "reason": "Signed at registration"})


def test_outpatient_visit(api):
    pid = patient(api)
    enc = api(NURSE, "POST", "/encounters", {"patient_id": pid, "definition_key": "opd_visit"})
    page = api(DR, "GET", f"/note-pages?patient_id={pid}&encounter_id={enc['id']}")[0]
    api(DR, "PATCH", f"/note-pages/{page['id']}", {"blocks": {"chief_complaint": "Headache", "bp_systolic": 150, "bp_diastolic": 96, "assessment_plan": "Hypertension"}})
    done = api(DR, "POST", f"/note-pages/{page['id']}/transition", {"to": "finalized", "reason": "Signed"})
    assert done["status"] == "finalized"
    assert {"bp_systolic", "bp_diastolic"} <= {o["code"] for o in api(DR, "GET", f"/observations?patient_id={pid}")}
    api(DR, "PATCH", f"/note-pages/{page['id']}", {"blocks": {"chief_complaint": "x"}}, expect=409)
    api(DR, "POST", f"/note-pages/{page['id']}/addenda", {"text": "Patient called, headache resolved."})


def test_careplan(api):
    pid = patient(api)
    api(DR, "POST", "/conditions", {"patient_id": pid, "code": "htn", "verification": "confirmed"})
    plan = api(DR, "POST", "/careplans", {"patient_id": pid, "definition_key": "hypertension_basic"})
    api(NURSE, "POST", f"/careplans/{plan['id']}/transition", {"to": "active"}, expect=403)
    assert api(DR, "POST", f"/careplans/{plan['id']}/transition", {"to": "active", "reason": "Agreed"})["status"] == "active"
    assert api(DR, "GET", f"/care-actions?patient_id={pid}")


def test_ai_proposal(api):
    pid = patient(api)
    body = {"patient_id": pid, "origin": "agent:copilot", "op": "create", "target_kind": "condition", "data": {"patient_id": pid, "code": "ckd"}, "rationale": "Mentioned in notes"}
    api(AGENT, "POST", "/proposals", body, expect=403)
    consent(api, pid, "ai")
    prop = api(AGENT, "POST", "/proposals", body)
    assert prop["status"] == "pending"
    assert api(DR, "GET", f"/conditions?patient_id={pid}") == []
    accepted = api(DR, "POST", f"/proposals/{prop['id']}/accept", {"edits": {"verification": "provisional"}})
    assert accepted["status"] == "accepted"
    assert api(DR, "GET", f"/conditions?patient_id={pid}")[0]["code"] == "ckd"
    other = api(AGENT, "POST", "/proposals", {**body, "data": {"patient_id": pid, "code": "x"}})
    assert api(DR, "POST", f"/proposals/{other['id']}/reject", {"reason": "Not supported by results"})["status"] == "rejected"


def test_approval(api):
    pid = patient(api)
    api(NURSE, "POST", "/encounters", {"patient_id": pid, "definition_key": "opd_visit"})
    page = api(DR, "GET", f"/note-pages?patient_id={pid}")[0]
    api(DR, "PATCH", f"/note-pages/{page['id']}", {"blocks": {"chief_complaint": "Headache", "assessment_plan": "Rest"}})
    approval = api(DR, "POST", f"/note-pages/{page['id']}/transition", {"to": "approved", "reason": "Please review"}, expect=202)
    api(DR, "POST", f"/approvals/{approval['id']}/decide", {"approve": True}, expect=403)


def test_second_person_approves(api):
    pid = patient(api)
    api(NURSE, "POST", "/encounters", {"patient_id": pid, "definition_key": "opd_visit"})
    page = api(DR, "GET", f"/note-pages?patient_id={pid}")[0]
    api(DR, "PATCH", f"/note-pages/{page['id']}", {"blocks": {"chief_complaint": "Headache", "assessment_plan": "Rest"}})
    approval = api(DR, "POST", f"/note-pages/{page['id']}/transition", {"to": "approved", "reason": "Please review"}, expect=202)
    assert api(DR2, "POST", f"/approvals/{approval['id']}/decide", {"approve": True, "note": "Reviewed"})["status"] == "approved"
    assert api(DR, "GET", f"/note-pages/{page['id']}")["status"] == "approved"


def test_assessment_and_journal(api):
    pid = patient(api)
    bmi = api(DR, "POST", "/assessments", {"patient_id": pid, "definition_key": "bmi", "answers": {"weight": 58, "height": 160}})
    assert bmi["score"]
    entry = api(("asha", "patient"), "POST", "/journal-entries", {"patient_id": pid, "plugin_key": "vitals_log", "definition_key": "vitals_log", "data": {"bp_systolic": 132, "weight": 58}})
    assert entry["status"] == "recorded"
    seen = api(DR, "GET", f"/observations?patient_id={pid}")
    assert any(o["code"] == "bp_systolic" and o["perceived_by"] == "patient" for o in seen)


def test_journey_board(api):
    pid = patient(api)
    j = api(NURSE, "POST", "/journeys", {"patient_id": pid, "definition_key": "opd_flow"})
    assert j["state"] == "registered"
    assert api(NURSE, "POST", f"/journeys/{j['id']}/move", {"to": "waiting"})["state"] == "waiting"
    api(NURSE, "POST", f"/journeys/{j['id']}/move", {"to": "done"}, expect=409)
    assert api(DR, "POST", f"/journeys/{j['id']}/move", {"to": "done", "reason": "Left early", "force": True})["state"] == "done"


def test_tasks_and_tick(api):
    pid = patient(api)
    t = api(NURSE, "POST", "/tasks", {"patient_id": pid, "title": "Call patient", "assignee_role": "nurse", "priority": "high", "checklist": [{"item": "Confirm medicines"}]})
    assert t["status"]
    states = api(DR, "GET", "/concepts")
    assert "task" in str(states)
    api(("system", "system"), "POST", "/tick", {})


def test_site_definition(api):
    d = api(ADMIN, "POST", "/definitions", {"def_kind": "task", "key": "welcome_call", "name": "Welcome call", "config": {"defaults": {"title": "Welcome call", "priority": "normal"}}})
    assert d["status"] == "draft"
    published = api(ADMIN, "POST", f"/definitions/{d['id']}/transition", {"to": "published", "reason": "Ready"})
    assert published["status"] == "published"
    pid = patient(api)
    assert api(NURSE, "POST", "/tasks", {"patient_id": pid, "definition_key": "welcome_call"})["title"] == "Welcome call"


def test_tools_audit_blocks(api):
    pid = patient(api)
    consent(api, pid, "ai")
    tools = api(AGENT, "GET", f"/tools?scope=patient&patient_id={pid}")
    assert tools
    out = api(AGENT, "POST", "/tools/call", {"name": "summarize_patient", "args": {}, "scope": "patient", "patient_id": pid})
    assert out
    assert api(DR, "GET", f"/patients/{pid}/timeline/verify")["valid"] is True
    assert api(DR, "GET", "/ai/content-blocks")
    assert api(DR, "GET", "/packs")


def test_document_share(api):
    pid = patient(api)
    doc = api(DR, "POST", "/clinical-documents", {"patient_id": pid, "doc_type": "clinical_report", "title": "Report", "shareable_with_patient": True})
    api(DR, "POST", f"/clinical-documents/{doc['id']}/transition", {"to": "ready", "reason": "Signed"})
    api(DR, "POST", f"/clinical-documents/{doc['id']}/share", {}, expect=403)
    consent(api, pid, "data_sharing")
    assert api(DR, "POST", f"/clinical-documents/{doc['id']}/share", {})


def test_documented_routes_exist():
    import pathlib
    import re
    app = create_app(Harness(packs=["core"]), auth=DevHeaderAuth())
    norm = lambda p: re.sub(r"\{[^}]*\}", "{}", p).rstrip("/")
    real = {(m, norm(r.path)) for r in app.routes if hasattr(r, "methods") for m in r.methods}
    missing = []
    docs = pathlib.Path(__file__).parents[1] / "docs"
    for f in docs.glob("*.md"):
        for m, p in re.findall(r"`(GET|POST|PATCH|PUT|DELETE) (/v1/[^`\s?]*)", f.read_text(encoding="utf-8")):
            if "{resource}" not in p and "{path}" not in p and (m, norm(p)) not in real:
                missing.append((f.name, m, p))
    assert not missing, missing


def test_note_pages_filter_by_encounter(api):
    pid = patient(api)
    first = api(NURSE, "POST", "/encounters", {"patient_id": pid, "definition_key": "opd_visit"})
    second = api(NURSE, "POST", "/encounters", {"patient_id": pid, "definition_key": "opd_visit"})
    everything = api(DR, "GET", f"/note-pages?patient_id={pid}")
    one = api(DR, "GET", f"/note-pages?patient_id={pid}&encounter_id={first['id']}")
    other = api(DR, "GET", f"/note-pages?patient_id={pid}&encounter_id={second['id']}")
    assert one and other and len(one) + len(other) == len(everything)
    assert not {p["id"] for p in one} & {p["id"] for p in other}
    assert api(DR, "GET", f"/note-pages?patient_id={pid}&encounter_id=enc_missing") == []
