"""
Structured content blocks for agents: instead of long markdown, an agent calls ``render_blocks`` with typed blocks
(patient header, stats, vitals, trends, scores, medications, care-plan progress, timeline, alerts, actions ...)
and the console draws them as real components.

The catalog below is the single source of truth: each component's props schema feeds the tool definition the
model sees, :func:`validate` trims what it sends to that shape (unknown types dropped, sizes capped), and each
``example`` drives the gallery in Admin → UI components. Components carry data only, never markup or script.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

__all__ = ["COMPONENTS", "TOOL", "catalog", "validate", "PROMPT_HINT"]

S, N, B = {"type": "string"}, {"type": "number"}, {"type": "boolean"}
STATUS = {"type": "string", "enum": ["normal", "info", "warning", "critical", "done", "pending", "overdue", "inactive"]}


def _arr(item: dict, max_items: int = 30) -> dict:
    return {"type": "array", "items": item, "maxItems": max_items}


def _obj(props: dict, required: Tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": props, "required": list(required)}


COMPONENTS: Dict[str, dict] = {
    "patient_header": {
        "label": "Patient header", "use": "Top of a patient answer: who, key tags and overall risk.",
        "props": _obj({"name": S, "subtitle": S, "tags": _arr(S, 8), "risk": _obj({"level": STATUS, "label": S})}, ("name",)),
        "example": {"name": "Arjun Nair", "subtitle": "52 y · M · MRN DEMO-0042 · Ward 4", "tags": ["Diabetes", "Hypertension"],
                    "risk": {"level": "warning", "label": "Moderate risk"}},
    },
    "stat_grid": {
        "label": "Stat grid", "use": "2-4 headline numbers (latest readings, counts) with status and trend.",
        "props": _obj({"title": S, "items": _arr(_obj({"label": S, "value": {"type": ["string", "number"]}, "unit": S, "status": STATUS,
                                                         "trend": {"type": "string", "enum": ["up", "down", "flat"]}, "hint": S}, ("label", "value")), 8)}, ("items",)),
        "example": {"items": [{"label": "BP", "value": "148/94", "unit": "mmHg", "status": "warning", "trend": "up"},
                              {"label": "SpO₂", "value": 94, "unit": "%", "status": "warning", "trend": "down"},
                              {"label": "FEV1/FVC", "value": 0.68, "status": "critical", "hint": "below 0.70"},
                              {"label": "Weight", "value": 71, "unit": "kg", "status": "normal", "trend": "flat"}]},
    },
    "observation_table": {
        "label": "Observation table", "use": "Vitals or lab results with reference ranges and flags.",
        "props": _obj({"title": S, "rows": _arr(_obj({"name": S, "value": {"type": ["string", "number"]}, "unit": S, "range": S, "flag": STATUS, "date": S}, ("name", "value")), 40)}, ("rows",)),
        "example": {"title": "Latest labs", "rows": [{"name": "HbA1c", "value": 8.2, "unit": "%", "range": "< 7.0", "flag": "warning", "date": "02 Oct 2026"},
                                                     {"name": "Creatinine", "value": 1.0, "unit": "mg/dL", "range": "0.7–1.3", "flag": "normal", "date": "02 Oct 2026"}]},
    },
    "trend_chart": {
        "label": "Trend chart", "use": "A reading over time (one or two series), optionally with a target band.",
        "props": _obj({"title": S, "unit": S, "series": _arr(_obj({"name": S, "points": _arr(_obj({"t": S, "v": N}, ("t", "v")), 60)}, ("name", "points")), 3),
                       "band": _obj({"low": N, "high": N, "label": S})}, ("series",)),
        "example": {"title": "Systolic BP", "unit": "mmHg", "band": {"low": 90, "high": 130, "label": "target"},
                    "series": [{"name": "Systolic", "points": [{"t": "Jun", "v": 152}, {"t": "Jul", "v": 146}, {"t": "Aug", "v": 141}, {"t": "Sep", "v": 138}, {"t": "Oct", "v": 148}]}]},
    },
    "score": {
        "label": "Score / gauge", "use": "A clinical score or risk estimate with its band and meaning.",
        "props": _obj({"name": S, "value": N, "max": N, "band": S, "status": STATUS, "interpretation": S}, ("name", "value")),
        "example": {"name": "mMRC dyspnoea", "value": 2, "max": 4, "band": "Moderate", "status": "warning", "interpretation": "Walks slower than peers because of breathlessness."},
    },
    "condition_list": {
        "label": "Conditions", "use": "Problem list with status, onset and severity.",
        "props": _obj({"title": S, "items": _arr(_obj({"name": S, "status": S, "since": S, "severity": STATUS, "note": S}, ("name",)))}, ("items",)),
        "example": {"items": [{"name": "Example chronic condition", "status": "active", "since": "2023", "severity": "warning", "note": "Illustrative only"},
                              {"name": "Essential hypertension", "status": "active", "since": "2021", "severity": "normal"}]},
    },
    "medication_list": {
        "label": "Medications", "use": "Current regimen: drug, dose, frequency, status.",
        "props": _obj({"title": S, "items": _arr(_obj({"name": S, "dose": S, "frequency": S, "route": S, "status": STATUS, "note": S}, ("name",)))}, ("items",)),
        "example": {"items": [{"name": "Amlodipine", "dose": "5 mg", "frequency": "once daily", "route": "oral", "status": "normal"},
                              {"name": "Tiotropium", "dose": "18 mcg", "frequency": "once daily", "route": "inhaled", "status": "pending", "note": "adherence 60% this month"}]},
    },
    "care_plan_progress": {
        "label": "Care plan progress", "use": "Goals with current vs target and percent progress.",
        "props": _obj({"title": S, "goals": _arr(_obj({"name": S, "current": S, "target": S, "progress": N, "status": STATUS}, ("name",)), 12)}, ("goals",)),
        "example": {"title": "Hypertension programme", "goals": [{"name": "BP below 130/80", "current": "148/94", "target": "<130/80", "progress": 40, "status": "warning"},
                                                                 {"name": "Daily walk 30 min", "current": "5/7 days", "target": "7/7", "progress": 71, "status": "normal"}]},
    },
    "checklist": {
        "label": "Checklist", "use": "Tasks, pending actions or due items with status.",
        "props": _obj({"title": S, "items": _arr(_obj({"text": S, "status": STATUS, "meta": S}, ("text",)))}, ("items",)),
        "example": {"title": "Pending", "items": [{"text": "Annual spirometry", "status": "overdue", "meta": "due 12 Sep"},
                                                  {"text": "Eye examination", "status": "pending", "meta": "due 30 Oct"},
                                                  {"text": "Chest X-ray", "status": "done", "meta": "04 Oct"}]},
    },
    "timeline": {
        "label": "Timeline", "use": "Recent events in date order (visits, results, changes).",
        "props": _obj({"title": S, "items": _arr(_obj({"date": S, "title": S, "detail": S, "status": STATUS}, ("date", "title")))}, ("items",)),
        "example": {"items": [{"date": "04 Oct", "title": "Annual review", "detail": "BP 148/94, SpO₂ 94%"},
                              {"date": "12 Sep", "title": "Spirometry missed", "status": "overdue"},
                              {"date": "20 Aug", "title": "Started amlodipine 5 mg", "status": "info"}]},
    },
    "alert": {
        "label": "Alert", "use": "One red flag or important caution. Use sparingly.",
        "props": _obj({"level": STATUS, "title": S, "body": S}, ("title",)),
        "example": {"level": "critical", "title": "SpO₂ trending down", "body": "94% today vs 97% three months ago with new exertional breathlessness. Consider review this week."},
    },
    "key_value": {
        "label": "Key facts", "use": "Short labelled facts (occupation, allergies, contacts).",
        "props": _obj({"title": S, "items": _arr(_obj({"label": S, "value": S}, ("label", "value")), 20)}, ("items",)),
        "example": {"title": "Background", "items": [{"label": "Occupation", "value": "Teacher"}, {"label": "Lives with", "value": "Family"}, {"label": "Allergies", "value": "None known"}]},
    },
    "table": {
        "label": "Table", "use": "Anything tabular that no other component fits.",
        "props": _obj({"title": S, "columns": _arr(S, 8), "rows": _arr(_arr(S, 8), 40)}, ("columns", "rows")),
        "example": {"title": "Cohort", "columns": ["Patient", "Unit", "Last visit"], "rows": [["Arjun Nair", "Ward 4", "04 Oct"], ["Meera Shah", "Clinic B", "18 Sep"]]},
    },
    "actions": {
        "label": "Action buttons", "use": "Buttons that open a console screen for the user.",
        "props": _obj({"items": _arr(_obj({"label": S, "screen": {"type": "string", "enum": ["patient", "case_sheet", "care_programs", "care_plans", "care_teams", "tasks", "inbox", "patients", "orders", "records"]},
                                           "patient_id": S}, ("label", "screen")), 6)}, ("items",)),
        "example": {"items": [{"label": "Open case sheet", "screen": "case_sheet"}, {"label": "View tasks", "screen": "tasks"}]},
    },
}

TOOL = {
    "name": "render_blocks",
    "description": ("Show structured information to the user as UI components instead of long text. Pass blocks in display order; "
                    "each block is {type, props} with type one of: "
                    + "; ".join(f"{k} ({v['use']})" for k, v in COMPONENTS.items())
                    + ". Use real values from the record only. Call it once per answer with all blocks, then reply with one or two short sentences."),
    "input_schema": _obj({"blocks": _arr({"oneOf": [_obj({"type": {"const": k}, "props": v["props"]}, ("type", "props")) for k, v in COMPONENTS.items()]}, 12)}, ("blocks",)),
}

PROMPT_HINT = ("\n\nWhen the answer has structure (a patient overview, readings, results, medications, goals, tasks, events), "
               "show it with the render_blocks tool and keep your text to a short lead-in or conclusion. Do not repeat in text what the components show.")


def catalog() -> List[dict]:
    return [{"type": k, "label": v["label"], "use": v["use"], "props": v["props"], "example": v["example"]} for k, v in COMPONENTS.items()]


def _fit(value: Any, schema: dict, depth: int = 0) -> Any:
    """Trim a value to the schema: drop unknown keys, cap arrays and strings, coerce scalars. None = drop."""
    if depth > 6 or value is None:
        return None
    t = schema.get("type")
    types = t if isinstance(t, list) else [t]
    if "object" in types:
        if not isinstance(value, dict):
            return None
        out = {}
        for k, sub in schema.get("properties", {}).items():
            if k in value:
                v = _fit(value[k], sub, depth + 1)
                if v is not None:
                    out[k] = v
        return out if all(r in out for r in schema.get("required", [])) else None
    if "array" in types:
        if not isinstance(value, list):
            return None
        items = [_fit(v, schema.get("items", {}), depth + 1) for v in value[: schema.get("maxItems", 30)]]
        return [v for v in items if v is not None]
    if "number" in types and isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if "number" in types and "string" not in types:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if "boolean" in types:
        return bool(value)
    text = str(value)[:600]
    if "enum" in schema and text not in schema["enum"]:
        return None
    return text


def validate(blocks: Any) -> List[dict]:
    """Keep only known component types with props trimmed to their schema (at most 12 blocks)."""
    out = []
    for b in (blocks if isinstance(blocks, list) else [])[:12]:
        if not isinstance(b, dict) or b.get("type") not in COMPONENTS:
            continue
        props = _fit(b.get("props") or {}, COMPONENTS[b["type"]]["props"])
        if props is not None:
            out.append({"type": b["type"], "props": props})
    return out
