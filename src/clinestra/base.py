"""Shared building blocks: who is acting and the fields every stored record carries."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["Actor", "Record", "utcnow", "new_id", "ROLES", "STAFF", "CLINICAL_DECIDERS", "system_actor", "admin_all_roles", "acting_role"]

Role = Literal["clinician", "nurse", "chw", "coordinator", "admin", "clinical_lead", "patient", "system"]
ROLES = ("clinician", "nurse", "chw", "coordinator", "admin", "clinical_lead", "patient", "system")
STAFF = frozenset({"clinician", "nurse", "chw", "coordinator"})
# Roles that may make a clinical decision (commit a clinical state, prescribe, accept a proposal ...).
CLINICAL_DECIDERS = frozenset({"clinician"})


def admin_all_roles() -> bool:
    """Pilot setting: an administrator also holds the clinician's and the patient's permissions.
    Off by default. Set CLINESTRA_ADMIN_ALL_ROLES=1 only for pilots; it removes separation of duties."""
    return os.environ.get("CLINESTRA_ADMIN_ALL_ROLES", "0") in ("1", "true", "on")


def acting_role(actor) -> str:
    """The role permission checks use: a human admin acts as a clinician while admin_all_roles() is on."""
    if actor.role == "admin" and getattr(actor, "actor_type", "human") == "human" and admin_all_roles():
        return "clinician"
    return actor.role


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Actor(BaseModel):
    """Whoever is performing an action.

    In the HTTP server the actor always comes from authentication, never from a
    request body. ``actor_type='agent'`` marks an AI assistant: it may read,
    converse, draft and propose, but never makes a clinical decision.

    A ``patient`` actor acts for ``patient_id``; when that person is a guardian
    or care-taker, ``represents`` lists the other patients they may act for.
    Each is checked against an active guardian relationship at access time.
    """
    model_config = ConfigDict(frozen=True)

    id: str
    site: str
    role: Role
    actor_type: Literal["human", "agent", "system", "device"] = "human"
    patient_id: Optional[str] = Field(None, description="Set when role='patient': the person this actor is.")
    represents: List[str] = Field(default_factory=list, description="Patients this actor may act for as guardian.")


def system_actor(site: str, why: str = "reactions") -> Actor:
    """The actor that applies automatic (non-clinical) reactions."""
    return Actor(id=f"system:{why}", site=site, role="system", actor_type="system")


class Record(BaseModel):
    """Fields every stored record carries."""
    model_config = ConfigDict(extra="forbid")

    id: str
    site: str
    created_at: datetime = Field(default_factory=utcnow)
    created_by: str
