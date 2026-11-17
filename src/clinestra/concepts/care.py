"""What can be and is being done: cohorts, protocols, careplans, care actions, tasks, routines, regimens, journeys."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from .common import Instance, PatientInstance, Schedule

__all__ = ["CohortMembership", "ProtocolAssignment", "Careplan", "CareAction", "Task", "ChecklistItem", "Routine",
           "Regimen", "CodedItem", "Journey", "JourneyStep"]


class CohortMembership(PatientInstance):
    """A patient belonging to a cohort (definition kind 'cohort'). Entering/exiting runs the cohort's actions."""
    cohort_key: str
    overridden: bool = Field(False, description="True when added despite the cohort rule not matching")
    reason: str = Field(..., min_length=3)


class ProtocolAssignment(PatientInstance):
    """A protocol (definition kind 'protocol') assigned to a patient. Assigning instantiates its templates."""
    protocol_key: str
    cohort_membership_id: Optional[str] = None
    reason: Optional[str] = None


class Careplan(PatientInstance):
    """Everything planned for the patient: care actions (patient does) and tasks (care team does).

    A prescription, once given, becomes a careplan. Activating one is a clinical decision.
    """
    title: str
    protocol_assignment_id: Optional[str] = None
    encounter_id: Optional[str] = None
    goals: List[str] = Field(default_factory=list, description="Outcome ids this plan is aiming at")


class CareAction(PatientInstance):
    """Something the patient does or receives: an order, referral, education, journalling, medication, self care.

    Orders, referrals and medications are clinical. Repeating ones carry a schedule and surface in the patient journal.
    """
    careplan_id: Optional[str] = None
    action_type: Literal["order", "referral", "education", "journal", "medication", "self_care", "lifestyle", "appointment"]
    title: str
    code: Optional[str] = None
    details: Dict[str, Any] = Field(default_factory=dict)
    setting_id: Optional[str] = Field(None, description="Clinical setting that fulfils it (orders, referrals)")
    journal_plugin_key: Optional[str] = Field(None, description="For journal actions: plugin used to log it")
    schedule: Optional[Schedule] = None
    completions: List[datetime] = Field(default_factory=list)
    order_kind: Optional[Literal["lab", "imaging", "procedure", "vaccination", "other"]] = None
    order_status: Optional[Literal["proposed", "signed", "sent", "resulted", "closed"]] = None


class ChecklistItem(BaseModel):
    item: str
    done: bool = False


class Task(Instance):
    """Something the care team does. Can ask for a note page or assessment, or be a simple checklist."""
    patient_id: Optional[str] = None
    title: str
    careplan_id: Optional[str] = None
    encounter_id: Optional[str] = None
    assignee_role: Optional[str] = None
    assignee_id: Optional[str] = None
    due_at: Optional[datetime] = None
    checklist: List[ChecklistItem] = Field(default_factory=list)
    target_kind: Optional[str] = Field(None, description="Concept this task asks for, e.g. assessment, note_page")
    target_definition_key: Optional[str] = None
    target_id: Optional[str] = Field(None, description="Set when the requested record is created")
    routine_id: Optional[str] = None
    occurrence: Optional[datetime] = None
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    usage_id: Optional[str] = None


class Routine(Instance):
    """A repeating task tagged to a role or a member, active while they are on duty. Generates tasks when due."""
    patient_id: Optional[str] = None
    title: str
    assignee_role: Optional[str] = None
    assignee_id: Optional[str] = None
    duty_state: Optional[str] = Field(None, description="e.g. 'on_shift', 'on_call'; empty = always")
    schedule: Schedule
    task_template: Dict[str, Any] = Field(default_factory=dict, description="Fields copied into each generated task")


class CodedItem(BaseModel):
    code: str
    display: Optional[str] = None
    dose: Optional[str] = None
    frequency: Optional[str] = None
    duration_days: Optional[int] = None
    instructions: Optional[str] = None
    order_kind: Optional[Literal["lab", "imaging", "procedure", "vaccination", "other"]] = None


class Regimen(PatientInstance):
    """Evidence-based orders and drugs prescribed together. Prescribing is clinical; it lands in a careplan."""
    name: str
    regimen_type: Literal["management", "diagnostic", "self_care"] = "management"
    careplan_id: Optional[str] = None
    orders: List[CodedItem] = Field(default_factory=list)
    drugs: List[CodedItem] = Field(default_factory=list)
    reason: Optional[str] = None
    supersedes: Optional[str] = None
    signed_by: Optional[str] = None
    counter_signed_by: Optional[str] = None


class JourneyStep(BaseModel):
    state: str
    at: datetime
    by: str
    reason: Optional[str] = None
    forced: bool = Field(False, description="Moved past unmet requirements or outside the allowed transitions")


class Journey(PatientInstance):
    """A patient's path through configurable states, tied to a care setting, encounter or care program."""
    association_type: Literal["care_setting", "encounter", "care_program"] = "care_program"
    association_id: Optional[str] = None
    state: Optional[str] = Field(None, description="Current journey state; set from the definition's initial state")
    history: List[JourneyStep] = Field(default_factory=list)
