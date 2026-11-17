"""
The three levels of context an agent works at. The level decides the agent's type, which tools and skills it can be
given, and what it may never do. Levels are an API-layer idea: the core harness only sees an ``agent`` actor.

* **personal** - one per team member, generalised. Knows its owner's own work (tasks, waiting decisions) and the
  site's library. It never opens a patient's record.
* **team** - one per care team (a care team definition, e.g. the OHS care team) or care setting. Instance specific
  but not patient specific: queues, workload, schedules, handovers. It may run on its own (a scheduled skill), but
  only with read tools and never on one patient's record.
* **patient** - works on one patient's record, opened by a person from that patient's screen. Every change it wants
  is a proposal a clinician accepts. It never runs on its own.
"""
from __future__ import annotations

from typing import Dict, List

__all__ = ["LEVELS", "LEVEL_IDS", "level_of_scope", "tool_scope", "tool_levels", "tool_kind", "public_levels"]

LEVEL_IDS = ("personal", "team", "patient")

LEVELS: Dict[str, dict] = {
    "personal": {
        "name": "Personal",
        "context": "Generalised: the team member and their own work",
        "instances": "One per team member, created on first use",
        "tool_scopes": ["general"],
        "autonomous": False,
        "can": ["Plan the member's day from their own tasks and waiting decisions",
                "Search the library: templates, care programs, definitions, evidence",
                "Draft messages, handovers and notes for the member to send",
                "Open screens for the member"],
        "cannot": ["Open any patient's record",
                   "Propose or make any change to the record",
                   "Run on its own"],
    },
    "team": {
        "name": "Care team",
        "context": "Instance specific, not patient specific: one care team or care setting",
        "instances": "One Care Coordination Agent per care team, added automatically",
        "tool_scopes": ["setting", "general"],
        "autonomous": True,
        "can": ["See the team's queues: tasks, waiting proposals and approvals, visits on the workflow board",
                "Balance workload, list what is due or overdue, write handover notes",
                "Run scheduled skills on its own (read only), for example the morning due list"],
        "cannot": ["Open one patient's clinical record",
                   "Propose or make patient-specific changes",
                   "Write anything when running on its own"],
    },
    "patient": {
        "name": "Patient",
        "context": "One patient's record, opened by a person from that patient's screen",
        "instances": "Shared agent types (clinical copilot, case-sheet assistant); the patient comes from the screen",
        "tool_scopes": ["patient", "encounter"],
        "autonomous": False,
        "can": ["Read the patient's record (with the patient's AI consent)",
                "Summarise, flag risks, prepare the visit, draft the case sheet",
                "Propose changes, which a clinician accepts or rejects"],
        "cannot": ["Act on its own: it only answers the person who opened it",
                   "Make any change directly: every write is a proposal",
                   "Sign, finalize or decide"],
    },
}

_SCOPE_TO_LEVEL = {"general": "personal", "setting": "team", "patient": "patient", "encounter": "patient"}


def level_of_scope(scope: str) -> str:
    """Older agent configs carried a scope; map it to a level."""
    return _SCOPE_TO_LEVEL.get(scope, scope if scope in LEVEL_IDS else "personal")


def tool_scope(level: str, encounter: bool = False) -> str:
    """The Clinical API tool context an agent at this level calls with."""
    return {"personal": "general", "team": "setting", "patient": "encounter" if encounter else "patient"}[level]


def tool_levels(scopes) -> List[str]:
    """Default levels a tool may be given to, from the contexts it exists in."""
    out = []
    for lv in LEVEL_IDS:
        if any(s in LEVELS[lv]["tool_scopes"] for s in scopes):
            out.append(lv)
    return out


def tool_kind(name: str, op: str | None) -> str:
    """read | propose | write | navigate | render, for administration and for autonomous runs (read only)."""
    if name.startswith("ui_open"):
        return "navigate"
    if name == "render_blocks":
        return "render"
    if op in ("list", "get") or op is None:
        return "read"
    if op == "propose":
        return "propose"
    return "write"


def public_levels() -> List[dict]:
    return [{"id": k, **{f: v for f, v in LEVELS[k].items() if f != "tool_scopes"}} for k in LEVEL_IDS]
