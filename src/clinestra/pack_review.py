"""Clinical review sheets. The sheet lists values; applying a review never changes config."""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import List

_NUMBER = re.compile(r"(?<![\w.])(-?\d+(?:\.\d+)?)")


def _literals(obj, path="") -> List[tuple]:
    found = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "evidence":
                continue
            found.extend(_literals(value, f"{path}.{key}" if path else key))
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            found.extend(_literals(value, f"{path}[{i}]"))
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        found.append((path, obj))
    elif isinstance(obj, str):
        for match in _NUMBER.findall(obj):
            found.append((path, match))
    return found


def review_rows(definitions: List[dict]) -> List[dict]:
    rows = []
    for d in definitions:
        cfg = d.get("config") or {}
        base = {
            "def_kind": d.get("def_kind"), "key": d.get("key"), "version": d.get("version", 1),
            "review_status": d.get("review_status", "draft"),
            "reviewer_decision": "", "corrected_value": "", "citation": "", "notes": "",
        }
        lits = _literals(cfg) or [("", "")]
        for path, value in lits:
            warning = ""
            if path.endswith("reference_range_low"):
                high = cfg.get("reference_range_high")
                if high is not None and str(high) == str(value):
                    warning = "degenerate range: low == high"
            rows.append({**base, "item": path, "value": value, "lint": warning})
        if d.get("def_kind") in ("protocol", "regimen", "reaction", "cohort"):
            evidence = cfg.get("evidence")
            if not isinstance(evidence, list) or not evidence:
                rows.append({**base, "item": "evidence", "value": "", "lint": "no evidence citations"})
        if d.get("def_kind") == "calculator" and not cfg.get("inputs"):
            rows.append({**base, "item": "inputs", "value": "", "lint": "calculator has no inputs"})
    return rows


def write_sheet(definitions: List[dict], path: str) -> int:
    rows = review_rows(definitions)
    dest = Path(path)
    fields = ["def_kind", "key", "version", "item", "value", "lint", "review_status",
              "reviewer_decision", "corrected_value", "citation", "notes"]
    with dest.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def apply_review(pack_dir: str, sheet_path: str) -> dict:
    """Record reviewer decisions on definition files. Config values are never written."""
    pack = Path(pack_dir)
    decisions = {}
    with open(sheet_path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            decision = (row.get("reviewer_decision") or "").strip().lower()
            if decision not in ("approve", "reject"):
                continue
            decisions[(row["def_kind"], row["key"])] = {
                "review_status": "approved" if decision == "approve" else "rejected",
                "review_notes": row.get("notes") or "",
                "review_ref": row.get("citation") or "",
            }
    changed = 0
    manifest = json.loads((pack / "pack.json").read_text(encoding="utf-8"))
    for name in manifest.get("definitions", []):
        file = pack / name
        data = json.loads(file.read_text(encoding="utf-8"))
        items = data if isinstance(data, list) else [data]
        touched = False
        for item in items:
            before = json.dumps(item.get("config"), sort_keys=True, default=str)
            found = decisions.get((item.get("def_kind"), item.get("key")))
            if not found:
                continue
            item["review_status"] = found["review_status"]
            item["review_notes"] = found["review_notes"]
            item["review_ref"] = found["review_ref"]
            after = json.dumps(item.get("config"), sort_keys=True, default=str)
            if before != after:
                raise RuntimeError(f"apply-review tried to change config of {item.get('key')}")
            touched = True
            changed += 1
        if touched:
            file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return {"updated": changed}
