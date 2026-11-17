"""Engine changes from the clinical-engine instruction set. Neutral fixtures only."""
import os
from conftest import TEST_PACKS
import json
from pathlib import Path

import pytest

from clinestra import Actor, Harness, Invalid, InvariantViolation, PermissionDenied
from clinestra import secrets as sec
from clinestra.server.agentic.deid import redact, secrets_from_patient
from clinestra.server.agentic.defaults import DEFAULT_POLICY
from clinestra.engine.compute import eval_reference_equation
from clinestra.engine.units import convert
from clinestra import secrets as sec
from clinestra import secrets as sec
from clinestra.server.auth import APIKeyAuth, HASHED_FORMAT, accept_new_key, hash_api_key

S = "site_a"
DR = Actor(id="dr_a", site=S, role="clinician")
DR2 = Actor(id="dr_b", site=S, role="clinician")
ADMIN = Actor(id="admin", site=S, role="admin")
LEAD = Actor(id="lead_a", site=S, role="clinical_lead")
AGENT = Actor(id="bot", site=S, role="clinician", actor_type="agent")
SYS = Actor(id="system:reactions", site=S, role="system", actor_type="system")


@pytest.fixture
def h():
    return Harness(packs=TEST_PACKS)


def patient(h):
    return h.create(DR, "patient", {"name": "Asha", "country": "IN", "gender": "female", "age_years": 34})


def _evidence():
    return [{"id": "fx-1", "title": "Fixture source", "source_type": "local_sop"}]


def test_engine_source_has_no_site_specific_names():
    root = Path(__file__).resolve().parents[1] / "src" / "clinestra"
    # Terms that must not ship are kept out of this public repo: set them in CI as a comma-separated secret.
    banned = [w for w in os.environ.get("CLINESTRA_BANNED_TERMS", "").split(",") if w]
    hits = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for word in banned:
            if word in text:
                hits.append(f"{path.relative_to(root)}: {word}")
    assert hits == []


def test_default_policy_does_not_send_identifiers():
    assert DEFAULT_POLICY["send_patient_identifiers"] is False


def test_deidentify_strips_name_phone_and_employee_number():
    patient_doc = {"name": "FORGE_TEST Worker", "demographics": {"phone": "9876543210"},
                   "custom_fields": {"employee_no": "E12345"}}
    text = redact(
        "See FORGE_TEST Worker, phone 9876543210, employee E12345.",
        secrets_from_patient(patient_doc),
    )
    assert "FORGE_TEST" not in text
    assert "9876543210" not in text
    assert "E12345" not in text


def test_null_reference_range_has_no_flag():
    result = eval_reference_equation({"a": 3}, [{"expression": "a + 1"}], {})
    assert result["flag"] is None
    assert result["value"] == 4


def test_bad_expressions_are_invalid_not_server_errors(h):
    for expression in ("1 / a", "__import__('os').getcwd()", "().__class__", "[a for a in [1]]", "lambda: 1", "9**9**9"):
        with pytest.raises(Invalid):
            h.run_calculator(DR, "eval_reference_equation", {"a": 0}, [{"expression": expression}])


def test_calculator_range_names_the_field(h):
    with pytest.raises(Invalid) as caught:
        h.run_calculator(DR, "eval_reference_equation", {"height_cm": 1}, None,
                         {"expression": "height_cm", "inputs": [{"name": "height_cm", "unit": "cm", "min": 50, "max": 250}]})
    assert "height_cm" in caught.value.fields


def test_height_conversion_makes_bmi_agree():
    metres = convert(170, "cm", "m")
    assert round(70 / (metres ** 2), 4) == round(70 / ((170 / 100) ** 2), 4)


def test_regimen_needs_a_second_clinician_before_medications_exist(h):
    p = patient(h)
    regimen = h.create(DR, "regimen", {"patient_id": p.id, "name": "Trial",
                                       "drugs": [{"code": "sertraline", "dose": "50 mg"}],
                                       "orders": [{"code": "cbc", "display": "CBC", "order_kind": "lab"}]})
    assert regimen.status == "proposed"
    assert h.list(DR, "medication", p.id) == []
    h.transition(DR, "regimen", regimen.id, "signed")
    with pytest.raises(PermissionDenied):
        h.transition(DR, "regimen", regimen.id, "counter_signed")
    with pytest.raises(PermissionDenied):
        h.transition(AGENT, "regimen", regimen.id, "counter_signed")
    with pytest.raises(PermissionDenied):
        h.transition(SYS, "regimen", regimen.id, "counter_signed")
    h.transition(DR2, "regimen", regimen.id, "counter_signed")
    active = h.transition(DR, "regimen", regimen.id, "active")
    assert active.status == "active"
    meds = h.list(DR, "medication", p.id)
    assert len(meds) == 1
    assert meds[0].source == "regimen" and meds[0].signed_by == DR.id and meds[0].counter_signed_by == DR2.id
    orders = [a for a in h.list(DR, "care_action", p.id) if a.action_type == "order"]
    assert len(orders) == 1 and orders[0].order_status == "proposed"


def test_publish_gate_requires_citations_and_approval(h):
    bare = h.create(ADMIN, "definition", {"def_kind": "protocol", "key": "proto_fx", "name": "Fixture", "config": {}})
    h.review_definition(LEAD, bare.id, "approve", review_ref="fx")
    with pytest.raises(Invalid, match="Protocol proto_fx has no evidence citations"):
        h.transition(ADMIN, "definition", bare.id, "published")
    cited = h.create(ADMIN, "definition", {"def_kind": "protocol", "key": "proto_fx2", "name": "Fixture 2",
                                           "config": {"evidence": _evidence()}})
    with pytest.raises(Invalid, match="review status is not approved"):
        h.transition(ADMIN, "definition", cited.id, "published")
    with pytest.raises(PermissionDenied):
        h.review_definition(ADMIN, cited.id, "approve")
    h.review_definition(LEAD, cited.id, "approve", review_ref="fx")
    assert h.transition(ADMIN, "definition", cited.id, "published").status == "published"


def test_production_blocks_unapproved_definitions(h, monkeypatch):
    p = patient(h)
    monkeypatch.setenv("CLINESTRA_MODE", "production")
    with pytest.raises(InvariantViolation, match="not clinically approved"):
        h.create(DR, "assessment", {"patient_id": p.id, "definition_key": "phq9",
                                    "answers": {f"item_{i}": 0 for i in range(1, 10)}})


def test_short_keys_are_rejected_and_hashed_keys_match():
    with pytest.raises(ValueError):
        accept_new_key("too-short")
    raw = "TEST_ONLY_" + ("k" * 24)
    accept_new_key(raw)
    auth = APIKeyAuth({"format": HASHED_FORMAT, "keys": [{
        "key_id": "key_1", "prefix": raw[:6], "hash": hash_api_key(raw),
        "actor": {"id": "dr_a", "site": S, "role": "clinician"}, "revoked": False,
    }]})
    assert auth._match(raw).id == "dr_a"
    assert auth._match(raw + "nope") is None
    assert "TEST_ONLY_" not in json.dumps(auth.entries)


def test_production_refuses_a_master_key_file(monkeypatch):
    monkeypatch.setenv("CLINESTRA_MODE", "production")
    monkeypatch.delenv("CLINESTRA_MASTER_KEY", raising=False)
    sec.reset()
    try:
        with pytest.raises(sec.SecretError, match="CLINESTRA_MASTER_KEY"):
            sec.master_source()
    finally:
        sec.reset()


def test_reencrypt_round_trip():
    from cryptography.fernet import Fernet
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    token = Fernet(old.encode()).encrypt(json.dumps({"api_key": "TEST_ONLY_not_a_key"}).encode()).decode()
    moved = sec.reencrypt(token, old, new)
    assert json.loads(Fernet(new.encode()).decrypt(moved.encode()))["api_key"] == "TEST_ONLY_not_a_key"
    with pytest.raises(sec.SecretError):
        sec.reencrypt(moved, old, new)


def test_production_refuses_plaintext_keys(monkeypatch):
    monkeypatch.setenv("CLINESTRA_MODE", "production")
    with pytest.raises(RuntimeError, match="plaintext"):
        APIKeyAuth({"k": {"id": "a", "site": S, "role": "admin"}})


def test_calculator_returns_a_stable_inputs_hash(h):
    first = h.run_calculator(DR, "eval_reference_equation", {"a": 2}, [{"expression": "a"}], {})
    second = h.run_calculator(DR, "eval_reference_equation", {"a": 2}, [{"expression": "a"}], {})
    assert first["usage_id"] != second["usage_id"]
    runs = h.store.list("run", S, None)
    hashes = {row["inputs_hash"] for row in runs if row["usage_id"] in (first["usage_id"], second["usage_id"])}
    assert len(hashes) == 1


def test_production_refuses_a_master_key_file(monkeypatch):
    monkeypatch.setenv("CLINESTRA_MODE", "production")
    monkeypatch.delenv("CLINESTRA_MASTER_KEY", raising=False)
    sec.reset()
    try:
        with pytest.raises(sec.SecretError, match="CLINESTRA_MASTER_KEY"):
            sec.master_source()
    finally:
        sec.reset()


def test_production_refuses_a_master_key_file(monkeypatch):
    monkeypatch.setenv("CLINESTRA_MODE", "production")
    monkeypatch.delenv("CLINESTRA_MASTER_KEY", raising=False)
    sec.reset()
    with pytest.raises(sec.SecretError, match="CLINESTRA_MASTER_KEY"):
        sec.master_source()
    sec.reset()


def test_health_reports_unreviewed_override(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from clinestra.server import create_app
    monkeypatch.setenv("CLINESTRA_ALLOW_UNREVIEWED_PACKS", "1")
    monkeypatch.setenv("CLINESTRA_DEV_AUTH", "1")
    monkeypatch.delenv("CLINESTRA_API_KEYS", raising=False)
    client = TestClient(create_app())
    body = client.get("/v1/health").json()
    assert body["unreviewed_content"] is True
    assert body["mode"] == "pilot"
