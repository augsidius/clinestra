"""MTES: the line format an AI assistant uses to log clinical terms it finds in text.

Self-contained.
One term per ``~``-separated line::

    TYPE|term|main>>parent>>grandparent|key:value::presence:absent

MTES parsing for case discussions, with presence/absence flags.

A
discussion needs two things it doesn't provide:

  1. **Presence.** A doctor's line "no fever, no chest pain" is clinical signal
     of exactly the same weight as a positive finding — a pertinent negative is
     what rules a differential *out*. So every term carries
     `presence: present | absent`, and absent terms are kept, not dropped.
  2. **Tolerant property parsing.** The reference parser does
     `key, value = property.split(":")`, which raises on any value containing a
     colon (a BP of `120:80`, a time of `14:30`) and — because the raise happens
     inside the per-term try — silently discards the whole term. Here the split
     is bounded, so a colon in a value costs nothing.
"""
from __future__ import annotations

ENTITY_TYPES = {
    "SYM": "Symptom", "OBS": "Observation", "CON": "Condition", "INV": "Investigation",
    "MED": "Medication", "PRO": "Procedure", "MSR": "Measure", "EXT": "External",
}
entity_type_master = ENTITY_TYPES

PRESENT = "present"
ABSENT = "absent"

# What the model may write in `presence:`; anything else is read as present,
# since an unflagged finding in a discussion is a positive one.
_ABSENT_WORDS = {
    "absent", "negative", "denied", "denies", "ruled out", "ruled_out",
    "not present", "no", "false",
}


def _normalise_presence(raw) -> str:
    if raw is None:
        return PRESENT
    value = str(raw).strip().lower().replace("-", " ")
    return ABSENT if value in _ABSENT_WORDS else PRESENT


def parse_case_mtes(mtes_text: str) -> list[dict]:
    """Parse an MTES block into term dicts carrying a presence flag.

    Returns [] for empty/garbage input — never raises. A single malformed line
    is skipped without taking the rest of the block with it.
    """
    if not mtes_text or not mtes_text.strip():
        return []

    terms: list[dict] = []
    for raw_line in mtes_text.replace("\n", "").split("~"):
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        entity_type, entity_term, hierarchy_raw = parts[0], parts[1], parts[2]
        properties_raw = parts[3] if len(parts) > 3 else ""

        properties: dict = {}
        for prop in properties_raw.split("::"):
            if ":" not in prop:
                continue
            key, value = prop.split(":", 1)
            key, value = key.strip(), value.strip()
            if not key:
                continue
            properties[key] = value.split("___") if "___" in value else value

        levels = [level.strip() for level in hierarchy_raw.split(">>")]
        presence = _normalise_presence(properties.pop("presence", None))

        terms.append({
            "entity_type": entity_type_master.get(entity_type.strip(), entity_type.strip()),
            "entity_term": entity_term.strip(),
            "hierarchy_raw": hierarchy_raw.strip(),
            "properties_dict": properties,
            "main_term": levels[0] if levels else hierarchy_raw.strip(),
            "parent_term": levels[1] if len(levels) > 1 else None,
            "grand_parent_term": levels[2] if len(levels) > 2 else None,
            "presence": presence,
        })
    return terms


def term_key(term: dict) -> tuple:
    """Identity of a finding, independent of how it was phrased.

    Used to supersede rather than accumulate: if the doctor first says "no
    fever" and later "fever 39C since morning", the case memory must show one
    fever, present — not a contradiction sitting in two lines.
    """
    return (
        (term.get("entity_type") or "").lower(),
        (term.get("main_term") or term.get("entity_term") or "").lower(),
    )


__all__ = ["parse_case_mtes", "term_key", "PRESENT", "ABSENT", "ENTITY_TYPES"]
