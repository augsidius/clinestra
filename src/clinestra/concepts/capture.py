"""Interactions and data capture: encounters, clinical records, note pages, assessments, documents, journals, conversations."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from ..base import utcnow
from .common import Instance, PatientInstance

__all__ = ["Encounter", "ClinicalRecord", "NotePage", "Addendum", "Assessment", "ClinicalDocument", "JournalEntry",
           "Conversation", "Turn"]


class Encounter(PatientInstance):
    """A clinical interaction between care team and patient, grouped by time or care setting."""
    encounter_type: str = Field("visit", description="opd_visit, teleconsult, home_visit, admission ...")
    setting_id: Optional[str] = None
    start_at: Optional[datetime] = None
    duration_min: Optional[int] = None
    reason: Optional[str] = None


class ClinicalRecord(PatientInstance):
    """The group of note pages captured for one encounter. Holds no clinical data itself.

    Always belongs to an encounter: if none is given, one is created.
    """
    encounter_id: Optional[str] = None
    title: str = "Clinical record"


class Addendum(BaseModel):
    text: str
    by: str
    at: datetime = Field(default_factory=utcnow)


class NotePage(PatientInstance):
    """A page in a clinical record: a case sheet (structured blocks) or a clinical chart (observation query + capture).

    Finalized pages cannot change; blocks mapped to observation codes become observations on finalize.
    """
    record_id: str
    page_type: Literal["case_sheet", "chart"] = "case_sheet"
    title: str = "Case sheet"
    blocks: Dict[str, Any] = Field(default_factory=dict, description="Block key -> captured value")
    chart: Dict[str, Any] = Field(default_factory=dict, description="Chart config: observation query and component id")
    page_clinical_data: List[Dict[str, Any]] = Field(default_factory=list, description="Canvas sections (Clinestra 2.0 EMR shape): "
                                                    "section_id, section_name, section_type, config, data, is_deleted. Values also land in blocks")
    template_metadata: Dict[str, Any] = Field(default_factory=dict, description="Canvas template metadata (name, key, description, type)")
    addenda: List[Addendum] = Field(default_factory=list)
    ai_drafted: bool = False


class Assessment(PatientInstance):
    """Just-in-time structured capture: a questionnaire or a clinical calculator. Need not belong to an encounter."""
    assessment_type: Literal["questionnaire", "calculator"] = "questionnaire"
    encounter_id: Optional[str] = None
    answers: Dict[str, Any] = Field(default_factory=dict)
    taken_at: datetime = Field(default_factory=utcnow)
    result: Dict[str, Any] = Field(default_factory=dict)
    score: Optional[float] = None
    band: Optional[str] = None
    safety_flag: bool = False
    usage_id: Optional[str] = None


class ClinicalDocument(PatientInstance):
    """Anything printable that leaves the system: prescription, report, MLC document, consent form ..."""
    doc_type: str = Field(..., description="prescription, clinical_report, mlc, consent_form, diet_chart ...")
    title: str
    source_kind: Optional[str] = Field(None, description="clinical_record, note_page or assessment")
    source_id: Optional[str] = None
    body_html: Optional[str] = None
    header_html: Optional[str] = None
    footer_html: Optional[str] = None
    watermark: Optional[str] = None
    blob_ref: Optional[str] = Field(None, description="Where the rendered file lives (S3 URL, path ...), from the blob store")
    shareable_with_patient: bool = False
    protected: bool = False
    signed: bool = False
    signed_by: Optional[str] = None
    expires_at: Optional[datetime] = None


class JournalEntry(PatientInstance):
    """A patient-side log through a journal plugin. Mapped fields become (patient-perceived) observations."""
    plugin_key: str
    care_action_id: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)
    recorded_at: datetime = Field(default_factory=utcnow)


class Turn(BaseModel):
    id: str
    speaker_kind: Literal["human", "agent"]
    speaker_id: str
    speaker_role: str
    content: str
    at: datetime = Field(default_factory=utcnow)
    proposals: List[str] = Field(default_factory=list, description="Proposals extracted from this turn")


class Conversation(Instance):
    """An AI-driven conversation, about one patient or general. The actor role shapes its structure."""
    patient_id: Optional[str] = None
    scope: Literal["patient", "general"] = "patient"
    actor_role: str = Field("clinician", description="Whose conversation this is: clinician, patient, coordinator ...")
    title: str = "Conversation"
    structure: Dict[str, Any] = Field(default_factory=dict, description="From the conversation definition for this actor role")
    turns: List[Turn] = Field(default_factory=list)
