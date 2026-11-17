"""
In-process access to the Clinical API for the AI runtime and the patient portal.

``call(h, actor, "GET", "/patients", params={...})`` behaves like the HTTP API (same paths, JSON-shaped results,
same errors) but runs the harness directly, as ``actor``. The harness therefore enforces every rule exactly as it does
over HTTP: an agent actor needs the patient's AI consent and can only propose, a patient actor reads only their own
record.

Service identities (the AI agent, the portal service) are looked up by id in the server's key file when it has one,
and otherwise built with fixed, minimal roles. They exist only in-process; nothing can authenticate as them over HTTP
unless an administrator issues them a key.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from ...base import Actor
from ...errors import ClinestraError
from ...spec import SPECS
from .tools import registry

__all__ = ["HarnessError", "call", "identity", "AGENT_ID", "PORTAL_ID", "HARNESS_TOOLS", "tool_layer", "tool_concept_op"]

AGENT_ID = "clinestra_agent"
PORTAL_ID = "portal_service"
_SERVICE_DEFAULTS = {AGENT_ID: {"role": "clinician", "actor_type": "agent"}, PORTAL_ID: {"role": "coordinator"}}

HARNESS_TOOLS = {"generate_definition", "extract_observations", "summarize_patient", "clinical_reasoning", "fetch_clinical_data",
                 "fetch_evidence", "pending_actions", "definition_schema"}
CLINICAL_OPS = ("_list", "_get", "_create", "_update", "_transition", "_propose")
_BY_PLURAL = {s.plural: s.kind for s in SPECS.values()}


class HarnessError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def identity(actor_id: str, site: str, keys: Optional[dict] = None) -> Actor:
    """A service identity at ``site``: from the key file if configured there, else the built-in default."""
    for v in (keys or {}).values():
        a = v if isinstance(v, Actor) else Actor(**v)
        if a.id == actor_id and a.site == site:
            return a
    if actor_id not in _SERVICE_DEFAULTS:
        raise HarnessError(503, f"No identity '{actor_id}' at site '{site}'")
    return Actor(id=actor_id, site=site, **_SERVICE_DEFAULTS[actor_id])


def _json(out: Any) -> Any:
    if hasattr(out, "model_dump"):
        return out.model_dump(mode="json")
    if isinstance(out, list):
        return [_json(o) for o in out]
    if isinstance(out, dict):
        return {k: _json(v) for k, v in out.items()}
    return json.loads(json.dumps(out, default=str)) if out is not None else None


def call(h, actor: Actor, method: str, path: str, body: Any = None, params: Optional[dict] = None, timeout: float = 0) -> Any:
    """Run one Clinical API request in-process. ``timeout`` is accepted for signature compatibility."""
    params = {k: v for k, v in (params or {}).items() if v is not None}
    parts = [p for p in path.strip("/").split("/") if p]
    try:
        return _json(_dispatch(h, actor, method.upper(), parts, body or {}, params))
    except ClinestraError as exc:
        raise HarnessError(exc.status_code, str(exc)) from exc


def _dispatch(h, actor: Actor, method: str, parts: list, body: dict, params: dict) -> Any:
    if parts == ["me"]:
        return actor
    if parts == ["tools"] and method == "GET":
        return registry(h).definitions({"scope": params.get("scope", "patient"), "patient_id": params.get("patient_id"),
                                    "encounter_id": params.get("encounter_id")}, actor)
    if parts == ["tools", "call"] and method == "POST":
        return registry(h).call(actor, body["name"], body.get("args") or {},
                            {"scope": body.get("scope", "patient"), "patient_id": body.get("patient_id"),
                             "encounter_id": body.get("encounter_id")})
    kind = _BY_PLURAL.get(parts[0]) if parts else None
    if not kind:
        raise HarnessError(404, f"No in-process route for /{'/'.join(parts)}")
    if len(parts) == 1 and method == "GET":
        where = {k: params[k] for k in ("encounter_id", "definition_key", "def_kind") if k in params}
        return h.list(actor, kind, params.get("patient_id"), params.get("status"), False, **where)
    if len(parts) == 1 and method == "POST":
        return h.create(actor, kind, body)
    if len(parts) == 2 and method == "GET":
        return h.get(actor, kind, parts[1])
    if len(parts) == 2 and method == "PATCH":
        return h.update(actor, kind, parts[1], body)
    if len(parts) == 3 and parts[0] == "conversations" and parts[2] == "turns" and method == "POST":
        return h.add_turn(actor, parts[1], body["content"])
    if len(parts) == 3 and parts[2] == "transition" and method == "POST":
        return h.transition(actor, kind, parts[1], body["to"], body.get("reason"))
    raise HarnessError(404, f"No in-process route for {method} /{'/'.join(parts)}")


def tool_layer(name: str) -> str:
    """Agentic (harness reasoning tools) vs clinical (generated per-concept Clinical API tools)."""
    return "agentic" if name in HARNESS_TOOLS else "clinical"


def tool_concept_op(name: str):
    for op in CLINICAL_OPS:
        if name.endswith(op):
            return name[: -len(op)], op[1:]
    return None, None
