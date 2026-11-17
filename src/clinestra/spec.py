"""
The concept registry: one :class:`ConceptSpec` per concept.

A spec says what a concept's states are, which moves between them are allowed,
who may write it, whether it is *clinical* (a decision only a clinician makes;
automatic reactions and AI agents can only *propose* it), which states need an
approval, and which states freeze the record. The harness, the HTTP API and the
AI tool registry are all generated from these specs, so every concept gets
read, create, update, transition and delete with the same rules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, FrozenSet, Iterable, Optional, Tuple, Type, Union

from pydantic import BaseModel, ConfigDict, create_model

from .base import STAFF
from .concepts import capture, care, clinical, governance, people
from .concepts.common import SYSTEM_FIELDS, Instance

__all__ = ["ConceptSpec", "SPECS", "spec_for", "by_plural"]

ALL_HUMANS = STAFF | {"admin", "patient"}
Clinical = Union[bool, Callable[[dict], bool]]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass
class ConceptSpec:
    kind: str
    model: Type[Instance]
    plural: str
    group: str
    states: Tuple[str, ...]
    initial: str
    transitions: Dict[str, Tuple[str, ...]]
    write_roles: FrozenSet[str] = STAFF
    clinical: Clinical = False
    clinical_to: FrozenSet[str] = frozenset()
    frozen_states: FrozenSet[str] = frozenset()
    approval_states: FrozenSet[str] = frozenset()
    patient_can_create: bool = False
    agent_can_create: bool = False
    deletable: bool = True
    readonly: FrozenSet[str] = frozenset()
    immutable: FrozenSet[str] = frozenset({"patient_id"})
    definition_kind: Optional[str] = None
    summary: str = ""
    create_model: Type[BaseModel] = field(init=False)
    api_create_model: Type[BaseModel] = field(init=False)
    update_model: Type[BaseModel] = field(init=False)

    def __post_init__(self):
        name = "".join(p.title() for p in self.kind.split("_"))
        fields = {k: f for k, f in self.model.model_fields.items() if k not in SYSTEM_FIELDS | self.readonly}
        self.create_model = create_model(f"{name}Create", __base__=_Body,
                                         **{k: (f.annotation, f) for k, f in fields.items()})
        # Over HTTP and tools, fields a definition can supply are optional; the harness validates after merging.
        if self.definition_kind:
            keep = {"patient_id", "record_id"}
            self.api_create_model = create_model(
                f"{name}Create", __base__=_Body,
                **{k: (f.annotation, f) if (k in keep or not f.is_required()) else (Optional[f.annotation], None)
                   for k, f in fields.items()})
        else:
            self.api_create_model = self.create_model
        upd = {k: (Optional[f.annotation], None) for k, f in fields.items() if k not in self.immutable | {"definition_key"}}
        self.update_model = create_model(f"{name}Update", __base__=_Body, **upd)
        self.summary = self.summary or (self.model.__doc__ or "").strip().split("\n")[0]

    @property
    def scope(self) -> str:
        f = self.model.model_fields.get("patient_id")
        if f is None or self.kind == "patient":
            return "patient" if self.kind == "patient" else "global"
        return "patient" if f.is_required() else "mixed"

    @property
    def encounter_scoped(self) -> bool:
        return "encounter_id" in self.model.model_fields or self.kind in ("note_page",)

    def is_clinical(self, doc: dict) -> bool:
        return self.clinical(doc) if callable(self.clinical) else bool(self.clinical)

    def can_move(self, frm: str, to: str) -> bool:
        return to in self.transitions.get(frm, ())


def _t(**moves: Iterable[str]) -> Dict[str, Tuple[str, ...]]:
    return {k: tuple(v) for k, v in moves.items()}


_CLINICAL_ACTIONS = {"order", "referral", "medication"}

_SPECS = [
    # ── people ──
    ConceptSpec("patient", people.Patient, "patients", "People", ("active", "inactive", "deceased"), "active",
                _t(active=("inactive", "deceased"), inactive=("active", "deceased")),
                write_roles=STAFF | {"admin"}, clinical_to=frozenset({"deceased"}), deletable=False,
                readonly=frozenset({"patient_id"}), immutable=frozenset()),
    ConceptSpec("relationship", people.Relationship, "relationships", "People", ("active", "ended"), "active",
                _t(active=("ended",))),
    ConceptSpec("provider", people.Provider, "providers", "People", ("active", "in_break", "resigned"), "active",
                _t(active=("in_break", "resigned"), in_break=("active", "resigned")),
                write_roles=frozenset({"admin", "coordinator"}), deletable=False, immutable=frozenset()),
    ConceptSpec("care_team_member", people.CareTeamMember, "care-team-members", "People",
                ("active", "in_break", "resigned"), "active",
                _t(active=("in_break", "resigned"), in_break=("active", "resigned")),
                write_roles=frozenset({"admin", "coordinator"}), deletable=False, immutable=frozenset()),
    ConceptSpec("care_team", people.CareTeam, "care-teams", "People", ("active", "disbanded"), "active",
                _t(active=("disbanded",)), write_roles=frozenset({"clinician", "coordinator", "system"}),
                definition_kind="care_team"),
    ConceptSpec("clinical_setting", people.ClinicalSetting, "clinical-settings", "People", ("active", "inactive"),
                "active", _t(active=("inactive",), inactive=("active",)), write_roles=frozenset({"admin"}),
                definition_kind="clinical_setting", immutable=frozenset()),
    # ── how the patient is ──
    ConceptSpec("observation", clinical.Observation, "observations", "How they are",
                ("preliminary", "final", "amended", "entered_in_error"), "final",
                _t(preliminary=("final", "entered_in_error"), final=("amended", "entered_in_error"),
                   amended=("amended", "entered_in_error")),
                write_roles=STAFF | {"system"}, frozen_states=frozenset({"entered_in_error"}), deletable=False),
    ConceptSpec("condition", clinical.Condition, "conditions", "How they are",
                ("active", "remission", "resolved", "entered_in_error"), "active",
                _t(active=("remission", "resolved", "entered_in_error"), remission=("active", "resolved"),
                   resolved=("active",)), clinical=True, deletable=False),
    ConceptSpec("medication", clinical.Medication, "medications", "How they are", ("active", "stopped", "completed"),
                "active", _t(active=("stopped", "completed")), clinical=True,
                frozen_states=frozenset({"stopped", "completed"})),
    ConceptSpec("clinical_state", clinical.ClinicalState, "clinical-states", "How they are",
                ("current", "superseded", "entered_in_error"), "current", _t(current=("entered_in_error",)),
                clinical=True, frozen_states=frozenset({"current", "superseded", "entered_in_error"}), deletable=False),
    ConceptSpec("outcome", clinical.Outcome, "outcomes", "How they are", ("expected", "achieved", "unachieved"),
                "expected", _t(expected=("achieved", "unachieved"), unachieved=("expected",)), clinical=True,
                frozen_states=frozenset({"achieved"})),
    ConceptSpec("health_profile", clinical.HealthProfile, "health-profiles", "How they are",
                ("active", "pinned", "inactive"), "active",
                _t(active=("pinned", "inactive"), pinned=("active", "inactive"), inactive=("active",)),
                write_roles=STAFF | {"system"}, definition_kind="health_profile"),
    # ── what can be done / what is being done ──
    ConceptSpec("cohort_membership", care.CohortMembership, "cohort-memberships", "What can be done",
                ("active", "closed"), "active", _t(active=("closed",)), clinical=True,
                frozen_states=frozenset({"closed"}), definition_kind="cohort", deletable=False),
    ConceptSpec("protocol_assignment", care.ProtocolAssignment, "protocol-assignments", "What can be done",
                ("assigned", "cancelled", "compromised"), "assigned", _t(assigned=("cancelled", "compromised")),
                clinical=True, frozen_states=frozenset({"cancelled", "compromised"}), definition_kind="protocol",
                deletable=False),
    ConceptSpec("careplan", care.Careplan, "careplans", "What is being done",
                ("draft", "active", "completed", "cancelled"), "draft",
                _t(draft=("active", "cancelled"), active=("completed", "cancelled")),
                write_roles=STAFF | {"system"}, clinical_to=frozenset({"active"}),
                frozen_states=frozenset({"completed", "cancelled"}), definition_kind="careplan"),
    ConceptSpec("care_action", care.CareAction, "care-actions", "What is being done",
                ("planned", "active", "completed", "cancelled"), "planned",
                _t(planned=("active", "cancelled"), active=("completed", "cancelled")),
                write_roles=STAFF | {"system"}, clinical=lambda d: d.get("action_type") in _CLINICAL_ACTIONS,
                frozen_states=frozenset({"completed", "cancelled"})),
    ConceptSpec("task", care.Task, "tasks", "What is being done", ("open", "in_progress", "done", "cancelled"), "open",
                _t(open=("in_progress", "done", "cancelled"), in_progress=("open", "done", "cancelled")),
                write_roles=STAFF | {"system"}, frozen_states=frozenset({"done", "cancelled"}), definition_kind="task",
                immutable=frozenset()),
    ConceptSpec("routine", care.Routine, "routines", "What is being done", ("active", "paused", "ended"), "active",
                _t(active=("paused", "ended"), paused=("active", "ended")), frozen_states=frozenset({"ended"}),
                definition_kind="routine", immutable=frozenset()),
    ConceptSpec("regimen", care.Regimen, "regimens", "What is being done",
                ("proposed", "signed", "counter_signed", "active", "stopped", "superseded"), "proposed",
                _t(proposed=("signed", "active", "stopped"), signed=("counter_signed", "stopped"),
                   counter_signed=("active", "stopped"), active=("stopped", "superseded")),
                clinical=True, clinical_to=frozenset({"signed", "counter_signed", "active"}),
                agent_can_create=True,
                frozen_states=frozenset({"stopped", "superseded"}), definition_kind="regimen",
                readonly=frozenset({"signed_by", "counter_signed_by"})),
    ConceptSpec("journey", care.Journey, "journeys", "What is being done", ("active", "completed", "cancelled"),
                "active", _t(active=("completed", "cancelled")), write_roles=STAFF | {"system"},
                frozen_states=frozenset({"completed", "cancelled"}), definition_kind="journey",
                readonly=frozenset({"state", "history"})),
    # ── interactions and capture ──
    ConceptSpec("encounter", capture.Encounter, "encounters", "Interactions",
                ("pending", "scheduled", "closed", "cancelled"), "pending",
                _t(pending=("scheduled", "closed", "cancelled"), scheduled=("pending", "closed", "cancelled")),
                write_roles=STAFF | {"system"}, frozen_states=frozenset({"closed", "cancelled"}),
                definition_kind="encounter"),
    ConceptSpec("clinical_record", capture.ClinicalRecord, "clinical-records", "Interactions",
                ("pending", "saved", "finalized"), "pending",
                _t(pending=("saved", "finalized"), saved=("pending", "finalized")),
                write_roles=STAFF | {"system"}, clinical_to=frozenset({"finalized"}),
                frozen_states=frozenset({"finalized"})),
    ConceptSpec("note_page", capture.NotePage, "note-pages", "Interactions",
                ("drafted", "unapproved", "approved", "finalized"), "drafted",
                _t(drafted=("unapproved", "approved", "finalized"), unapproved=("drafted", "approved"),
                   approved=("finalized",)),
                clinical_to=frozenset({"finalized"}), frozen_states=frozenset({"approved", "finalized"}),
                approval_states=frozenset({"approved"}), agent_can_create=True, definition_kind="note_page",
                readonly=frozenset({"addenda", "ai_drafted"}), immutable=frozenset({"patient_id", "record_id"})),
    ConceptSpec("assessment", capture.Assessment, "assessments", "Interactions", ("pending", "saved", "finalized"),
                "pending", _t(pending=("saved", "finalized"), saved=("pending", "finalized")),
                frozen_states=frozenset({"finalized"}), patient_can_create=True, definition_kind="assessment",
                readonly=frozenset({"result", "score", "band", "safety_flag", "assessment_type", "usage_id"})),
    ConceptSpec("clinical_document", capture.ClinicalDocument, "clinical-documents", "Interactions",
                ("draft", "ready", "expired"), "draft", _t(draft=("ready",), ready=("expired",)),
                clinical_to=frozenset({"ready"}), frozen_states=frozenset({"ready", "expired"}), agent_can_create=True,
                definition_kind="clinical_document", readonly=frozenset({"signed", "signed_by", "blob_ref"})),
    ConceptSpec("journal_entry", capture.JournalEntry, "journal-entries", "Interactions",
                ("recorded", "entered_in_error"), "recorded", _t(recorded=("entered_in_error",)),
                write_roles=STAFF | {"patient", "system"}, patient_can_create=True,
                frozen_states=frozenset({"recorded", "entered_in_error"}), definition_kind="journal_plugin",
                deletable=False),
    ConceptSpec("conversation", capture.Conversation, "conversations", "Interactions", ("open", "closed"), "open",
                _t(open=("closed",)), write_roles=ALL_HUMANS, patient_can_create=True, agent_can_create=True,
                frozen_states=frozenset({"closed"}), definition_kind="conversation", readonly=frozenset({"turns"})),
    # ── governance ──
    ConceptSpec("consent", governance.Consent, "consents", "Governance",
                ("requested", "granted", "refused", "withdrawn", "expired"), "requested",
                _t(requested=("granted", "refused"), granted=("withdrawn", "expired")),
                write_roles=STAFF | {"patient"}, patient_can_create=True,
                frozen_states=frozenset({"refused", "withdrawn", "expired"}), deletable=False),
    ConceptSpec("approval", governance.Approval, "approvals", "Governance", ("pending", "approved", "rejected"),
                "pending", {}, write_roles=frozenset(), frozen_states=frozenset({"approved", "rejected"}),
                deletable=False, readonly=frozenset({"decided_by", "decision_note"})),
    ConceptSpec("proposal", governance.Proposal, "proposals", "Governance", ("pending", "accepted", "rejected"),
                "pending", {}, write_roles=STAFF | {"system"}, agent_can_create=True,
                frozen_states=frozenset({"accepted", "rejected"}), deletable=False,
                readonly=frozenset({"result_id", "decided_by"})),
    ConceptSpec("definition", governance.Definition, "definitions", "Governance", ("draft", "published", "retired"),
                "draft", _t(draft=("published",), published=("retired",)),
                write_roles=frozenset({"admin", "clinician", "coordinator"}),
                frozen_states=frozenset({"published", "retired"}),
                readonly=frozenset({"version", "pack_id", "review_status", "reviewed_by", "reviewed_at", "review_ref", "review_notes"}),
                immutable=frozenset({"def_kind", "key"})),
]

SPECS: Dict[str, ConceptSpec] = {s.kind: s for s in _SPECS}
_PLURALS = {s.plural: s for s in _SPECS}


def spec_for(kind: str) -> ConceptSpec:
    from .errors import NotFound
    if kind not in SPECS:
        raise NotFound(f"Unknown concept '{kind}'. Known: {sorted(SPECS)}")
    return SPECS[kind]


def by_plural(plural: str) -> ConceptSpec:
    return _PLURALS[plural]

