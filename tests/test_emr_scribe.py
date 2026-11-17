"""EMR scribe: a dictation becomes EMRU commands that the server applies to the page's template sections and blocks,
so the standard case sheet gets the values as well as the canvas. Transcription and the model are stubbed."""
from conftest import TEST_PACKS
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from clinestra import Actor, Harness
from clinestra.server.app import create_app

S = "site_a"
DR = Actor(id="dr_a", site=S, role="clinician")
ADMIN = Actor(id="admin", site=S, role="admin")
EMRU = ("158::unit:mg/dL//glycaemia//fbs//pop~ Metformin::dose:1 g::frequency:BD//meds//medication_name//add~ "
        "Feet normal on monofilament//notes//pop~ NO_UPDATE_REQUIRED")
PAGE_DEF = {"page_type": "case_sheet",
            "blocks": [{"key": "glycaemia__fbs", "label": "FBS", "input_type": "number", "observation_code": "fbs"},
                       {"key": "meds", "label": "Medications"}, {"key": "notes", "label": "Notes"}, {"key": "additional_sections", "label": "x"}],
            "sections": [{"section_id": "glycaemia", "section_name": "Glycaemia", "section_type": "key_value",
                          "config": {"fields": [{"key": "fbs", "label": "Fasting glucose", "type": "number", "unit": "mg/dL"}]}},
                         {"section_id": "meds", "section_name": "Medications", "section_type": "table_form",
                          "config": {"columns": [{"name": "medication_name"}, {"name": "dose"}, {"name": "frequency"}]}},
                         {"section_id": "notes", "section_name": "Notes", "section_type": "text_area", "config": {}}]}


class Auth:
    keys = {}

    def __call__(self, request):
        return DR


@pytest.fixture
def setup(monkeypatch):
    from clinestra.server.agentic import runtime
    from clinestra.server.portal import interview

    h = Harness(packs=TEST_PACKS)
    d = h.create(ADMIN, "definition", {"def_kind": "note_page", "key": "dm", "name": "Diabetes review", "config": PAGE_DEF})
    h.transition(ADMIN, "definition", d.id, "published")
    p = h.create(DR, "patient", {"name": "Asha", "country": "IN", "gender": "female", "age_years": 50})
    enc = h.create(DR, "encounter", {"patient_id": p.id})
    rec = h.create(DR, "clinical_record", {"patient_id": p.id, "encounter_id": enc.id})
    page = h.create(DR, "note_page", {"patient_id": p.id, "record_id": rec.id, "definition_key": "dm",
                                      "blocks": {"meds": [{"medication_name": "Amlodipine", "dose": "5 mg"}]}})
    monkeypatch.setattr(interview, "_api_key", lambda st: "test-key")
    monkeypatch.setattr(httpx, "post", lambda url, **k: httpx.Response(200, text="Fasting sugar one fifty eight ...", request=httpx.Request("POST", url)))
    monkeypatch.setattr(runtime, "resolve", lambda st, feature, alias=None: ({"model_id": "m"}, {"platform": "openai"}))
    monkeypatch.setattr(runtime, "stream_text", lambda st, m, p, system, prompt: iter([("text", EMRU)]))
    return h, TestClient(create_app(harness=h, auth=Auth())), page


def test_dictation_lands_in_the_page_blocks(setup):
    h, c, page = setup
    r = c.post("/v1/emr/clinical_notes/process_audio_in_fragments/", data={"page_id": page.id, "recording_id": "r1", "sequence_id": "1"},
               files={"file": ("a.webm", b"x" * 200, "audio/webm")})
    assert r.json()["response"]["transcript"].startswith("Fasting sugar")
    r = c.post("/v1/emr/clinical_notes/voice_to_clinical_notes_sse/", data={"page_id": page.id, "recording_id": "r1"})
    events = [json.loads(x[6:]) for x in r.text.splitlines() if x.startswith("data: ")]
    assert [e["section_id"] for e in events if e.get("section_id")] == ["glycaemia", "meds", "notes"]
    assert events[-1]["message"] == "Sections updated" and events[-1]["applied"] == 3
    blocks = h.get(DR, "note_page", page.id).blocks
    assert blocks["glycaemia__fbs"] == "158" and blocks["notes"] == "Feet normal on monofilament"
    assert [m["medication_name"] for m in blocks["meds"]] == ["Amlodipine", "Metformin"]  # the typed row is kept
    assert blocks["meds"][1]["frequency"] == "BD"
