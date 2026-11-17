"""Consent forms: library -> template -> send -> patient signs from a link (OTP) -> clinician signs -> signed document,
granted consent, FHIR Consent. Ported from Clinestra 2.0; templates from its TinyMCE editor are upgraded."""
from conftest import TEST_PACKS
import hashlib
import re

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from clinestra import Harness  # noqa: E402
from clinestra.server import APIKeyAuth, create_app  # noqa: E402
from clinestra.server.consents import fields_of, render_final, upgrade_legacy  # noqa: E402

S = "site_a"
KEYS = {"k_dr": {"id": "dr_a", "site": S, "role": "clinician"}, "k_nurse": {"id": "nurse_a", "site": S, "role": "nurse"}}
TINY = 'A 1x1 PNG: data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='
SIG = TINY.split(": ")[1]


def auth(k):
    return {"Authorization": f"Bearer {k}"}


@pytest.fixture
def c(monkeypatch):
    monkeypatch.setenv("CLINESTRA_ENV", "development")
    return TestClient(create_app(harness=Harness(packs=TEST_PACKS), auth=APIKeyAuth(KEYS)))


def test_fields_variables_and_legacy_templates():
    body = ('<p><span data-field="text" data-signer="clinician" data-name="procedure" data-label="Procedure" data-required="true">x</span>'
            '<span data-field="checkbox" data-signer="patient" data-label="Agree">Agree</span>'
            '<span data-field="signature" data-signer="patient" data-label="Sign">Sign</span></p>')
    f = fields_of(body)
    assert [(x["key"], x["type"], x["signer"], x["required"]) for x in f] == [
        ("procedure", "text", "clinician", True), ("patient_checkbox", "checkbox", "patient", False), ("patient_signature", "signature", "patient", True)]
    out = render_final(body, {"procedure": "<b>Biopsy</b>", "patient_checkbox": True}, {"patient_signature": SIG})
    assert "&lt;b&gt;Biopsy&lt;/b&gt;" in out and "☑" in out and 'src="data:image/png' in out and "data-field" not in out
    old = ('<p>Name <span class="mymention" data-mention-id="beneficiary_name">{{beneficiary_name}}</span> '
           '<span class="mymention" data-mention-id="external_signature_input_field">sign</span>'
           '<span class="mymention" data-mention-id="manual_text_input_field_internal">note</span></p>')
    new = upgrade_legacy(old)
    assert 'data-variable="patient_name"' in new and "mymention" not in new
    assert [(x["type"], x["signer"]) for x in fields_of(new)] == [("signature", "patient"), ("text", "clinician")]


def test_send_sign_store_and_export(c):
    pid = c.post("/v1/patients", json={"name": "Asha Devi", "country": "IN", "gender": "female", "age_years": 34}, headers=auth("k_dr")).json()["id"]
    lib = c.get("/v1/consent-forms/library", headers=auth("k_dr")).json()
    assert {t["id"] for t in lib} >= {"general_treatment", "ai_in_care", "procedure", "general_treatment_hi"}
    d = c.post("/v1/consent-forms/library/general_treatment/install", headers=auth("k_dr"))
    assert d.status_code == 201, d.text
    d = d.json()
    cfg = {**d["config"], "require_otp": True}
    assert c.patch(f"/v1/definitions/{d['id']}", json={"config": cfg}, headers=auth("k_dr")).status_code == 200
    assert c.post(f"/v1/definitions/{d['id']}/transition", json={"to": "published"}, headers=auth("k_dr")).status_code == 200

    sent = c.post("/v1/consent-forms/sessions", json={"definition_key": "consent_general_treatment", "patient_id": pid}, headers=auth("k_nurse"))
    assert sent.status_code == 201, sent.text
    sent = sent.json()
    assert "Asha Devi" in sent["html"] and "{{" not in sent["html"]
    token = sent["patient_link"].removeprefix("/sign/")

    # the patient's link: code first, then only their own fields
    assert c.get(f"/v1/public/consent/{token}").json()["otp_required"] is True
    otp = c.post(f"/v1/public/consent/{token}/otp").json()["dev_otp"]
    assert c.post(f"/v1/public/consent/{token}/otp/verify", json={"code": "000000" if otp != "000000" else "111111"}).status_code == 400
    view = c.post(f"/v1/public/consent/{token}/otp/verify", json={"code": otp}).json()
    assert view["signer"] == "patient" and "patient_signature" in view["editable"] and "clinician_signature" not in view["editable"]
    assert c.post(f"/v1/public/consent/{token}/sign", json={"values": {}, "signatures": {}}).status_code == 400  # required items missing
    bad = c.post(f"/v1/public/consent/{token}/sign", json={"values": {"understands_withdrawal": True}, "signatures": {"patient_signature": "javascript:alert(1)"}})
    assert bad.status_code == 400
    r = c.post(f"/v1/public/consent/{token}/sign", json={"values": {"understands_withdrawal": True, "patient_notes": "<script>x</script>"},
                                                         "signatures": {"patient_signature": SIG}})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "open"  # the clinician still has to sign
    assert c.get(f"/v1/public/consent/{token}").status_code == 404  # one signature per link

    # the clinician countersigns in the console; the form is complete
    assert c.post(f"/v1/consent-forms/sessions/{sent['id']}/sign", json={"signer": "patient"}, headers=auth("k_dr")).status_code == 400
    done = c.post(f"/v1/consent-forms/sessions/{sent['id']}/sign", json={"signer": "clinician", "signatures": {"clinician_signature": SIG}}, headers=auth("k_dr"))
    assert done.status_code == 200, done.text
    done = done.json()
    assert done["status"] == "completed" and done["document_id"] and done["consent_id"] and re.fullmatch(r"[0-9a-f]{64}", done["hash"])

    doc = c.get(f"/v1/clinical-documents/{done['document_id']}", headers=auth("k_dr")).json()
    assert doc["status"] == "ready" and doc["signed"] and doc["doc_type"] == "consent_form"
    assert "&lt;script&gt;" in doc["body_html"] and "<script>" not in doc["body_html"]
    assert hashlib.sha256(doc["body_html"].encode()).hexdigest() == done["hash"]
    consent = c.get(f"/v1/consents/{done['consent_id']}", headers=auth("k_dr")).json()
    assert consent["status"] == "granted" and consent["consent_scope"] == "treatment" and consent["document_id"] == doc["id"]

    fhir = c.get(f"/v1/consent-forms/sessions/{sent['id']}/fhir", headers=auth("k_dr")).json()
    assert fhir["resourceType"] == "Consent" and fhir["patient"]["reference"] == f"Patient/{pid}"
    assert fhir["sourceReference"]["reference"] == f"DocumentReference/{doc['id']}" and fhir["extension"][0]["valueString"] == done["hash"]


def test_links_expire_and_staff_only(c):
    assert c.get("/v1/public/consent/site_a~nope").status_code == 404
    assert c.get("/v1/public/consent/garbage").status_code == 404
    pid = c.post("/v1/patients", json={"name": "Ravi", "country": "IN", "gender": "male", "age_years": 40}, headers=auth("k_dr")).json()["id"]
    d = c.post("/v1/consent-forms/library/ai_in_care/install", headers=auth("k_dr")).json()
    c.post(f"/v1/definitions/{d['id']}/transition", json={"to": "published"}, headers=auth("k_dr"))
    s = c.post("/v1/consent-forms/sessions", json={"definition_key": "consent_ai_in_care", "patient_id": pid}, headers=auth("k_dr")).json()
    token = s["patient_link"].removeprefix("/sign/")
    new = c.post(f"/v1/consent-forms/sessions/{s['id']}/link", headers=auth("k_dr")).json()["patient_link"].removeprefix("/sign/")
    assert c.get(f"/v1/public/consent/{token}").status_code == 404 and c.get(f"/v1/public/consent/{new}").status_code == 200
    # declining is recorded as a signed form, without granting the consent
    r = c.post(f"/v1/public/consent/{new}/sign", json={"values": {"choice": "I do not agree"}, "signatures": {"patient_signature": SIG}}).json()
    assert r["status"] == "completed" and r["document_id"] and not r["consent_id"]
