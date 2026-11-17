"""Encounter workflows: journeys tied to an encounter, with stages, gates, requirements and automatic moves."""
from conftest import TEST_PACKS
import pytest

from clinestra import Actor, Harness, InvariantViolation, PermissionDenied

S = "site_a"
DR = Actor(id="dr_a", site=S, role="clinician")
NURSE = Actor(id="nurse_a", site=S, role="nurse")
ADMIN = Actor(id="admin", site=S, role="admin")
AGENT = Actor(id="bot", site=S, role="clinician", actor_type="agent")

FLOW = {
    "states": ["arrived", "with_doctor", "review", "done"],
    "initial": "arrived",
    "transitions": {"arrived": ["with_doctor"], "with_doctor": ["review"], "review": ["done", "with_doctor"]},
    "terminal": ["done"],
    "association_type": "encounter",
    "stages": {"arrived": {"label": "Arrived", "category": "todo"}, "done": {"label": "Done", "category": "done"}},
    "gates": {"arrived": [], "done": ["close_encounter"]},
    "requires": {"with_doctor": [{"kind": "note_page", "state": "finalized", "label": "Case sheet finalized"}]},
    "advance_on": [{"when_kind": "note_page", "when_state": "finalized", "to": "review"}],
    "on_exit": {"review": [{"op": "create", "kind": "task", "data": {"title": "Book the follow-up"}}]},
}


@pytest.fixture
def h():
    h = Harness(packs=TEST_PACKS)
    for kind, key, cfg in (("journey", "opd_flow", FLOW), ("encounter", "opd", {"encounter_type": "opd", "workflow": "opd_flow"})):
        d = h.create(ADMIN, "definition", {"def_kind": kind, "key": key, "name": key, "config": cfg})
        h.transition(ADMIN, "definition", d.id, "published")
    return h


def start(h):
    p = h.create(DR, "patient", {"name": "Asha", "country": "IN", "gender": "female", "age_years": 34})
    enc = h.create(NURSE, "encounter", {"patient_id": p.id, "definition_key": "opd"})
    j = h.list(NURSE, "journey", p.id)[0]
    return p, enc, j


def test_encounter_starts_its_workflow(h):
    p, enc, j = start(h)
    assert j.association_type == "encounter" and j.association_id == enc.id and j.state == "arrived"


def test_gates_block_case_sheet_until_the_stage_allows_it(h):
    p, enc, j = start(h)
    rec = h.create(NURSE, "clinical_record", {"patient_id": p.id, "encounter_id": enc.id})
    page = h.create(DR, "note_page", {"patient_id": p.id, "record_id": rec.id})
    with pytest.raises(InvariantViolation, match="Arrived"):
        h.update(DR, "note_page", page.id, {"blocks": {"note": "x"}})
    h.move_journey(NURSE, j.id, "with_doctor")
    h.update(DR, "note_page", page.id, {"blocks": {"note": "Cough 3 days"}})  # with_doctor has no gates


def test_requirements_block_leaving_and_finalizing_advances_automatically(h):
    p, enc, j = start(h)
    h.move_journey(NURSE, j.id, "with_doctor")
    with pytest.raises(InvariantViolation, match="Case sheet finalized"):
        h.move_journey(NURSE, j.id, "review")
    rec = h.create(NURSE, "clinical_record", {"patient_id": p.id, "encounter_id": enc.id})
    page = h.create(DR, "note_page", {"patient_id": p.id, "record_id": rec.id, "blocks": {"note": "ok"}})
    h.transition(DR, "note_page", page.id, "finalized")
    assert h.get(NURSE, "journey", j.id).state == "review"  # advance_on
    done = h.move_journey(NURSE, j.id, "done")
    assert done.status == "completed"
    assert any(t.title == "Book the follow-up" for t in h.list(NURSE, "task", p.id))  # on_exit of review


def test_force_needs_staff_and_a_reason_and_is_recorded(h):
    p, enc, j = start(h)
    with pytest.raises(InvariantViolation):
        h.move_journey(NURSE, j.id, "done")
    with pytest.raises(PermissionDenied):
        h.move_journey(NURSE, j.id, "done", force=True)
    with pytest.raises(PermissionDenied):
        h.move_journey(AGENT, j.id, "done", "skip", force=True)
    moved = h.move_journey(NURSE, j.id, "review", "Seen at the camp", force=True)
    assert moved.history[-1].forced and moved.state == "review"


def test_close_encounter_gate(h):
    p, enc, j = start(h)
    with pytest.raises(InvariantViolation):
        h.transition(NURSE, "encounter", enc.id, "closed")  # arrived allows nothing
    h.move_journey(NURSE, j.id, "with_doctor")
    h.transition(NURSE, "encounter", enc.id, "closed")  # no gates at with_doctor
