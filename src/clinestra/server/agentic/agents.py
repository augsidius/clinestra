"""
Agents, managed at three levels of context (see :mod:`.levels`):

* **personal** - every team member gets their own agent, copied from the personal template on first use.
* **team** - every care team (a care team definition) gets a Care Coordination Agent, copied from its template.
* **patient** - agent types (clinical copilot, health coach) that work on the patient on screen.

An agent has a model, a system prompt, its own tool patterns and its **skills** (:mod:`.skills`). Its tools are the
union of both, filtered by the level and by tool administration (:mod:`.toolbox`). Every agent runs as the
``clinestra_agent`` identity (actor_type=agent), so the harness enforces the rest itself: it needs the patient's AI
consent, it reads and drafts, and anything clinical becomes a proposal. On top of that this layer enforces:

* a personal agent never gets a patient context or a patient tool;
* a team agent never reads one patient's record and never writes anything that names a patient;
* nothing patient specific runs on its own: autonomous runs are team level and read only.

Without a model, agents answer from the deterministic tools.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, List, Optional

from ...base import Actor
from . import runtime, toolbox
from . import skills as sk
from . import content_blocks as uic
from .defaults import DEFAULT_AGENTS
from .levels import LEVEL_IDS, LEVELS, level_of_scope
from .local import HarnessError, call, tool_layer
from .state import SiteState, new_id

__all__ = ["ensure_defaults", "normalize", "save", "all_agents", "get", "delete", "personal_for", "team_agents", "visible_to",
           "explain", "chat", "run", "runs", "LevelError"]

SEED_VERSION = 2
_TOOL_SCOPE = {"personal": "general", "team": "setting", "patient": "patient"}


class LevelError(ValueError):
    """A request outside what the agent's level allows."""


def normalize(cfg: dict) -> dict:
    """Configs saved before levels existed carry a scope and two tool lists; read them as a level and one list."""
    c = dict(cfg)
    c.setdefault("level", level_of_scope(c.get("scope", "personal")))
    if "tools" not in c:
        c["tools"] = list(c.get("agentic_tools") or []) + list(c.get("clinical_tools") or [])
        if c.get("ui_tools"):
            c["tools"].append("ui_open")
            if c.get("content_blocks") is not False:
                c["tools"].append("render_blocks")
    c.setdefault("skills", [])
    c.setdefault("kind", "type")
    c["scope"] = _TOOL_SCOPE[c["level"]]  # older clients read scope
    return c


def ensure_defaults(st: SiteState) -> None:
    if (st.kv_get("agents_seed_version") or 0) >= SEED_VERSION:
        return
    for old in ("cura", "care_coordinator"):  # replaced by the personal and care-team templates (unedited defaults only)
        d = st.get("_ai.agent", old)
        if d and not d.get("_deleted") and d.get("created_by") == "system":
            st.delete("_ai.agent", old)
    for a in DEFAULT_AGENTS:
        d = st.get("_ai.agent", a["id"])
        if not d or d.get("_deleted"):
            save(st, a, "system")
    st.kv_set("agents_seed_version", SEED_VERSION)


def save(st: SiteState, cfg: dict, actor: str) -> dict:
    cfg = normalize(cfg)
    if cfg["level"] not in LEVEL_IDS:
        raise LevelError(f"Level must be one of {LEVEL_IDS}")
    prev = st.get("_ai.agent", cfg["id"])
    created_by = prev["created_by"] if prev and not prev.get("_deleted") else actor
    st.put("_ai.agent", cfg["id"], {"config": cfg, "created_by": created_by, "updated_by": actor, "updated_at": time.time()})
    return cfg


def all_agents(st: SiteState) -> list:
    ensure_defaults(st)
    order = {lv: i for i, lv in enumerate(LEVEL_IDS)}
    out = [{**normalize(d["config"]), "updated_at": d["updated_at"]} for d in st.records("_ai.agent")]
    return sorted(out, key=lambda a: (order[a["level"]], a.get("kind") != "template", a["name"]))


def get(st: SiteState, agent_id: str) -> dict:
    ensure_defaults(st)
    d = st.get("_ai.agent", agent_id)
    if not d or d.get("_deleted"):
        raise LookupError(f"No agent '{agent_id}'")
    return normalize(d["config"])


def delete(st: SiteState, agent_id: str) -> None:
    st.delete("_ai.agent", agent_id)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", s.lower()).strip("_")[:40]


def personal_for(st: SiteState, me: dict) -> dict:
    """The member's own personal agent, created from the template the first time they use it."""
    aid = f"personal__{_slug(me['id'])}"
    try:
        return get(st, aid)
    except LookupError:
        tpl = get(st, "personal_assistant")
        first = str(me.get("name") or me["id"]).replace("_", " ").split(" ")[0].title()
        cfg = {**tpl, "id": aid, "kind": "personal", "owner_id": me["id"], "name": f"{first}'s agent",
               "audience": [me["role"]], "template_id": tpl["id"], "is_default": False}
        st.admin_log("system", "agent.personal_created", {"id": aid, "owner": me["id"]})
        return save(st, cfg, "system")


def team_agents(h, st: SiteState, agent_actor: Actor) -> List[dict]:
    """One Care Coordination Agent per care team (care team definition), added automatically."""
    try:
        teams = call(h, agent_actor, "GET", "/definitions", params={"def_kind": "care_team"})
    except HarnessError:
        teams = []
    seen, out = set(), []
    for t in sorted(teams, key=lambda d: -(d.get("version") or 0)):
        key = t.get("key")
        if not key or key in seen or t.get("def_kind") != "care_team" or t.get("status") == "retired":
            continue
        seen.add(key)
        aid = f"team__{_slug(key)}"
        try:
            a = get(st, aid)
        except LookupError:
            tpl = get(st, "care_coordination")
            name = t.get("name") or key
            a = save(st, {**tpl, "id": aid, "kind": "team", "care_team_key": key, "care_team_name": name,
                          "name": f"Care Coordination · {name}", "template_id": tpl["id"], "is_default": False}, "system")
            st.admin_log("system", "agent.team_created", {"id": aid, "care_team": key})
        out.append(a)
    return out


def skills_of(st: SiteState, agent: dict) -> List[dict]:
    out = []
    for sid in agent.get("skills") or []:
        try:
            s = sk.get(st, sid)
        except LookupError:
            continue
        if s.get("enabled", True) and s["level"] == agent["level"]:
            out.append(s)
    return out


def visible_to(h, st: SiteState, me: dict, agent_actor: Actor) -> List[dict]:
    """The agents a member works with: their own personal agent, every care team's agent and the patient agent types."""
    if me["role"] == "patient":
        return [a for a in all_agents(st) if a["level"] == "patient" and "patient" in a.get("audience", []) and a.get("enabled", True)]
    out = [personal_for(st, me)] + team_agents(h, st, agent_actor)
    out += [a for a in all_agents(st) if a["level"] == "patient" and a.get("kind") == "type"
            and (me["role"] in a.get("audience", []) or "any" in a.get("audience", []))]
    return [a for a in out if a.get("enabled", True)]


def explain(h, st: SiteState, agent: dict, agent_actor: Actor, role: str) -> dict:
    """An agent as the Agent Explorer shows it: level, skills and the tools it would get."""
    skills = skills_of(st, agent)
    tools = toolbox.effective(h, st, agent_actor, agent, skills, role, agent["level"], None)
    return {**agent,
            "skills_detail": [{k: s.get(k) for k in ("id", "name", "description", "starter", "schedule")} for s in skills],
            "tool_names": sorted(t["name"] for t in tools)}


def _summary(result: Any) -> str:
    s = json.dumps(result, default=str)
    return s if len(s) <= 400 else s[:400] + "…"


def _names_patient(args: dict) -> bool:
    data = args.get("data") if isinstance(args.get("data"), dict) else {}
    return bool(args.get("patient_id") or data.get("patient_id"))


def check_level(agent: dict, me: dict, context: dict, autonomous: bool = False) -> None:
    level = agent["level"]
    if autonomous and not LEVELS[level]["autonomous"]:
        raise LevelError(f"A {LEVELS[level]['name'].lower()} agent never runs on its own: a person has to ask it")
    if level == "personal":
        if context.get("patient_id") or context.get("encounter_id"):
            raise LevelError("A personal agent never works on a patient: open the patient agent from the patient's screen")
        if agent.get("owner_id") and agent["owner_id"] != me["id"]:
            raise LevelError("This personal agent belongs to another team member")
    elif level == "team":
        if context.get("patient_id"):
            raise LevelError("A care-team agent works on the team's queues, not on one patient: use the patient agent")
    elif not context.get("patient_id") and me["role"] != "patient":
        raise LevelError("This agent works on one patient: open it from a patient's screen")


def _system(agent: dict, me: dict, level: str, tool_ctx: dict, skills: List[dict], chosen: Optional[dict], autonomous: bool, render: bool) -> str:
    L = LEVELS[level]
    owner = str(me.get("name") or me["id"]).replace("_", " ")
    parts = [agent["system_prompt"].replace("{owner}", owner).replace("{team}", agent.get("care_team_name") or "this care team"),
             f"Level: {L['name']}. You may: " + "; ".join(L["can"]) + ". You may not: " + "; ".join(L["cannot"]) + ".",
             f"User: {me['id']} ({me['role']}). Context: {tool_ctx['scope']}"
             + (f", patient_id={tool_ctx['patient_id']}" if tool_ctx.get("patient_id") else "")
             + (f", encounter_id={tool_ctx['encounter_id']}" if tool_ctx.get("encounter_id") else "")
             + (f", care team={agent.get('care_team_key')}" if level == "team" else "")
             + ". Patient and encounter ids are pinned by the context; tools do not need them."]
    if skills:
        parts.append("Skills you have (use the one that fits):\n" + "\n".join(f"- {s['name']}: {s['description']} How: {s['instructions']}" for s in skills))
    if chosen:
        parts.append(f"The person started the skill '{chosen['name']}'. Follow it: {chosen['instructions']}")
    if autonomous:
        parts.append("You are running on your own (scheduled). You are read only: write a short report for the team.")
    return "\n\n".join(parts) + (uic.PROMPT_HINT if render else "")


def chat(h, st: SiteState, agent: dict, me: dict, user: Actor, agent_actor: Actor, message: str,
         conversation_id: Optional[str], context: dict, policy: dict, skill_id: Optional[str] = None, autonomous: bool = False) -> dict:
    """One message through an agent. Conversation turns are stored in the clinical record (who said what, auditable);
    the model thread (with tool blocks, kept verbatim and append-only) is private AI state."""
    check_level(agent, me, context, autonomous)
    level = agent["level"]
    pid = context.get("patient_id") if level == "patient" else None
    eid = context.get("encounter_id") if level == "patient" else None
    if me["role"] == "patient":
        pid = pid or me.get("patient_id")
    tool_ctx = {"scope": "encounter" if (level == "patient" and eid) else _TOOL_SCOPE[level], "patient_id": pid, "encounter_id": eid}
    skills = skills_of(st, agent)
    chosen = next((s for s in skills if s["id"] == skill_id), None)
    if skill_id and not chosen:
        raise LevelError(f"Skill '{skill_id}' is not one of this agent's skills")
    message = message or (chosen or {}).get("starter") or ""

    if not autonomous:
        if not conversation_id:
            body = {"scope": "patient" if pid else "general", "patient_id": pid, "actor_role": me["role"],
                    "title": f"{agent['name']}: {message[:60]}"}
            if agent.get("conversation_key"):
                body["definition_key"] = agent["conversation_key"]
            conversation_id = call(h, user, "POST", "/conversations", body)["id"]
        call(h, user, "POST", f"/conversations/{conversation_id}/turns", {"content": message})

    steps, proposals, ui_actions, ui_blocks = [], [], [], []
    t0 = time.time()
    try:
        m, p = runtime.resolve(st, "agent", agent.get("model_alias"))
    except LookupError:
        m = p = None

    if not m:
        text = _offline(h, st, agent, skills, chosen, agent_actor, tool_ctx, message, steps, me, autonomous)
    else:
        tools = toolbox.effective(h, st, agent_actor, agent, skills, me["role"], level, pid, autonomous, set(policy.get("disabled_tools", [])))
        names = {t["name"] for t in tools}
        system = _system(agent, me, level, tool_ctx, skills, chosen, autonomous, "render_blocks" in names)
        doc = (st.get("_ai.thread", conversation_id) or {}) if conversation_id else {}
        thread = doc.get("messages", []) if doc.get("actor") in (None, me["id"]) else []
        thread.append({"role": "user", "content": message})
        text = ""
        usage_in = usage_out = 0
        for _ in range(int(policy.get("max_agent_steps", 8))):
            resp = runtime.agent_step(st, m, p, system, thread, tools)
            usage_in += resp.usage.input_tokens
            usage_out += resp.usage.output_tokens
            content = resp.to_dict()["content"]
            thread.append({"role": "assistant", "content": content})
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text") or text
            calls = [b for b in content if b.get("type") == "tool_use"]
            if resp.stop_reason == "refusal":
                text += "\n\n_The model declined this request._"
                break
            if not calls:
                break
            results = []
            for c in calls:
                results.append(_run_tool(h, agent, level, c, names, tool_ctx, me, context, agent_actor, steps, proposals, ui_actions, ui_blocks))
            thread.append({"role": "user", "content": results})
        if conversation_id:
            st.put("_ai.thread", conversation_id, {"agent_id": agent["id"], "actor": me["id"], "messages": thread, "updated_at": time.time()})
        st.audit(actor=me["id"], role=me["role"], feature=f"agent:{agent['id']}", model=m["model_id"], provider=p["name"], status="ok",
                 input_tokens=usage_in, output_tokens=usage_out, latency_ms=int((time.time() - t0) * 1000),
                 tool_calls=[{"name": s["tool"], "arguments": s.get("args")} for s in steps], patient_id=pid,
                 detail={"level": level, "skill": skill_id, "autonomous": autonomous})

    if conversation_id:
        try:
            call(h, agent_actor, "POST", f"/conversations/{conversation_id}/turns", {"content": text or "(no reply)"})
        except HarnessError:
            pass  # e.g. AI consent missing for a patient conversation: the reply still reaches the user
    return {"conversation_id": conversation_id, "message": text, "steps": steps, "proposals": proposals, "ui_actions": ui_actions,
            "ui_blocks": ui_blocks, "model": m["model_id"] if m else None, "level": level, "skill": skill_id}


def _run_tool(h, agent, level, c, names, tool_ctx, me, context, agent_actor, steps, proposals, ui_actions, ui_blocks) -> dict:
    name, args = c["name"], c.get("input") or {}
    if name not in names:
        steps.append({"tool": name, "layer": "api", "args": args, "ok": False, "error": "not available to this agent"})
        return {"type": "tool_result", "tool_use_id": c["id"], "content": f"Tool '{name}' is not available to this agent.", "is_error": True}
    if name == "render_blocks":
        blocks = uic.validate(args.get("blocks"))
        ui_blocks.extend(blocks)
        steps.append({"tool": name, "layer": "ui", "args": {"blocks": [b["type"] for b in blocks]}, "ok": bool(blocks)})
        return {"type": "tool_result", "tool_use_id": c["id"],
                "content": f"Rendered {len(blocks)} component(s) for the user." if blocks else "Nothing rendered: no valid blocks.", **({} if blocks else {"is_error": True})}
    if name == "ui_open":
        ui_actions.append(args)
        steps.append({"tool": name, "layer": "ui", "args": args, "ok": True})
        return {"type": "tool_result", "tool_use_id": c["id"], "content": "Opened for the user."}
    try:
        if level == "team" and _names_patient(args) and not name.endswith(("_list", "_get")):
            raise HarnessError(403, "A care-team agent may not change patient-specific records; a person or the patient agent must")
        if toolbox.is_api_tool(name):
            out, layer = toolbox.call_api_tool(h, agent_actor, name, args, me, {**context, "care_team_key": agent.get("care_team_key")}), "api"
        else:
            out, layer = call(h, agent_actor, "POST", "/tools/call", {"name": name, "args": args, **tool_ctx}), tool_layer(name)
        steps.append({"tool": name, "layer": layer, "args": args, "ok": True, "preview": _summary(out)})
        for item in (out if isinstance(out, list) else [out]):
            if isinstance(item, dict) and item.get("origin") and item.get("target_kind") and item.get("status") == "pending":
                proposals.append(item)
        return {"type": "tool_result", "tool_use_id": c["id"], "content": json.dumps(out, default=str)[:20000]}
    except HarnessError as e:
        steps.append({"tool": name, "layer": tool_layer(name), "args": args, "ok": False, "error": e.detail})
        return {"type": "tool_result", "tool_use_id": c["id"], "content": f"Refused ({e.status}): {e.detail}", "is_error": True}


def run(h, st: SiteState, agent: dict, me: dict, agent_actor: Actor, skill_id: str, policy: dict) -> dict:
    """Run a team skill on its own, as its schedule would: read only, no conversation; the report is kept for the team."""
    out = chat(h, st, agent, me, agent_actor, agent_actor, "", None, {}, policy, skill_id, autonomous=True)
    skill = next(s for s in skills_of(st, agent) if s["id"] == skill_id)
    rec = {"agent_id": agent["id"], "skill_id": skill_id, "skill": skill["name"], "triggered_by": me["id"], "at": time.time(),
           "report": out["message"], "steps": out["steps"], "model": out["model"]}
    st.put("_ai.run", new_id("run"), rec)
    return rec


def runs(st: SiteState, agent_id: str, limit: int = 20) -> List[dict]:
    return sorted((r for r in st.records("_ai.run") if r["agent_id"] == agent_id), key=lambda r: -r["at"])[:limit]


def _offline(h, st, agent: dict, skills: List[dict], chosen: Optional[dict], agent_actor: Actor, ctx: dict, message: str,
             steps: list, me: dict, autonomous: bool) -> str:
    """No model configured: run the deterministic tools that fit the level and the skill, so agents still help."""
    level = agent["level"]
    allowed = {t["name"] for t in toolbox.effective(h, st, agent_actor, agent, skills, me["role"], level, ctx.get("patient_id"), autonomous)}
    low = message.lower()
    want: List[str] = []
    if chosen:
        want = [t for t in chosen.get("tools") or [] if t in allowed and "*" not in t and not t.endswith(("_propose", "_get"))][:3]
    elif level == "personal":
        want = [t for t in ("my_work",) if t in allowed]
    elif level == "team":
        want = [t for t in ("team_board", "task_list") if t in allowed]
    else:
        for name, words in (("summarize_patient", ("summar", "synopsis", "overview", "patient", "how is", "brief")),
                            ("pending_actions", ("pending", "due", "next", "todo", "action")),
                            ("fetch_clinical_data", ("data", "vital", "bp", "result", "lab", "medic"))):
            if name in allowed and any(w in low for w in words):
                want.append(name)
        if not want:
            want = [n for n in ("summarize_patient", "pending_actions") if n in allowed]
    parts = ["_No model is connected, so this answer comes from the deterministic tools. An administrator can connect a model in Admin → Model providers._"]
    for name in want:
        try:
            if toolbox.is_api_tool(name):
                out = toolbox.call_api_tool(h, agent_actor, name, {}, me, {"care_team_key": agent.get("care_team_key")})
            else:
                out = call(h, agent_actor, "POST", "/tools/call", {"name": name, "args": {}, **ctx})
            steps.append({"tool": name, "layer": "api" if toolbox.is_api_tool(name) else tool_layer(name), "args": {}, "ok": True, "preview": _summary(out)})
            parts.append(f"### {name.replace('_', ' ').title()}\n" + _readable(out))
        except HarnessError as e:
            steps.append({"tool": name, "layer": tool_layer(name), "args": {}, "ok": False, "error": e.detail})
            parts.append(f"**{name}** was refused: {e.detail}")
    if not want:
        parts.append("Connect a model for open questions, or start one of the agent's skills.")
    return "\n\n".join(parts)


def _when(v: Any) -> str:
    if not isinstance(v, str) or len(v) < 10 or v[4] != "-":
        return ""
    try:
        from datetime import datetime
        return datetime.fromisoformat(v.replace("Z", "+00:00")).strftime("%d %b %Y")
    except ValueError:
        return v[:10]


def _line(r: dict) -> str:
    """One record as a short plain-language line (no ids, no JSON)."""
    name = r.get("display") or r.get("title") or r.get("name") or r.get("code") or r.get("kind") or "item"
    name = str(name).split(": ")[-1].replace("_", " ")
    parts = [f"**{name[:1].upper() + name[1:]}**"]
    if r.get("value") not in (None, ""):
        parts.append(f"{r['value']}{(' ' + r['unit']) if r.get('unit') else ''}")
    for k in ("dose", "frequency"):
        if r.get(k):
            parts.append(str(r[k]))
    if r.get("band") or r.get("verdict"):
        parts.append(str(r.get("band") or r.get("verdict")).replace("_", " "))
    if r.get("status") and r.get("status") not in ("final", "active", "recorded"):
        parts.append(f"({str(r['status']).replace('_', ' ')})")
    when = _when(r.get("effective_at") or r.get("due") or r.get("due_at") or r.get("taken_at") or r.get("recorded_at") or r.get("created_at"))
    if when:
        parts.append(f"· {when}")
    return "- " + " ".join(parts)


NL = chr(10)


def _records(rows: list, limit: int = 12) -> str:
    rows = [r for r in rows if isinstance(r, dict)]
    if rows and "code" in rows[0] and "value" in rows[0]:  # observations: latest per code
        latest: dict = {}
        for r in sorted(rows, key=lambda x: str(x.get("effective_at") or x.get("created_at") or "")):
            latest[r["code"]] = r
        rows = list(latest.values())
    out = [_line(r) for r in rows[:limit]]
    if len(rows) > limit:
        out.append(f"- …and {len(rows) - limit} more")
    return NL.join(out) or "- none"


def _readable(out: Any) -> str:
    """Render a deterministic tool result as plain-language markdown (never raw JSON)."""
    if isinstance(out, str):
        return out
    if isinstance(out, dict) and isinstance(out.get("summary"), str):
        lines = []
        for ln in out["summary"].split(NL):
            if ln.startswith("[") and "]" in ln:
                lines.append(NL + "**" + ln[1:ln.index("]")] + "**" + ln[ln.index("]") + 1:])
            else:
                lines.append("- " + ln)
        return NL.join(lines)
    if isinstance(out, list):
        return _records(out)
    if isinstance(out, dict):
        sections = []
        for k, v in out.items():
            label = str(k).replace("_", " ").capitalize()
            if isinstance(v, list) and v:
                sections.append(f"**{label}**" + NL + _records(v))
            elif isinstance(v, dict) and v and all(isinstance(x, list) for x in v.values()):
                sections.append(_readable(v))
            elif isinstance(v, (str, int, float)) and k not in ("id", "patient_id", "site") and not str(k).endswith("_id"):
                sections.append(f"- **{label}**: {v}")
        return (NL + NL).join(sections) or "Nothing recorded yet."
    return str(out)

