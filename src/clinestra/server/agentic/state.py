"""
Per-site AI and portal state kept in the harness store: settings, model providers (secrets encrypted), agents, the
AI audit log, model threads and portal sign-in state.

These are private document kinds (prefixed ``_ai.`` / ``_portal.``). They are not concepts: no Clinical API route
reads them, and they never reach the hash-chained clinical timeline. Every read and write is scoped to one site.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

from ...storage import Store

__all__ = ["SiteState", "new_id"]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class SiteState:
    def __init__(self, store: Store, site: str):
        self.store, self.site = store, site

    # ── documents ──
    def put(self, kind: str, id: str, data: dict) -> dict:
        doc = {**data, "id": id, "site": self.site}
        doc.setdefault("created_at", f"{time.time():017.6f}")
        self.store.put(kind, doc)
        return doc

    def get(self, kind: str, id: str) -> Optional[dict]:
        doc = self.store.get(kind, id, self.site)
        return doc if doc and doc.get("site") == self.site else None

    def all(self, kind: str, **where) -> List[dict]:
        return [d for d in self.store.list(kind, self.site, None, **where) if d.get("site") == self.site]

    def delete(self, kind: str, id: str) -> None:
        if hasattr(self.store, "delete"):
            self.store.delete(kind, id, self.site)
        else:  # stores without delete: tombstone (readers skip it)
            self.put(kind, id, {"_deleted": True})

    # ── key/value settings ──
    def kv_get(self, key: str, default: Any = None) -> Any:
        doc = self.get("_ai.kv", key)
        return doc["value"] if doc and not doc.get("_deleted") else default

    def kv_set(self, key: str, value: Any) -> None:
        self.put("_ai.kv", key, {"value": value})

    # ── logs ──
    def admin_log(self, actor: str, action: str, detail: Any = None) -> None:
        self.put("_ai.admin_log", new_id("adm"), {"at": time.time(), "actor": actor, "action": action, "detail": detail})

    def audit(self, **f: Any) -> str:
        aid = new_id("ai")
        self.put("_ai.audit", aid, {
            "at": time.time(), "actor": f.get("actor"), "role": f.get("role"), "feature": f.get("feature"),
            "model": f.get("model"), "provider": f.get("provider"), "status": f.get("status"),
            "input_tokens": f.get("input_tokens", 0) or 0, "output_tokens": f.get("output_tokens", 0) or 0,
            "latency_ms": f.get("latency_ms", 0) or 0, "tool_calls": f.get("tool_calls") or [],
            "detail": f.get("detail"), "patient_id": f.get("patient_id")})
        return aid

    def audit_since(self, since: float, actor: Optional[str] = None) -> List[dict]:
        return [r for r in self.all("_ai.audit") if r["at"] > since and (actor is None or r["actor"] == actor)]

    def tokens_since(self, since: float, actor: Optional[str] = None) -> int:
        return sum((r["input_tokens"] or 0) + (r["output_tokens"] or 0) for r in self.audit_since(since, actor))

    # ── model conversation (console-driven tool loop) ──
    def conversation(self, cid: Optional[str], actor: str) -> tuple:
        if cid:
            doc = self.get("_ai.chat", cid)
            if doc and doc["actor"] == actor:
                return cid, doc["messages"]
        cid = new_id("conv")
        self.put("_ai.chat", cid, {"actor": actor, "messages": [], "updated_at": time.time()})
        return cid, []

    def save_conversation(self, cid: str, messages: list) -> None:
        doc = self.get("_ai.chat", cid) or {}
        self.put("_ai.chat", cid, {**doc, "messages": messages, "updated_at": time.time()})

    def records(self, kind: str) -> List[Dict[str, Any]]:
        return [d for d in self.all(kind) if not d.get("_deleted")]
