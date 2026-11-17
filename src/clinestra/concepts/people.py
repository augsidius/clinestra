"""People: patients (and family members, who are patients too), providers, care team members, care teams, settings."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, computed_field

from .common import Instance, PatientInstance

__all__ = ["Patient", "Relationship", "Provider", "CareTeamMember", "TeamSlot", "CareTeam", "ClinicalSetting",
           "STANDARD_DEMOGRAPHICS"]

# Optional structured demographics a site can enable (see the ``patient_fields`` definition).
STANDARD_DEMOGRAPHICS: Dict[str, Dict[str, Any]] = {
    "occupation": {"type": "choice", "options": ["unemployed", "student", "homemaker", "unskilled", "semi_skilled",
                                                  "skilled", "clerical", "professional", "retired", "other"]},
    "socio_economic_status": {"type": "choice", "options": ["lower", "upper_lower", "lower_middle", "upper_middle", "upper"]},
    "marital_status": {"type": "choice", "options": ["single", "married", "divorced", "separated", "widowed", "other"]},
    "diet": {"type": "choice", "options": ["vegetarian", "non_vegetarian", "eggetarian", "vegan", "other"]},
    "education": {"type": "choice", "options": ["none", "primary", "secondary", "graduate", "postgraduate"]},
    "tobacco_use": {"type": "choice", "options": ["never", "former", "current"]},
    "alcohol_use": {"type": "choice", "options": ["never", "occasional", "regular", "former"]},
    "physical_activity": {"type": "choice", "options": ["sedentary", "light", "moderate", "high"]},
    "religion": {"type": "text"}, "language": {"type": "text"}, "phone": {"type": "text"}, "email": {"type": "text"},
    "address": {"type": "text"}, "pincode": {"type": "text"},
}


class Patient(Instance):
    """Anyone seeking health or care. Family members are registered as patients too, with a flag.

    Mandatory: name, country, gender, and either ``date_of_birth`` or an
    ``age_years`` recorded on ``age_recorded_on`` (age is then computed, so it never goes stale).
    """
    patient_id: Optional[str] = Field(None, description="Same as id; set by the harness")
    name: str
    country: str = Field(..., min_length=2)
    gender: Literal["male", "female", "other", "unknown"]
    date_of_birth: Optional[date] = None
    age_years: Optional[int] = Field(None, ge=0, le=130)
    age_recorded_on: Optional[date] = None
    age_is_estimated: bool = False
    demographics: Dict[str, Any] = Field(default_factory=dict, description="Standard optional fields enabled by the site")
    custom_fields: Dict[str, Any] = Field(default_factory=dict, description="Site-specific fields from the patient_fields definition")
    identifiers: Dict[str, str] = Field(default_factory=dict, description="e.g. {'abha': '...', 'mrn': '...'}")
    is_guardian: bool = False
    is_care_taker: bool = False

    @computed_field  # type: ignore[misc]
    @property
    def age(self) -> Optional[int]:
        today = date.today()
        if self.date_of_birth:
            d = self.date_of_birth
            return today.year - d.year - ((today.month, today.day) < (d.month, d.day))
        if self.age_years is not None:
            base = self.age_recorded_on or self.created_at.date()
            return self.age_years + max(0, (today - base).days // 365)
        return None


class Relationship(PatientInstance):
    """Links a patient to a family member (another patient record). Consent and communication can be split."""
    related_patient_id: str
    relation: str = Field(..., description="mother, spouse, son, friend ...")
    is_guardian: bool = False
    is_care_taker: bool = False
    consent_scopes: List[str] = Field(default_factory=list, description="Consent scopes this person may grant for the patient")
    receives_communication: bool = False


class Provider(Instance):
    """Primary care provider: any clinician (generalist, specialist, super-specialist) who makes clinical decisions."""
    name: str
    specialty: Optional[str] = None
    super_specialty: Optional[str] = None
    qualifications: List[str] = Field(default_factory=list)
    registration_no: Optional[str] = None
    user_id: Optional[str] = Field(None, description="Login id of this person, to match an authenticated actor")


class CareTeamMember(Instance):
    """Anyone in a role other than the primary clinical decision-maker: nurses, coordinators, CHWs, dietitians ..."""
    name: str
    skills: List[str] = Field(default_factory=list)
    roles: List[str] = Field(default_factory=list, description="Roles this person may fill, e.g. nurse, coordinator")
    reports_to: Optional[str] = Field(None, description="Provider or care team member id")
    user_id: Optional[str] = None


class TeamSlot(BaseModel):
    person_id: str
    person_kind: Literal["provider", "care_team_member"]
    role: str = Field(..., description="Role key from the care team definition. 'primary' is the primary care provider.")
    encounter_id: Optional[str] = Field(None, description="Set when this role applies to one encounter only")
    since: Optional[datetime] = None


class CareTeam(PatientInstance):
    """The roster of people caring for one patient. The primary care provider is the member in the 'primary' role."""
    name: str = "Care team"
    members: List[TeamSlot] = Field(default_factory=list)


class ClinicalSetting(Instance):
    """A group of available services that clinical orders can be mapped to, with flags and integrations."""
    name: str
    setting_type: Optional[str] = Field(None, description="opd, ipd, home, tele, lab, pharmacy ...")
    services: List[str] = Field(default_factory=list, description="Services/orders this setting can fulfil")
    flags: Dict[str, bool] = Field(default_factory=dict, description="e.g. {'has_pharmacy': true, 'accepts_referrals': true}")
    integrations: List[Dict[str, Any]] = Field(default_factory=list, description="[{name, kind, endpoint}] order fulfilment, LIS, devices")
