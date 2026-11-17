"""The optional AI, admin and portal routes (clinestra[ai]): secrets, settings, agents, portal sign-in."""
from conftest import TEST_PACKS
import pytest

pytest.importorskip("cryptography")
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from clinestra import Harness, secrets as sec  # noqa: E402
from clinestra.server import APIKeyAuth, create_app  # noqa: E402

S = "site_a"
KEYS = {
    "k_admin": {"id": "admin_a", "site": S, "role": "admin"},
    "k_dr": {"id": "dr_a", "site": S, "role": "clinician"},
    "k_nurse": {"id": "nurse_a", "site": S, "role": "nurse"},
    "k_other_admin": {"id": "admin_b", "site": "site_b", "role": "admin"},
}
PROVIDER_KEY = "TEST_ONLY_not_a_real_key"


def auth(k):
    return {"Authorization": f"Bearer {k}"}


@pytest.fixture(autouse=True)
def master_key(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("CLINESTRA_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("CLINESTRA_SITE", S)
    sec.reset()
    yield
    sec.reset()


@pytest.fixture
def h():
    return Harness(packs=TEST_PACKS)


@pytest.fixture
def client(h):
    return TestClient(create_app(harness=h, auth=APIKeyAuth(KEYS)))


def patient(client, phone=None, consent=True):
    body = {"name": "Asha Devi", "country": "IN", "gender": "female", "age_years": 34}
    if phone:
        body["demographics"] = {"phone": phone}
    pid = client.post("/v1/patients", json=body, headers=auth("k_dr")).json()["id"]
    if consent:
        c = client.post("/v1/consents", json={"patient_id": pid, "consent_scope": "ai"}, headers=auth("k_dr")).json()
        client.post(f"/v1/consents/{c['id']}/transition", json={"to": "granted"}, headers=auth("k_dr"))
    return pid


# ───────────── secrets ─────────────
def test_secrets_roundtrip_mask_and_wrong_key(monkeypatch):
    token = sec.encrypt({"api_key": PROVIDER_KEY})
    assert PROVIDER_KEY not in token
    assert sec.decrypt(token) == {"api_key": PROVIDER_KEY}
    assert sec.mask(PROVIDER_KEY).endswith("_key") and PROVIDER_KEY not in sec.mask(PROVIDER_KEY)
    assert sec.master_source() == "environment variable CLINESTRA_MASTER_KEY"
    from cryptography.fernet import Fernet
    monkeypatch.setenv("CLINESTRA_MASTER_KEY", Fernet.generate_key().decode())
    sec.reset()
    with pytest.raises(sec.SecretError):
        sec.decrypt(token)


def test_secrets_key_file_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("CLINESTRA_MASTER_KEY")
    monkeypatch.setenv("CLINESTRA_MASTER_KEY_FILE", str(tmp_path / "m.key"))
    sec.reset()
    assert sec.decrypt(sec.encrypt({"a": 1})) == {"a": 1}
    assert (tmp_path / "m.key").exists() and sec.master_source().startswith("key file")


# ───────────── settings ─────────────
def test_admin_only_and_site_scoped(client):
    assert client.get("/v1/admin/status", headers=auth("k_dr")).status_code == 403
    assert client.get("/v1/admin/status", headers=auth("k_admin")).status_code == 200
    r = client.put("/v1/admin/policy", json={"max_agent_steps": 3, "not_a_setting": 1}, headers=auth("k_admin")).json()
    assert r["max_agent_steps"] == 3 and "not_a_setting" not in r
    assert client.get("/v1/admin/policy", headers=auth("k_other_admin")).json()["max_agent_steps"] == 8  # other site untouched
    # A clinician stays out of admin writes once a role=admin key exists, even if listed.
    client.put("/v1/admin/admins", json=["admin_a", "dr_a"], headers=auth("k_admin"))
    assert client.get("/v1/admin/status", headers=auth("k_dr")).status_code == 403
    assert client.put("/v1/admin/policy", json={"max_agent_steps": 4}, headers=auth("k_dr")).status_code == 403


def test_provider_key_is_write_only_and_encrypted(client, h):
    r = client.post("/v1/admin/providers", json={"name": "Claude", "platform": "anthropic", "secrets": {"api_key": PROVIDER_KEY}},
                    headers=auth("k_admin"))
    assert r.status_code == 201
    assert PROVIDER_KEY not in r.text and "secret" not in r.json() and r.json()["secret_hint"]["api_key"].endswith("_key")
    listed = client.get("/v1/admin/providers", headers=auth("k_admin"))
    assert PROVIDER_KEY not in listed.text
    stored = h.store.get("_ai.provider", r.json()["id"], S)
    assert PROVIDER_KEY not in str(stored) and sec.decrypt(stored["secret"])["api_key"] == PROVIDER_KEY
    # update without secrets keeps the stored key
    pid = r.json()["id"]
    client.put(f"/v1/admin/providers/{pid}", json={"name": "Renamed", "platform": "anthropic"}, headers=auth("k_admin"))
    assert sec.decrypt(h.store.get("_ai.provider", pid, S)["secret"])["api_key"] == PROVIDER_KEY
    assert client.get("/v1/admin/providers", headers=auth("k_other_admin")).json() == []


def test_routing_makes_health_report_llm(client, h):
    assert client.get("/v1/health").json()["llm"] is False
    pid = client.post("/v1/admin/providers", json={"name": "Claude", "platform": "anthropic", "secrets": {"api_key": PROVIDER_KEY}},
                      headers=auth("k_admin")).json()["id"]
    assert client.put("/v1/admin/models/opus", json={"provider_id": pid, "model_id": "claude-opus-5-5"}, headers=auth("k_admin")).status_code == 200
    assert client.put("/v1/admin/routes", json={"agent": "nope"}, headers=auth("k_admin")).status_code == 400
    assert client.put("/v1/admin/routes", json={"agent": "opus"}, headers=auth("k_admin")).json() == {"agent": "opus"}
    assert client.get("/v1/health").json()["llm"] is True
    assert client.get("/v1/ai/status", headers=auth("k_dr")).json()["connected"] is True
    assert client.delete("/v1/admin/models/opus", headers=auth("k_admin")).status_code == 409  # still routed
    assert client.delete(f"/v1/admin/providers/{pid}", headers=auth("k_admin")).status_code == 409


# ───────────── agents ─────────────
def test_console_config_lists_agents_for_role(client):
    cfg = client.get("/v1/ai/config", headers=auth("k_nurse")).json()
    ids = {a["id"] for a in cfg["agents"]}
    assert "clinical_copilot" in ids and "patient_coach" not in ids
    assert cfg["ai"]["connected"] is False and cfg["me"]["id"] == "nurse_a"


def test_agent_chat_offline_fallback_runs_as_agent_with_consent(client):
    pid = patient(client)
    r = client.post("/v1/ai/agents/clinical_copilot/chat", json={"message": "Summarise this patient", "context": {"patient_id": pid}},
                    headers=auth("k_dr"))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["model"] is None and "No model is connected" in out["message"]
    assert any(s["tool"] == "summarize_patient" and s["ok"] for s in out["steps"])
    conv = client.get(f"/v1/conversations/{out['conversation_id']}", headers=auth("k_dr")).json()
    assert len(conv["turns"]) == 2  # the user's turn and the agent identity's reply are in the record

    # without the patient's AI consent the harness refuses the agent's tool call
    pid2 = patient(client, consent=False)
    out = client.post("/v1/ai/agents/clinical_copilot/chat", json={"message": "Summarise", "context": {"patient_id": pid2}},
                      headers=auth("k_dr")).json()
    assert out["steps"] and not any(s["ok"] for s in out["steps"])


def test_agent_audience_and_scope(client):
    assert client.post("/v1/ai/agents/patient_coach/chat", json={"message": "hi"}, headers=auth("k_dr")).status_code == 403
    assert client.post("/v1/ai/agents/clinical_copilot/chat", json={"message": "hi"}, headers=auth("k_dr")).status_code == 403  # patient level needs a patient
    assert client.post("/v1/ai/agents/missing/chat", json={"message": "hi"}, headers=auth("k_dr")).status_code == 404


# ───────────── portal ─────────────
def test_portal_otp_session_and_patient_scope(client):
    pid = patient(client, phone="98765 43210")
    other = patient(client, phone="9000000000")
    assert client.post("/v1/portal/otp/request", json={"phone": "12"}).status_code == 400
    r = client.post("/v1/portal/otp/request", json={"phone": "9876543210"}).json()
    assert r["sent"] and r["phone"] == "919876543210" and len(r["dev_otp"]) == 6
    assert client.post("/v1/portal/otp/request", json={"phone": "9876543210"}).status_code == 400  # 30 s cool-down
    assert client.post("/v1/portal/otp/verify", json={"phone": "9876543210", "otp": "000000" if r["dev_otp"] != "000000" else "111111"}).status_code == 400
    v = client.post("/v1/portal/otp/verify", json={"phone": "9876543210", "otp": r["dev_otp"]}).json()
    assert [p["id"] for p in v["profiles"]] == [pid]
    assert client.post("/v1/portal/session", json={"ticket": v["ticket"], "patient_id": other}).status_code == 400
    s = client.post("/v1/portal/session", json={"ticket": v["ticket"], "patient_id": pid}).json()
    tok = s["token"]
    assert tok.startswith("pt-")
    me = client.get("/v1/me", headers=auth(tok)).json()
    assert me["role"] == "patient" and me["patient_id"] == pid
    assert client.get(f"/v1/patients/{pid}", headers=auth(tok)).status_code == 200
    assert client.get(f"/v1/patients/{other}", headers=auth(tok)).status_code in (403, 404)
    sessions = client.get("/v1/admin/portal", headers=auth("k_admin")).json()["sessions"]
    assert len(sessions) == 1 and sessions[0]["phone"].endswith("3210") and "98765" not in sessions[0]["phone"]
    # the patient can ask their coach (offline: deterministic tools, as the agent identity, consent enforced)
    r = client.post("/v1/ai/agents/patient_coach/chat", json={"message": "What is due today?"}, headers=auth(tok))
    assert r.status_code == 200, r.text
    assert client.post("/v1/portal/logout", headers=auth(tok)).json() == {"ok": True}
    assert client.get("/v1/me", headers=auth(tok)).status_code == 401


def test_otp_responses_do_not_reveal_registration_and_lock_after_five_tries(client):
    patient(client, phone="9876543211")
    known = client.post("/v1/portal/otp/request", json={"phone": "9876543211"}).json()
    unknown = client.post("/v1/portal/otp/request", json={"phone": "9000000001"}).json()
    assert set(known) == set(unknown)
    assert "dev_note" not in known and known["dev_otp"] != unknown["dev_otp"]
    for _ in range(5):
        assert client.post("/v1/portal/otp/verify", json={"phone": "9876543211", "otp": "000000"}).status_code == 400
    assert client.post("/v1/portal/otp/verify", json={"phone": "9876543211", "otp": known["dev_otp"]}).status_code == 400


def test_dev_fixed_otp_is_refused_outside_development(client, monkeypatch):
    monkeypatch.delenv("CLINESTRA_ENV", raising=False)
    client.put("/v1/admin/portal", json={"otp_provider": "dev_fixed", "dev_fixed_otp": "654321"}, headers=auth("k_admin"))
    refused = client.post("/v1/portal/otp/request", json={"phone": "9876543210"})
    assert refused.status_code == 400 and "dev_fixed" in refused.json()["detail"]


def test_portal_config_and_site(client):
    client.put("/v1/admin/portal", json={"site_id": "demo", "app_name": "Demo Health", "organization": {"name": "Demo Org"}},
               headers=auth("k_admin"))
    assert client.get("/v1/portal/site").json() == {"site_id": "demo"}
    cfg = client.get("/v1/portal/config").json()
    assert cfg["app_name"] == "Demo Health" and cfg["dev_mode"] and cfg["interview"]["enabled"] is False  # no voice key yet
    assert client.get("/v1/portal/site/demo/config").json()["organization"]["name"] == "Demo Org"
    assert client.get("/v1/portal/site/other/config").status_code == 404


def test_otp_responses_do_not_reveal_registration_and_lock_out(client, h):
    from clinestra.server.agentic.state import SiteState
    from clinestra.server.portal.sessions import invalidate_sessions
    patient(client, phone="9876543210")
    known = client.post("/v1/portal/otp/request", json={"phone": "9876543210"}).json()
    unknown = client.post("/v1/portal/otp/request", json={"phone": "9000000099"}).json()
    assert set(known) == set(unknown)
    assert "dev_note" not in known and known["dev_otp"] != unknown["dev_otp"]
    phone = "9123456780"
    patient(client, phone=phone)
    issued = client.post("/v1/portal/otp/request", json={"phone": phone}).json()
    for _ in range(5):
        assert client.post("/v1/portal/otp/verify", json={"phone": phone, "otp": "000000"}).status_code == 400
    assert client.post("/v1/portal/otp/verify", json={"phone": phone, "otp": issued["dev_otp"]}).status_code == 400
    saved = client.put("/v1/admin/portal", json={"otp_provider": "dev_fixed", "dev_fixed_otp": "654321"},
                       headers=auth("k_admin"))
    assert saved.status_code == 200
    refused = client.post("/v1/portal/otp/request", json={"phone": "9876543210"})
    assert refused.status_code == 400 and "dev_fixed" in refused.json()["detail"]
    client.put("/v1/admin/portal", json={"otp_provider": "dev_screen"}, headers=auth("k_admin"))
    again = client.post("/v1/portal/otp/request", json={"phone": "9000000002"})
    # 30s cooldown does not apply to a new number
    assert again.status_code == 200
    pid = patient(client, phone="9000000002")
    ticket = client.post("/v1/portal/otp/verify", json={"phone": "9000000002", "otp": again.json()["dev_otp"]}).json()["ticket"]
    tok = client.post("/v1/portal/session", json={"ticket": ticket, "patient_id": pid}).json()["token"]
    assert client.get("/v1/me", headers=auth(tok)).status_code == 200
    invalidate_sessions(SiteState(h.store, S))
    assert client.get("/v1/me", headers=auth(tok)).status_code == 401


def test_realtime_key_write_only(client, h):
    r = client.put("/v1/admin/realtime", json={"settings": {"api_key": "TEST_ONLY_openai_not_a_real_key", "voice": "cedar"}},
                   headers=auth("k_admin")).json()
    assert r["settings"]["key_configured"] and r["settings"]["voice"] == "cedar" and "secret" not in r["settings"]
    assert "TEST_ONLY_openai_not_a_real_key" not in str(client.get("/v1/admin/realtime", headers=auth("k_admin")).json())


def test_otp_responses_do_not_reveal_registration_and_lock_out(client, h):
    from clinestra.server.agentic.state import SiteState
    from clinestra.server.portal.sessions import invalidate_sessions
    patient(client, phone="9876543210")
    known = client.post("/v1/portal/otp/request", json={"phone": "9876543210"}).json()
    unknown = client.post("/v1/portal/otp/request", json={"phone": "9000000001"}).json()
    assert set(known) == set(unknown) and "dev_note" not in known
    assert known["dev_otp"] != unknown["dev_otp"]
    wrong = "000000" if known["dev_otp"] != "000000" else "111111"
    for _ in range(5):
        assert client.post("/v1/portal/otp/verify", json={"phone": "9876543210", "otp": wrong}).status_code == 400
    assert client.post("/v1/portal/otp/verify", json={"phone": "9876543210", "otp": known["dev_otp"]}).status_code == 400
    fresh = client.post("/v1/portal/otp/request", json={"phone": "9111111111"}).json()
    verified = client.post("/v1/portal/otp/verify", json={"phone": "9111111111", "otp": fresh["dev_otp"]})
    assert verified.status_code == 400
    again = client.post("/v1/portal/otp/request", json={"phone": "9876543210"})
    assert again.status_code == 400  # still inside the 30 second window from the first request
    client.put("/v1/admin/portal", json={"otp_provider": "dev_fixed", "dev_fixed_otp": "654321"}, headers=auth("k_admin"))
    refused = client.post("/v1/portal/otp/request", json={"phone": "9444444444"})
    assert refused.status_code == 400 and "dev_fixed" in refused.json()["detail"]
    client.put("/v1/admin/portal", json={"otp_provider": "dev_screen"}, headers=auth("k_admin"))
    # A session from this process dies when the epoch changes (deploy / operator invalidate).
    other = patient(client, phone="9222222222")
    issued = client.post("/v1/portal/otp/request", json={"phone": "9222222222"}).json()
    ticket = client.post("/v1/portal/otp/verify", json={"phone": "9222222222", "otp": issued["dev_otp"]}).json()["ticket"]
    token = client.post("/v1/portal/session", json={"ticket": ticket, "patient_id": other}).json()["token"]
    assert client.get("/v1/me", headers=auth(token)).status_code == 200
    invalidate_sessions(SiteState(h.store, S))
    assert client.get("/v1/me", headers=auth(token)).status_code == 401
    assert client.get("/v1/portal/config").json()["interview"]["enabled"] is True


def test_otp_responses_do_not_reveal_registration_and_lock_out(client, h):
    from clinestra.server.agentic.state import SiteState
    from clinestra.server.portal.sessions import invalidate_sessions
    patient(client, phone="9876543210")
    known = client.post("/v1/portal/otp/request", json={"phone": "9876543210"}).json()
    unknown = client.post("/v1/portal/otp/request", json={"phone": "9000000099"}).json()
    assert set(known) == set(unknown)
    assert "dev_note" not in known and known["dev_otp"] != unknown["dev_otp"]
    locked = client.post("/v1/portal/otp/request", json={"phone": "9111111111"}).json()
    for _ in range(5):
        assert client.post("/v1/portal/otp/verify", json={"phone": "9111111111", "otp": "000000"}).status_code == 400
    assert client.post("/v1/portal/otp/verify", json={"phone": "9111111111", "otp": locked["dev_otp"]}).status_code == 400
    saved = client.put("/v1/admin/portal", json={"otp_provider": "dev_fixed", "dev_fixed_otp": "654321"},
                       headers=auth("k_admin"))
    assert saved.status_code == 200
    refused = client.post("/v1/portal/otp/request", json={"phone": "9876543210"})
    assert refused.status_code == 400 and "dev_fixed" in refused.json()["detail"]
    v = client.post("/v1/portal/otp/verify", json={"phone": "9876543210", "otp": known["dev_otp"]}).json()
    token = client.post("/v1/portal/session", json={"ticket": v["ticket"], "patient_id": v["profiles"][0]["id"]}).json()["token"]
    assert client.get("/v1/me", headers=auth(token)).status_code == 200
    invalidate_sessions(SiteState(h.store, S))
    assert client.get("/v1/me", headers=auth(token)).status_code == 401
