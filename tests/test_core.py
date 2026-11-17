from conftest import TEST_PACKS
from datetime import timedelta

import pytest

from clinestra import Actor, Harness, InvariantViolation, Invalid, NotFound, PermissionDenied, SQLiteStore
from clinestra.base import utcnow
from clinestra.spec import SPECS

S = "site_a"
DR = Actor(id="dr_a", site=S, role="clinician")
DR2 = Actor(id="dr_b", site=S, role="clinician")
NURSE = Actor(id="nurse_a", site=S, role="nurse")
ADMIN = Actor(id="admin", site=S, role="admin")
AGENT = Actor(id="bot", site=S, role="clinician", actor_type="agent")
LEAD = Actor(id="lead_a", site=S, role="clinical_lead")


def _cited(config):
    return {**config, "evidence": [{"id": "fx-1", "title": "Fixture source", "source_type": "local_sop"}]}


def _approve_and_publish(h, definition_id):
    h.review_definition(LEAD, definition_id, "approve", review_ref="fx")
    return h.transition(ADMIN, "definition", definition_id, "published")


@pytest.fixture
def h():
    return Harness(packs=TEST_PACKS)


def patient(h, **kw):
    data = {"name": "Asha", "country": "IN", "gender": "female", "age_years": 34, **kw}
    return h.create(DR, "patient", data)


def pt_actor(p):
    return Actor(id=p.id, site=S, role="patient", patient_id=p.id)


def grant(h, pid, scope):
    c = h.create(DR, "consent", {"patient_id": pid, "consent_scope": scope})
    return h.transition(DR, "consent", c.id, "granted")


# ── registry ──
def test_every_concept_has_states_and_generated_bodies():
    assert len(SPECS) == 31
    for s in SPECS.values():
        assert s.initial in s.states
        for frm, tos in s.transitions.items():
            assert frm in s.states and set(tos) <= set(s.states), s.kind
        assert "id" not in s.create_model.model_fields and "status" not in s.create_model.model_fields


# ── patients ──
def test_patient_needs_country_gender_and_age_or_dob(h):
    with pytest.raises(Invalid):
        h.create(DR, "patient", {"name": "X", "country": "IN", "gender": "male"})
    with pytest.raises(Invalid):
        h.create(DR, "patient", {"name": "X", "gender": "male", "age_years": 3})
    p = patient(h)
    assert p.age == 34 and p.patient_id == p.id
    assert [x.name for x in h.list(DR, "health_profile", p.id)] == ["General"]


def test_standard_demographics_are_validated(h):
    with pytest.raises(Invalid):
        patient(h, demographics={"diet": "carnivore"})
    with pytest.raises(Invalid):
        patient(h, demographics={"favourite_colour": "blue"})
    assert patient(h, demographics={"diet": "vegetarian"}).demographics["diet"] == "vegetarian"


def test_site_patient_fields_definition_enforces_custom_fields(h):
    d = h.create(ADMIN, "definition", {"def_kind": "patient_fields", "key": "default", "name": "Fields", "config": {
        "custom": [{"key": "caste_category", "field_type": "choice", "options": ["gen", "obc", "sc", "st"], "required": True}]}})
    h.transition(ADMIN, "definition", d.id, "published")
    with pytest.raises(Invalid):
        patient(h)
    assert patient(h, custom_fields={"caste_category": "obc"}).custom_fields == {"caste_category": "obc"}


def test_family_member_is_a_patient_flagged_by_relationship_and_can_act_as_guardian(h):
    child = patient(h, name="Child", age_years=8)
    mum = patient(h, name="Mum")
    h.create(DR, "relationship", {"patient_id": child.id, "related_patient_id": mum.id, "relation": "mother",
                                  "is_guardian": True, "consent_scopes": ["treatment", "ai"]})
    assert h.get(DR, "patient", mum.id).is_guardian
    mum_actor = Actor(id=mum.id, site=S, role="patient", patient_id=mum.id, represents=[child.id])
    c = h.create(mum_actor, "consent", {"patient_id": child.id, "consent_scope": "treatment"})
    assert c.granted_by == "guardian" and c.guardian_id == mum.id
    h.transition(mum_actor, "consent", c.id, "granted")
    with pytest.raises(PermissionDenied):  # scope not delegated
        h.create(mum_actor, "consent", {"patient_id": child.id, "consent_scope": "research"})
    stranger = Actor(id="x", site=S, role="patient", patient_id=mum.id, represents=[patient(h).id])
    assert stranger.represents[0] not in h._patient_ids(stranger)


# ── care team ──
def test_care_team_primary_role_slot(h):
    p = patient(h)
    prv = h.create(ADMIN, "provider", {"name": "Dr A"})
    prv2 = h.create(ADMIN, "provider", {"name": "Dr B"})
    m = h.create(ADMIN, "care_team_member", {"name": "N", "roles": ["nurse"]})
    team = h.create(DR, "care_team", {"patient_id": p.id, "definition_key": "standard", "members": [
        {"person_id": prv.id, "person_kind": "provider", "role": "primary"},
        {"person_id": m.id, "person_kind": "care_team_member", "role": "nurse"}]})
    with pytest.raises(InvariantViolation):
        h.update(DR, "care_team", team.id, {"members": [
            {"person_id": prv.id, "person_kind": "provider", "role": "primary"},
            {"person_id": prv2.id, "person_kind": "provider", "role": "primary"}]})
    with pytest.raises(Invalid):
        h.update(DR, "care_team", team.id, {"members": [{"person_id": m.id, "person_kind": "care_team_member", "role": "primary"}]})
    with pytest.raises(Invalid):
        h.update(DR, "care_team", team.id, {"members": [{"person_id": m.id, "person_kind": "care_team_member", "role": "pilot"}]})
    enc_override = h.update(DR, "care_team", team.id, {"members": [
        {"person_id": prv.id, "person_kind": "provider", "role": "primary"},
        {"person_id": prv2.id, "person_kind": "provider", "role": "primary", "encounter_id": "enc_1"}]})
    assert len(enc_override.members) == 2


# ── clinical decisions ──
def test_clinical_concepts_need_a_clinician(h):
    p = patient(h)
    with pytest.raises(PermissionDenied):
        h.create(NURSE, "condition", {"patient_id": p.id, "code": "htn"})
    h.create(DR, "condition", {"patient_id": p.id, "code": "htn"})
    with pytest.raises(PermissionDenied):
        h.create(NURSE, "care_action", {"patient_id": p.id, "action_type": "order", "title": "CBC"})
    assert h.create(NURSE, "care_action", {"patient_id": p.id, "action_type": "education", "title": "Leaflet"}).status == "planned"


def test_one_current_clinical_state_per_condition_and_general(h):
    p = patient(h)
    a = h.create(DR, "clinical_state", {"patient_id": p.id, "scope": "condition", "condition_code": "mdd", "state": "moderate"})
    h.create(DR, "clinical_state", {"patient_id": p.id, "scope": "condition", "condition_code": "htn", "state": "stage1"})
    h.create(DR, "clinical_state", {"patient_id": p.id, "scope": "general", "state": "pregnant"})
    b = h.create(DR, "clinical_state", {"patient_id": p.id, "scope": "condition", "condition_code": "mdd", "state": "severe"})
    current = {(s.condition_code, s.state) for s in h.current_states(DR, p.id)}
    assert current == {("mdd", "severe"), ("htn", "stage1"), (None, "pregnant")}
    assert h.get(DR, "clinical_state", a.id).status == "superseded"
    with pytest.raises(InvariantViolation):
        h.update(DR, "clinical_state", b.id, {"state": "mild"})
    with pytest.raises(Invalid):
        h.create(DR, "clinical_state", {"patient_id": p.id, "scope": "condition", "state": "x"})


def test_transitions_follow_the_state_machine(h):
    p = patient(h)
    plan = h.create(NURSE, "careplan", {"patient_id": p.id, "title": "Plan"})
    with pytest.raises(InvariantViolation):
        h.transition(DR, "careplan", plan.id, "completed")
    with pytest.raises(PermissionDenied):
        h.transition(NURSE, "careplan", plan.id, "active")
    h.transition(DR, "careplan", plan.id, "active")
    h.transition(DR, "careplan", plan.id, "completed")
    with pytest.raises(InvariantViolation):
        h.update(DR, "careplan", plan.id, {"title": "Edited"})


def test_soft_delete_rules(h):
    p = patient(h)
    t = h.create(NURSE, "task", {"patient_id": p.id, "title": "Call"})
    with pytest.raises(Invalid):
        h.delete(NURSE, "task", t.id, "")
    h.delete(NURSE, "task", t.id, "Duplicate")
    with pytest.raises(NotFound):
        h.get(NURSE, "task", t.id)
    assert h.list(NURSE, "task", p.id, include_deleted=True)[0].deleted
    o = h.create(NURSE, "observation", {"patient_id": p.id, "code": "pulse", "value": 80})
    with pytest.raises(InvariantViolation):
        h.delete(NURSE, "observation", o.id, "Wrong patient")
    assert h.transition(NURSE, "observation", o.id, "entered_in_error").status == "entered_in_error"


# ── definitions ──
def test_definitions_are_versioned_and_frozen_when_published(h):
    d1 = h.create(ADMIN, "definition", {"def_kind": "task", "key": "call", "name": "Call", "config": {"defaults": {"title": "Call v1"}}})
    with pytest.raises(Invalid):
        h.create(ADMIN, "definition", {"def_kind": "journey", "key": "bad", "name": "Bad", "config": {}})
    h.transition(ADMIN, "definition", d1.id, "published")
    with pytest.raises(InvariantViolation):
        h.update(ADMIN, "definition", d1.id, {"name": "x"})
    d2 = h.create(ADMIN, "definition", {"def_kind": "task", "key": "call", "name": "Call", "config": {"defaults": {"title": "Call v2"}}})
    assert d2.version == 2
    p = patient(h)
    assert h.create(NURSE, "task", {"patient_id": p.id, "definition_key": "call"}).title == "Call v1"
    h.transition(ADMIN, "definition", d2.id, "published")
    t = h.create(NURSE, "task", {"patient_id": p.id, "definition_key": "call"})
    assert t.title == "Call v2" and t.definition_version == 2
    lib = h.resolve_definition(S, "assessment", "phq9")
    with pytest.raises(InvariantViolation):
        h.update(ADMIN, "definition", lib["id"], {"name": "hacked"})


# ── reactions, proposals ──
def test_cohort_entry_runs_actions_tiered(h):
    p = patient(h)
    with pytest.raises(Invalid):  # rule: needs an mdd condition
        h.create(DR, "cohort_membership", {"patient_id": p.id, "cohort_key": "cohort_mdd", "reason": "Suspected"})
    m = h.create(DR, "cohort_membership", {"patient_id": p.id, "cohort_key": "cohort_mdd", "reason": "Clinical judgement",
                                           "overridden": True})
    assert "Depression" in [x.name for x in h.list(DR, "health_profile", p.id)]  # non-clinical: applied
    props = h.list(DR, "proposal", p.id, status="pending")
    assert [x.target_kind for x in props] == ["protocol_assignment"]                     # clinical: proposed
    assert props[0].data["cohort_membership_id"] == m.id
    with pytest.raises(InvariantViolation):
        h.create(DR, "cohort_membership", {"patient_id": p.id, "cohort_key": "cohort_mdd", "reason": "again", "overridden": True})


def _publish_cohort_version(h, label):
    d = h.create(ADMIN, "definition", {"def_kind": "cohort", "key": "pinned", "name": "Pinned", "config": _cited({
        "exit_actions": [{"op": "create", "kind": "task", "data": {"title": f"Exit {label}"}}]})})
    _approve_and_publish(h, d.id)
    return d


def test_cohort_exit_uses_the_version_the_patient_enrolled_under(h):
    v1 = _publish_cohort_version(h, "v1")
    p = patient(h)
    m = h.create(DR, "cohort_membership", {"patient_id": p.id, "cohort_key": "pinned", "reason": "Enrol"})
    assert (m.definition_id, m.definition_version) == (v1.id, 1)
    _publish_cohort_version(h, "v2")
    h.transition(ADMIN, "definition", v1.id, "retired")  # a retired version must still govern its enrolled patients
    h.transition(DR, "cohort_membership", m.id, "closed")
    assert [t.title for t in h.list(DR, "task", p.id)] == ["Exit v1"]


def test_protocol_instantiates_templates_and_proposals_need_a_clinician(h):
    p = patient(h)
    h.create(DR, "condition", {"patient_id": p.id, "code": "mdd"})
    h.create(DR, "protocol_assignment", {"patient_id": p.id, "protocol_key": "proto_mdd_acute_phase"})
    plan = h.list(DR, "careplan", p.id)[0]
    assert plan.status == "draft"
    assert {a.definition_key for a in h.list(DR, "assessment", p.id)} == {"phq9", "gad7"}
    assert h.list(DR, "journey", p.id)[0].state == "intake"
    nonclinical = h.list(DR, "care_action", p.id)
    assert {c.action_type for c in nonclinical} == {"journal", "education", "self_care"}
    assert all(c.careplan_id == plan.id for c in nonclinical)
    pending = h.list(DR, "proposal", p.id, status="pending")
    assert sorted(x.target_kind for x in pending) == ["care_action", "care_action", "outcome", "regimen"]
    with pytest.raises(PermissionDenied):
        h.accept_proposal(NURSE, pending[0].id)
    reg = next(x for x in pending if x.target_kind == "regimen")
    h.accept_proposal(DR, reg.id)
    created = h.list(DR, "regimen", p.id)[0]
    assert created.status == "proposed"
    assert h.list(DR, "medication", p.id) == []
    h.reject_proposal(DR, pending[0].id, "Not now")
    with pytest.raises(InvariantViolation):
        h.accept_proposal(DR, pending[0].id)


def test_configured_reaction_and_safety_task(h):
    p = patient(h)
    a = h.create(DR, "assessment", {"patient_id": p.id, "definition_key": "phq9", "answers": {f"item_{i}": 3 for i in range(1, 10)}})
    assert a.score == 27 and a.safety_flag
    h.transition(DR, "assessment", a.id, "finalized")
    obs = h.list(DR, "observation", p.id, code="phq9_score")
    assert obs[0].value == 27 and obs[0].source_id == a.id
    titles = [t.title for t in h.list(DR, "task", p.id)]
    assert any("safety" in t for t in titles) and any("Severe PHQ-9" in t for t in titles)
    state_props = [x for x in h.list(DR, "proposal", p.id, status="pending") if x.target_kind == "clinical_state"]
    assert state_props[0].data["state"] == "severe" and state_props[0].data["basis"] == [a.id]
    assert state_props[0].origin == "reaction:severe_phq9_flag"


def test_reaction_depth_is_bounded(h):
    d = h.create(ADMIN, "definition", {"def_kind": "reaction", "key": "loop", "name": "Loop", "config": _cited({
        "when_kind": "task", "when_state": "open", "effects": [{"op": "create", "kind": "task", "data": {"title": "again"}}]})})
    _approve_and_publish(h, d.id)
    p = patient(h)
    h.create(NURSE, "task", {"patient_id": p.id, "title": "start"})
    assert len(h.list(NURSE, "task", p.id)) == 1 + h.max_reaction_depth


def test_outcome_achieved_proposes_state(h):
    p = patient(h)
    o = h.create(DR, "outcome", {"patient_id": p.id, "name": "Remission", "condition_code": "mdd",
                                 "on_achieved_state": "remission"})
    h.transition(DR, "outcome", o.id, "achieved")
    prop = next(x for x in h.list(DR, "proposal", p.id) if x.target_kind == "clinical_state")
    h.accept_proposal(DR, prop.id)
    assert h.current_states(DR, p.id)[0].state == "remission"
    assert any(t.title.startswith("Outcome achieved") for t in h.list(DR, "task", p.id))


def test_suggest_cohorts_from_rules(h):
    d = h.create(ADMIN, "definition", {"def_kind": "cohort", "key": "elderly_htn", "name": "Elderly HTN", "config": _cited({
        "rule": "age >= 60 and cond_htn and obs_bp_systolic >= 140", "manual_only": False})})
    _approve_and_publish(h, d.id)
    p = patient(h, age_years=70)
    h.create(DR, "condition", {"patient_id": p.id, "code": "htn"})
    h.create(NURSE, "observation", {"patient_id": p.id, "code": "bp_systolic", "value": 150})
    props = h.suggest_cohorts(DR, p.id)
    assert [x.data["cohort_key"] for x in props] == ["elderly_htn"]
    assert h.suggest_cohorts(DR, p.id) == []      # already proposed
    young = patient(h, age_years=30)
    assert h.suggest_cohorts(DR, young.id) == []


# ── approvals ──
def test_note_page_approval_gate_and_finalize(h):
    p = patient(h)
    enc = h.create(NURSE, "encounter", {"patient_id": p.id, "definition_key": "opd_visit"})
    rec = h.list(DR, "clinical_record", p.id)[0]
    page = h.list(DR, "note_page", p.id)[0]
    assert rec.encounter_id == enc.id and page.title == "General case sheet"
    with pytest.raises(Invalid):
        h.update(DR, "note_page", page.id, {"blocks": {"unknown": 1}})
    h.update(DR, "note_page", page.id, {"blocks": {"chief_complaint": "Cough", "pulse": 90}})
    with pytest.raises(Invalid):   # assessment_plan required
        h.transition(DR, "note_page", page.id, "finalized")
    h.update(DR, "note_page", page.id, {"blocks": {"chief_complaint": "Cough", "pulse": 90, "assessment_plan": "URTI"}})
    apr = h.transition(NURSE, "note_page", page.id, "approved", "Countersign please")
    assert apr.status == "pending" and h.get(DR, "note_page", page.id).status == "drafted"
    with pytest.raises(PermissionDenied):
        h.decide_approval(NURSE, apr.id, True)
    h.decide_approval(DR, apr.id, True)
    with pytest.raises(InvariantViolation):
        h.transition(DR, "clinical_record", rec.id, "finalized")
    h.transition(DR, "note_page", page.id, "finalized")
    assert [o.code for o in h.list(DR, "observation", p.id)] == ["pulse"]
    with pytest.raises(InvariantViolation):
        h.update(DR, "note_page", page.id, {"blocks": {"pulse": 1}})
    assert len(h.add_addendum(DR, page.id, "Also mild fever").addenda) == 1
    h.transition(DR, "clinical_record", rec.id, "finalized")


def test_nobody_approves_their_own_request(h):
    p = patient(h)
    rec = h.create(DR, "clinical_record", {"patient_id": p.id})
    assert rec.encounter_id  # auto-created encounter
    page = h.create(DR, "note_page", {"patient_id": p.id, "record_id": rec.id})
    apr = h.transition(DR, "note_page", page.id, "approved")
    with pytest.raises(PermissionDenied):
        h.decide_approval(DR, apr.id, True)
    h.decide_approval(DR2, apr.id, False, "Incomplete")
    assert h.get(DR, "note_page", page.id).status == "drafted"


# ── agents and consent ──
def test_agents_need_ai_consent_and_never_decide(h):
    p = patient(h)
    with pytest.raises(PermissionDenied):
        h.list(AGENT, "observation", p.id)
    grant(h, p.id, "ai")
    assert h.list(AGENT, "observation", p.id) == []
    with pytest.raises(PermissionDenied):
        h.create(AGENT, "condition", {"patient_id": p.id, "code": "mdd"})
    with pytest.raises(PermissionDenied):
        h.create(AGENT, "task", {"patient_id": p.id, "title": "x"})
    prop = h.propose(AGENT, "create", "condition", {"patient_id": p.id, "code": "mdd"}, rationale="Mentioned in notes")
    assert prop.origin == "agent:bot"
    with pytest.raises(PermissionDenied):
        h.accept_proposal(AGENT, prop.id)
    h.accept_proposal(DR, prop.id)
    rec = h.create(DR, "clinical_record", {"patient_id": p.id})
    page = h.create(AGENT, "note_page", {"patient_id": p.id, "record_id": rec.id, "blocks": {"s": "draft"}})
    assert page.ai_drafted
    h.update(AGENT, "note_page", page.id, {"blocks": {"s": "draft 2"}})
    with pytest.raises(PermissionDenied):
        h.transition(AGENT, "note_page", page.id, "finalized")


def test_patient_sees_only_own_records_and_shared_documents(h):
    p, other = patient(h), patient(h, name="Other")
    me = pt_actor(p)
    assert [x.id for x in h.list(me, "patient")] == [p.id]
    with pytest.raises(PermissionDenied):
        h.get(me, "patient", other.id)
    with pytest.raises(PermissionDenied):
        h.create(me, "condition", {"patient_id": p.id, "code": "x"})
    with pytest.raises(PermissionDenied):
        h.create(me, "assessment", {"patient_id": p.id, "definition_key": "bmi"})  # not patient-reported
    doc = h.create(DR, "clinical_document", {"patient_id": p.id, "definition_key": "prescription"})
    with pytest.raises(PermissionDenied):
        h.get(me, "clinical_document", doc.id)
    h.transition(DR, "clinical_document", doc.id, "ready")
    assert h.get(me, "clinical_document", doc.id).signed
    with pytest.raises(PermissionDenied):
        h.share_document(DR, doc.id)    # no data_sharing consent
    grant(h, p.id, "data_sharing")
    assert "Prescription" in h.share_document(DR, doc.id)["html"]


def test_consent_expiry_and_withdrawal(h):
    p = patient(h)
    c = h.create(DR, "consent", {"patient_id": p.id, "consent_scope": "ai",
                                 "valid_until": (utcnow() + timedelta(days=1)).isoformat()})
    h.transition(DR, "consent", c.id, "granted")
    assert h._has_consent(S, p.id, "ai")
    out = h.tick(DR, utcnow() + timedelta(days=2))
    assert c.id in out["expired"] and not h._has_consent(S, p.id, "ai")


# ── journeys, journals, routines, documents ──
def test_journey_moves_by_definition(h):
    p = patient(h)
    with pytest.raises(Invalid):
        h.create(NURSE, "journey", {"patient_id": p.id})
    j = h.create(NURSE, "journey", {"patient_id": p.id, "definition_key": "mdd_care_program"})
    with pytest.raises(InvariantViolation):
        h.move_journey(NURSE, j.id, "outcome")
    h.move_journey(NURSE, j.id, "active_treatment")
    h.move_journey(NURSE, j.id, "monitoring")      # on_enter creates a task
    assert any("PHQ-9 review" in t.title for t in h.list(NURSE, "task", p.id))
    h.move_journey(NURSE, j.id, "outcome")
    done = h.move_journey(NURSE, j.id, "discharged")
    assert done.status == "completed" and [s.state for s in done.history][-1] == "discharged"


def test_journal_entry_makes_patient_observations_and_completes_care_action(h):
    p = patient(h)
    ca = h.create(NURSE, "care_action", {"patient_id": p.id, "action_type": "journal", "title": "Diary",
                                         "journal_plugin_key": "mood_diary", "schedule": {"every_days": 1}})
    me = pt_actor(p)
    with pytest.raises(Invalid):
        h.create(me, "journal_entry", {"patient_id": p.id, "plugin_key": "mood_diary", "data": {"weight": 3}})
    h.create(me, "journal_entry", {"patient_id": p.id, "plugin_key": "mood_diary", "care_action_id": ca.id,
                                   "data": {"mood": 6, "sleep": 5}})
    obs = h.list(DR, "observation", p.id)
    assert {(o.code, o.status, o.perceived_by) for o in obs} == {("mood_score", "preliminary", "patient"),
                                                                 ("sleep_hours", "preliminary", "patient")}
    assert len(h.get(DR, "care_action", ca.id).completions) == 1


def test_routines_generate_tasks_once_per_occurrence(h):
    r = h.create(NURSE, "routine", {"definition_key": "ward_vitals", "schedule": {"every_days": 1, "grace_days": 0,
                                    "anchor": (utcnow() - timedelta(hours=1)).isoformat()}})
    made = h.run_routines(NURSE)
    assert len(made) == 1 and made[0].routine_id == r.id and made[0].assignee_role == "nurse"
    assert h.run_routines(NURSE) == []
    assert h.run_routines(NURSE, duty_states=["off_shift"]) == []


def test_profile_view_shows_present_and_absent(h):
    p = patient(h)
    h.create(NURSE, "observation", {"patient_id": p.id, "code": "pulse", "value": 72, "unit": "/min"})
    hp = h.list(DR, "health_profile", p.id)[0]
    v = h.profile_view(DR, hp.id)
    m = {x["code"]: x for x in v["measurements"]}
    assert m["pulse"]["present"] and m["pulse"]["latest"] == 72 and not m["spo2"]["present"]
    surgery = h.create(NURSE, "health_profile", {"patient_id": p.id, "definition_key": "surgery"})
    assert surgery.profile_type == "episode" and surgery.expires_at
    assert surgery.id in h.tick(NURSE, utcnow() + timedelta(days=31))["expired"]



def test_sqlite_store_round_trip_and_timeline(tmp_path):
    h = Harness(store=SQLiteStore(str(tmp_path / "c.db")), packs=TEST_PACKS)
    p = h.create(DR, "patient", {"name": "A", "country": "IN", "gender": "male", "age_years": 50})
    h.create(NURSE, "observation", {"patient_id": p.id, "code": "pulse", "value": 70})
    h2 = Harness(store=SQLiteStore(str(tmp_path / "c.db")), packs=TEST_PACKS)
    assert h2.list(DR, "observation", p.id)[0].value == 70
    assert h2.verify_timeline(DR, p.id)["valid"]


def test_site_isolation(h):
    p = patient(h)
    other = Actor(id="dr_x", site="site_b", role="clinician")
    with pytest.raises(NotFound):
        h.get(other, "patient", p.id)
    assert h.list(other, "patient") == []
