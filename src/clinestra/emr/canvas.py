"""The Clinestra 2.0 EMR canvas on Clinestra 0.2 concepts.

A canvas page is a ``note_page``. Its sections (the 2.0 ``page_clinical_data``) are stored on the page, and every value is
also written to the page's ``blocks`` with the keys the 0.2 case sheet uses (``<section>__<field>`` for form fields,
``<section>`` for everything else, ad-hoc sections under ``additional_sections``). Both case-sheet UIs therefore read
and write the same page, and finalizing turns mapped fields into observations as usual.

Live edits arrive over the canvas socket, are kept in memory (last write wins, like the Redis buffer in 2.0) and are
flushed to the page on save, on read, on finalize, when the last socket closes, and a few seconds after typing stops.
"""
from __future__ import annotations

import copy
import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional

FIELD_TYPES = ("key_value", "readings", "regular_form")
TYPE_MAP = {"number": "numerical", "numeric": "numerical"}


def _field(f: dict) -> dict:
    key = f.get("key") or f.get("field_name") or f.get("name")
    return {"field_id": key, "field_name": key, "label": f.get("label") or key, "type": TYPE_MAP.get(f.get("type"), f.get("type") or "text"),
            "unit": f.get("unit"), "options": f.get("options") or [], "required": bool(f.get("required")), "min": f.get("min"),
            "max": f.get("max"), "placeholder": f.get("placeholder"), "observation_code": f.get("observation_code"), "value": ""}


def section_from_definition(s: dict) -> dict:
    """A 0.2 template section (``config.sections`` item) as a 2.0 canvas section with empty data."""
    st = s.get("section_type") or "text_area"
    cfg = copy.deepcopy(s.get("config") or {})
    for c in cfg.get("columns") or []:  # 2.0 tables show column labels
        c.setdefault("label", (c.get("name") or "").replace("_", " ").capitalize())
    out = {"section_id": s["section_id"], "section_name": s.get("section_name") or s["section_id"],
           "section_description": s.get("section_description") or cfg.get("description"), "config": cfg}
    if st in FIELD_TYPES:
        out["section_type"] = "regular_form"
        out["data"] = [_field(f) for f in cfg.get("fields") or []]
    elif st in ("tasks", "checklist"):
        out["section_type"] = st
        out["data"] = [{"id": o.get("id"), "field_id": o.get("id"), "label": o.get("label"), "value": o.get("label"), "checked": bool(o.get("checked"))}
                       for o in cfg.get("options") or []]
    else:
        out["section_type"] = st
        out["data"] = "" if st == "text_area" else []
    return out


def sections_from_definition(definition: Optional[dict]) -> List[dict]:
    cfg = (definition or {}).get("config") or {}
    if cfg.get("sections"):
        return [section_from_definition(s) for s in cfg["sections"]]
    # a block-only template: one field form holding every block
    blocks = cfg.get("blocks") or []
    if not blocks:
        return []
    fields = [{"key": b["key"], "label": b["label"], "type": "number" if b.get("input_type") == "number" else "text",
               "unit": b.get("unit"), "observation_code": b.get("observation_code"), "required": b.get("required")} for b in blocks]
    return [{"section_id": "page", "section_name": definition.get("name", "Case sheet"), "section_type": "regular_form", "config": {},
             "data": [_field(f) for f in fields], "_flat": True}]


def template_metadata(definition: Optional[dict], fallback: str = "Case sheet") -> dict:
    d = definition or {}
    return {"name": d.get("name") or fallback, "key": d.get("key"), "description": d.get("description") or "",
            "type": "clinical_note", "definition_id": d.get("id")}


def _block_key(section: dict, field: dict) -> str:
    return field["field_name"] if section.get("_flat") else f"{section['section_id']}__{field['field_name']}"


def overlay_blocks(sections: List[dict], blocks: Dict[str, Any]) -> List[dict]:
    """Fill section data from page blocks (values written by the standard case-sheet UI)."""
    if not blocks:
        return sections
    out = []
    known = set()
    for s in sections:
        s = copy.deepcopy(s)
        if s.get("section_type") in FIELD_TYPES and isinstance(s.get("data"), list):
            for f in s["data"]:
                if isinstance(f, dict) and f.get("field_name"):
                    k = _block_key(s, f)
                    known.add(k)
                    if k in blocks and blocks[k] not in (None, ""):
                        f["value"] = blocks[k]
        elif s["section_id"] in blocks:
            known.add(s["section_id"])
            v = blocks[s["section_id"]]
            if s.get("section_type") in ("tasks", "checklist") and isinstance(v, list) and v and isinstance(v[0], str):
                v = [{**it, "checked": it.get("id") in v or it.get("label") in v} for it in (s.get("data") or []) if isinstance(it, dict)]
            if v not in (None, ""):
                s["data"] = v
        out.append(s)
    ids = {s["section_id"] for s in out}
    for extra in blocks.get("additional_sections") or []:
        sec = (extra or {}).get("section") or {}
        if sec.get("section_id") and sec["section_id"] not in ids:
            s = section_from_definition(sec)
            v = extra.get("value")
            if v not in (None, ""):
                if s["section_type"] in FIELD_TYPES and isinstance(v, dict):
                    for f in s["data"]:
                        f["value"] = v.get(f["field_name"], "")
                else:
                    s["data"] = v
            out.append(s)
    return out


def blocks_from_sections(sections: List[dict], known: Optional[set] = None) -> Dict[str, Any]:
    """Page blocks for the canvas sections. ``known`` is the template's block keys (others go to additional_sections)."""
    blocks: Dict[str, Any] = {}
    extra = []
    for s in sections:
        if s.get("is_deleted"):
            continue
        st = s.get("section_type")
        data = s.get("data")
        if st in FIELD_TYPES and isinstance(data, list):
            vals = {f["field_name"]: f.get("value") for f in data if isinstance(f, dict) and f.get("field_name")}
            keys = {_block_key(s, {"field_name": k}): v for k, v in vals.items()}
            if known is None or any(k in known for k in keys):
                blocks.update({k: v for k, v in keys.items() if v not in (None, "") and (known is None or k in known)})
            else:
                extra.append({"section": _section_def(s), "value": {k: v for k, v in vals.items() if v not in (None, "")}})
        else:
            if st in ("tasks", "checklist") and isinstance(data, list):
                data = [it.get("label") or it.get("value") for it in data if isinstance(it, dict) and it.get("checked")]
            if st == "text_area" and isinstance(data, str) and "<" in data:  # the 2.0 editor writes HTML; blocks hold text
                from .markdown import html_to_markdown
                data = html_to_markdown(data)
            if known is None or s["section_id"] in known:
                if data not in (None, "", []):
                    blocks[s["section_id"]] = data
            else:
                extra.append({"section": _section_def(s), "value": data})
    if extra and (known is None or "additional_sections" in known):
        blocks["additional_sections"] = extra
    return blocks


def _section_def(s: dict) -> dict:
    cfg = dict(s.get("config") or {})
    if s.get("section_type") in FIELD_TYPES:
        cfg["fields"] = [{"key": f.get("field_name"), "label": f.get("label"), "type": f.get("type"), "unit": f.get("unit")}
                         for f in s.get("data") or [] if isinstance(f, dict)]
    return {"section_id": s["section_id"], "section_name": s.get("section_name"), "section_type": s.get("section_type"), "config": cfg}


# ───────────────────────── live edit buffer ─────────────────────────
class CanvasBuffer:
    """Pending socket operations per page, applied on flush (last write wins per section and operation)."""

    def __init__(self, flush: Callable[[str, str, List[dict]], None], idle_seconds: float = 4.0):
        self._ops: Dict[str, Dict[tuple, dict]] = {}
        self._site: Dict[str, str] = {}
        self._lock = threading.RLock()
        self._flush = flush
        self._idle = idle_seconds
        self._timers: Dict[str, threading.Timer] = {}

    def put(self, site: str, page_id: str, op: dict) -> None:
        key = (op.get("section_id"), op.get("type"))
        with self._lock:
            ops = self._ops.setdefault(page_id, {})
            if op.get("type") == "editor_new_section":  # each new section is its own op
                key = (op.get("section_id") or str(time.time()), "editor_new_section")
            ops.pop(key, None)
            ops[key] = op
            self._site[page_id] = site
            t = self._timers.pop(page_id, None)
            if t:
                t.cancel()
            t = threading.Timer(self._idle, self.flush, args=(page_id,))
            t.daemon = True
            self._timers[page_id] = t
            t.start()

    def flush(self, page_id: str) -> None:
        with self._lock:
            ops = list((self._ops.pop(page_id, None) or {}).values())
            site = self._site.get(page_id)
            t = self._timers.pop(page_id, None)
            if t:
                t.cancel()
        if ops and site:
            self._flush(site, page_id, ops)


def apply_ops(sections: List[dict], ops: List[dict]) -> List[dict]:
    """Apply socket operations to a page's sections (the 2.0 flush_redis_to_mongo rules)."""
    sections = copy.deepcopy(sections)
    by_id = {s.get("section_id"): s for s in sections}
    for op in ops:
        t, sid = op.get("type"), op.get("section_id")
        if t == "editor_new_section":
            try:
                content = json.loads(op.get("content") or "{}") if isinstance(op.get("content"), str) else (op.get("content") or {})
            except ValueError:
                continue
            if content.get("section_id") and content["section_id"] not in by_id:
                sections.append(content)
                by_id[content["section_id"]] = content
        elif t == "delete_section" and sid in by_id:
            by_id[sid]["is_deleted"] = True
        elif t == "section_wrapper_update" and sid in by_id:
            c = op.get("content") or {}
            for k in ("section_name", "section_type"):
                if c.get(k):
                    by_id[sid][k] = c[k]
        elif t == "editor_update" and sid in by_id:
            by_id[sid]["data"] = op.get("content")
            if op.get("items") is not None:
                by_id[sid]["items"] = op.get("items")
        if sid in by_id:
            by_id[sid]["meta_info"] = {"updated_by": op.get("user_id"), "updated_at": op.get("timestamp")}
    return sections


def reorder(sections: List[dict], sequence: List[str]) -> List[dict]:
    pos = {sid: i for i, sid in enumerate(sequence or [])}
    return sorted(sections, key=lambda s: pos.get(s.get("section_id"), len(pos)))


# ───────────────────────── EMRU scribe: apply dictated updates on the server ─────────────────────────
def _strip_html(s: Any) -> str:
    import re as _re
    return _re.sub(r"<[^>]+>", "", s).strip() if isinstance(s, str) else ("" if s is None else str(s))


def _find_section(sections: List[dict], section_id: str) -> Optional[dict]:
    for s in sections:
        if s.get("section_id") == section_id and not s.get("is_deleted"):
            return s
    norm = (section_id or "").lower().replace(" ", "_")
    for s in sections:
        if not s.get("is_deleted") and (s.get("section_name") or "").lower().replace(" ", "_") == norm:
            return s
    return None


def apply_emru(sections: List[dict], cmd: dict) -> bool:
    """Apply one parsed EMRU command to the sections in place (the 2.0 canvas rules, CanvasBasedEmrEditor.jsx).
    Returns True when a section changed."""
    if not cmd or cmd.get("type") not in (None, "emru_update") or not cmd.get("section_id") or not cmd.get("update_type"):
        return False
    s = _find_section(sections, cmd["section_id"])
    if not s:
        return False
    st, ut = s.get("section_type"), cmd["update_type"]
    data, fid, props = cmd.get("data"), cmd.get("field_id"), {k: v for k, v in (cmd.get("properties") or {}).items() if v not in (None, "null")}
    if data == "null":
        data = None
    cur = s.get("data")
    if ut in ("upd", "pop"):
        if st == "text_area":
            s["data"] = data or ""
        elif st in FIELD_TYPES:
            rows = cur if isinstance(cur, list) else []
            hit = False
            for f in rows:
                if isinstance(f, dict) and fid and (f.get("field_id") == fid or f.get("field_name") == fid):
                    f["value"] = data
                    if props.get("unit"):
                        f["unit"] = props["unit"]
                    hit = True
            if not hit and rows and not fid:
                rows[0]["value"] = data
                hit = True
            if not hit:
                return False
        elif st == "table_form":
            rows = list(cur) if isinstance(cur, list) else []
            first = ((s.get("config") or {}).get("columns") or [{}])[0].get("name") or "value"
            key = str(data or fid or "").strip().lower()
            row = {first: data or fid, **props} if not isinstance(data, dict) else {**data, **props}
            idx = next((i for i, r in enumerate(rows) if isinstance(r, dict) and str(r.get(first, "")).strip().lower() == key), -1)
            if idx >= 0:
                rows[idx] = {**rows[idx], **row}
            else:
                rows.append(row)
            s["data"] = rows
        elif st in ("ordered_list", "unordered_list"):
            s["data"] = (cur if isinstance(cur, list) else []) + ([data] if data else [])
        else:
            s["data"] = data
    elif ut in ("app", "add"):
        if st == "text_area":
            text, new = _strip_html(cur), _strip_html(data)
            if not new or text.lower().endswith(new.lower()):
                return False
            s["data"] = f"{text} {new}".strip()
        elif st in ("ordered_list", "unordered_list"):
            items = cur if isinstance(cur, list) else []
            if any(str(x).strip().lower() == str(data).strip().lower() for x in items):
                return False
            s["data"] = items + [data]
        elif st == "table_form":
            cols = (s.get("config") or {}).get("columns") or [{}]
            first = cols[0].get("name") or "value"
            target = fid or next((c.get("name") for c in cols if c.get("required")), first)
            row = {target: data, **props}
            rows = list(cur) if isinstance(cur, list) else []
            key = str(row.get(first, "")).strip().lower()
            idx = next((i for i, r in enumerate(rows) if isinstance(r, dict) and key and str(r.get(first, "")).strip().lower() == key), -1)
            if idx >= 0:
                rows[idx] = {**rows[idx], **row}
            else:
                rows.append(row)
            s["data"] = rows
        elif st in FIELD_TYPES and fid:
            return apply_emru(sections, {**cmd, "update_type": "pop"})
        else:
            return False
    elif ut in ("check", "uncheck") and st in ("tasks", "checklist"):
        items = cur if isinstance(cur, list) and cur else [dict(o) for o in (s.get("config") or {}).get("options") or []]
        hit = False
        for it in items:
            if fid and fid in (it.get("field_id"), it.get("id"), it.get("value"), it.get("label")):
                it["checked"] = ut == "check"
                hit = True
        if not hit:
            return False
        s["data"] = items
    else:
        return False
    s["meta_info"] = {"updated_by": "emr_scribe"}
    return True
