"""
The AI harness: tools for agents, generated from the concept registry, plus general harness tools.

Two layers:

* **Clinical API tools** - for every concept: ``<kind>_list``, ``_get``,
  ``_create``, ``_update``, ``_transition`` and ``_propose``. They are scoped
  by context: a *patient* context pins ``patient_id``, an *encounter* context
  pins ``patient_id`` and ``encounter_id``, a *setting* context exposes
  site-wide concepts. For an AI agent the registry offers only what it may do:
  read, create drafts where allowed, and propose everything else.
* **Harness tools** - ``generate_definition``, ``extract_observations``,
  ``summarize_patient``, ``clinical_reasoning``, ``fetch_clinical_data``,
  ``fetch_evidence``, ``pending_actions`` and ``definition_schema``. They work
  without an LLM (deterministic fallbacks) and get better with one
  (:class:`~clinestra.providers.LLMProvider`).

Tool definitions use the common ``{name, description, input_schema}`` shape,
so they can be handed to any LLM tool-use API or served over MCP.
"""
from __future__ import annotations

import json
import re
import weakref
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel

from ...base import Actor
from ...concepts.definitions import DEF_CONFIGS
from ...errors import Invalid, NotFound, PermissionDenied
from ...spec import SPECS, ConceptSpec

__all__ = ["Tool", "ToolRegistry", "Context", "registry"]

CONTEXTS = ("patient", "encounter", "setting", "general")


class Context(BaseModel):
    scope: str = "patient"
    patient_id: Optional[str] = None
    encounter_id: Optional[str] = None


@dataclass
class Tool:
    name: str
    description: str
    input_schema: Dict[str, Any]
    scopes: tuple
    layer: str  # "clinical_api" | "harness"
    fn: Callable[[Actor, dict, Context], Any]
    concept: Optional[str] = None
    op: Optional[str] = None
    agent_ok: Callable[[], bool] = field(default=lambda: True)

    def definition(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


def _dump(x):
    if isinstance(x, BaseModel):
        return x.model_dump(mode="json")
    if isinstance(x, list):
        return [_dump(i) for i in x]
    if isinstance(x, dict):
        return {k: _dump(v) for k, v in x.items()}
    return x


def _schema(model) -> dict:
    s = model.model_json_schema()
    s.pop("title", None)
    return s


def _obj(props: dict, required=()) -> dict:
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


_REGISTRIES: "weakref.WeakKeyDictionary" = None  # set below


def registry(harness) -> "ToolRegistry":
    """The tool registry for a harness, built once. Tools are API-layer: the core harness knows nothing about them."""
    global _REGISTRIES
    if _REGISTRIES is None:
        _REGISTRIES = weakref.WeakKeyDictionary()
    reg = _REGISTRIES.get(harness)
    if reg is None:
        reg = _REGISTRIES[harness] = ToolRegistry(harness)
    return reg


class ToolRegistry:
    def __init__(self, harness):
        self.h = harness
        self.tools: Dict[str, Tool] = {}
        for spec in SPECS.values():
            if spec.kind in ("approval",):
                continue
            self._concept_tools(spec)
        self._harness_tools()

    # ───────────────────────── discovery and calls ─────────────────────────
    def list(self, context: Optional[dict] = None, actor: Optional[Actor] = None) -> List[Tool]:
        ctx = Context(**(context or {}))
        if ctx.scope not in CONTEXTS:
            raise Invalid(f"scope must be one of {CONTEXTS}")
        out = [t for t in self.tools.values() if ctx.scope in t.scopes]
        if actor is not None and actor.actor_type == "agent":
            out = [t for t in out if self._agent_may(t)]
        return out

    def definitions(self, context: Optional[dict] = None, actor: Optional[Actor] = None) -> List[dict]:
        """Tool definitions for an LLM: patient/encounter ids are pinned by context, not chosen by the model."""
        ctx = Context(**(context or {}))
        out = []
        for t in self.list(context, actor):
            d = t.definition()
            d["input_schema"] = self._pin_schema(d["input_schema"], ctx)
            out.append(d)
        return out

    def call(self, actor: Actor, name: str, args: Optional[dict] = None, context: Optional[dict] = None) -> Any:
        ctx = Context(**(context or {}))
        tool = self.tools.get(name)
        if not tool or ctx.scope not in tool.scopes:
            raise NotFound(f"No tool '{name}' in a {ctx.scope} context")
        if actor.actor_type == "agent" and not self._agent_may(tool):
            raise PermissionDenied(f"Tool '{name}' is not available to AI agents; use the matching _propose tool")
        if ctx.scope in ("patient", "encounter") and not ctx.patient_id:
            raise Invalid(f"A {ctx.scope} context needs patient_id")
        if ctx.scope == "encounter" and not ctx.encounter_id:
            raise Invalid("An encounter context needs encounter_id")
        return _dump(tool.fn(actor, dict(args or {}), ctx))

    @staticmethod
    def _agent_may(t: Tool) -> bool:
        if t.layer == "harness" or t.op in ("list", "get", "propose"):
            return True
        spec = SPECS[t.concept]
        if t.op == "create":
            return spec.agent_can_create and not callable(spec.clinical) and not spec.clinical
        if t.op == "update":
            return spec.agent_can_create
        return False

    @staticmethod
    def _pin_schema(schema: dict, ctx: Context) -> dict:
        s = json.loads(json.dumps(schema))
        targets = [s] + [v for v in s.get("properties", {}).values() if isinstance(v, dict) and "properties" in v]
        for target in targets:
            for key, val in (("patient_id", ctx.patient_id), ("encounter_id", ctx.encounter_id)):
                if val and key in target.get("properties", {}):
                    target["properties"].pop(key)
                    if key in target.get("required", []):
                        target["required"].remove(key)
        return s

    @staticmethod
    def _bind(ctx: Context, data: dict, spec: ConceptSpec) -> dict:
        fields = spec.model.model_fields
        if ctx.patient_id and "patient_id" in fields:
            data["patient_id"] = ctx.patient_id
        if ctx.encounter_id and "encounter_id" in fields:
            data["encounter_id"] = ctx.encounter_id
        return data

    def _in_context(self, ctx: Context, rec) -> None:
        pid = getattr(rec, "patient_id", None)
        if ctx.patient_id and pid and pid != ctx.patient_id:
            raise PermissionDenied("That record belongs to another patient than this context")

    # ───────────────────────── clinical API tools ─────────────────────────
    def _concept_tools(self, spec: ConceptSpec):
        k, h = spec.kind, self.h
        if spec.scope == "patient":
            scopes = ("patient", "encounter") if spec.encounter_scoped else ("patient",)
            if k == "patient":
                scopes = ("patient", "encounter", "setting")
        elif spec.scope == "mixed":
            scopes = ("patient", "encounter", "setting") if spec.encounter_scoped else ("patient", "setting")
        else:
            scopes = ("setting", "general") if k == "definition" else ("setting",)
        label = k.replace("_", " ")
        id_prop = {"id": {"type": "string"}}

        def lst(actor, a, ctx):
            filters = {f: a[f] for f in ("status",) if a.get(f)}
            pid = ctx.patient_id if spec.scope != "global" else None
            if k == "patient" and ctx.patient_id:
                return [h.get(actor, "patient", ctx.patient_id)]
            where = {}
            if ctx.encounter_id and "encounter_id" in spec.model.model_fields and a.get("this_encounter_only", True):
                where["encounter_id"] = ctx.encounter_id
            if k == "definition" and a.get("def_kind"):
                where["def_kind"] = a["def_kind"]
            return h.list(actor, k, pid, **filters, **where)[: int(a.get("limit", 50))]

        def get(actor, a, ctx):
            rec = h.get(actor, k, a["id"])
            self._in_context(ctx, rec)
            return rec

        def create(actor, a, ctx):
            return h.create(actor, k, self._bind(ctx, dict(a), spec))

        def update(actor, a, ctx):
            self._in_context(ctx, h.get(actor, k, a["id"]))
            return h.update(actor, k, a["id"], a.get("patch") or {})

        def transition(actor, a, ctx):
            self._in_context(ctx, h.get(actor, k, a["id"]))
            return h.transition(actor, k, a["id"], a["to"], a.get("reason"))

        def propose(actor, a, ctx):
            if a.get("op", "create") == "create":
                return h.propose(actor, "create", k, self._bind(ctx, dict(a.get("data") or {}), spec),
                                 rationale=a.get("rationale"))
            self._in_context(ctx, h.get(actor, k, a["target_id"]))
            return h.propose(actor, a["op"], k, a.get("data") or {}, target_id=a["target_id"],
                             to_state=a.get("to"), rationale=a.get("rationale"))

        list_props = {"status": {"type": "string", "enum": list(spec.states)}, "limit": {"type": "integer"}}
        if k == "definition":
            list_props["def_kind"] = {"type": "string", "enum": sorted(DEF_CONFIGS)}
        if "encounter_id" in spec.model.model_fields:
            list_props["this_encounter_only"] = {"type": "boolean"}
        self._add(Tool(f"{k}_list", f"List {label} records in this context. {spec.summary}", _obj(list_props),
                       scopes, "clinical_api", lst, k, "list"))
        self._add(Tool(f"{k}_get", f"Read one {label}.", _obj(id_prop, ["id"]), scopes, "clinical_api", get, k, "get"))
        if k in ("proposal",):
            return
        clinical = " Clinical: a clinician must do this." if spec.clinical is True else ""
        self._add(Tool(f"{k}_create", f"Create a {label}. {spec.summary}{clinical}", _schema(spec.api_create_model),
                       scopes, "clinical_api", create, k, "create"))
        self._add(Tool(f"{k}_update", f"Change fields of a {label} (not allowed once {sorted(spec.frozen_states)}).",
                       _obj({**id_prop, "patch": _schema(spec.update_model)}, ["id", "patch"]),
                       scopes, "clinical_api", update, k, "update"))
        if spec.transitions:
            moves = "; ".join(f"{a}->{'/'.join(b)}" for a, b in spec.transitions.items())
            self._add(Tool(f"{k}_transition", f"Move a {label} to another state ({moves}).",
                           _obj({**id_prop, "to": {"type": "string", "enum": list(spec.states)},
                                 "reason": {"type": "string"}}, ["id", "to"]),
                           scopes, "clinical_api", transition, k, "transition"))
        self._add(Tool(f"{k}_propose", f"Propose creating or moving a {label}; a clinician accepts or rejects it.",
                       _obj({"op": {"type": "string", "enum": ["create", "transition", "update"]},
                             "data": {"type": "object"}, "target_id": {"type": "string"},
                             "to": {"type": "string", "enum": list(spec.states)}, "rationale": {"type": "string"}},
                            ["op", "rationale"]),
                       scopes, "clinical_api", propose, k, "propose"))

    def _add(self, t: Tool):
        self.tools[t.name] = t

    # ───────────────────────── harness tools ─────────────────────────
    def _harness_tools(self):
        h = self.h
        P = ("patient", "encounter")

        def need_pid(ctx, a):
            pid = ctx.patient_id or a.get("patient_id")
            if not pid:
                raise Invalid("patient_id is required")
            return pid

        def fetch(actor, a, ctx):
            pid = need_pid(ctx, a)
            codes = set(a.get("codes") or [])
            obs = [o for o in h.list(actor, "observation", pid) if o.status != "entered_in_error"
                   and (not codes or o.code in codes)]
            obs.sort(key=lambda o: str(o.effective_at))
            return {"observations": obs[-int(a.get("limit", 100)):],
                    "states": h.current_states(actor, pid),
                    "conditions": [c for c in h.list(actor, "condition", pid) if c.status in ("active", "remission")],
                    "medications": h.list(actor, "medication", pid, status="active"),
                    "careplans": [c for c in h.list(actor, "careplan", pid) if c.status in ("draft", "active")]}

        def summarize(actor, a, ctx):
            pid = need_pid(ctx, a)
            s = h.patient_summary(actor, pid)
            p = s["patient"]
            lines = [f"{p['name']}, {p.get('age')}y {p['gender']}, {p['country']}"]
            for prof in s["profiles"]:
                states = ", ".join(f"{x['state']}" + (f" ({x['condition_code']})" if x.get("condition_code") else "")
                                   for x in prof["states"])
                meas = ", ".join(f"{m['code']}={m['latest']}{m['unit'] or ''}" for m in prof["measurements"] if m["present"])
                absent = [m["code"] for m in prof["measurements"] if not m["present"]]
                lines.append(f"[{prof['profile']['name']}] states: {states or 'none'}; latest: {meas or 'none'}"
                             + (f"; not yet measured: {', '.join(absent)}" if absent else ""))
            lines.append(f"Pending actions: {len(s['pending_actions'])}")
            text = "\n".join(lines)
            if h.llm and a.get("prose"):
                text = h.llm.complete("Summarise this patient for a clinician in 5 lines. Do not invent facts.",
                                      json.dumps(s, default=str))
            return {"summary": text, "structured": s}

        def extract(actor, a, ctx):
            pid = need_pid(ctx, a)
            text = a["text"]
            if h.llm:
                schema = {"type": "object", "properties": {"observations": {"type": "array", "items": {
                    "type": "object", "properties": {"code": {"type": "string"}, "display": {"type": "string"},
                                                     "value": {}, "unit": {"type": "string"}}, "required": ["code", "value"]}}}}
                found = (h.llm.complete("Extract clinical observations as coded values. Use the vocabulary given.",
                                        json.dumps({"text": text, "vocabulary": self._vocabulary(actor)}), schema)
                         or {}).get("observations", [])
            else:
                found = self._rule_extract(actor, text)
            props = [h.propose(actor, "create", "observation",
                               {"patient_id": pid, "code": f["code"], "display": f.get("display"), "value": f["value"],
                                "unit": f.get("unit"), "source_kind": a.get("source_kind"), "source_id": a.get("source_id"),
                                "encounter_id": ctx.encounter_id},
                               rationale=f"Extracted from text: {f.get('evidence', '')}".strip())
                     for f in found] if a.get("propose", True) else []
            return {"extracted": found, "proposals": props}

        def evidence(actor, a, ctx):
            q = a["query"]
            if h.evidence_provider:
                return {"source": "evidence_provider", "results": h.evidence_provider.search(q, int(a.get("limit", 5)))}
            words = [w for w in re.findall(r"\w+", q.lower()) if len(w) > 2]
            hits = []
            for d in h.store.list("definition", actor.site, None):
                blob = json.dumps({k: d.get(k) for k in ("name", "description")} | {"config": {
                    k: d["config"].get(k) for k in ("pico", "evidence") if d["config"].get(k)}}).lower()
                score = sum(w in blob for w in words)
                if score and (d["config"].get("evidence") or d["config"].get("pico")):
                    hits.append({"score": score, "definition": f"{d['def_kind']}:{d['key']}", "name": d["name"],
                                 "pico": d["config"].get("pico"), "evidence": d["config"].get("evidence")})
            hits.sort(key=lambda x: -x["score"])
            return {"source": "pack_definitions", "results": hits[: int(a.get("limit", 5))],
                    "note": None if hits else "No evidence provider connected and no pack evidence matched."}

        def reasoning(actor, a, ctx):
            pid = need_pid(ctx, a)
            facts = dict(h.patient_facts(actor, pid))
            matches = []
            for d in h.store.list("definition", actor.site, None, def_kind="cohort"):
                rule = d["config"].get("rule")
                if d["status"] == "published" and rule and h._rule(rule, h.patient_facts(actor, pid)):
                    matches.append(d["key"])
            context = {"question": a.get("question"), "facts": facts, "matching_cohorts": sorted(set(matches)),
                       "pending_actions": h.pending_actions(actor, pid)}
            if not h.llm:
                return {"mode": "deterministic", "context": context,
                        "note": "Connect an LLMProvider for narrative reasoning. Suggestions here are rule-based only."}
            schema = {"type": "object", "properties": {"assessment": {"type": "string"}, "suggestions": {
                "type": "array", "items": {"type": "object", "properties": {
                    "kind": {"type": "string"}, "data": {"type": "object"}, "rationale": {"type": "string"}},
                    "required": ["kind", "data", "rationale"]}}}, "required": ["assessment"]}
            out = h.llm.complete("You support a clinician. Reason step by step from the facts only. Suggest, never decide.",
                                 json.dumps(_dump(context), default=str), schema) or {}
            props = []
            for s in out.get("suggestions", []):
                if s.get("kind") in SPECS:
                    props.append(h.propose(actor, "create", s["kind"], {**s.get("data", {}), "patient_id": pid},
                                           rationale=s.get("rationale")))
            return {"mode": "llm", "assessment": out.get("assessment"), "proposals": props}

        def generate(actor, a, ctx):
            kind = a["def_kind"]
            schema = h.definition_schema(kind)
            if not h.llm:
                return {"needs_llm": True, "def_kind": kind, "schema": schema,
                        "note": "No LLMProvider connected: fill this schema and create the definition yourself."}
            cfg = h.llm.complete(f"Write a Clinestra '{kind}' definition config. Return JSON matching the schema.",
                                 a["brief"], schema)
            cfg = h._validate_config(kind, cfg)
            data = {"def_kind": kind, "key": a["key"], "name": a.get("name") or a["key"], "config": cfg,
                    "description": a["brief"][:500]}
            if actor.actor_type == "agent":
                return {"proposal": h.propose(actor, "create", "definition", data,
                                              rationale="Generated definition draft")}
            return {"definition": h.create(actor, "definition", data)}

        reg = [
            Tool("fetch_clinical_data", "Observations (optionally filtered by codes), current clinical states, active "
                 "conditions, medications and careplans for the patient in context.",
                 _obj({"codes": {"type": "array", "items": {"type": "string"}}, "limit": {"type": "integer"},
                       "patient_id": {"type": "string"}}), P, "harness", fetch),
            Tool("summarize_patient", "Summary built from the patient's health profiles: states, latest measurements, "
                 "what is not yet measured, pending actions.",
                 _obj({"prose": {"type": "boolean"}, "patient_id": {"type": "string"}}), P, "harness", summarize),
            Tool("extract_observations", "Extract coded observations from free text (a note, a transcript). Each one "
                 "becomes a proposal for a clinician to accept.",
                 _obj({"text": {"type": "string"}, "source_kind": {"type": "string"}, "source_id": {"type": "string"},
                       "propose": {"type": "boolean"}, "patient_id": {"type": "string"}}, ["text"]),
                 P, "harness", extract),
            Tool("clinical_reasoning", "Gather the facts, matching cohorts, pending actions for a "
                 "question; with an LLM, reason over them and propose (never perform) actions.",
                 _obj({"question": {"type": "string"}, "patient_id": {"type": "string"}}, ["question"]),
                 P, "harness", reasoning),
            Tool("pending_actions", "What is waiting for this patient (open tasks, due care "
                 "actions, pending assessments and proposals).",
                 _obj({"patient_id": {"type": "string"}}), P, "harness",
                 lambda actor, a, ctx: h.pending_actions(actor, need_pid(ctx, a))),
            Tool("fetch_evidence", "Look up evidence for a clinical question (evidence provider, else pack PICO/evidence).",
                 _obj({"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]),
                 CONTEXTS, "harness", evidence),
            Tool("definition_schema", "JSON schema of a definition kind's config (to write templates).",
                 _obj({"def_kind": {"type": "string", "enum": sorted(DEF_CONFIGS)}}, ["def_kind"]),
                 ("setting", "general"), "harness", lambda actor, a, ctx: h.definition_schema(a["def_kind"])),
            Tool("generate_definition", "Generate a config/template (cohort, protocol, note page, journey, assessment "
                 "...) from a brief. Agents get a proposal; people get a draft definition.",
                 _obj({"def_kind": {"type": "string", "enum": sorted(DEF_CONFIGS)}, "key": {"type": "string"},
                       "name": {"type": "string"}, "brief": {"type": "string"}}, ["def_kind", "key", "brief"]),
                 ("setting", "general"), "harness", generate),
        ]
        for t in reg:
            self._add(t)

    # ───────────────────────── deterministic extraction ─────────────────────────
    def _vocabulary(self, actor: Actor) -> List[dict]:
        vocab: Dict[str, dict] = {}
        for d in self.h.store.list("definition", actor.site, None):
            for b in d["config"].get("blocks", []) + d["config"].get("fields", []):
                if b.get("observation_code"):
                    vocab.setdefault(b["observation_code"], {"code": b["observation_code"], "label": b["label"],
                                                             "unit": b.get("unit")})
            if d["def_kind"] == "assessment" and d["config"].get("observation_code"):
                vocab.setdefault(d["config"]["observation_code"], {"code": d["config"]["observation_code"],
                                                                   "label": d["name"], "unit": None})
        return list(vocab.values())

    def _rule_extract(self, actor: Actor, text: str) -> List[dict]:
        found = []
        bp = re.search(r"\bBP\b[^\d]{0,10}(\d{2,3})\s*/\s*(\d{2,3})", text, re.I)
        if bp:
            found += [{"code": "bp_systolic", "value": float(bp.group(1)), "unit": "mmHg", "evidence": bp.group(0)},
                      {"code": "bp_diastolic", "value": float(bp.group(2)), "unit": "mmHg", "evidence": bp.group(0)}]
        taken = {f["code"] for f in found}
        for v in self._vocabulary(actor):
            if v["code"] in taken:
                continue
            label = v["label"].split("(")[0].strip()
            names = {label, label.split(" ")[0], v["code"].replace("_", " "), v["code"].split("_")[0]}
            for n in sorted(names, key=len, reverse=True):
                if len(n) < 3:
                    continue
                m = re.search(rf"\b{re.escape(n)}\b[^\d\n]{{0,12}}(-?\d+(?:\.\d+)?)", text, re.I)
                if m:
                    found.append({"code": v["code"], "display": v["label"], "value": float(m.group(1)),
                                  "unit": v.get("unit"), "evidence": m.group(0)})
                    break
        return found
