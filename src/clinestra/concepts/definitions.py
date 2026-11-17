"""Configuration schemas for every definition kind (the templates a site or pack configures).

Each config may carry ``defaults``: fields copied into an instance created from
the definition (anything given at creation wins). The other fields give the
definition its behaviour.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Type

from pydantic import BaseModel, ConfigDict, Field

from .clinical import ProfileTags
from .common import Effect, Schedule

__all__ = ["DEF_CONFIGS", "DEF_INSTANCE_KIND", "DefConfig"]


class DefConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    defaults: Dict[str, Any] = Field(default_factory=dict, description="Fields prefilled on instances")
    approval_required_for: List[str] = Field(default_factory=list, description="States that need an approval to enter")


class HealthProfileConfig(DefConfig):
    profile_type: Literal["general", "condition", "episode"] = "general"
    tags: ProfileTags = Field(default_factory=ProfileTags)
    ttl_days: Optional[int] = Field(None, description="Short-lived profiles expire after this many days")
    auto_for_conditions: List[str] = Field(default_factory=list, description="Create this profile when one of these conditions is recorded")


class CohortConfig(DefConfig):
    rule: Optional[str] = Field(None, description="safe_eval expression over patient facts (age, gender, conditions, latest_<code>)")
    manual_only: bool = True
    protocols: List[str] = Field(default_factory=list, description="Protocol keys surfaced to members")
    entry_actions: List[Effect] = Field(default_factory=list)
    exit_actions: List[Effect] = Field(default_factory=list)


class Template(BaseModel):
    kind: str = Field(..., description="Concept to instantiate: careplan, task, assessment, health_profile, journey, outcome, regimen ...")
    definition_key: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)
    required: bool = False


class Citation(BaseModel):
    id: str
    title: str
    source_type: Literal["guideline", "regulation", "circular", "paper", "local_sop"]
    issuer: Optional[str] = None
    year: Optional[int] = None
    url_or_ref: Optional[str] = None
    applies_to: List[str] = Field(default_factory=list)
    accessed_on: Optional[str] = None


class ProtocolConfig(DefConfig):
    cohorts: List[str] = Field(default_factory=list)
    patient_criteria: Optional[str] = Field(None, description="safe_eval expression; must be true to assign")
    templates: List[Template] = Field(default_factory=list, description="Instantiated when the protocol is assigned")
    pico: Dict[str, str] = Field(default_factory=dict)
    evidence: List[Dict[str, Any]] = Field(default_factory=list)


class CareplanConfig(DefConfig):
    care_actions: List[Dict[str, Any]] = Field(default_factory=list)
    tasks: List[Dict[str, Any]] = Field(default_factory=list)


class JournalField(BaseModel):
    key: str
    label: str
    field_type: Literal["number", "text", "choice", "boolean", "scale"] = "number"
    options: List[str] = Field(default_factory=list)
    observation_code: Optional[str] = None
    unit: Optional[str] = None


class JournalPluginConfig(DefConfig):
    fields: List[JournalField] = Field(default_factory=list)


class Block(BaseModel):
    key: str
    label: str
    input_type: Literal["narrative", "free_text", "number", "choice", "boolean", "date", "coded", "structured_pick"] = "free_text"
    required: bool = False
    options: List[str] = Field(default_factory=list)
    observation_code: Optional[str] = Field(None, description="Becomes an observation with this code on finalize")
    unit: Optional[str] = None


class NotePageConfig(DefConfig):
    page_type: Literal["case_sheet", "chart"] = "case_sheet"
    blocks: List[Block] = Field(default_factory=list)
    chart: Dict[str, Any] = Field(default_factory=dict, description="{component, query: {codes: [...]}, capture_blocks: [...]}")
    exports_to: List[str] = Field(default_factory=list, description="Clinical document definition keys")


class EncounterConfig(DefConfig):
    encounter_type: str = "visit"
    default_duration_min: Optional[int] = None
    note_pages: List[str] = Field(default_factory=list, description="Note page definitions opened in the record by default")
    workflow: Optional[str] = Field(None, description="Journey definition (association_type encounter) started with each encounter")


# Workflow operations a stage can allow (gates). A stage that lists gates blocks every operation it does not list,
# for the encounter the journey belongs to. A stage without gates allows everything.
GATE_OPERATIONS = {
    "case_sheet": "Write and finalize case-sheet pages",
    "finalize_record": "Finalize the clinical record",
    "close_encounter": "Close the encounter",
}


class Stage(BaseModel):
    """How a journey state shows on the workflow board (the 2.0 encounter-workflow status)."""
    label: Optional[str] = None
    category: Literal["todo", "in_progress", "done"] = "in_progress"
    color: Optional[str] = Field(None, description="Column accent, e.g. #0d9488")
    description: Optional[str] = None
    sla_minutes: Optional[int] = Field(None, description="Cards waiting longer than this are flagged on the board")


class Requirement(BaseModel):
    """Something that must be true for the encounter before the journey may leave a state (unless forced)."""
    kind: Literal["note_page", "clinical_record", "assessment", "task", "observation"]
    state: Optional[str] = Field(None, description="Required state, e.g. finalized; empty = exists")
    definition_key: Optional[str] = None
    label: Optional[str] = None


class AdvanceRule(BaseModel):
    """Move the journey automatically when a record of the encounter enters a state (2.0 action_based_status_map)."""
    when_kind: str
    when_state: str
    to: str
    from_states: List[str] = Field(default_factory=list, description="Only from these states; empty = any earlier state")
    definition_key: Optional[str] = None


class JourneyConfig(DefConfig):
    states: List[str]
    initial: str
    transitions: Dict[str, List[str]] = Field(default_factory=dict, description="state -> allowed next states; empty = any")
    terminal: List[str] = Field(default_factory=list)
    association_type: Literal["care_setting", "encounter", "care_program"] = "care_program"
    on_enter: Dict[str, List[Effect]] = Field(default_factory=dict, description="Journey state -> effects")
    on_exit: Dict[str, List[Effect]] = Field(default_factory=dict, description="Journey state -> effects when it is left")
    stages: Dict[str, Stage] = Field(default_factory=dict, description="Board presentation of each state")
    gates: Dict[str, List[str]] = Field(default_factory=dict, description=f"State -> operations allowed: {sorted(GATE_OPERATIONS)}")
    requires: Dict[str, List[Requirement]] = Field(default_factory=dict, description="State -> what must be done before leaving it")
    advance_on: List[AdvanceRule] = Field(default_factory=list, description="Automatic moves on encounter events")


class Role(BaseModel):
    key: str
    label: str
    person_kind: Literal["provider", "care_team_member"] = "care_team_member"
    skills: List[str] = Field(default_factory=list)
    max: Optional[int] = None


class CareTeamConfig(DefConfig):
    roles: List[Role]


class AssessmentConfig(DefConfig):
    assessment_type: Literal["questionnaire", "calculator"] = "questionnaire"
    patient_reported: bool = False
    items: List[Dict[str, Any]] = Field(default_factory=list, description="[{param, text, options: [{label, points}]}]; empty = free inputs")
    kernel: str = Field("weighted_point_sum", description="Engine kernel that scores it")
    reference_rows: List[Dict[str, Any]] = Field(default_factory=list)
    kernel_config: Dict[str, Any] = Field(default_factory=dict)
    observation_code: Optional[str] = Field(None, description="Score is written as this observation on finalize")
    lower_is_better: Optional[bool] = None
    inputs: List[Any] = Field(default_factory=list, description="Input names, or {name, unit, min, max, required}")
    alert_bands: List[str] = Field(default_factory=list, description="Bands that raise an urgent task as soon as the result exists")
    flag_rules: List[Dict[str, Any]] = Field(default_factory=list)


class ClinicalDocumentConfig(DefConfig):
    doc_type: str
    header_html: Optional[str] = None
    footer_html: Optional[str] = None
    watermark: Optional[str] = None
    body_template: Optional[str] = Field(None, description="HTML with {placeholders} filled from the source record")
    shareable_with_patient: bool = False
    protected: bool = False
    expires_after_days: Optional[int] = None
    # consent forms (doc_type "consent_form"): who signs, which consent it grants, how the signing link is protected
    consent_scope: Optional[Literal["treatment", "data_sharing", "ai", "procedure", "communication", "research"]] = None
    signers: List[Literal["patient", "clinician", "witness"]] = Field(default_factory=list, description="Who must sign before the form is complete")
    require_otp: bool = Field(False, description="The patient's signing link asks for a one-time code first")
    link_expiry_minutes: int = Field(30, ge=5, le=60 * 24 * 7)
    language: str = Field("en", description="BCP 47 language tag of the form's text")


class ClinicalSettingConfig(DefConfig):
    pass


class RegimenConfig(DefConfig):
    pass


class TaskConfig(DefConfig):
    pass


class RoutineConfig(DefConfig):
    schedule: Optional[Schedule] = None


class FieldSpec(BaseModel):
    key: str
    label: Optional[str] = None
    field_type: Literal["text", "number", "choice", "boolean", "date"] = "text"
    options: List[str] = Field(default_factory=list)
    required: bool = False
    enabled: bool = True


class PatientFieldsConfig(DefConfig):
    standard: List[FieldSpec] = Field(default_factory=list, description="Which standard demographics are enabled/required")
    custom: List[FieldSpec] = Field(default_factory=list, description="Site-only fields")


class ReactionConfig(DefConfig):
    when_kind: str
    when_state: str = Field(..., description="Fires when a record of when_kind enters this state (creation counts)")
    condition: Optional[str] = Field(None, description="safe_eval expression over the record's fields")
    effects: List[Effect]


class ConversationConfig(DefConfig):
    actor_role: str = "clinician"
    scope: Literal["patient", "general"] = "patient"
    structure: Dict[str, Any] = Field(default_factory=dict, description="Sections, tone, allowed tools for this actor")


DEF_CONFIGS: Dict[str, Type[DefConfig]] = {
    "health_profile": HealthProfileConfig, "cohort": CohortConfig, "careplan": CareplanConfig,
    "journal_plugin": JournalPluginConfig, "note_page": NotePageConfig, "encounter": EncounterConfig,
    "journey": JourneyConfig, "care_team": CareTeamConfig, "assessment": AssessmentConfig,
    "clinical_document": ClinicalDocumentConfig, "clinical_setting": ClinicalSettingConfig, "regimen": RegimenConfig,
    "protocol": ProtocolConfig, "task": TaskConfig, "routine": RoutineConfig, "patient_fields": PatientFieldsConfig,
    "reaction": ReactionConfig, "conversation": ConversationConfig,
}

# Definition kind -> instance kind it templates (None: used by the harness itself).
DEF_INSTANCE_KIND: Dict[str, Optional[str]] = {
    "health_profile": "health_profile", "cohort": "cohort_membership", "careplan": "careplan",
    "journal_plugin": "journal_entry", "note_page": "note_page", "encounter": "encounter", "journey": "journey",
    "care_team": "care_team", "assessment": "assessment", "clinical_document": "clinical_document",
    "clinical_setting": "clinical_setting", "regimen": "regimen", "protocol": "protocol_assignment", "task": "task",
    "routine": "routine", "conversation": "conversation", "patient_fields": None, "reaction": None,
}
