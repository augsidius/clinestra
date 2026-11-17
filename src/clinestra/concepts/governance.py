"""Governance: consents, approvals, proposals and the definitions (templates) every configurable concept uses."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Literal, Optional

from pydantic import Field

from .common import Instance, PatientInstance

__all__ = ["Consent", "Approval", "Proposal", "Definition", "CONSENT_SCOPES"]

CONSENT_SCOPES = ("treatment", "data_sharing", "ai", "procedure", "communication", "research")


class Consent(PatientInstance):
    """A patient's (or guardian's) consent for one scope. Gated actions are refused without an active one."""
    consent_scope: Literal["treatment", "data_sharing", "ai", "procedure", "communication", "research"]
    granted_by: Literal["self", "guardian"] = "self"
    guardian_id: Optional[str] = Field(None, description="Patient id of the guardian, when granted_by='guardian'")
    document_id: Optional[str] = Field(None, description="Signed consent form (clinical document)")
    valid_until: Optional[datetime] = None
    details: Optional[str] = None


class Approval(Instance):
    """A request to move a record into a gated state. Decided by someone other than the requester."""
    patient_id: Optional[str] = None
    subject_kind: str
    subject_id: str
    to_state: str
    reason: Optional[str] = None
    decided_by: Optional[str] = None
    decision_note: Optional[str] = None


class Proposal(Instance):
    """A change suggested by a reaction, an AI agent or a cohort rule. A clinician accepts or rejects it."""
    patient_id: Optional[str] = None
    op: Literal["create", "transition", "update"]
    target_kind: str
    target_id: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)
    to_state: Optional[str] = None
    origin: str = Field(..., description="reaction:<key>, agent:<id>, cohort_rule:<key> ...")
    rationale: Optional[str] = None
    result_id: Optional[str] = None
    decided_by: Optional[str] = None


class Definition(Instance):
    """A configured template for a concept (a cohort, a protocol, a note page layout ...). Versioned; published ones are frozen.

    Library definitions come from packs and are shared across sites; sites can add their own.
    """
    def_kind: str
    key: str
    version: int = 1
    name: str
    description: Optional[str] = None
    config: Dict[str, Any] = Field(default_factory=dict)
    pack_id: Optional[str] = None
    review_status: Literal["draft", "in_review", "approved", "rejected"] = "draft"
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    review_ref: Optional[str] = None
    review_notes: Optional[str] = None

