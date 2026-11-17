"""
The Harness: every concept, with its rules, behind one object.

    >>> from clinestra import Harness, Actor
    >>> h = Harness()                                   # in-memory store, core + mdd packs
    >>> dr = Actor(id="dr_rao", site="clinic_1", role="clinician")
    >>> p = h.create(dr, "patient", {"name": "Asha K", "country": "IN", "gender": "female", "age_years": 34})

Five generic operations work on every concept: ``create``, ``get``/``list``,
``update``, ``transition`` and ``delete``. Each checks the actor against the
concept's spec, saves the record and appends a hash-chained timeline entry.

Design principles:

* **Reactions.** When a stateful record changes state it can create or move
  other records: built-in behaviour (a protocol instantiates its templates)
  plus configured ``reaction`` definitions.
* **Tiered.** Non-clinical effects (tasks, journeys, profiles, documents,
  observations from signed pages) apply automatically. Clinical effects
  (states, regimens, orders, outcomes, cohort and protocol decisions) become
  *proposals* that a clinician accepts or rejects.
* **Agents propose, people decide.** The core only knows an agent as a kind of
  actor (``actor_type='agent'``): it may read, draft and propose, never decide,
  and it needs the patient's ``ai`` consent. Agents themselves (models, tools,
  skills, levels) live in the API layer, :mod:`clinestra.server.agentic`.
* **Approvals.** States listed as gated (a note page becoming *approved*, or
  anything a definition lists) need someone other than the requester to approve.
* **Definitions.** Fourteen concepts can be created from configured templates
  (``definition_key``); published definitions are frozen and versioned.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

from pydantic import BaseModel, ValidationError

from . import engine
from .audit import make_entry, verify_chain
from .base import CLINICAL_DECIDERS, Actor, new_id, system_actor, utcnow, acting_role
from .concepts.common import Effect, Instance
from .concepts.definitions import DEF_CONFIGS, DEF_INSTANCE_KIND
from .concepts.people import STANDARD_DEMOGRAPHICS
from .errors import ClinestraError, Invalid, InvariantViolation, NotFound, PermissionDenied
from .packs_loader import Pack, load_pack
from .providers import BlobStore, EvidenceProvider, LLMProvider, MemoryBlobStore
from .spec import SPECS, ConceptSpec, spec_for
from .storage import LIBRARY_SITE, MemoryStore, Store

__all__ = ["Harness"]

_SITE_CHAIN = "_site"
_PREFIX = {
    "patient": "pat", "relationship": "rel", "provider": "prv", "care_team_member": "ctm", "care_team": "team",
    "clinical_setting": "set", "observation": "obs", "condition": "cond", "medication": "med",
    "clinical_state": "cs", "outcome": "out", "health_profile": "hp",
    "cohort_membership": "coh", "protocol_assignment": "pa", "careplan": "plan", "care_action": "act",
    "task": "task", "routine": "rtn", "regimen": "reg", "journey": "jrn", "encounter": "enc",
    "clinical_record": "rec", "note_page": "page", "assessment": "asm", "clinical_document": "doc",
    "journal_entry": "jnl", "conversation": "conv", "consent": "cns", "approval": "apr", "proposal": "prop",
    "definition": "def",
}


class _Facts(dict):
    """Variables for cohort rules and protocol criteria. Unknown cond_/obs_/state_ names read as False/None."""

    def __contains__(self, key):
        return super().__contains__(key) or str(key).startswith(("cond_", "obs_", "state_"))

    def __getitem__(self, key):
        if super().__contains__(key):
            return super().__getitem__(key)
        return False if str(key).startswith(("cond_", "state_")) else None


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Harness:
    def __init__(self, store: Optional[Store] = None, packs: Sequence[str] = ("core",),
                 evidence_provider: Optional[EvidenceProvider] = None, llm: Optional[LLMProvider] = None,
                 blob_store: Optional[BlobStore] = None, max_reaction_depth: int = 4):
        self.store = store or MemoryStore()
        self.evidence_provider = evidence_provider
        self.llm = llm
        self.blobs = blob_store or MemoryBlobStore()
        self.max_reaction_depth = max_reaction_depth
        self.packs: Dict[str, Pack] = {}
        for p in packs:
            self.load_pack(p)

    # ════════════════════════════ packs and definitions ════════════════════════════
    def load_pack(self, source: str) -> Pack:
        pack = load_pack(source)
        for d in pack.definitions:
            cfg = self._validate_config(d["def_kind"], d.get("config", {}))
            doc = {"id": f"def_{pack.pack_id}_{d['def_kind']}_{d['key']}_v{d.get('version', 1)}", "site": LIBRARY_SITE,
                   "created_by": f"pack:{pack.pack_id}", "status": "published", "def_kind": d["def_kind"],
                   "key": d["key"], "version": d.get("version", 1), "name": d.get("name", d["key"]),
                   "description": d.get("description"), "config": cfg, "pack_id": pack.pack_id,
                   "review_status": d.get("review_status", "draft"),
                   "review_ref": d.get("review_ref"), "review_notes": d.get("review_notes")}
            model = SPECS["definition"].model(**doc)
            self.store.put("definition", model.model_dump(mode="json"))
        self.packs[pack.pack_id] = pack
        return pack

    def resolve_definition(self, site: str, def_kind: str, key: str, version: Optional[int] = None) -> dict:
        """Latest published definition for ``key`` (the site's own first, then the shared library)."""
        docs = [d for d in self.store.list("definition", site, None, def_kind=def_kind, key=key)
                if d["status"] == "published" and not d.get("deleted") and (version is None or d["version"] == version)]
        if not docs:
            raise NotFound(f"No published {def_kind} definition '{key}'")
        docs.sort(key=lambda d: (d["site"] != LIBRARY_SITE, d["version"]))
        return docs[-1]

    def _pinned_definition(self, actor: Actor, rec, def_kind: str, key: str) -> dict:
        """The exact definition a record was created under, even if since retired. Unpinned older records get the latest."""
        doc = self.store.get("definition", rec.definition_id, actor.site) if rec.definition_id else None
        return doc or self.resolve_definition(actor.site, def_kind, key)

    def definition_schema(self, def_kind: str) -> dict:
        if def_kind not in DEF_CONFIGS:
            raise NotFound(f"Unknown definition kind '{def_kind}'. Known: {sorted(DEF_CONFIGS)}")
        return DEF_CONFIGS[def_kind].model_json_schema()

    @staticmethod
    def _validate_config(def_kind: str, config: dict) -> dict:
        if def_kind not in DEF_CONFIGS:
            raise Invalid(f"Unknown definition kind '{def_kind}'. Known: {sorted(DEF_CONFIGS)}")
        try:
            return DEF_CONFIGS[def_kind](**(config or {})).model_dump(mode="json")
        except ValidationError as exc:
            raise Invalid(f"Invalid {def_kind} config: {exc}") from exc

    # ════════════════════════════ generic operations ════════════════════════════
    def create(self, actor: Actor, kind: str, data: Union[BaseModel, dict], *, _depth: int = 0,
               _status: Optional[str] = None) -> Instance:
        spec = spec_for(kind)
        if isinstance(data, BaseModel):
            data = data.model_dump(exclude_unset=True)
        data = {k: v for k, v in data.items() if v is not None or k not in spec.create_model.model_fields
                or not spec.create_model.model_fields[k].is_required()}
        definition = None
        if data.get("definition_key") and spec.definition_kind:
            definition = self.resolve_definition(actor.site, spec.definition_kind, data["definition_key"])
            fill = getattr(self, f"_fill_{kind}", None)
            base = {**(fill(definition) if fill else {}), **(definition["config"].get("defaults") or {})}
            data = {**{k: v for k, v in base.items() if k in spec.create_model.model_fields}, **data}
        try:
            given = spec.create_model(**data).model_dump(exclude_unset=True)
        except ValidationError as exc:
            raise Invalid(str(exc)) from exc
        hook = getattr(self, f"_pre_{kind}", None)
        if hook:
            given = hook(actor, given, definition) or given
        if definition is None and given.get("definition_key") and spec.definition_kind:
            # A hook may derive the key (cohort_key, protocol_key). The record must still pin the version it used.
            definition = self.resolve_definition(actor.site, spec.definition_kind, given["definition_key"])
        rid = new_id(_PREFIX.get(kind, kind[:4]))
        if kind == "patient":
            given["patient_id"] = rid
        self._authorize(actor, spec, "create", given)
        self._require_known_links(actor, kind, given)
        self._require_reviewed(definition)
        status = _status or spec.initial
        try:
            rec = spec.model(id=rid, site=actor.site, created_by=actor.id, status=status,
                             definition_id=definition["id"] if definition else None,
                             definition_version=definition["version"] if definition else None, **given)
        except ValidationError as exc:
            raise Invalid(str(exc)) from exc
        self._put(kind, rec)
        self._audit(actor, self._pid(rec), f"{kind}.created", kind, rec.id, {"status": status})
        post = getattr(self, f"_post_{kind}", None)
        if post:
            post(actor, rec, definition, _depth)
        self._react(kind, rec, status, _depth)
        return self.get(system_actor(actor.site, "read"), kind, rec.id)

    def get(self, actor: Actor, kind: str, id: str) -> Instance:
        spec = spec_for(kind)
        doc = self.store.get(kind, id, actor.site)
        if not doc or (doc.get("deleted") and actor.role != "system"):
            raise NotFound(f"No {kind} '{id}'")
        self._authorize(actor, spec, "read", doc)
        return self._load(spec, doc)

    def list(self, actor: Actor, kind: str, patient_id: Optional[str] = None, status: Optional[str] = None,
             include_deleted: bool = False, **where) -> List[Instance]:
        if kind == "note_page" and where.get("encounter_id"):
            # Note pages belong to an encounter through their clinical record.
            encounter_id = where.pop("encounter_id")
            records = self.list(actor, "clinical_record", patient_id, None, include_deleted, encounter_id=encounter_id)
            return [page for record in records
                    for page in self.list(actor, kind, patient_id, status, include_deleted, record_id=record.id, **where)]
        spec = spec_for(kind)
        if actor.actor_type == "agent" and patient_id:
            self.require_consent(actor, patient_id, "ai")
        if actor.role == "patient" and kind == "definition":
            # Patient apps need published content (journal plugins, questionnaires); drafts stay staff-only.
            status = "published"
            pids = [None]
        elif actor.role == "patient":
            allowed = self._patient_ids(actor)
            if patient_id and patient_id not in allowed:
                raise PermissionDenied("A patient may only read their own record (or one they are guardian for)")
            pids = [patient_id] if patient_id else sorted(allowed)
        else:
            pids = [patient_id]
        out = []
        for pid in pids:
            for doc in self.store.list(kind, actor.site, pid, **{k: v for k, v in where.items() if v is not None}):
                if (status and doc.get("status") != status) or (doc.get("deleted") and not include_deleted):
                    continue
                try:
                    self._authorize(actor, spec, "read", doc)
                except PermissionDenied:
                    if actor.actor_type == "agent":
                        raise
                    continue
                out.append(self._load(spec, doc))
        return out

    def update(self, actor: Actor, kind: str, id: str, patch: Union[BaseModel, dict]) -> Instance:
        spec = spec_for(kind)
        rec = self._raw(actor, kind, id)
        if isinstance(patch, BaseModel):
            patch = patch.model_dump(exclude_unset=True)
        try:
            changes = spec.update_model(**patch).model_dump(exclude_unset=True)
        except ValidationError as exc:
            raise Invalid(str(exc)) from exc
        if not changes:
            return self._load(spec, rec)
        if rec["status"] in spec.frozen_states:
            raise InvariantViolation(f"{kind} '{id}' is {rec['status']} and cannot be changed")
        merged = {**rec, **changes}
        self._authorize(actor, spec, "update", merged, existing=rec)
        hook = getattr(self, f"_preupdate_{kind}", None)
        if hook:
            merged = hook(actor, rec, merged) or merged
        merged.update(updated_at=utcnow(), updated_by=actor.id)
        new = self._load(spec, merged)
        self._put(kind, new)
        self._audit(actor, self._pid(new), f"{kind}.updated", kind, id, {"fields": sorted(changes)})
        return new

    def transition(self, actor: Actor, kind: str, id: str, to: str, reason: Optional[str] = None, *,
                   _approved: bool = False, _depth: int = 0) -> Instance:
        """Move a record to state ``to``. Returns the record, or an :class:`Approval` when the state is gated."""
        spec = spec_for(kind)
        if kind in ("approval", "proposal"):
            raise InvariantViolation(f"Use decide/accept/reject for {kind}s")
        rec = self._raw(actor, kind, id)
        frm = rec["status"]
        if not spec.can_move(frm, to):
            allowed = list(spec.transitions.get(frm, ()))
            raise InvariantViolation(f"{kind} cannot move from '{frm}' to '{to}'. Allowed: {allowed}")
        self._authorize(actor, spec, "transition", rec, to=to)
        check = getattr(self, f"_check_{kind}", None)
        if check:
            check(actor, rec, to)
        if not _approved and to in self._gated_states(spec, rec):
            return self._request_approval(actor, kind, rec, to, reason)
        rec = {**rec, "status": to, "updated_at": utcnow(), "updated_by": actor.id}
        moved = getattr(self, f"_moved_{kind}", None)
        if moved:
            rec = moved(actor, rec, frm, to) or rec
        new = self._load(spec, rec)
        self._put(kind, new)
        self._audit(actor, self._pid(new), f"{kind}.{to}", kind, id, {"from": frm, "reason": reason})
        after = getattr(self, f"_after_{kind}", None)
        if after:
            after(actor, new, frm, to, _depth)
        self._react(kind, new, to, _depth)
        return self.get(system_actor(actor.site, "read"), kind, id)

    def delete(self, actor: Actor, kind: str, id: str, reason: str) -> Instance:
        """Soft delete. Records in a frozen state, and clinical data (use entered_in_error), cannot be deleted."""
        spec = spec_for(kind)
        rec = self._raw(actor, kind, id)
        if not spec.deletable:
            raise InvariantViolation(f"{kind} records are never deleted; move them to an error/closed state instead")
        if rec["status"] in spec.frozen_states:
            raise InvariantViolation(f"{kind} '{id}' is {rec['status']} and cannot be deleted")
        if not reason or len(reason) < 3:
            raise Invalid("A reason is required to delete")
        self._authorize(actor, spec, "update", rec, existing=rec)
        rec.update(deleted=True, updated_at=utcnow(), updated_by=actor.id)
        self._put(kind, self._load(spec, rec))
        self._audit(actor, rec.get("patient_id"), f"{kind}.deleted", kind, id, {"reason": reason})
        return self._load(spec, rec)

    # ════════════════════════════ proposals and approvals ════════════════════════════
    def propose(self, actor: Actor, op: str, target_kind: str, data: Optional[dict] = None, *,
                target_id: Optional[str] = None, to_state: Optional[str] = None, rationale: Optional[str] = None,
                origin: Optional[str] = None, patient_id: Optional[str] = None) -> Instance:
        spec_for(target_kind)
        data = data or {}
        pid = patient_id or data.get("patient_id")
        if target_id and not pid:
            doc = self.store.get(target_kind, target_id, actor.site)
            pid = doc.get("patient_id") if doc else None
        origin = origin or (f"agent:{actor.id}" if actor.actor_type == "agent" else f"{actor.role}:{actor.id}")
        return self.create(actor, "proposal", {"op": op, "target_kind": target_kind, "target_id": target_id,
                                               "data": data, "to_state": to_state, "origin": origin,
                                               "rationale": rationale, "patient_id": pid})

    def accept_proposal(self, actor: Actor, proposal_id: str, edits: Optional[dict] = None) -> Instance:
        """A clinician accepts a proposal; the change is made under their name. ``edits`` amend the data first."""
        self._require_decider(actor)
        p = self._raw(actor, "proposal", proposal_id)
        if p["status"] != "pending":
            raise InvariantViolation(f"Proposal is already {p['status']}")
        data = {**p["data"], **(edits or {})}
        if p["op"] == "create":
            result = self.create(actor, p["target_kind"], data)
        elif p["op"] == "transition":
            result = self.transition(actor, p["target_kind"], p["target_id"], p["to_state"], p.get("rationale"))
        else:
            result = self.update(actor, p["target_kind"], p["target_id"], data)
        p.update(status="accepted", result_id=result.id, decided_by=actor.id, updated_at=utcnow(), updated_by=actor.id)
        self._put("proposal", self._load(SPECS["proposal"], p))
        self._audit(actor, p.get("patient_id"), "proposal.accepted", "proposal", proposal_id,
                    {"target": p["target_kind"], "result_id": result.id})
        return self._load(SPECS["proposal"], p)

    def reject_proposal(self, actor: Actor, proposal_id: str, reason: str) -> Instance:
        if actor.actor_type == "agent" or acting_role(actor) not in CLINICAL_DECIDERS | {"coordinator"}:
            raise PermissionDenied("Only a clinician or coordinator may reject a proposal")
        p = self._raw(actor, "proposal", proposal_id)
        if p["status"] != "pending":
            raise InvariantViolation(f"Proposal is already {p['status']}")
        p.update(status="rejected", decided_by=actor.id, updated_at=utcnow(), updated_by=actor.id)
        self._put("proposal", self._load(SPECS["proposal"], p))
        self._audit(actor, p.get("patient_id"), "proposal.rejected", "proposal", proposal_id, {"reason": reason})
        return self._load(SPECS["proposal"], p)

    def decide_approval(self, actor: Actor, approval_id: str, approve: bool, note: Optional[str] = None) -> Instance:
        """Approve or reject a gated transition. Nobody decides their own request."""
        self._require_decider(actor)
        a = self._raw(actor, "approval", approval_id)
        if a["status"] != "pending":
            raise InvariantViolation(f"Approval is already {a['status']}")
        if a["created_by"] == actor.id:
            raise PermissionDenied("Nobody may approve their own request")
        if approve:
            self.transition(actor, a["subject_kind"], a["subject_id"], a["to_state"], note, _approved=True)
        a.update(status="approved" if approve else "rejected", decided_by=actor.id, decision_note=note,
                 updated_at=utcnow(), updated_by=actor.id)
        self._put("approval", self._load(SPECS["approval"], a))
        self._audit(actor, a.get("patient_id"), f"approval.{a['status']}", "approval", approval_id,
                    {"subject": f"{a['subject_kind']}:{a['subject_id']}", "to": a["to_state"]})
        return self._load(SPECS["approval"], a)

    def _request_approval(self, actor, kind, rec, to, reason):
        existing = [a for a in self.store.list("approval", actor.site, rec.get("patient_id"), subject_id=rec["id"])
                    if a["status"] == "pending" and a["to_state"] == to]
        if existing:
            return self._load(SPECS["approval"], existing[0])
        a = SPECS["approval"].model(id=new_id("apr"), site=actor.site, created_by=actor.id, status="pending",
                                    patient_id=rec.get("patient_id"), subject_kind=kind, subject_id=rec["id"],
                                    to_state=to, reason=reason)
        self._put("approval", a)
        self._audit(actor, rec.get("patient_id"), "approval.requested", "approval", a.id,
                    {"subject": f"{kind}:{rec['id']}", "to": to})
        return a

    def _gated_states(self, spec: ConceptSpec, rec: dict) -> set:
        gated = set(spec.approval_states)
        if rec.get("definition_id"):
            d = self.store.get("definition", rec["definition_id"], rec["site"])
            if d:
                gated |= set(d["config"].get("approval_required_for") or [])
        return gated

    # ════════════════════════════ reactions ════════════════════════════
    def _react(self, kind: str, rec: Instance, state: str, depth: int):
        if kind in ("definition", "approval", "proposal"):
            return
        reactions = [d for d in self.store.list("definition", rec.site, None, def_kind="reaction")
                     if d["status"] == "published" and not d.get("deleted")
                     and d["config"]["when_kind"] == kind and d["config"]["when_state"] == state]
        src = rec.model_dump(mode="json")
        for r in reactions:
            cond = r["config"].get("condition")
            if cond:
                try:
                    if not engine.safe_eval(cond, _Facts({k: v for k, v in src.items() if not isinstance(v, (dict, list))})):
                        continue
                except Exception:
                    continue
            for eff in r["config"]["effects"]:
                self.apply_effect(Effect(**eff), src, f"reaction:{r['key']}", depth)
        if kind != "journey":
            self._advance_workflows(kind, src, state, depth)

    def apply_effect(self, effect: Effect, source: dict, origin: str, depth: int = 0) -> Optional[Instance]:
        """Apply one effect from ``source``. Clinical effects become proposals; the rest apply as the system actor."""
        site = source["site"]
        sysa = system_actor(site)
        if depth >= self.max_reaction_depth:
            self._audit(sysa, source.get("patient_id"), "reaction.depth_limit", "effect", origin, {"kind": effect.kind})
            return None

        def resolve(v):
            if isinstance(v, str) and v.startswith("$"):
                cur: Any = source
                for part in v[1:].split("."):
                    cur = cur.get(part) if isinstance(cur, dict) else None
                return cur
            if isinstance(v, dict):
                return {k: resolve(x) for k, x in v.items()}
            if isinstance(v, list):
                return [resolve(x) for x in v]
            return v

        data = resolve(effect.data)
        spec = spec_for(effect.kind)
        if "patient_id" in spec.model.model_fields and "patient_id" not in data and source.get("patient_id"):
            data["patient_id"] = source["patient_id"]
        if effect.op == "create":
            if spec.is_clinical(data):
                return self.propose(sysa, "create", effect.kind, data, origin=origin, rationale=effect.rationale)
            return self.create(sysa, effect.kind, data, _depth=depth + 1)
        target = resolve(effect.target_id) or source["id"]
        if spec.is_clinical(data) or effect.to in spec.clinical_to:
            return self.propose(sysa, "transition", effect.kind, target_id=target, to_state=effect.to, origin=origin,
                                rationale=effect.rationale, patient_id=source.get("patient_id"))
        return self.transition(sysa, effect.kind, target, effect.to, effect.rationale, _depth=depth + 1)

    def _spawn(self, actor: Actor, kind: str, data: dict, origin: str, depth: int, rationale: Optional[str] = None):
        """Create a consequence: clinical ones are proposed, the rest created by the system."""
        return self.apply_effect(Effect(kind=kind, data=data, rationale=rationale),
                                 {"site": actor.site, "id": origin, "patient_id": data.get("patient_id")}, origin, depth)

    # ════════════════════════════ permissions ════════════════════════════
    def _patient_ids(self, actor: Actor) -> set:
        ids = {actor.patient_id} if actor.patient_id else set()
        for pid in actor.represents:
            rels = self.store.list("relationship", actor.site, pid, related_patient_id=actor.patient_id)
            if any(r["status"] == "active" and (r["is_guardian"] or r["is_care_taker"]) for r in rels):
                ids.add(pid)
        return ids

    def _has_consent(self, site: str, patient_id: str, scope: str) -> bool:
        now = utcnow()
        for c in self.store.list("consent", site, patient_id, consent_scope=scope):
            if c["status"] == "granted" and (not c.get("valid_until") or _aware(c["valid_until"]) > now):
                return True
        return False

    def require_consent(self, actor: Actor, patient_id: str, scope: str):
        if not self._has_consent(actor.site, patient_id, scope):
            raise PermissionDenied(f"No active '{scope}' consent for patient {patient_id}")

    def _require_decider(self, actor: Actor):
        if actor.actor_type == "agent":
            raise PermissionDenied("An AI agent may propose but never decide. A clinician must do this.")
        if acting_role(actor) not in CLINICAL_DECIDERS:
            raise PermissionDenied(f"Role '{actor.role}' may not make clinical decisions")

    def _authorize(self, actor: Actor, spec: ConceptSpec, op: str, doc: dict, to: Optional[str] = None,
                   existing: Optional[dict] = None):
        pid = doc.get("patient_id")
        if actor.role == "patient":
            if pid is None and spec.scope != "global" and op != "read":
                raise PermissionDenied("A patient may only act on their own record")
            if pid is not None and pid not in self._patient_ids(actor):
                raise PermissionDenied("A patient may only act on their own record (or one they are guardian for)")
            if op == "read" and spec.kind == "clinical_document" and not (doc.get("shareable_with_patient") and doc.get("status") == "ready"):
                raise PermissionDenied("This document is not shared with the patient")
            if op == "read" and spec.kind in ("definition", "clinical_setting", "provider", "care_team_member"):
                return
            if op == "read" and spec.scope == "global":
                raise PermissionDenied("Patients cannot read this")
        if actor.actor_type == "agent" and pid and not self._has_consent(actor.site, pid, "ai"):
            raise PermissionDenied(f"AI agents need the patient's 'ai' consent to access patient {pid}")
        if op == "read":
            return
        if actor.role == "system":
            if spec.kind == "regimen" and op == "create":
                return
            if spec.is_clinical(doc) or (to and to in spec.clinical_to):
                raise PermissionDenied(f"Automatic reactions cannot make a clinical decision ({spec.kind})")
            return
        if actor.actor_type == "agent":
            if op == "create" and spec.kind == "regimen":
                return
            if op == "create" and spec.agent_can_create and not spec.is_clinical(doc):
                return
            if op == "update" and existing and existing.get("created_by") == actor.id and existing["status"] == spec.initial:
                return
            raise PermissionDenied(f"An AI agent may propose but never perform this on {spec.kind}. A person must do this.")
        if actor.role == "patient":
            if op == "create" and spec.patient_can_create:
                return
            if op == "update" and existing and existing.get("created_by") == actor.id:
                return
            if op == "transition" and spec.kind in ("consent", "conversation", "journal_entry"):
                return
            raise PermissionDenied(f"Patients may not {op} {spec.kind}")
        if actor.role not in spec.write_roles and acting_role(actor) not in spec.write_roles:
            raise PermissionDenied(f"Role '{actor.role}' may not {op} {spec.kind}")
        if acting_role(actor) == "admin" and (spec.is_clinical(doc) or (to and to in spec.clinical_to)):
            raise PermissionDenied("Administrators cannot write clinical records")
        if spec.is_clinical(doc) or (to and to in spec.clinical_to):
            self._require_decider(actor)

    _LINKS = {"patient_id": "patient", "encounter_id": "encounter", "careplan_id": "careplan",
              "care_action_id": "care_action", "record_id": "clinical_record",
              "cohort_membership_id": "cohort_membership", "related_patient_id": "patient"}

    def _require_known_links(self, actor: Actor, kind: str, data: dict):
        if kind in ("definition", "proposal", "approval"):
            return
        for field, target in self._LINKS.items():
            if kind == "patient" and field == "patient_id":
                continue
            ref = data.get(field)
            if not isinstance(ref, str) or not ref:
                continue
            doc = self.store.get(target, ref, actor.site)
            if not doc or doc.get("deleted"):
                other = self.store.site_of(target, ref) if hasattr(self.store, "site_of") else None
                if other and other not in (actor.site, LIBRARY_SITE):
                    raise PermissionDenied(f"{field} belongs to another site")
                raise NotFound(f"No {target} '{ref}'")
            if field == "patient_id" and doc.get("status") in ("deceased", "inactive") and kind not in (
                    "consent", "clinical_document", "relationship"):
                raise InvariantViolation(f"Patient is {doc['status']}; new {kind} records are not accepted")

    def _require_reviewed(self, definition):
        if not definition or os.environ.get("CLINESTRA_MODE") != "production":
            return
        if definition.get("review_status", "draft") != "approved":
            raise InvariantViolation(
                f"Definition '{definition.get('key')}' is not clinically approved and cannot be used in production")

    def review_definition(self, actor: Actor, definition_id: str, decision: str, notes: Optional[str] = None,
                          review_ref: Optional[str] = None) -> Instance:
        if actor.actor_type != "human" or actor.role not in ("admin", "clinical_lead"):
            raise PermissionDenied("Only an administrator or clinical lead may review a definition")
        if decision not in ("approve", "reject"):
            raise Invalid("decision must be approve or reject")
        rec = self._raw(actor, "definition", definition_id)
        if rec.get("created_by") == actor.id:
            raise PermissionDenied("Nobody may approve their own request")
        rec["review_status"] = "approved" if decision == "approve" else "rejected"
        rec["reviewed_by"] = actor.id
        rec["reviewed_at"] = utcnow()
        rec["review_ref"] = review_ref
        rec["review_notes"] = notes
        rec.update(updated_at=utcnow(), updated_by=actor.id)
        self._put("definition", self._load(SPECS["definition"], rec))
        self._audit(actor, None, f"definition.review_{rec['review_status']}", "definition", definition_id,
                    {"decision": decision, "review_ref": review_ref})
        return self.get(system_actor(actor.site, "read"), "definition", definition_id)

    def record_run(self, actor: Actor, **fields) -> str:
        usage = str(uuid.uuid4())
        doc = {"id": usage, "site": actor.site, "usage_id": usage, "actor_id": actor.id,
               "actor_type": actor.actor_type, "created_at": utcnow().isoformat(), **fields}
        self.store.put("run", doc)
        return usage

    def _citations_ok(self, config: dict) -> bool:
        evidence = (config or {}).get("evidence")
        if not isinstance(evidence, list) or not evidence:
            return False
        return all(isinstance(item, dict) and item.get("id") and item.get("title") and item.get("source_type")
                   for item in evidence)

    # ════════════════════════════ concept behaviour ════════════════════════════
    # patient
    def _pre_patient(self, actor, d, definition):
        if not d.get("date_of_birth") and d.get("age_years") is None:
            raise Invalid("Either date_of_birth or age_years is required")
        if d.get("age_years") is not None and not d.get("age_recorded_on"):
            d["age_recorded_on"] = date.today()
        self._check_demographics(actor.site, d)
        return d

    def _preupdate_patient(self, actor, old, new):
        self._check_demographics(actor.site, new)
        return new

    def _check_demographics(self, site, d):
        for k, v in (d.get("demographics") or {}).items():
            spec = STANDARD_DEMOGRAPHICS.get(k)
            if spec is None:
                raise Invalid(f"Unknown demographic '{k}'. Standard fields: {sorted(STANDARD_DEMOGRAPHICS)}; "
                              "site-specific ones go in custom_fields")
            if spec["type"] == "choice" and v not in spec["options"]:
                raise Invalid(f"{k} must be one of {spec['options']}")
        try:
            cfg = self.resolve_definition(site, "patient_fields", "default")["config"]
        except NotFound:
            return
        for f in cfg.get("standard", []):
            if f["required"] and f["enabled"] and (d.get("demographics") or {}).get(f["key"]) in (None, ""):
                raise Invalid(f"Demographic '{f['key']}' is required at this site")
        custom = {f["key"]: f for f in cfg.get("custom", []) if f["enabled"]}
        for k in d.get("custom_fields") or {}:
            if k not in custom:
                raise Invalid(f"Unknown custom field '{k}'. Configured: {sorted(custom)}")
        for k, f in custom.items():
            v = (d.get("custom_fields") or {}).get(k)
            if f["required"] and v in (None, ""):
                raise Invalid(f"Custom field '{k}' is required at this site")
            if v is not None and f["field_type"] == "choice" and v not in f["options"]:
                raise Invalid(f"{k} must be one of {f['options']}")

    def _post_patient(self, actor, rec, definition, depth):
        data = {"patient_id": rec.id, "name": "General", "profile_type": "general"}
        try:
            self.resolve_definition(actor.site, "health_profile", "general")
            data["definition_key"] = "general"
        except NotFound:
            pass
        self._spawn(actor, "health_profile", data, f"patient:{rec.id}", depth)

    # relationship
    def _pre_relationship(self, actor, d, definition):
        if d["patient_id"] == d["related_patient_id"]:
            raise Invalid("A patient cannot be related to themselves")
        for pid in (d["patient_id"], d["related_patient_id"]):
            self._raw(system_actor(actor.site, "read"), "patient", pid)
        return d

    def _post_relationship(self, actor, rec, definition, depth):
        flags = {k: True for k in ("is_guardian", "is_care_taker") if getattr(rec, k)}
        if flags:
            other = self._raw(system_actor(actor.site, "read"), "patient", rec.related_patient_id)
            other.update(flags)
            self._put("patient", self._load(SPECS["patient"], other))

    # care team: the primary care provider is the member in the 'primary' role
    def _pre_care_team(self, actor, d, definition):
        self._raw(system_actor(actor.site, "read"), "patient", d["patient_id"])
        self._check_team(actor.site, d, definition)
        return d

    def _preupdate_care_team(self, actor, old, new):
        d = self.store.get("definition", old["definition_id"], actor.site) if old.get("definition_id") else None
        self._check_team(actor.site, new, d)
        return new

    def _check_team(self, site, d, definition):
        roles = {r["key"]: r for r in (definition or {}).get("config", {}).get("roles", [])}
        primaries = 0
        for m in d.get("members") or []:
            m = m if isinstance(m, dict) else m.model_dump()
            if not self.store.get(m["person_kind"], m["person_id"], site):
                raise NotFound(f"No {m['person_kind']} '{m['person_id']}'")
            if roles and m["role"] not in roles:
                raise Invalid(f"Role '{m['role']}' is not in this care team definition: {sorted(roles)}")
            if m["role"] == "primary":
                if m["person_kind"] != "provider":
                    raise Invalid("The 'primary' role must be filled by a provider (primary care provider)")
                if not m.get("encounter_id"):
                    primaries += 1
        if primaries > 1:
            raise InvariantViolation("A care team has one primary care provider (per encounter overrides are allowed)")

    # clinical states: one current per general scope and per condition
    def _pre_clinical_state(self, actor, d, definition):
        if d["scope"] == "condition" and not d.get("condition_code"):
            raise Invalid("A condition clinical state needs condition_code")
        if d["scope"] == "general":
            d["condition_code"] = None
        return d

    def _post_clinical_state(self, actor, rec, definition, depth):
        for old in self.store.list("clinical_state", actor.site, rec.patient_id, scope=rec.scope):
            if old["id"] != rec.id and old["status"] == "current" and old.get("condition_code") == rec.condition_code:
                self._cascade(actor, "clinical_state", old["id"], "superseded", f"Superseded by {rec.id}")

    def current_states(self, actor: Actor, patient_id: str) -> List[Instance]:
        return self.list(actor, "clinical_state", patient_id, status="current")

    # outcomes
    def _after_outcome(self, actor, rec, frm, to, depth):
        if to == "achieved" and rec.on_achieved_state:
            data = {"patient_id": rec.patient_id, "scope": "condition" if rec.condition_code else "general",
                    "condition_code": rec.condition_code, "state": rec.on_achieved_state, "source_outcome_id": rec.id,
                    "basis": list(rec.evidence)}
            self.propose(system_actor(actor.site), "create", "clinical_state", data, origin=f"outcome:{rec.id}",
                         rationale=f"Outcome '{rec.name}' achieved")

    def evaluate_outcome(self, actor: Actor, outcome_id: str, now: Optional[datetime] = None) -> dict:
        """Trajectory of the outcome's measure against its target. Informs; never changes the outcome."""
        o = self.get(actor, "outcome", outcome_id)
        if not o.measure_code:
            raise Invalid("This outcome has no measure_code to evaluate")
        pts = [{"at": x.effective_at, "value": x.value, "source_id": x.id}
               for x in self.list(actor, "observation", o.patient_id, code=o.measure_code)
               if x.status != "entered_in_error" and isinstance(x.value, (int, float))]
        pts.sort(key=lambda p: _aware(p["at"]))
        return engine.compute_trajectory(pts, o.comparator, o.target, o.due_by, now or utcnow())

    # health profiles
    @staticmethod
    def _fill_health_profile(definition):
        cfg = definition["config"]
        return {"name": definition["name"], "profile_type": cfg.get("profile_type", "general"), "tags": cfg.get("tags") or {}}

    def _pre_health_profile(self, actor, d, definition):
        if definition:
            cfg = definition["config"]
            if cfg.get("ttl_days") and not d.get("expires_at"):
                d["expires_at"] = utcnow() + timedelta(days=cfg["ttl_days"])
        return d

    def profile_view(self, actor: Actor, profile_id: str) -> dict:
        """What is present and what is absent for this profile's tags, plus current states."""
        hp = self.get(actor, "health_profile", profile_id)
        pid = hp.patient_id
        obs = [o for o in self.list(actor, "observation", pid) if o.status != "entered_in_error"]
        conds = {c.code: c for c in self.list(actor, "condition", pid) if c.status in ("active", "remission")}
        meds = {m.code: m for m in self.list(actor, "medication", pid, status="active")}
        outs = {o.measure_code or o.name: o for o in self.list(actor, "outcome", pid)}
        measurements = []
        for code in hp.tags.measurements:
            series = sorted((o for o in obs if o.code == code), key=lambda o: _aware(o.effective_at))
            last = series[-1] if series else None
            measurements.append({"code": code, "present": bool(series), "count": len(series),
                                 "latest": last.value if last else None, "unit": last.unit if last else None,
                                 "at": last.effective_at.isoformat() if last else None})
        return {
            "profile": hp.model_dump(mode="json"),
            "states": [s.model_dump(mode="json") for s in self.current_states(actor, pid)],
            "conditions": [{"code": c, "present": c in conds, "status": conds[c].status if c in conds else None}
                           for c in hp.tags.conditions],
            "measurements": measurements,
            "medications": [{"code": m, "present": m in meds} for m in hp.tags.medications],
            "outcomes": [{"key": k, "present": k in outs, "status": outs[k].status if k in outs else None}
                         for k in hp.tags.outcomes],
        }

    def patient_summary(self, actor: Actor, patient_id: str) -> dict:
        """Profiles ordered pinned first, then those whose priority states match a current state."""
        p = self.get(actor, "patient", patient_id)
        states = {s.state for s in self.current_states(actor, patient_id)}
        profiles = [h for h in self.list(actor, "health_profile", patient_id) if h.status != "inactive"]
        profiles.sort(key=lambda h: (h.status != "pinned", not (set(h.priority_states) & states), h.created_at))
        return {"patient": p.model_dump(mode="json"), "profiles": [self.profile_view(actor, h.id) for h in profiles],
                "pending_actions": self.pending_actions(actor, patient_id)}

    # cohorts and protocols
    def patient_facts(self, actor: Actor, patient_id: str) -> _Facts:
        p = self.get(actor, "patient", patient_id)
        f = _Facts(age=p.age, gender=p.gender, country=p.country, is_guardian=p.is_guardian)
        for c in self.list(actor, "condition", patient_id):
            if c.status in ("active", "remission"):
                f[f"cond_{c.code.replace(':', '_').replace('-', '_')}"] = True
        for s in self.current_states(actor, patient_id):
            f[f"state_{s.state}"] = True
        for o in sorted(self.list(actor, "observation", patient_id), key=lambda o: _aware(o.effective_at)):
            if o.status != "entered_in_error" and isinstance(o.value, (int, float)):
                f[f"obs_{o.code.replace(':', '_').replace('-', '_')}"] = o.value
        return f

    def _rule(self, expr: Optional[str], facts: dict) -> bool:
        if not expr:
            return True
        try:
            return bool(engine.safe_eval(expr, facts))
        except TypeError:
            return False

    def _pre_cohort_membership(self, actor, d, definition):
        definition = definition or self.resolve_definition(actor.site, "cohort", d["cohort_key"])
        d["definition_key"] = d["cohort_key"] = definition["key"]
        if any(m["status"] == "active" for m in self.store.list("cohort_membership", actor.site, d["patient_id"],
                                                                 cohort_key=d["cohort_key"])):
            raise InvariantViolation(f"Patient is already in cohort '{d['cohort_key']}'")
        rule = definition["config"].get("rule")
        if rule and not d.get("overridden") and not self._rule(rule, self.patient_facts(actor, d["patient_id"])):
            raise Invalid(f"Patient does not match the cohort rule ({rule}). Set overridden=true with a reason to add anyway.")
        return d

    def _post_cohort_membership(self, actor, rec, definition, depth):
        cfg = (definition or self._pinned_definition(actor, rec, "cohort", rec.cohort_key))["config"]
        for eff in cfg.get("entry_actions") or []:
            self.apply_effect(Effect(**eff), rec.model_dump(mode="json"), f"cohort:{rec.cohort_key}", depth)

    def _after_cohort_membership(self, actor, rec, frm, to, depth):
        if to == "closed":
            cfg = self._pinned_definition(actor, rec, "cohort", rec.cohort_key)["config"]
            for eff in cfg.get("exit_actions") or []:
                self.apply_effect(Effect(**eff), rec.model_dump(mode="json"), f"cohort:{rec.cohort_key}", depth)

    def suggest_cohorts(self, actor: Actor, patient_id: str) -> List[Instance]:
        """Propose memberships for rule-based cohorts the patient matches but is not in. A clinician decides."""
        facts = self.patient_facts(actor, patient_id)
        current = {m.cohort_key for m in self.list(actor, "cohort_membership", patient_id, status="active")}
        pending = {p.data.get("cohort_key") for p in self.list(actor, "proposal", patient_id, status="pending",
                                                                target_kind="cohort_membership")}
        out = []
        seen = set()
        for d in self.store.list("definition", actor.site, None, def_kind="cohort"):
            cfg = d["config"]
            if d["status"] != "published" or d["key"] in seen or d["key"] in current | pending:
                continue
            seen.add(d["key"])
            if cfg.get("rule") and not cfg.get("manual_only") and self._rule(cfg["rule"], facts):
                out.append(self.propose(system_actor(actor.site), "create", "cohort_membership",
                                        {"patient_id": patient_id, "cohort_key": d["key"],
                                         "reason": f"Matches cohort rule: {cfg['rule']}"},
                                        origin=f"cohort_rule:{d['key']}", rationale=cfg["rule"]))
        return out

    def _pre_protocol_assignment(self, actor, d, definition):
        definition = definition or self.resolve_definition(actor.site, "protocol", d["protocol_key"])
        d["definition_key"] = d["protocol_key"] = definition["key"]
        if any(a["status"] == "assigned" for a in self.store.list("protocol_assignment", actor.site, d["patient_id"],
                                                                  protocol_key=d["protocol_key"])):
            raise InvariantViolation(f"Protocol '{d['protocol_key']}' is already assigned")
        crit = definition["config"].get("patient_criteria")
        if crit and not self._rule(crit, self.patient_facts(actor, d["patient_id"])):
            raise Invalid(f"Patient does not meet the protocol criteria ({crit})")
        return d

    def _post_protocol_assignment(self, actor, rec, definition, depth):
        """Instantiate the protocol's templates. Non-clinical ones are created; clinical ones are proposed."""
        definition = definition or self.resolve_definition(actor.site, "protocol", rec.protocol_key)
        cfg = definition["config"]
        origin = f"protocol:{rec.protocol_key}"
        templates = cfg.get("templates") or []
        plan_t = next((t for t in templates if t["kind"] == "careplan"), {"kind": "careplan", "data": {}})
        plan_data = {**plan_t.get("data", {}), "patient_id": rec.patient_id, "protocol_assignment_id": rec.id}
        if plan_t.get("definition_key"):
            plan_data["definition_key"] = plan_t["definition_key"]
        else:
            plan_data.setdefault("title", cfg.get("defaults", {}).get("title") or definition["name"])
        plan = self._spawn(actor, "careplan", plan_data, origin, depth)
        for t in templates:
            if t["kind"] == "careplan":
                continue
            spec = spec_for(t["kind"])
            data = {**t.get("data", {}), "patient_id": rec.patient_id}
            if t.get("definition_key"):
                data["definition_key"] = t["definition_key"]
            if "careplan_id" in spec.model.model_fields and isinstance(plan, SPECS["careplan"].model):
                data["careplan_id"] = plan.id
            if "protocol_assignment_id" in spec.model.model_fields:
                data["protocol_assignment_id"] = rec.id
            self._spawn(actor, t["kind"], data, origin, depth, rationale=f"From protocol {rec.protocol_key}")

    # careplans and care actions
    def _post_careplan(self, actor, rec, definition, depth):
        if not definition:
            return
        for ca in definition["config"].get("care_actions") or []:
            self._spawn(actor, "care_action", {**ca, "patient_id": rec.patient_id, "careplan_id": rec.id},
                        f"careplan:{rec.id}", depth)
        for t in definition["config"].get("tasks") or []:
            self._spawn(actor, "task", {**t, "patient_id": rec.patient_id, "careplan_id": rec.id},
                        f"careplan:{rec.id}", depth)

    def _after_careplan(self, actor, rec, frm, to, depth):
        if to == "active":
            for ca in self.list(actor, "care_action", rec.patient_id, careplan_id=rec.id, status="planned"):
                self._cascade(actor, "care_action", ca.id, "active", "Careplan activated")
        elif to in ("completed", "cancelled"):
            for ca in self.list(actor, "care_action", rec.patient_id, careplan_id=rec.id):
                if ca.status in ("planned", "active"):
                    self._cascade(actor, "care_action", ca.id, "cancelled" if to == "cancelled" else "completed",
                                  f"Careplan {to}")

    def _pre_care_action(self, actor, d, definition):
        if d.get("careplan_id"):
            plan = self._raw(system_actor(actor.site, "read"), "careplan", d["careplan_id"])
            if plan["patient_id"] != d["patient_id"]:
                raise Invalid("Careplan belongs to another patient")
            if plan["status"] in ("completed", "cancelled"):
                raise InvariantViolation(f"Careplan is {plan['status']}")
        if d.get("order_status") == "sent" and actor.actor_type != "device":
            raise PermissionDenied("Only an integration actor can mark an order sent")
        if d.get("schedule") and not d["schedule"].get("anchor"):
            d["schedule"]["anchor"] = utcnow()
        return d

    def schedule_status(self, actor: Actor, care_action_id: str, now: Optional[datetime] = None) -> dict:
        ca = self.get(actor, "care_action", care_action_id)
        if not ca.schedule:
            return {"occurrences": [], "counts": {"done": 0, "due": 0, "overdue": 0, "missed": 0},
                    "next_due": None, "adherence": None}
        s = ca.schedule
        return engine.schedule_status(s.anchor or ca.created_at, s.every_days, list(ca.completions), now or utcnow(),
                                      s.grace_days, s.until)

    def _counter_sign_required(self, site: str) -> bool:
        return os.environ.get("CLINESTRA_REGIMEN_COUNTER_SIGN", "required") != "off"

    def _check_regimen(self, actor, rec, to):
        if actor.actor_type in ("agent", "system") or actor.role == "system":
            raise PermissionDenied("An agent or system actor cannot sign a prescription")
        if to in ("signed", "counter_signed", "active") and acting_role(actor) != "clinician":
            raise PermissionDenied("Only a clinician can sign a prescription")
        if to == "counter_signed" and actor.id == rec.get("signed_by"):
            raise PermissionDenied("A different clinician must counter-sign")
        if to == "active" and rec.get("status") == "proposed" and self._counter_sign_required(actor.site):
            raise InvariantViolation("Counter-signing is required before a regimen can become active")

    def _moved_regimen(self, actor, rec, frm, to):
        if to == "signed":
            rec["signed_by"] = actor.id
        if to == "counter_signed":
            rec["counter_signed_by"] = actor.id
        return rec

    def _post_regimen(self, actor, rec, definition, depth):
        if rec.supersedes:
            old = self._raw(actor, "regimen", rec.supersedes)
            if old["status"] == "active":
                self.transition(actor, "regimen", rec.supersedes, "superseded", f"Superseded by {rec.id}")

    def _after_regimen(self, actor, rec, frm, to, depth):
        if to == "active":
            for drug in rec.drugs:
                self.create(actor, "medication", {"patient_id": rec.patient_id, "code": drug.code, "display": drug.display,
                                                  "dose": drug.dose, "frequency": drug.frequency, "regimen_id": rec.id,
                                                  "source": "regimen", "signed_by": rec.signed_by,
                                                  "counter_signed_by": rec.counter_signed_by, "start": utcnow()},
                            _depth=depth + 1)
            for order in rec.orders:
                self.create(actor, "care_action", {"patient_id": rec.patient_id, "action_type": "order",
                                                   "title": order.display or order.code, "code": order.code,
                                                   "order_kind": order.order_kind or "other", "order_status": "proposed",
                                                   "details": {"suggested": True, "not_sent": True}}, _depth=depth + 1)
            return
        if to in ("stopped", "superseded"):
            for m in self.list(actor, "medication", rec.patient_id, regimen_id=rec.id, status="active"):
                self._cascade(actor, "medication", m.id, "stopped", f"Regimen {to}")

    # journeys: definition-driven states
    @staticmethod
    def _fill_journey(definition):
        return {"association_type": definition["config"].get("association_type", "care_program")}

    def _pre_journey(self, actor, d, definition):
        if not definition:
            raise Invalid("A journey needs a definition_key (journey definitions hold the states)")
        return d

    def _post_journey(self, actor, rec, definition, depth):
        self.move_journey(system_actor(actor.site), rec.id, definition["config"]["initial"], "Journey started",
                          _depth=depth)

    def move_journey(self, actor: Actor, journey_id: str, to: str, reason: Optional[str] = None, *,
                     force: bool = False, _depth: int = 0) -> Instance:
        """Move a journey to another state. ``force`` (care-team staff, with a reason) skips the allowed transitions
        and the state's requirements; the step is recorded as forced."""
        j = self._raw(actor, "journey", journey_id)
        self._authorize(actor, SPECS["journey"], "update", j, existing=j)
        if j["status"] != "active":
            raise InvariantViolation(f"Journey is {j['status']}")
        cfg = self.store.get("definition", j["definition_id"], actor.site)["config"]
        if to not in cfg["states"]:
            raise Invalid(f"'{to}' is not a state of this journey: {cfg['states']}")
        if force and (actor.role in ("patient", "system") or actor.actor_type == "agent" or not (reason or "").strip()):
            raise PermissionDenied("Only care-team staff may force a move, and a reason is required")
        cur = j.get("state")
        allowed = cfg.get("transitions", {}).get(cur) if cur else None
        forced = False
        if cur and allowed is not None and to not in allowed:
            if not force:
                raise InvariantViolation(f"Journey cannot move from '{cur}' to '{to}'. Allowed: {allowed}")
            forced = True
        if cur and cur != to:
            unmet = self.workflow_unmet(actor.site, j, cur)
            if unmet:
                if not force:
                    raise InvariantViolation("Finish first: " + "; ".join(unmet))
                forced = True
        j["state"] = to
        j["history"] = (j.get("history") or []) + [{"state": to, "at": utcnow(), "by": actor.id, "reason": reason, "forced": forced}]
        j.update(updated_at=utcnow(), updated_by=actor.id)
        if to in cfg.get("terminal", []):
            j["status"] = "completed"
        new = self._load(SPECS["journey"], j)
        self._put("journey", new)
        self._audit(actor, new.patient_id, "journey.moved", "journey", journey_id, {"from": cur, "to": to, "forced": forced})
        src = new.model_dump(mode="json")
        if cur and cur != to:
            for eff in (cfg.get("on_exit") or {}).get(cur, []):
                self.apply_effect(Effect(**eff), src, f"journey:{j['definition_key']}", _depth)
        for eff in (cfg.get("on_enter") or {}).get(to, []):
            self.apply_effect(Effect(**eff), src, f"journey:{j['definition_key']}", _depth)
        return new

    # workflows: journeys tied to an encounter (stages, gates, requirements, automatic moves)
    def _encounter_of(self, site: str, kind: str, rec: dict) -> Optional[str]:
        if kind == "encounter":
            return rec.get("id")
        if rec.get("encounter_id"):
            return rec["encounter_id"]
        if kind == "note_page" and rec.get("record_id"):
            r = self.store.get("clinical_record", rec["record_id"], site)
            return (r or {}).get("encounter_id")
        if kind == "journey" and rec.get("association_type") == "encounter":
            return rec.get("association_id")
        return None

    def _encounter_workflows(self, site: str, encounter_id: str) -> List[tuple]:
        """Active journeys of an encounter, with their definition config."""
        out = []
        enc = self.store.get("encounter", encounter_id, site)
        if not enc:
            return out
        for j in self.store.list("journey", site, enc["patient_id"], association_id=encounter_id):
            if j.get("status") == "active" and not j.get("deleted") and j.get("definition_id"):
                d = self.store.get("definition", j["definition_id"], site)
                if d:
                    out.append((j, d["config"]))
        return out

    def workflow_unmet(self, site: str, journey: dict, state: str) -> List[str]:
        """Requirements of ``state`` not yet met for the journey's encounter, as labels."""
        d = self.store.get("definition", journey["definition_id"], site) if journey.get("definition_id") else None
        reqs = ((d or {}).get("config") or {}).get("requires", {}).get(state) or []
        enc_id = journey.get("association_id") if journey.get("association_type") == "encounter" else None
        if not reqs or not enc_id:
            return []
        unmet = []
        for r in reqs:
            docs = [x for x in self.store.list(r["kind"], site, journey["patient_id"])
                    if not x.get("deleted") and self._encounter_of(site, r["kind"], x) == enc_id
                    and (not r.get("definition_key") or x.get("definition_key") == r["definition_key"])]
            if not docs or (r.get("state") and not any(x.get("status") == r["state"] for x in docs)):
                what = r.get("label") or " ".join(x for x in (r["kind"].replace("_", " "), r.get("definition_key"),
                                                               r.get("state") or "recorded") if x)
                unmet.append(what)
        return unmet

    def check_gate(self, site: str, encounter_id: Optional[str], operation: str) -> None:
        """Refuse ``operation`` when the encounter's workflow stage does not allow it."""
        if not encounter_id:
            return
        for j, cfg in self._encounter_workflows(site, encounter_id):
            allowed = (cfg.get("gates") or {}).get(j.get("state"))
            if allowed is not None and operation not in allowed:
                label = ((cfg.get("stages") or {}).get(j.get("state")) or {}).get("label") or j.get("state")
                raise InvariantViolation(f"Not allowed while the encounter is at '{label}' ({operation.replace('_', ' ')})")

    def _advance_workflows(self, kind: str, rec: dict, state: str, depth: int) -> None:
        enc_id = self._encounter_of(rec["site"], kind, rec)
        if not enc_id:
            return
        sysa = system_actor(rec["site"], "workflow")
        for j, cfg in self._encounter_workflows(rec["site"], enc_id):
            cur = j.get("state")
            states = cfg["states"]
            for rule in cfg.get("advance_on") or []:
                if rule["when_kind"] != kind or rule["when_state"] != state or rule["to"] == cur:
                    continue
                if rule.get("definition_key") and rec.get("definition_key") != rule["definition_key"]:
                    continue
                if rule.get("from_states"):
                    if cur not in rule["from_states"]:
                        continue
                elif cur in states and rule["to"] in states and states.index(rule["to"]) <= states.index(cur):
                    continue  # by default only move forward
                try:
                    self.move_journey(sysa, j["id"], rule["to"], f"Automatic: {kind.replace('_', ' ')} {state}", _depth=depth + 1)
                except ClinestraError as e:
                    self._audit(sysa, j["patient_id"], "journey.advance_skipped", "journey", j["id"], {"to": rule["to"], "why": str(e)})
                break

    # encounters and clinical records
    def _post_encounter(self, actor, rec, definition, depth):
        wf = (definition or {}).get("config", {}).get("workflow")
        if wf:
            self._spawn(actor, "journey", {"patient_id": rec.patient_id, "definition_key": wf, "association_type": "encounter",
                                           "association_id": rec.id}, f"encounter:{rec.id}", depth)
        pages = (definition or {}).get("config", {}).get("note_pages") or []
        if pages:
            r = self._spawn(actor, "clinical_record", {"patient_id": rec.patient_id, "encounter_id": rec.id,
                                                       "title": f"{rec.encounter_type} record"}, f"encounter:{rec.id}", depth)
            for key in pages:
                self._spawn(actor, "note_page", {"patient_id": rec.patient_id, "record_id": r.id, "definition_key": key},
                            f"encounter:{rec.id}", depth)

    def _pre_clinical_record(self, actor, d, definition):
        if d.get("encounter_id"):
            enc = self._raw(system_actor(actor.site, "read"), "encounter", d["encounter_id"])
            if enc["patient_id"] != d["patient_id"]:
                raise Invalid("Encounter belongs to another patient")
        else:
            enc = self.create(actor if actor.role != "patient" else system_actor(actor.site), "encounter",
                              {"patient_id": d["patient_id"], "encounter_type": "unscheduled", "start_at": utcnow()})
            d["encounter_id"] = enc.id
        return d

    def _gate(self, actor, encounter_id, operation):
        if actor.role != "system":  # automatic consequences are not gated
            self.check_gate(actor.site, encounter_id, operation)

    def _check_encounter(self, actor, rec, to):
        if to == "closed":
            self._gate(actor, rec["id"], "close_encounter")

    def _check_clinical_record(self, actor, rec, to):
        if to == "finalized":
            self._gate(actor, rec.get("encounter_id"), "finalize_record")
            open_pages = [p for p in self.store.list("note_page", actor.site, rec["patient_id"], record_id=rec["id"])
                          if p["status"] != "finalized" and not p.get("deleted")]
            if open_pages:
                raise InvariantViolation(f"Finalize every page first ({len(open_pages)} not finalized)")

    # note pages
    @staticmethod
    def _fill_note_page(definition):
        cfg = definition["config"]
        return {"page_type": cfg.get("page_type", "case_sheet"), "title": definition["name"], "chart": cfg.get("chart") or {}}

    def _pre_note_page(self, actor, d, definition):
        rec = self._raw(system_actor(actor.site, "read"), "clinical_record", d["record_id"])
        if rec["patient_id"] != d["patient_id"]:
            raise Invalid("Clinical record belongs to another patient")
        if rec["status"] == "finalized":
            raise InvariantViolation("The clinical record is finalized")
        if definition:
            self._check_blocks(definition["config"], d.get("blocks") or {})
        if actor.actor_type == "agent":
            d["ai_drafted"] = True
        return d

    def _preupdate_note_page(self, actor, old, new):
        self._gate(actor, self._encounter_of(actor.site, "note_page", old), "case_sheet")
        if old.get("definition_id"):
            self._check_blocks(self.store.get("definition", old["definition_id"], actor.site)["config"], new["blocks"])
        return new

    @staticmethod
    def _check_blocks(cfg, blocks):
        known = {b["key"] for b in cfg.get("blocks", [])}
        if known and set(blocks) - known:
            raise Invalid(f"Unknown blocks {sorted(set(blocks) - known)}. This page has: {sorted(known)}")

    def _check_note_page(self, actor, rec, to):
        self._gate(actor, self._encounter_of(actor.site, "note_page", rec), "case_sheet")
        if to in ("approved", "finalized") and rec.get("definition_id"):
            cfg = self.store.get("definition", rec["definition_id"], actor.site)["config"]
            missing = [b["key"] for b in cfg.get("blocks", []) if b["required"] and rec["blocks"].get(b["key"]) in (None, "")]
            if missing:
                raise Invalid(f"Required blocks missing: {missing}")

    def _after_note_page(self, actor, rec, frm, to, depth):
        if to != "finalized" or not rec.definition_id:
            return
        cfg = self.store.get("definition", rec.definition_id, actor.site)["config"]
        enc = self.store.get("clinical_record", rec.record_id, actor.site).get("encounter_id")
        for b in cfg.get("blocks", []):
            v = rec.blocks.get(b["key"])
            if isinstance(v, str):  # choice/text inputs often carry numbers ("1", "2.6"); store them as numbers
                try:
                    v = int(v) if v.strip().lstrip("-").isdigit() else float(v)
                except ValueError:
                    pass
            if b.get("observation_code") and v not in (None, ""):
                self._spawn(actor, "observation", {"patient_id": rec.patient_id, "code": b["observation_code"],
                                                   "display": b["label"], "value": v, "unit": b.get("unit"),
                                                   "source_kind": "note_page", "source_id": rec.id,
                                                   "encounter_id": enc}, f"note_page:{rec.id}", depth)

    def add_addendum(self, actor: Actor, page_id: str, text: str) -> Instance:
        """The only way to add to a finalized page."""
        p = self._raw(actor, "note_page", page_id)
        self._authorize(actor, SPECS["note_page"], "transition", p)
        if p["status"] != "finalized":
            raise InvariantViolation("Addenda are for finalized pages; edit the page instead")
        p["addenda"] = (p.get("addenda") or []) + [{"text": text, "by": actor.id, "at": utcnow()}]
        new = self._load(SPECS["note_page"], p)
        self._put("note_page", new)
        self._audit(actor, new.patient_id, "note_page.addendum", "note_page", page_id, {})
        return new

    # assessments
    def _pre_assessment(self, actor, d, definition):
        if not definition:
            raise Invalid("An assessment needs a definition_key (the questionnaire or calculator)")
        cfg = definition["config"]
        if actor.role == "patient" and not cfg.get("patient_reported"):
            raise PermissionDenied("Patients may only complete patient-reported assessments")
        d["assessment_type"] = cfg.get("assessment_type", "questionnaire")
        return self._score_into(cfg, d, definition["name"])

    def _preupdate_assessment(self, actor, old, new):
        cfg = self.store.get("definition", old["definition_id"], actor.site)["config"]
        return self._score_into(cfg, new, old.get("definition_key"))

    def _score_into(self, cfg, d, name):
        answers = d.get("answers") or {}
        items = cfg.get("items") or []
        if not answers or (items and set(i["param"] for i in items) - set(answers)):
            return d
        scored = self.score(cfg, answers, name)
        r = scored["result"]
        d.update(answers=scored["answers"], result=r, score=r.get("total_score", r.get("value")),
                 band=r.get("band_label") or r.get("verdict"), safety_flag=bool(r.get("flagged")))
        return d

    def _post_assessment(self, actor, rec, definition, depth):
        if rec.score is None and not rec.result:
            return
        canonical = json.dumps(rec.answers or {}, sort_keys=True, default=str)
        usage = self.record_run(actor, kind="assessment", definition_id=getattr(rec, "definition_id", None),
                                definition_key=rec.definition_key, patient_id=rec.patient_id, status="ok",
                                inputs_hash=hashlib.sha256(canonical.encode()).hexdigest())
        raw = self._raw(system_actor(actor.site, "read"), "assessment", rec.id)
        raw["usage_id"] = usage
        self._put("assessment", self._load(SPECS["assessment"], raw))
        self._assessment_effects(actor, rec, definition, depth, urgent=True)

    def _assessment_effects(self, actor, rec, definition, depth, urgent=False):
        d = definition or self.store.get("definition", rec.definition_id, actor.site)
        if not d:
            return
        cfg = d["config"] if isinstance(d, dict) else {}
        if isinstance(d, dict) and "config" not in d and hasattr(d, "config"):
            cfg = d.config
        code = (cfg.get("observation_code") if isinstance(cfg, dict) else None) or f"{d['key'] if isinstance(d, dict) else rec.definition_key}_score"
        name = d["name"] if isinstance(d, dict) else rec.definition_key
        already = [o for o in self.store.list("observation", actor.site, rec.patient_id, source_id=rec.id)]
        if not already:
            self._spawn(actor, "observation", {"patient_id": rec.patient_id, "code": code, "display": name,
                                               "category": "score", "value": rec.score, "effective_at": rec.taken_at,
                                               "encounter_id": rec.encounter_id,
                                               "perceived_by": "patient" if (cfg or {}).get("patient_reported") else "care_team",
                                               "source_kind": "assessment", "source_id": rec.id,
                                               "tags": [rec.band] if rec.band else []}, f"assessment:{rec.id}", depth)
        bands = set((cfg or {}).get("alert_bands") or [])
        if urgent and (rec.safety_flag or (rec.band and rec.band in bands)):
            self._spawn(actor, "task", {"patient_id": rec.patient_id, "title": f"Review safety item on {name}",
                                        "priority": "urgent", "assignee_role": "clinician", "target_kind": "assessment",
                                        "target_id": rec.id, "usage_id": None}, f"assessment:{rec.id}", depth)

    def score(self, cfg: dict, answers: Dict[str, Any], name: str = "assessment") -> dict:
        """Validate answers against an assessment config and run its kernel. Answers may be points or labels."""
        clean: Dict[str, Any] = dict(answers)
        if cfg.get("items"):
            clean = {}
            for item in cfg["items"]:
                if item["param"] not in answers:
                    raise Invalid(f"{name}: missing answer for {item['param']}")
                raw = answers[item["param"]]
                match = next((o for o in item["options"] if raw == o.get("points") or raw == o.get("label")), None)
                if match is None:
                    raise Invalid(f"{name}: '{raw}' is not an allowed answer for {item['param']}")
                clean[item["param"]] = match["points"]
            extra = set(answers) - set(clean)
            if extra:
                raise Invalid(f"{name}: unexpected answers {sorted(extra)}")
        try:
            result = engine.run_kernel(cfg.get("kernel", "weighted_point_sum"), clean, cfg.get("reference_rows"),
                                       cfg.get("kernel_config"))
        except (KeyError, ValueError, TypeError) as exc:
            raise Invalid(f"{name}: {exc}") from exc
        return {"answers": clean, "result": result}

    def _check_assessment(self, actor, rec, to):
        if to == "finalized" and rec.get("score") is None and not rec.get("result"):
            raise Invalid("Answer every item before finalizing")

    def _check_definition(self, actor, rec, to):
        if to != "published" or rec.get("def_kind") not in ("protocol", "regimen", "reaction", "cohort"):
            return
        label = "Protocol" if rec["def_kind"] == "protocol" else rec["def_kind"]
        if not self._citations_ok(rec.get("config") or {}):
            raise Invalid(f"{label} {rec['key']} has no evidence citations")
        if rec.get("review_status") != "approved":
            raise Invalid(f"{label} {rec['key']} review status is not approved")

    def _after_assessment(self, actor, rec, frm, to, depth):
        if to != "finalized":
            return
        self._assessment_effects(actor, rec, None, depth, urgent=False)

    # clinical documents
    @staticmethod
    def _fill_clinical_document(definition):
        cfg = definition["config"]
        out = {k: cfg[k] for k in ("doc_type", "header_html", "footer_html", "watermark", "shareable_with_patient",
                                    "protected") if cfg.get(k) is not None}
        return {**out, "title": definition["name"]}

    def _pre_clinical_document(self, actor, d, definition):
        cfg = (definition or {}).get("config", {})
        if definition:
            if cfg.get("expires_after_days") and not d.get("expires_at"):
                d["expires_at"] = utcnow() + timedelta(days=cfg["expires_after_days"])
        src = None
        if d.get("source_kind") and d.get("source_id"):
            src = self._raw(system_actor(actor.site, "read"), d["source_kind"], d["source_id"])
            if src.get("patient_id") != d["patient_id"]:
                raise Invalid("Source belongs to another patient")
        if not d.get("body_html"):
            d["body_html"] = self._render_body(actor, cfg.get("body_template"), d, src)
        return d

    def _render_body(self, actor, template, d, src) -> str:
        ctx: Dict[str, Any] = {"title": d.get("title", "")}
        if src:
            ctx.update({k: v for k, v in src.items() if not isinstance(v, (dict, list))})
            for k, v in (src.get("blocks") or {}).items():
                ctx[f"block_{k}"] = v
            if src.get("score") is not None:
                ctx["score"] = src["score"]
        p = self.store.get("patient", d["patient_id"], actor.site) or {}
        ctx.update(patient_name=p.get("name", ""), patient_gender=p.get("gender", ""))
        safe = {k: html.escape(str(v)) for k, v in ctx.items()}
        if template:
            try:
                return template.format_map(_Default(safe))
            except (ValueError, IndexError):
                pass
        rows = "".join(f"<tr><th>{html.escape(k)}</th><td>{v}</td></tr>" for k, v in safe.items())
        return f"<h1>{safe['title']}</h1><table>{rows}</table>"

    def _moved_clinical_document(self, actor, rec, frm, to):
        if to == "ready":
            page = (f"<html><body>{rec.get('header_html') or ''}{rec.get('body_html') or ''}"
                    f"{rec.get('footer_html') or ''}</body></html>")
            rec["blob_ref"] = self.blobs.put(f"{rec['id']}.html", page.encode(), "text/html")
            rec.update(signed=True, signed_by=actor.id)
        return rec

    def share_document(self, actor: Actor, document_id: str) -> dict:
        """Release a ready, shareable document. Needs the patient's data_sharing consent."""
        d = self.get(actor, "clinical_document", document_id)
        if d.status != "ready":
            raise InvariantViolation("Only ready documents can be shared")
        if not d.shareable_with_patient or d.protected:
            raise PermissionDenied("This document is not shareable")
        self.require_consent(actor, d.patient_id, "data_sharing")
        self._audit(actor, d.patient_id, "clinical_document.shared", "clinical_document", d.id, {})
        return {"id": d.id, "blob_ref": d.blob_ref, "html": self.blobs.get(d.blob_ref).decode()}

    # patient journals
    def _pre_journal_entry(self, actor, d, definition):
        definition = definition or self.resolve_definition(actor.site, "journal_plugin", d["plugin_key"])
        d["definition_key"] = d["plugin_key"] = definition["key"]
        fields = {f["key"]: f for f in definition["config"].get("fields", [])}
        unknown = set(d.get("data") or {}) - set(fields)
        if fields and unknown:
            raise Invalid(f"Unknown journal fields {sorted(unknown)}. Plugin has: {sorted(fields)}")
        if d.get("care_action_id"):
            ca = self._raw(system_actor(actor.site, "read"), "care_action", d["care_action_id"])
            if ca["patient_id"] != d["patient_id"]:
                raise Invalid("Care action belongs to another patient")
        return d

    def _post_journal_entry(self, actor, rec, definition, depth):
        definition = definition or self.resolve_definition(actor.site, "journal_plugin", rec.plugin_key)
        for f in definition["config"].get("fields", []):
            v = rec.data.get(f["key"])
            if f.get("observation_code") and v not in (None, ""):
                sysa = system_actor(actor.site)
                self.create(sysa, "observation", {"patient_id": rec.patient_id, "code": f["observation_code"],
                                                  "display": f["label"], "value": v, "unit": f.get("unit"),
                                                  "perceived_by": "patient", "effective_at": rec.recorded_at,
                                                  "source_kind": "journal_entry", "source_id": rec.id},
                            _depth=depth + 1, _status="preliminary")
        if rec.care_action_id:
            ca = self._raw(system_actor(actor.site, "read"), "care_action", rec.care_action_id)
            ca["completions"] = (ca.get("completions") or []) + [rec.recorded_at]
            self._put("care_action", self._load(SPECS["care_action"], ca))

    # conversations
    @staticmethod
    def _fill_conversation(definition):
        cfg = definition["config"]
        return {"actor_role": cfg.get("actor_role", "clinician"), "scope": cfg.get("scope", "patient"),
                "structure": cfg.get("structure") or {}, "title": definition["name"]}

    def _pre_conversation(self, actor, d, definition):
        if d.get("scope", "patient") == "patient" and not d.get("patient_id"):
            raise Invalid("A patient conversation needs patient_id (or use scope='general')")
        if d.get("scope") == "general":
            d["patient_id"] = None
        d.setdefault("actor_role", actor.role if actor.role != "system" else "clinician")
        if d.get("patient_id"):
            self._raw(system_actor(actor.site, "read"), "patient", d["patient_id"])
        return d

    def add_turn(self, actor: Actor, conversation_id: str, content: str, proposals: Sequence[str] = ()) -> Instance:
        c = self._raw(actor, "conversation", conversation_id)
        self._authorize(actor, SPECS["conversation"], "read", c)
        if actor.role == "patient" and c.get("patient_id") not in self._patient_ids(actor):
            raise PermissionDenied("Not your conversation")
        if c["status"] != "open":
            raise InvariantViolation("Conversation is closed")
        turn = {"id": new_id("turn"), "speaker_kind": actor.actor_type, "speaker_id": actor.id,
                "speaker_role": actor.role, "content": content, "at": utcnow(), "proposals": list(proposals)}
        c["turns"] = (c.get("turns") or []) + [turn]
        c.update(updated_at=utcnow(), updated_by=actor.id)
        new = self._load(SPECS["conversation"], c)
        self._put("conversation", new)
        self._audit(actor, new.patient_id, "conversation.turn", "conversation", conversation_id,
                    {"speaker": actor.actor_type})
        return new

    # consents
    def _pre_consent(self, actor, d, definition):
        if actor.role == "patient" and d["patient_id"] != actor.patient_id:
            rels = self.store.list("relationship", actor.site, d["patient_id"], related_patient_id=actor.patient_id)
            if not any(r["status"] == "active" and r["is_guardian"] and
                       (not r["consent_scopes"] or d["consent_scope"] in r["consent_scopes"]) for r in rels):
                raise PermissionDenied(f"You are not a guardian who may give '{d['consent_scope']}' consent")
            d.update(granted_by="guardian", guardian_id=actor.patient_id)
        if d.get("granted_by") == "guardian" and not d.get("guardian_id"):
            raise Invalid("guardian_id is required when granted_by='guardian'")
        return d

    # definitions
    def _pre_definition(self, actor, d, definition):
        d["config"] = self._validate_config(d["def_kind"], d.get("config") or {})
        versions = [x["version"] for x in self.store.list("definition", actor.site, None, def_kind=d["def_kind"], key=d["key"])
                    if x["site"] == actor.site]
        d["version"] = max(versions, default=0) + 1
        return d

    def _preupdate_definition(self, actor, old, new):
        new["config"] = self._validate_config(old["def_kind"], new.get("config") or {})
        return new

    # routines and the periodic tick
    def run_routines(self, actor: Actor, now: Optional[datetime] = None,
                     duty_states: Optional[Iterable[str]] = None) -> List[Instance]:
        """Create a task for every due/overdue routine occurrence that has none yet."""
        now = now or utcnow()
        duty = set(duty_states) if duty_states is not None else None
        made = []
        for r in self.list(actor, "routine", status="active"):
            if r.duty_state and duty is not None and r.duty_state not in duty:
                continue
            s = r.schedule
            status = engine.schedule_status(s.anchor or r.created_at, s.every_days, [], now, s.grace_days, s.until)
            have = {_aware(t.occurrence) for t in self.list(actor, "task", r.patient_id, routine_id=r.id)}
            for occ in status["occurrences"]:
                at = _aware(datetime.fromisoformat(occ["due"]))
                if at in have or now - at > timedelta(days=s.grace_days + 1):
                    continue
                data = {"title": r.title, **r.task_template, "patient_id": r.patient_id, "routine_id": r.id,
                        "occurrence": at, "due_at": at, "assignee_role": r.assignee_role, "assignee_id": r.assignee_id}
                made.append(self.create(system_actor(actor.site, "routines"), "task", data))
        return made

    def tick(self, actor: Actor, now: Optional[datetime] = None) -> dict:
        """Periodic housekeeping: routines, document/consent/profile expiry, overdue-outcome proposals."""
        now = now or utcnow()
        sysa = system_actor(actor.site, "tick")
        out: Dict[str, List[str]] = {"tasks": [t.id for t in self.run_routines(actor, now)], "expired": [], "proposals": []}
        for kind, field, frm, to in (("clinical_document", "expires_at", "ready", "expired"),
                                     ("consent", "valid_until", "granted", "expired")):
            for doc in self.store.list(kind, actor.site, None):
                if doc["status"] == frm and doc.get(field) and _aware(doc[field]) <= now:
                    self.transition(sysa, kind, doc["id"], to, "Expired")
                    out["expired"].append(doc["id"])
        for hp in self.store.list("health_profile", actor.site, None):
            if hp["status"] != "inactive" and hp.get("expires_at") and _aware(hp["expires_at"]) <= now:
                self.transition(sysa, "health_profile", hp["id"], "inactive", "Profile expired")
                out["expired"].append(hp["id"])
        for o in self.store.list("outcome", actor.site, None):
            if o["status"] == "expected" and o.get("due_by") and _aware(o["due_by"]) <= now:
                if not any(p["status"] == "pending" for p in self.store.list("proposal", actor.site, o["patient_id"], target_id=o["id"])):
                    p = self.propose(sysa, "transition", "outcome", target_id=o["id"], to_state="unachieved",
                                     origin=f"outcome_due:{o['id']}", rationale="Due date passed without being achieved")
                    out["proposals"].append(p.id)
        return out

    # pending actions
    def pending_actions(self, actor: Actor, patient_id: str, now: Optional[datetime] = None) -> List[dict]:
        """What is waiting: protocol-driven tasks and scheduled care actions."""
        now = now or utcnow()
        out: List[dict] = []
        for t in self.list(actor, "task", patient_id):
            if t.status in ("open", "in_progress"):
                out.append({"source": "task", "kind": "task", "id": t.id, "title": t.title, "performer": "care_team",
                            "due": t.due_at.isoformat() if t.due_at else None, "priority": t.priority})
        for ca in self.list(actor, "care_action", patient_id):
            if ca.status == "active" and ca.schedule:
                st = self.schedule_status(actor, ca.id, now)
                if st["counts"]["due"] or st["counts"]["overdue"]:
                    out.append({"source": "care_action", "kind": "care_action", "id": ca.id, "title": ca.title,
                                "performer": "patient", "due": st["next_due"], "overdue": st["counts"]["overdue"]})
        for a in self.list(actor, "assessment", patient_id, status="pending"):
            out.append({"source": "assessment", "kind": "assessment", "id": a.id, "title": a.definition_key,
                        "performer": "patient" if a.created_by.startswith("system") else "care_team", "due": None})
        for p in self.list(actor, "proposal", patient_id, status="pending"):
            out.append({"source": p.origin, "kind": "proposal", "id": p.id,
                        "title": f"Review: {p.op} {p.target_kind}", "performer": "care_team", "due": None})
        return out

    # calculators and timeline
    def run_calculator(self, actor: Actor, kernel: str, inputs: dict, reference_rows=None, config=None) -> dict:
        import hashlib
        import json as _json
        if reference_rows and os.environ.get("CLINESTRA_MODE") == "production" and actor.role not in ("admin", "clinical_lead"):
            raise PermissionDenied("Raw reference rows are an administrator tool")
        self._check_calculator_inputs(inputs or {}, config)
        canonical = _json.dumps(inputs or {}, sort_keys=True, default=str)
        try:
            result = engine.run_kernel(kernel, inputs, reference_rows, config)
            usage = self.record_run(actor, kind="calculator", kernel=kernel, status="ok",
                                    inputs_hash=hashlib.sha256(canonical.encode()).hexdigest(),
                                    outputs_hash=hashlib.sha256(_json.dumps(result, sort_keys=True, default=str).encode()).hexdigest())
        except KeyError as exc:
            self.record_run(actor, kind="calculator", kernel=kernel, status="error", error_code="unknown_kernel")
            raise NotFound(exc.args[0] if exc.args else "Unknown kernel") from exc
        except (engine.UnsafeExpression, ZeroDivisionError, OverflowError, ValueError, TypeError) as exc:
            self.record_run(actor, kind="calculator", kernel=kernel, status="error", error_code="expression")
            raise Invalid(f"Expression error: {exc}") from exc
        return {**result, "usage_id": usage}

    @staticmethod
    def _check_calculator_inputs(inputs: dict, config) -> None:
        declared = (config or {}).get("inputs") if isinstance(config, dict) else None
        if not declared:
            return
        for item in declared:
            if isinstance(item, str):
                if item not in inputs:
                    raise Invalid(f"Missing input '{item}'", fields=[item])
                continue
            name = item.get("name")
            if not name:
                continue
            if item.get("required", True) and name not in inputs:
                raise Invalid(f"Missing input '{name}'", fields=[name])
            value = inputs.get(name)
            if value is None:
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise Invalid(f"{name} must be a number", fields=[name])
            low, high = item.get("min"), item.get("max")
            if low is not None and value < low:
                raise Invalid(f"{name} is below the minimum", fields=[name])
            if high is not None and value > high:
                raise Invalid(f"{name} is above the maximum", fields=[name])

    def timeline(self, actor: Actor, patient_id: str) -> List[dict]:
        self.get(actor, "patient", patient_id)
        return self.store.list("timeline", actor.site, patient_id)

    def verify_timeline(self, actor: Actor, patient_id: str) -> dict:
        return verify_chain(self.timeline(actor, patient_id))

    # ════════════════════════════ internals ════════════════════════════
    def _cascade(self, actor: Actor, kind: str, id: str, to: str, reason: str):
        """A state change that follows from a decision already made (by ``actor``). Audited, not re-authorized."""
        doc = self.store.get(kind, id, actor.site)
        frm = doc["status"]
        doc.update(status=to, updated_at=utcnow(), updated_by=actor.id)
        self._put(kind, self._load(SPECS[kind], doc))
        self._audit(actor, doc.get("patient_id"), f"{kind}.{to}", kind, id, {"from": frm, "reason": reason, "cascade": True})

    @staticmethod
    def _pid(rec: Instance) -> Optional[str]:
        return getattr(rec, "patient_id", None)

    @staticmethod
    def _load(spec: ConceptSpec, doc: dict) -> Instance:
        doc = {k: v for k, v in doc.items() if k not in spec.model.model_computed_fields}
        try:
            return spec.model(**doc)
        except ValidationError as exc:
            raise Invalid(str(exc)) from exc

    def _put(self, kind: str, rec: Instance):
        self.store.put(kind, rec.model_dump(mode="json", exclude=set(type(rec).model_computed_fields)))

    def _raw(self, actor: Actor, kind: str, id: str) -> dict:
        doc = self.store.get(kind, id, actor.site)
        if not doc or doc.get("deleted"):
            raise NotFound(f"No {kind} '{id}'")
        if doc["site"] == LIBRARY_SITE and kind == "definition" and actor.role != "system":
            raise InvariantViolation("Library definitions come from packs and are read-only; create a site definition")
        self._authorize(actor, spec_for(kind), "read", doc)
        return doc

    def _audit(self, actor: Actor, patient_id: Optional[str], event: str, kind: str, subject_id: str, data: Optional[dict]):
        chain = patient_id or _SITE_CHAIN
        prev = self.store.list("timeline", actor.site, chain)
        e = make_entry(actor, chain, event, kind, subject_id, data, prev[-1]["hash"] if prev else None)
        self.store.put("timeline", e.model_dump(mode="json"))



class _Default(dict):
    def __missing__(self, key):
        return ""

