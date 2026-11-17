"""Base classes for concept instances and small shared value types."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field

from ..base import Record

__all__ = ["Instance", "PatientInstance", "Schedule", "Effect", "SYSTEM_FIELDS"]

# Fields the harness sets. They never appear in create or update bodies.
SYSTEM_FIELDS = frozenset({"id", "site", "created_at", "created_by", "status", "updated_at", "updated_by",
                           "deleted", "definition_id", "definition_version"})


class Instance(Record):
    """A stored concept instance. ``status`` follows the concept's state machine."""
    status: str = ""
    definition_key: Optional[str] = Field(None, description="Configured template this was created from (latest published version is used).")
    definition_id: Optional[str] = None
    definition_version: Optional[int] = None
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None
    deleted: bool = False


class PatientInstance(Instance):
    patient_id: str


class Schedule(BaseModel):
    """A repeating schedule, computed by the cadence engine."""
    every_days: int = Field(..., gt=0)
    anchor: Optional[datetime] = Field(None, description="First occurrence; defaults to creation time")
    until: Optional[datetime] = None
    grace_days: int = 2


class Effect(BaseModel):
    """What a reaction does when a source concept changes state.

    Values in ``data`` that are strings starting with ``$`` are looked up in
    the source record, e.g. ``"$patient_id"`` or ``"$id"``. Effects on clinical
    concepts become proposals; everything else is applied automatically.
    """
    op: Literal["create", "transition"] = "create"
    kind: str
    data: Dict[str, Any] = Field(default_factory=dict)
    target_id: Optional[str] = Field(None, description="For transition: id (or $field) of the record to move")
    to: Optional[str] = None
    rationale: Optional[str] = None

