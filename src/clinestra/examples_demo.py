"""``clinestra demo``: one patient through the whole structure, printed step by step."""
from __future__ import annotations

from datetime import timedelta

from .base import Actor, utcnow
from .harness import Harness


def run(h: Harness = None, quiet: bool = False) -> dict:
    h = h or Harness()
    site = "demo_clinic"
    admin = Actor(id="admin_1", site=site, role="admin")
    dr = Actor(id="dr_rao", site=site, role="clinician")
    dr2 = Actor(id="dr_iyer", site=site, role="clinician")
    nurse = Actor(id="nurse_meena", site=site, role="nurse")
    agent = Actor(id="copilot", site=site, role="clinician", actor_type="agent")

    def say(*a):
        if not quiet:
            print(*a)

    say("── People ──")
    prv = h.create(admin, "provider", {"name": "Dr Rao", "specialty": "general medicine", "user_id": "dr_rao"})
    mem = h.create(admin, "care_team_member", {"name": "Meena", "roles": ["nurse"], "skills": ["vitals"]})
    h.create(admin, "clinical_setting", {"definition_key": "clinic"})
    p = h.create(dr, "patient", {"name": "Asha K", "country": "IN", "gender": "female", "age_years": 34,
                                 "demographics": {"diet": "vegetarian", "occupation": "professional"}})
    mother = h.create(dr, "patient", {"name": "Lata K", "country": "IN", "gender": "female", "age_years": 61})
    h.create(dr, "relationship", {"patient_id": p.id, "related_patient_id": mother.id, "relation": "mother",
                                  "is_care_taker": True, "receives_communication": True})
    h.create(dr, "care_team", {"patient_id": p.id, "definition_key": "standard", "members": [
        {"person_id": prv.id, "person_kind": "provider", "role": "primary"},
        {"person_id": mem.id, "person_kind": "care_team_member", "role": "nurse"}]})
    say(f"Patient {p.name} ({p.age}y), care taker {mother.name}; profiles:",
        [x.name for x in h.list(dr, "health_profile", p.id)])

    say("── Consent ──")
    for scope in ("treatment", "ai", "data_sharing"):
        c = h.create(dr, "consent", {"patient_id": p.id, "consent_scope": scope})
        h.transition(dr, "consent", c.id, "granted", "Signed at registration")
    say("Granted: treatment, ai, data_sharing")

    say("── Encounter → clinical record → note page ──")
    enc = h.create(nurse, "encounter", {"patient_id": p.id, "definition_key": "opd_visit", "start_at": utcnow()})
    rec = h.list(dr, "clinical_record", p.id, encounter_id=enc.id)[0]
    page = h.list(dr, "note_page", p.id, record_id=rec.id)[0]
    h.update(dr, "note_page", page.id, {"blocks": {"chief_complaint": "Headaches, high BP at a pharmacy check",
                                                   "bp_systolic": 128, "bp_diastolic": 82, "weight": 58,
                                                   "assessment_plan": "Hypertension. Lifestyle plan and home BP log."}})
    h.transition(dr, "note_page", page.id, "finalized", "Signed")
    h.transition(dr, "clinical_record", rec.id, "finalized")
    say("Observations from the signed page:", sorted(o.code for o in h.list(dr, "observation", p.id)))

    say("── Condition and careplan ──")
    htn = h.create(dr, "condition", {"patient_id": p.id, "code": "htn", "display": "Hypertension"})
    plan = h.create(dr, "careplan", {"patient_id": p.id, "definition_key": "hypertension_basic"})
    h.transition(dr, "careplan", plan.id, "active", "Agreed with patient")
    actions = h.list(dr, "care_action", p.id, careplan_id=plan.id)
    say(f"Condition {htn.code}; careplan with {len(actions)} care actions:", [a.title for a in actions])

    say("── Proposals: an agent suggests, a clinician decides ──")
    prop = h.propose(agent, "create", "condition", {"patient_id": p.id, "code": "obesity", "display": "Obesity"},
                     rationale="Mentioned in the signed note")
    say("Agent proposed:", prop.op, prop.target_kind, "- status", prop.status)
    h.reject_proposal(dr, prop.id, "Not supported by the examination")
    say("Clinician rejected it; conditions:", [c.code for c in h.list(dr, "condition", p.id)])

    say("── Calculator ──")
    bmi = h.create(dr, "assessment", {"patient_id": p.id, "definition_key": "bmi", "answers": {"weight": 58, "height": 160}})
    say("BMI:", bmi.score)

    say("── Patient journal ──")
    log = next(c for c in actions if c.action_type == "journal")
    if log.status != "active":
        h.transition(dr, "care_action", log.id, "active", "Start")
    pt = Actor(id=p.id, site=site, role="patient", patient_id=p.id)
    h.create(pt, "journal_entry", {"patient_id": p.id, "plugin_key": "vitals_log", "care_action_id": log.id,
                                   "data": {"bp_systolic": 126, "bp_diastolic": 80}})
    say("Home BP logged; schedule:", h.schedule_status(dr, log.id)["counts"])

    say("── Approval gate: an AI-drafted page needs a second clinician ──")
    rec2 = h.create(dr, "clinical_record", {"patient_id": p.id, "title": "Follow-up"})
    draft = h.create(agent, "note_page", {"patient_id": p.id, "record_id": rec2.id, "title": "AI draft",
                                          "blocks": {"summary": "Home BP improving; continue the plan"}})
    apr = h.transition(dr, "note_page", draft.id, "approved", "Please countersign")
    h.decide_approval(dr2, apr.id, True, "Agreed")
    say("Draft by agent:", draft.ai_drafted, "→", h.get(dr, "note_page", draft.id).status)

    say("── AI harness tools (patient context) ──")
    ctx = {"scope": "patient", "patient_id": p.id}
    try:
        from .server.agentic.tools import registry  # the AI tools are API-layer: pip install "clinestra[full]"
    except ImportError:
        say('Skipped. Install them with: pip install "clinestra[full]"')
    else:
        tools = registry(h)
        say(f"{len(tools.list(ctx, agent))} tools for the agent in this context")
        ex = tools.call(agent, "extract_observations", {"text": "BP 142/88, weight 59 kg"}, ctx)
        say("Extracted → proposals:", [(e["code"], e["value"]) for e in ex["extracted"]])
        say(tools.call(agent, "summarize_patient", {}, ctx)["summary"])

    say("── Document ──")
    doc = h.create(dr, "clinical_document", {"patient_id": p.id, "definition_key": "prescription",
                                             "source_kind": "note_page", "source_id": page.id})
    h.transition(dr, "clinical_document", doc.id, "ready", "Signed")
    say("Shared:", h.share_document(dr, doc.id)["blob_ref"])
    say("Timeline valid:", h.verify_timeline(dr, p.id))
    return {"patient_id": p.id, "harness": h}


if __name__ == "__main__":
    run()
