"""
Timeline: an append-only, hash-chained record of everything done to a patient.

Each
entry stores a SHA-256 hash of its content plus the previous entry's hash, so
any edit or deletion is detectable with :func:`verify_chain`.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .base import Actor, new_id, utcnow

__all__ = ["TimelineEntry", "make_entry", "verify_chain"]


class TimelineEntry(BaseModel):
    id: str
    site: str
    patient_id: Optional[str]
    event: str = Field(..., description="What happened, e.g. 'cohort.assigned'")
    subject_kind: str
    subject_id: str
    actor_id: str
    actor_role: str
    actor_type: str
    data: Dict[str, Any] = {}
    created_at: datetime = Field(default_factory=utcnow)
    prev_hash: Optional[str] = None
    hash: str = ""


def _digest(entry: dict) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def make_entry(actor: Actor, patient_id: Optional[str], event: str, subject_kind: str, subject_id: str,
               data: Optional[dict], prev_hash: Optional[str]) -> TimelineEntry:
    e = TimelineEntry(id=new_id("evt"), site=actor.site, patient_id=patient_id, event=event,
                      subject_kind=subject_kind, subject_id=subject_id, actor_id=actor.id,
                      actor_role=actor.role, actor_type=actor.actor_type, data=data or {}, prev_hash=prev_hash)
    d = json.loads(e.model_dump_json())
    e.hash = _digest(d)
    return e


def verify_chain(entries: List[dict]) -> Dict[str, Any]:
    """Check a patient's timeline. Returns ``{"valid": bool, "checked": n, "broken_at": id|None}``."""
    prev = None
    for i, e in enumerate(entries):
        if e.get("prev_hash") != prev or _digest(e) != e.get("hash"):
            return {"valid": False, "checked": i, "broken_at": e.get("id")}
        prev = e["hash"]
    return {"valid": True, "checked": len(entries), "broken_at": None}
