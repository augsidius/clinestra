"""Run: python examples/quickstart.py"""
from clinestra import Actor, Harness

h = Harness()
dr = Actor(id="dr_rao", site="clinic_1", role="clinician")
nurse = Actor(id="nurse_meena", site="clinic_1", role="nurse")

p = h.create(dr, "patient", {"name": "Asha K", "country": "IN", "gender": "female", "age_years": 34})
h.create(nurse, "encounter", {"patient_id": p.id, "definition_key": "opd_visit"})
page = h.list(dr, "note_page", p.id)[0]
h.update(dr, "note_page", page.id, {"blocks": {"chief_complaint": "Headache", "bp_systolic": 150,
                                               "bp_diastolic": 96, "assessment_plan": "Hypertension, start plan"}})
h.transition(dr, "note_page", page.id, "finalized")
h.create(dr, "condition", {"patient_id": p.id, "code": "htn"})
plan = h.create(dr, "careplan", {"patient_id": p.id, "definition_key": "hypertension_basic"})
h.transition(dr, "careplan", plan.id, "active")

print("Active care actions:", [a.title for a in h.list(dr, "care_action", p.id, status="active")])
print("Observations:", [(o.code, o.value) for o in h.list(dr, "observation", p.id)])
print("Pending:", [a["title"] for a in h.pending_actions(dr, p.id)])
print("Timeline valid:", h.verify_timeline(dr, p.id)["valid"])
