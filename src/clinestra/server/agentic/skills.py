"""
Skills: named, reusable abilities an agent can be given. A skill is instructions plus the tools it needs, for one
level. Giving an agent a skill gives it those tools (still filtered by the level and by tool administration), and a
person can start a skill directly from the agent panel.

Built-in skills are seeded per site; administrators edit them or add their own (``/v1/admin/skills``).
"""
from __future__ import annotations

import time
from typing import List

from .levels import LEVEL_IDS
from .state import SiteState

__all__ = ["DEFAULT_SKILLS", "all_skills", "get", "save", "delete", "ensure_defaults"]

_S = lambda id, level, name, description, instructions, tools, starter, schedule=None: {  # noqa: E731
    "id": id, "level": level, "name": name, "description": description, "instructions": instructions, "tools": tools,
    "starter": starter, "schedule": schedule, "enabled": True, "built_in": True}

DEFAULT_SKILLS = [
    # personal
    _S("plan_my_day", "personal", "Plan my day", "Orders the member's own tasks and waiting decisions into a plan for today.",
       "Call my_work first. Group what you find into: do first (urgent or overdue), today, and can wait. One line each, no patient details beyond the name on the task.",
       ["my_work"], "Plan my day"),
    _S("library_search", "personal", "Find in the library", "Finds templates, care programs, workflows and other definitions.",
       "Search definitions with definition_list (filter by def_kind when the person names one) and explain what each does in a sentence.",
       ["definition_list", "definition_get", "definition_schema"], "Which case-sheet templates do we have?"),
    _S("evidence_lookup", "personal", "Look up evidence", "Answers a general clinical question from the evidence sources, with citations.",
       "Use fetch_evidence. Answer in general terms with the sources; never apply it to a named patient.",
       ["fetch_evidence"], "What is the evidence for SGLT2 inhibitors in heart failure?"),
    _S("draft_message", "personal", "Draft a message", "Drafts a message, handover or memo for the member to send themselves.",
       "Write the draft only. The member sends it; you never send anything.", [], "Draft a note to the team about tomorrow's camp"),
    # team
    _S("due_list", "team", "Due and overdue list", "What is due and overdue for the team today, by stage and owner.",
       "Call team_board and task_list. List overdue items first, then due today, grouped by owner. Name patients only as they appear on the task.",
       ["team_board", "task_list", "routine_list"], "What is due and overdue today?", schedule="Every morning at 08:00"),
    _S("handover_note", "team", "Handover note", "A shift handover: open visits by stage, waiting decisions, unfinished tasks.",
       "Call team_board, task_list and proposal_list. Write a handover under three headings: visits in progress, waiting for a decision, unfinished tasks.",
       ["team_board", "task_list", "proposal_list"], "Write the handover for the evening shift", schedule="Every day at 17:30"),
    _S("workload_balance", "team", "Balance workload", "Shows who holds how much open work and suggests a fairer split.",
       "Call task_list and care_team_member_list. Count open tasks per owner and suggest moves as a list for the coordinator to make.",
       ["task_list", "care_team_member_list", "provider_list"], "Is the workload balanced?"),
    _S("waiting_decisions", "team", "Waiting decisions", "Proposals and approvals waiting for someone in the team, oldest first.",
       "Call proposal_list. List what is waiting, oldest first, and who can decide each one.", ["proposal_list"], "What is waiting for a decision?"),
    # patient
    _S("pre_visit_brief", "patient", "Pre-visit brief", "Why the patient is here, what changed since last time, and what is due.",
       "Call summarize_patient, pending_actions and encounter_list. Write: reason for visit, changes since the last visit, due or overdue items, questions to ask. Under 200 words.",
       ["summarize_patient", "pending_actions", "encounter_list", "fetch_clinical_data"], "Prepare the pre-visit brief"),
    _S("red_flags", "patient", "Red flags", "Safety concerns in the record that need attention now.",
       "Call summarize_patient and fetch_clinical_data. List only concerns backed by a record item, most urgent first, each with the item it comes from.",
       ["summarize_patient", "fetch_clinical_data", "clinical_reasoning"], "Any red flags?"),
    _S("medication_review", "patient", "Medication review", "Current medications against conditions, results and allergies.",
       "Call medication_list, condition_list and fetch_clinical_data. Point out gaps, interactions and monitoring due. Changes are proposals only.",
       ["medication_list", "condition_list", "fetch_clinical_data", "medication_propose"], "Review her medications"),
    _S("draft_case_sheet", "patient", "Draft the case sheet", "Pre-fills case-sheet fields from the record and notes, as proposals.",
       "Read the case sheet with note_page_list and note_page_get. Use extract_observations on notes. Every value is a proposal the clinician confirms.",
       ["note_page_list", "note_page_get", "extract_observations", "fetch_clinical_data"], "Draft today's case sheet"),
    _S("next_steps", "patient", "Suggest next steps", "Next steps from pending items, as proposals.",
       "Call pending_actions and clinical_reasoning. Propose at most three actions with the matching _propose tool and a rationale.",
       ["pending_actions", "clinical_reasoning", "*_propose"], "What should we do next?"),
]


def ensure_defaults(st: SiteState) -> None:
    have = {d["id"] for d in st.all("_ai.skill")}
    for s in DEFAULT_SKILLS:
        if s["id"] not in have:
            st.put("_ai.skill", s["id"], {"config": s, "created_by": "system", "updated_at": time.time()})


def all_skills(st: SiteState) -> List[dict]:
    ensure_defaults(st)
    order = {lv: i for i, lv in enumerate(LEVEL_IDS)}
    out = [{**d["config"], "updated_at": d["updated_at"]} for d in st.records("_ai.skill")]
    return sorted(out, key=lambda s: (order.get(s["level"], 9), s["name"]))


def get(st: SiteState, skill_id: str) -> dict:
    ensure_defaults(st)
    d = st.get("_ai.skill", skill_id)
    if not d or d.get("_deleted"):
        raise LookupError(f"No skill '{skill_id}'")
    return d["config"]


def save(st: SiteState, cfg: dict, actor: str) -> dict:
    if cfg.get("level") not in LEVEL_IDS:
        raise ValueError(f"Skill level must be one of {LEVEL_IDS}")
    if cfg.get("schedule") and cfg["level"] != "team":
        raise ValueError("Only care-team skills can run on a schedule: personal and patient agents never act on their own")
    st.put("_ai.skill", cfg["id"], {"config": cfg, "created_by": actor, "updated_at": time.time()})
    return cfg


def delete(st: SiteState, skill_id: str) -> None:
    st.delete("_ai.skill", skill_id)
