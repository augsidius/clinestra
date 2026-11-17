"""How the patient is: observations, conditions, medications, clinical states, outcomes, health profiles."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field

from ..base import utcnow
from .common import PatientInstance

__all__ = ["Observation", "Condition", "Medication", "ClinicalState", "Outcome", "HealthProfile", "ProfileTags"]


class Observation(PatientInstance):
    """The atomic clinical fact. Note pages, assessments, journals, devices and conversations all write observations."""
    code: str = Field(..., description="Coded concept, e.g. 'phq9_score', 'bp_systolic', 'loinc:8480-6'")
    display: Optional[str] = None
    system: Optional[str] = Field(None, description="Code system, e.g. loinc, snomed, local")
    category: Literal["vital", "lab", "score", "symptom", "finding", "social", "lifestyle", "note", "other"] = "finding"
    value: Union[float, int, bool, str, Dict[str, Any], None] = None
    unit: Optional[str] = None
    effective_at: datetime = Field(default_factory=utcnow)
    perceived_by: Literal["patient", "care_team", "device"] = "care_team"
    source_kind: Optional[str] = Field(None, description="Concept that produced it: note_page, assessment, journal_entry ...")
    source_id: Optional[str] = None
    encounter_id: Optional[str] = None
    tags: List[str] = Field(default_factory=list)


class Condition(PatientInstance):
    """A diagnosis or problem. Recording one is a clinical decision."""
    code: str
    display: Optional[str] = None
    system: Optional[str] = None
    verification: Literal["provisional", "confirmed"] = "provisional"
    onset: Optional[datetime] = None
    severity: Optional[str] = None
    encounter_id: Optional[str] = None
    notes: Optional[str] = None


class Medication(PatientInstance):
    """A medicine the patient is taking (prescribed here, or reported)."""
    code: str
    display: Optional[str] = None
    dose: Optional[str] = None
    frequency: Optional[str] = None
    route: Optional[str] = None
    regimen_id: Optional[str] = None
    reported_only: bool = Field(False, description="True when reported by the patient, not prescribed here")
    source: Optional[str] = None
    signed_by: Optional[str] = None
    counter_signed_by: Optional[str] = None
    start: Optional[datetime] = None
    end: Optional[datetime] = None


class ClinicalState(PatientInstance):
    """Grading/severity of a condition, or a general state (emergency, pregnant ...).

    At most one *current* state per patient for the general scope and per condition.
    A new one supersedes the old; nothing is overwritten.
    """
    scope: Literal["general", "condition"]
    condition_code: Optional[str] = Field(None, description="Required when scope='condition'")
    state: str = Field(..., description="e.g. 'moderate', 'severe', 'remission'; general: 'emergency', 'pregnant' ...")
    grade: Optional[str] = None
    basis: List[str] = Field(default_factory=list, description="Observation/assessment ids this rests on")
    source_outcome_id: Optional[str] = None
    note: Optional[str] = None


class Outcome(PatientInstance):
    """A terminal or semi-terminal point expected in a clinical state; perceived by the patient or the care team."""
    name: str
    outcome_type: Literal["clinical", "functional", "patient_reported", "process", "experience"] = "clinical"
    perceived_by: Literal["patient", "care_team"] = "care_team"
    condition_code: Optional[str] = None
    measure_code: Optional[str] = Field(None, description="Observation code tracked, e.g. 'phq9_score'")
    comparator: Optional[Literal["<", "<=", ">", ">=", "=="]] = None
    target: Optional[float] = None
    due_by: Optional[datetime] = None
    on_achieved_state: Optional[str] = Field(None, description="Clinical state to propose when achieved, e.g. 'remission'")
    protocol_assignment_id: Optional[str] = None
    evidence: List[str] = Field(default_factory=list, description="Observation ids that showed it")


class ProfileTags(BaseModel):
    conditions: List[str] = Field(default_factory=list)
    measurements: List[str] = Field(default_factory=list, description="Observation codes tracked")
    medications: List[str] = Field(default_factory=list)
    outcomes: List[str] = Field(default_factory=list)


class HealthProfile(PatientInstance):
    """A tagged, annotated view over a patient's observations. Shows what is present and what is absent.

    Every patient gets a 'general' profile. Others can be condition-specific or short-lived (e.g. a surgery profile).
    """
    name: str
    profile_type: Literal["general", "condition", "episode"] = "general"
    tags: ProfileTags = Field(default_factory=ProfileTags)
    priority_states: List[str] = Field(default_factory=list, description="Clinical states that raise this profile's priority")
    expires_at: Optional[datetime] = None


