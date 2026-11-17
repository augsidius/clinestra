"""
Tool administration: every tool an agent can be given, what kind it is, which levels may use it, and the site's own
settings for it (switched off, limited to some levels or roles).

Three sources of tools:
  - Clinical API tools generated from the concepts (``<kind>_list`` ... ``_propose``) - :mod:`.tools`
  - harness reasoning tools (``summarize_patient``, ``fetch_evidence`` ...) - :mod:`.tools`
  - API-layer tools defined here: ``my_work`` (personal), ``team_board`` (care team), ``ui_open`` and ``render_blocks``

The effective tool list for one agent turn is: tools in the level's contexts, minus tools switched off or not allowed
at that level or for the user's role, intersected with the agent's own tools and its skills' tools. An autonomous run
keeps only read tools.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ...base import Actor
from . import content_blocks as uic
from .levels import LEVEL_IDS, LEVELS, tool_kind, tool_levels
from .local import call, tool_concept_op, tool_layer
from .state import SiteState
from .tools import registry

__all__ = ["API_TOOLS", "catalog", "settings", "save_setting", "effective", "call_api_tool", "match", "is_api_tool"]

# What a harness tool does when an agent calls it (they have no concept op).
_HARNESS_KIND = {"extract_observations": "propose", "clinical_reasoning": "propose", "generate_definition": "propose"}

API_TOOLS: Dict[str, dict] = {
    "my_work": {"description": "The signed-in team member's own work: open tasks assigned to them or their role, proposals "
                               "and approvals waiting for them. Titles and due dates only.",
                "input_schema": {"type": "object", "properties": {}}, "levels": ["personal"], "kind": "read"},
    "team_board": {"description": "The care team's workflow board: visits per stage, time in stage and SLA breaches, plus "
                                  "overdue task counts. No clinical content.",
                   "input_schema": {"type": "object", "properties": {}}, "levels": ["team"], "kind": "read"},
    "ui_open": {"description": "Open a screen in the console for the user. Use after you have found what they asked for.",
                "input_schema": {"type": "object", "properties": {
                    "screen": {"type": "string", "enum": ["patient", "case_sheet", "care_programs", "care_plans", "care_teams", "tasks",
                                                          "inbox", "patients", "orders", "records", "workflows", "library"]},
                    "patient_id": {"type": "string"}}, "required": ["screen"]},
                "levels": list(LEVEL_IDS), "kind": "navigate"},
    "render_blocks": {"description": uic.TOOL["description"], "input_schema": uic.TOOL["input_schema"], "levels": ["team", "patient"], "kind": "render"},
}


def is_api_tool(name: str) -> bool:
    return name in API_TOOLS


def match(name: str, patterns: List[str]) -> bool:
    for p in patterns:
        if p == name or p == "*" or (p.startswith("*") and name.endswith(p[1:])) or (p.endswith("*") and name.startswith(p[:-1])):
            return True
    return False


def settings(st: SiteState) -> Dict[str, dict]:
    return st.kv_get("tool_settings") or {}


def save_setting(st: SiteState, name: str, cfg: dict) -> dict:
    s = settings(st)
    bad = [lv for lv in cfg.get("levels") or [] if lv not in LEVEL_IDS]
    if bad:
        raise ValueError(f"Unknown level(s): {bad}")
    s[name] = {k: cfg[k] for k in ("enabled", "levels", "roles", "note") if k in cfg}
    st.kv_set("tool_settings", s)
    return s[name]


def catalog(h, st: SiteState, agent_actor: Actor) -> List[dict]:
    """Every tool, once, with its default levels and the site's settings applied."""
    reg = registry(h)
    agent_ok = {t.name for t in reg.list({"scope": "patient"}, agent_actor)} | {t.name for t in reg.list({"scope": "setting"}, agent_actor)} \
        | {t.name for t in reg.list({"scope": "general"}, agent_actor)}
    sets = settings(st)
    out = []
    for t in reg.tools.values():
        concept, op = tool_concept_op(t.name)
        kind = _HARNESS_KIND.get(t.name) or tool_kind(t.name, op)
        default_levels = tool_levels(t.scopes)
        if t.name == "patient_get":  # a patient's record is never read at team level
            default_levels = [lv for lv in default_levels if lv != "team"]
        out.append(_row(t.name, t.description, tool_layer(t.name), concept, op, kind, default_levels, list(t.scopes),
                        t.name in agent_ok, sets.get(t.name)))
    for name, d in API_TOOLS.items():
        out.append(_row(name, d["description"], "ui" if name.startswith("ui_") else "api", None, None, d["kind"], d["levels"], [], True, sets.get(name)))
    return sorted(out, key=lambda r: (r["layer"], r["name"]))


def _row(name, description, layer, concept, op, kind, default_levels, scopes, agent_ok, setting) -> dict:
    s = setting or {}
    levels = [lv for lv in (s.get("levels") or default_levels) if lv in default_levels]
    return {"name": name, "description": description, "layer": layer, "concept": concept, "op": op, "kind": kind,
            "scopes": scopes, "default_levels": default_levels, "levels": levels, "roles": s.get("roles") or [],
            "enabled": s.get("enabled", True) and agent_ok, "agent_allowed": agent_ok, "note": s.get("note", "")}


def effective(h, st: SiteState, agent_actor: Actor, agent: dict, skills: List[dict], role: str, level: str,
              patient_id: Optional[str], autonomous: bool = False, disabled: Optional[set] = None) -> List[dict]:
    """The tool definitions this agent gets for one turn."""
    patterns = list(agent.get("tools") or [])
    for s in skills:
        patterns += s.get("tools") or []
    rows = {r["name"]: r for r in catalog(h, st, agent_actor)}
    scope = {"personal": "general", "team": "setting", "patient": "patient"}[level]
    defs = {d["name"]: d for d in call(h, agent_actor, "GET", "/tools", params={"scope": scope, "patient_id": patient_id})}
    out = []
    for name, r in rows.items():
        if not r["enabled"] or level not in r["levels"] or (r["roles"] and role not in r["roles"] and role != "admin"):
            continue
        if name in (disabled or set()) or not match(name, patterns):
            continue
        if autonomous and r["kind"] != "read":
            continue
        if name in API_TOOLS:
            d = API_TOOLS[name]
            out.append({"name": name, "description": d["description"], "input_schema": d["input_schema"]})
        elif name in defs:
            out.append(defs[name])
    return out


def call_api_tool(h, agent_actor: Actor, name: str, args: dict, me: dict, context: dict) -> Any:
    """Run a tool that lives in the API layer (not ui_*: the chat loop handles those)."""
    if name == "my_work":
        tasks = call(h, agent_actor, "GET", "/tasks", params={"status": "open"})
        mine = [t for t in tasks if t.get("assignee_id") == me["id"] or (not t.get("assignee_id") and t.get("assignee_role") == me["role"])]
        proposals = call(h, agent_actor, "GET", "/proposals", params={"status": "pending"})
        return {"open_tasks": [{"title": t.get("title"), "priority": t.get("priority"), "due": t.get("due_at")} for t in mine][:40],
                "proposals_waiting": len(proposals) if me["role"] == "clinician" else 0,
                "note": "Only the member's own items. Ask the patient agent from a patient's screen for clinical detail."}
    if name == "team_board":
        from datetime import datetime, timezone
        js = [j for j in call(h, agent_actor, "GET", "/journeys") if j.get("status") not in ("completed", "cancelled")]
        now = datetime.now(timezone.utc)
        stages: Dict[str, dict] = {}
        for j in js:
            stg = j.get("state") or "?"
            row = stages.setdefault(stg, {"stage": stg, "visits": 0, "longest_minutes": 0})
            row["visits"] += 1
            since = (j.get("history") or [{}])[-1].get("at") if j.get("history") else j.get("updated_at")
            try:
                mins = int((now - datetime.fromisoformat(str(since).replace("Z", "+00:00"))).total_seconds() // 60)
                row["longest_minutes"] = max(row["longest_minutes"], mins)
            except (TypeError, ValueError):
                pass
        tasks = call(h, agent_actor, "GET", "/tasks", params={"status": "open"})
        overdue = [t for t in tasks if t.get("due_at") and str(t["due_at"]) < now.isoformat()]
        return {"care_team": context.get("care_team_key"), "stages": list(stages.values()),
                "open_tasks": len(tasks), "overdue_tasks": len(overdue)}
    raise LookupError(f"No API tool '{name}'")


def level_rules() -> Dict[str, dict]:
    return {k: {"autonomous": v["autonomous"], "tool_scopes": v["tool_scopes"]} for k, v in LEVELS.items()}
