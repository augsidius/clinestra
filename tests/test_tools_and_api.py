from conftest import TEST_PACKS
import warnings

import pytest

from clinestra import Actor, Harness, NotFound, PermissionDenied
from clinestra.examples_demo import run
from clinestra.server.agentic.tools import registry

S = "site_a"
DR = Actor(id="dr_a", site=S, role="clinician")
AGENT = Actor(id="bot", site=S, role="clinician", actor_type="agent")


@pytest.fixture
def h():
    return Harness(packs=TEST_PACKS)


def setup_patient(h, consent=True):
    p = h.create(DR, "patient", {"name": "Asha", "country": "IN", "gender": "female", "age_years": 34})
    if consent:
        c = h.create(DR, "consent", {"patient_id": p.id, "consent_scope": "ai"})
        h.transition(DR, "consent", c.id, "granted")
    return p


def test_demo_runs_end_to_end():
    out = run(quiet=True)
    h = out["harness"]
    assert h.verify_timeline(Actor(id="x", site="demo_clinic", role="clinician"), out["patient_id"])["valid"]


def test_tools_are_scoped_by_context(h):
    p = setup_patient(h)
    patient_tools = {t.name for t in registry(h).list({"scope": "patient", "patient_id": p.id})}
    setting_tools = {t.name for t in registry(h).list({"scope": "setting"})}
    general_tools = {t.name for t in registry(h).list({"scope": "general"})}
    assert "observation_create" in patient_tools and "provider_list" not in patient_tools
    assert "provider_list" in setting_tools and "observation_create" not in setting_tools
    assert {"generate_definition", "fetch_evidence", "definition_list"} <= general_tools
    agent_tools = {t.name for t in registry(h).list({"scope": "patient", "patient_id": p.id}, AGENT)}
    assert "condition_propose" in agent_tools and "condition_create" not in agent_tools
    assert "note_page_create" in agent_tools and "careplan_transition" not in agent_tools


def test_tool_definitions_pin_context_ids(h):
    p = setup_patient(h)
    defs = {d["name"]: d for d in registry(h).definitions({"scope": "patient", "patient_id": p.id})}
    assert "patient_id" not in defs["observation_create"]["input_schema"]["properties"]
    out = registry(h).call(DR, "observation_create", {"code": "pulse", "value": 70}, {"scope": "patient", "patient_id": p.id})
    assert out["patient_id"] == p.id


def test_agent_tool_calls_go_through_the_rules(h):
    p = setup_patient(h)
    ctx = {"scope": "patient", "patient_id": p.id}
    with pytest.raises(PermissionDenied):
        registry(h).call(AGENT, "condition_create", {"code": "mdd"}, ctx)
    prop = registry(h).call(AGENT, "condition_propose", {"op": "create", "data": {"code": "mdd"}, "rationale": "Noted"}, ctx)
    assert prop["status"] == "pending" and prop["data"]["patient_id"] == p.id
    no_consent = setup_patient(h, consent=False)
    with pytest.raises(PermissionDenied):
        registry(h).call(AGENT, "observation_list", {}, {"scope": "patient", "patient_id": no_consent.id})
    other = setup_patient(h)
    obs = h.create(DR, "observation", {"patient_id": other.id, "code": "pulse", "value": 1})
    with pytest.raises(PermissionDenied):
        registry(h).call(AGENT, "observation_get", {"id": obs.id}, ctx)
    with pytest.raises(NotFound):
        registry(h).call(AGENT, "provider_list", {}, ctx)


def test_harness_tools_work_without_an_llm(h):
    p = setup_patient(h)
    ctx = {"scope": "patient", "patient_id": p.id}
    ex = registry(h).call(AGENT, "extract_observations", {"text": "BP 150/95, pulse 88, PHQ-9 score 14"}, ctx)
    codes = {e["code"]: e["value"] for e in ex["extracted"]}
    assert codes["bp_systolic"] == 150 and codes["pulse"] == 88 and codes["phq9_score"] == 14
    assert all(x["status"] == "pending" for x in ex["proposals"])
    assert "Asha" in registry(h).call(AGENT, "summarize_patient", {}, ctx)["summary"]
    assert registry(h).call(AGENT, "clinical_reasoning", {"question": "next step?"}, ctx)["mode"] == "deterministic"
    ev = registry(h).call(AGENT, "fetch_evidence", {"query": "depression SSRI remission"}, ctx)
    assert ev["results"] and ev["results"][0]["definition"].startswith("protocol:")
    gen = registry(h).call(AGENT, "generate_definition", {"def_kind": "journey", "key": "k", "brief": "OPD"}, {"scope": "general"})
    assert gen["needs_llm"] and "states" in gen["schema"]["properties"]


def test_generate_definition_with_an_llm_is_a_proposal_for_agents(h):
    class FakeLLM:
        def complete(self, system, prompt, json_schema=None):
            return {"states": ["a", "b"], "initial": "a", "terminal": ["b"]}
    h.llm = FakeLLM()
    out = registry(h).call(AGENT, "generate_definition", {"def_kind": "journey", "key": "simple", "brief": "Two steps"},
                       {"scope": "general"})
    assert out["proposal"]["target_kind"] == "definition"
    d = h.accept_proposal(DR, out["proposal"]["id"])
    assert h.get(DR, "definition", d.result_id).status == "draft"


# ── HTTP ──
@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from clinestra.server import APIKeyAuth, create_app
    keys = {"k_dr": {"id": "dr_a", "site": S, "role": "clinician"},
            "k_dr2": {"id": "dr_b", "site": S, "role": "clinician"},
            "k_nurse": {"id": "nurse", "site": S, "role": "nurse"},
            "k_bot": {"id": "bot", "site": S, "role": "clinician", "actor_type": "agent"}}
    return TestClient(create_app(Harness(packs=TEST_PACKS), APIKeyAuth(keys)))


def auth(k):
    return {"Authorization": f"Bearer {k}"}


def test_api_requires_auth_and_generates_typed_routes(client):
    assert client.get("/v1/patients").status_code == 401
    spec = client.get("/openapi.json").json()
    for path in ("/v1/patients", "/v1/clinical-states/{id}/transition", "/v1/note-pages/{id}/transition",
                 "/v1/proposals/{id}/accept", "/v1/tools/call", "/v1/journeys/{id}/move"):
        assert path in spec["paths"], path
    assert "ObservationCreate" in spec["components"]["schemas"]
    assert len(client.get("/v1/concepts", headers=auth("k_dr")).json()) == 31


def test_api_flow_with_proposals_and_approvals(client):
    r = client.post("/v1/patients", json={"name": "A", "country": "IN", "gender": "male", "age_years": 40}, headers=auth("k_dr"))
    assert r.status_code == 201
    pid = r.json()["id"]
    assert client.post("/v1/conditions", json={"patient_id": pid, "code": "mdd"}, headers=auth("k_nurse")).status_code == 403
    assert client.post("/v1/conditions", json={"patient_id": pid, "code": "mdd"}, headers=auth("k_dr")).status_code == 201
    r = client.post("/v1/cohort-memberships", json={"patient_id": pid, "cohort_key": "cohort_mdd", "reason": "Diagnosis"},
                    headers=auth("k_dr"))
    assert r.status_code == 201
    props = client.get("/v1/proposals", params={"patient_id": pid, "status": "pending"}, headers=auth("k_dr")).json()
    assert client.post(f"/v1/proposals/{props[0]['id']}/accept", json={}, headers=auth("k_dr")).status_code == 200
    plans = client.get("/v1/careplans", params={"patient_id": pid}, headers=auth("k_dr")).json()
    assert plans[0]["status"] == "draft"
    rec = client.post("/v1/clinical-records", json={"patient_id": pid}, headers=auth("k_nurse")).json()
    page = client.post("/v1/note-pages", json={"patient_id": pid, "record_id": rec["id"], "definition_key": "mdd_case_sheet"},
                       headers=auth("k_nurse")).json()
    r = client.post(f"/v1/note-pages/{page['id']}/transition", json={"to": "approved"}, headers=auth("k_nurse"))
    assert r.status_code == 422   # required blocks missing
    client.patch(f"/v1/note-pages/{page['id']}", json={"blocks": {"mood_and_affect_narrative": "Low",
                                                                  "suicidal_ideation_screen": "none"}}, headers=auth("k_nurse"))
    r = client.post(f"/v1/note-pages/{page['id']}/transition", json={"to": "approved"}, headers=auth("k_dr"))
    assert r.status_code == 202 and r.json()["subject_id"] == page["id"]
    assert client.post(f"/v1/approvals/{r.json()['id']}/decide", json={"approve": True}, headers=auth("k_dr")).status_code == 403
    assert client.post(f"/v1/approvals/{r.json()['id']}/decide", json={"approve": True}, headers=auth("k_dr2")).status_code == 200
    assert client.get(f"/v1/note-pages/{page['id']}", headers=auth("k_dr")).json()["status"] == "approved"
    assert client.get(f"/v1/patients/{pid}/timeline/verify", headers=auth("k_dr")).json()["valid"]


def test_api_tools_endpoint(client):
    pid = client.post("/v1/patients", json={"name": "A", "country": "IN", "gender": "male", "age_years": 40},
                      headers=auth("k_dr")).json()["id"]
    assert client.get("/v1/tools", params={"patient_id": pid}, headers=auth("k_bot")).status_code == 200
    r = client.post("/v1/tools/call", json={"name": "summarize_patient", "patient_id": pid}, headers=auth("k_bot"))
    assert r.status_code == 403   # no ai consent
    c = client.post("/v1/consents", json={"patient_id": pid, "consent_scope": "ai"}, headers=auth("k_dr")).json()
    client.post(f"/v1/consents/{c['id']}/transition", json={"to": "granted"}, headers=auth("k_dr"))
    r = client.post("/v1/tools/call", json={"name": "summarize_patient", "patient_id": pid}, headers=auth("k_bot"))
    assert r.status_code == 200 and "summary" in r.json()


def test_dev_auth_warns():
    from clinestra.server import DevHeaderAuth
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        DevHeaderAuth()
    assert w


def test_api_create_from_definition_fills_required_fields(client):
    pid = client.post("/v1/patients", json={"name": "A", "country": "IN", "gender": "male", "age_years": 40},
                      headers=auth("k_dr")).json()["id"]
    body = {"patient_id": pid, "definition_key": "ssri_sertraline"}
    assert client.post("/v1/regimens", json=body, headers=auth("k_nurse")).status_code == 403
    r = client.post("/v1/regimens", json=body, headers=auth("k_dr"))
    assert r.status_code == 201 and r.json()["name"] == "Sertraline first line"
    assert client.post("/v1/regimens", json={"patient_id": pid}, headers=auth("k_dr")).status_code == 422
