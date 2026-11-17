"""
The six deterministic calculation kernels behind Assessments (forms and
calculators).

Every kernel has
the same signature::

    kernel(inputs: dict, reference_rows: list | dict, config: dict) -> dict

``inputs`` are what the clinician or patient entered. ``reference_rows`` are the
values the result is interpreted against (score bands, dose bands, logistic
coefficients, decision gates, exposure weights); in Clinestra they come from a
pack rather than from code, so recalibrating is a data change.
"""
import math

from .rules import UnsafeExpression, safe_eval

__all__ = ["KERNELS", "run_kernel", "weighted_point_sum", "weight_banded_dose",
           "eval_reference_equation", "logistic_risk", "exposure_integral", "boolean_gate"]

# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def _numeric_values(inputs):
    """Flatten input values to a list of floats (band points / free numbers)."""
    out = []
    for value in inputs.values():
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, bool):
                out.append(1.0 if item else 0.0)
            elif isinstance(item, (int, float)):
                out.append(float(item))
            elif isinstance(item, dict) and isinstance(item.get("points"), (int, float)):
                out.append(float(item["points"]))
            else:
                try:
                    out.append(float(item))
                except (TypeError, ValueError):
                    continue
    return out


def _as_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _band_for_score(score, bands):
    """Pick the interpretation band whose [min, max] contains score."""
    for band in bands:
        low = _as_float(band.get("min"), float("-inf"))
        high = _as_float(band.get("max"), float("inf"))
        if low <= score <= high:
            return band
    return None


def _ckd_stage(egfr):
    """KDIGO GFR category from an eGFR value (mL/min/1.73m²)."""
    if egfr >= 90:
        return "G1"
    if egfr >= 60:
        return "G2"
    if egfr >= 45:
        return "G3a"
    if egfr >= 30:
        return "G3b"
    if egfr >= 15:
        return "G4"
    return "G5"


# ─────────────────────────────────────────────────────────────────────────────
# the 6 compute functions
# ─────────────────────────────────────────────────────────────────────────────

def weighted_point_sum(inputs, graph_data, compute_config):
    """Σ(band points) clamped to max_score, mapped to an interpretation band.

    Also surfaces safety-item state (e.g. PHQ-9 item 9) so a safety-banner
    output can flag it independent of the total.
    """
    total = sum(_numeric_values(inputs))
    max_score = compute_config.get("max_score")
    capped = False
    if isinstance(max_score, (int, float)) and total > max_score:
        total, capped = max_score, True
    band = _band_for_score(total, graph_data or [])

    # Safety items (config-declared): expose the first one's value + a flag.
    safety_items = compute_config.get("safety_items") or []
    item_value = None
    flagged = False
    for param in safety_items:
        raw = inputs.get(param)
        num = _as_float(raw.get("points") if isinstance(raw, dict) else raw)
        if num is not None:
            item_value = num if item_value is None else item_value
            if num >= 1:
                flagged = True
    return {
        "total_score": round(total, 4),
        "band_label": (band or {}).get("band_label"),
        "band_text": (band or {}).get("text"),
        "evidence_grade": (band or {}).get("grade"),
        "max_score": max_score,
        "capped": capped,
        "item_9_value": item_value,
        "flagged": flagged,
    }


def weight_banded_dose(inputs, graph_data, compute_config):
    """Pick the dose row matching patient weight and renal function.

    Renal function is read as a numeric eGFR-like input (declared via
    `compute_config.renal_adjust_param`, default "egfr") and matched against
    each graph row's own `min_egfr`/`max_egfr` band — the graph, not the
    caller, owns the renal-band boundaries and their `renal_band` label
    (e.g. "standard"/"renally_adjusted"/"contraindicated"). A row without
    egfr bounds matches any egfr (back-compat with weight-only dosing rules).
    """
    weight = None
    for key in ("body_weight_kg", "weight_kg", "weight"):
        if key in inputs:
            weight = _as_float(inputs[key])
            break
    renal_param = compute_config.get("renal_adjust_param", "egfr")
    egfr = _as_float(inputs.get(renal_param))

    chosen = None
    for row in graph_data or []:
        low = _as_float(row.get("min_weight"), float("-inf"))
        high = _as_float(row.get("max_weight"), float("inf"))
        if weight is not None and not (low <= weight <= high):
            continue
        e_low = _as_float(row.get("min_egfr"), float("-inf"))
        e_high = _as_float(row.get("max_egfr"), float("inf"))
        if egfr is not None and not (e_low <= egfr <= e_high):
            continue
        chosen = row
        break
    if not chosen:
        return {"dose_mg": None, "unit": None, "capped": False, "band": None,
                "note": "No dose band matched the provided inputs."}

    dose = _as_float(chosen.get("dose_mg"))
    band = chosen.get("renal_band") or "standard"

    # Elderly dose reduction — optional, applies a flat factor above a
    # configured age threshold (declared, not hardcoded per-drug).
    age = _as_float(inputs.get("age"))
    elderly_age = compute_config.get("elderly_reduction_age")
    elderly_applied = False
    if dose is not None and elderly_age is not None and age is not None and age >= elderly_age:
        dose *= _as_float(compute_config.get("elderly_reduction_factor"), 0.75)
        elderly_applied = True

    cap = _as_float(chosen.get("max_dose_mg"))
    capped = False
    if cap is not None and dose is not None and dose > cap:
        dose, capped = cap, True

    # Round down to the nearest available tablet strength so the dose is
    # actually prescribable, never rounding up past the computed target.
    if dose is not None and compute_config.get("round_to_tablet"):
        strengths = sorted(s for s in (compute_config.get("tablet_strengths") or []) if s > 0)
        below = [s for s in strengths if s <= dose]
        if below:
            unit_dose = max(below)
            dose = math.floor(dose / unit_dose) * unit_dose

    return {
        "dose_mg": dose,
        "unit": chosen.get("unit") or compute_config.get("unit", "mg"),
        "capped": capped,
        "cap_reason": chosen.get("cap_reason") or (
            "maximum daily dose reached" if capped else None),
        "band": band,
        "divided_doses": chosen.get("divided_doses", 2),
        "renal_adjustment_applied": band != "standard",
        "elderly_adjustment_applied": elderly_applied,
        # Target serum level + sampling note flow from the dose-band row when
        # the graph carries them (forward-compatible with the reference seed).
        "target_min": chosen.get("target_min"),
        "target_max": chosen.get("target_max"),
        "sampling_note": chosen.get("sampling_note"),
    }


def eval_reference_equation(inputs, graph_data, compute_config):
    """Evaluate a graph-held formula against bound input variables (AST-safe)."""
    row = (graph_data[0] if graph_data else {}) or {}
    expression = row.get("expression") or compute_config.get("expression")
    if not expression:
        return {"predicted_value": None, "note": "No equation defined."}
    variables = {}
    for key, value in inputs.items():
        num = _as_float(value)
        variables[key] = num if num is not None else value
    coefficients = row.get("coefficients") or compute_config.get("coefficients") or {}
    if isinstance(coefficients, dict):
        variables.update(coefficients)
    value = safe_eval(expression, variables)
    low = _as_float(row.get("reference_range_low"))
    high = _as_float(row.get("reference_range_high"))
    if low is None and high is None:
        flag = None
    else:
        flag = "normal"
        if isinstance(value, (int, float)):
            if low is not None and value < low:
                flag = "below_reference"
            elif high is not None and value > high:
                flag = "above_reference"
    for rule in (row.get("flag_rules") or compute_config.get("flag_rules") or []):
        expr = rule.get("expression")
        if expr and safe_eval(expr, {**variables, "value": value}):
            flag = rule.get("flag")
    percent = None
    ref_target = compute_config.get("percent_of")
    if isinstance(value, (int, float)) and isinstance(ref_target, (int, float)) and ref_target:
        percent = round(100.0 * value / ref_target, 1)
    rounded = round(value, 4) if isinstance(value, (int, float)) else value
    unit = row.get("unit") or compute_config.get("unit")
    out = {
        "predicted_value": rounded,
        "value": rounded,
        "percent_predicted": percent,
        "flag": flag,
        "unit": unit,
    }
    # GFR equations: expose a friendly `egfr` alias + KDIGO stage / lithium flag,
    # both deterministically derivable from the value (self-contained).
    if isinstance(rounded, (int, float)) and "1.73" in str(unit or ""):
        out["egfr"] = rounded
        out["ckd_stage"] = _ckd_stage(rounded)
        out["lithium_flag"] = "standard_dosing" if rounded >= 60 else "renal_adjustment"
    return out


def logistic_risk(inputs, graph_data, compute_config):
    """1 / (1 + e^-(β0 + Σ βx)) from graph coefficients."""
    coeffs = graph_data or []
    intercept = 0.0
    for row in coeffs:
        if row.get("intercept") is not None:
            intercept = _as_float(row.get("intercept"), 0.0)
            break
    if not intercept:
        intercept = _as_float(compute_config.get("intercept"), 0.0)

    present = set()
    for value in inputs.values():
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, dict):
                present.add(item.get("value") or item.get("predictor"))
            else:
                present.add(item)

    linear = intercept
    contributions = []
    for row in coeffs:
        predictor = row.get("predictor")
        beta = _as_float(row.get("beta"), 0.0)
        x = inputs.get(predictor)
        if x is None:
            x = 1.0 if predictor in present else 0.0
        else:
            x = _as_float(x, 0.0)
        linear += beta * x
        if x:
            contributions.append({"predictor": predictor, "beta": beta, "value": x})
    probability = 1.0 / (1.0 + math.exp(-linear))

    bands = compute_config.get("bands") or [
        {"band": "low", "max": 0.1}, {"band": "borderline", "max": 0.2},
        {"band": "intermediate", "max": 0.3}, {"band": "high", "max": 1.01},
    ]
    band_label = next((b.get("band") for b in bands
                       if probability <= _as_float(b.get("max"), 1.01)), "high")
    # Top contributors by |β·x|, shaped as {factor, weight} for the template.
    top = sorted(contributions, key=lambda c: abs(c["beta"] * c["value"]), reverse=True)
    top_contributors = [
        {"factor": str(c["predictor"]).replace("_", " ").title(),
         "weight": round(c["beta"], 2)}
        for c in top[:5]
    ]
    return {
        "probability": round(probability, 4),
        "band": band_label,
        "band_label": band_label,
        "linear_predictor": round(linear, 4),
        "contributions": contributions,
        "top_contributors": top_contributors,
    }


def exposure_integral(inputs, graph_data, compute_config):
    """Σ(intensity × duration × agent weight) with banded interpretation."""
    weights = {row.get("agent"): row for row in (graph_data or [])}
    default_weight = _as_float(compute_config.get("default_weight"), 1.0)

    rows = None
    for value in inputs.values():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            rows = value
            break
        if isinstance(value, dict) and "rows" in value:
            rows = value["rows"]
            break
    rows = rows or []

    index = 0.0
    dominant = None
    dominant_val = -1.0
    for row in rows:
        agent = row.get("agent") or row.get("job_role")
        intensity = _as_float(
            row.get("intensity") or row.get("concentration")
            or row.get("vibration_magnitude"), 0.0)
        duration = _as_float(row.get("duration") or row.get("years"), 0.0)
        weight_row = weights.get(agent, {})
        weight = _as_float(weight_row.get("weight"), default_weight)
        contribution = intensity * duration * weight
        index += contribution
        if contribution > dominant_val:
            dominant_val, dominant = contribution, agent

    band_label = None
    for row in graph_data or []:
        low = _as_float(row.get("threshold_low"), float("-inf"))
        high = _as_float(row.get("threshold_high"), float("inf"))
        if low <= index <= high and row.get("band_label"):
            band_label = row.get("band_label")
            break
    return {
        "index_value": round(index, 4),
        "unit": compute_config.get("unit", "index"),
        "band": band_label,
        "band_label": band_label,
        "dominant_contributor_row": dominant,
    }


def boolean_gate(inputs, graph_data, compute_config):
    """Evaluate ordered decision gates; the highest-priority firing gate wins."""
    variables = {}
    for key, value in inputs.items():
        num = _as_float(value)
        variables[key] = num if num is not None else value
        if isinstance(value, bool):
            variables[key] = value

    matched = []
    for gate in graph_data or []:
        expression = gate.get("expression")
        fired = False
        if expression:
            try:
                fired = bool(safe_eval(expression, variables))
            except UnsafeExpression:
                fired = False
        if fired:
            matched.append(gate)

    if matched:
        winner = matched[0]  # graph_data ordered by priority DESC
        # Human-readable criterion text for display; slugs kept separately so
        # logic/analytics can still key on gate_id.
        gate_labels = [g.get("criterion") or g.get("gate_id") for g in matched]
        gate_ids = [g.get("gate_id") or g.get("criterion") for g in matched]
        return {
            "verdict": winner.get("verdict"),
            "severity": winner.get("severity"),
            "matched": gate_ids,
            "failing_gate": winner.get("criterion") or winner.get("gate_id"),
            "failing_gates": gate_labels,
            "failing_gate_ids": gate_ids,
            "failing_gate_rationale": winner.get("rationale") or winner.get("criterion"),
            "restrictions": winner.get("restrictions"),
        }
    return {
        "verdict": compute_config.get("default_verdict", "clear_to_start"),
        "severity": compute_config.get("default_severity", "none"),
        "matched": [],
        "failing_gate": None,
        "failing_gates": [],
        "failing_gate_ids": [],
        "failing_gate_rationale": None,
        "restrictions": None,
    }


KERNELS = {
    "weighted_point_sum": weighted_point_sum,
    "weight_banded_dose": weight_banded_dose,
    "eval_reference_equation": eval_reference_equation,
    "logistic_risk": logistic_risk,
    "exposure_integral": exposure_integral,
    "boolean_gate": boolean_gate,
}


def run_kernel(name, inputs, reference_rows=None, config=None):
    """Run one kernel by name. Raises KeyError for an unknown kernel."""
    if name not in KERNELS:
        raise KeyError(f"Unknown kernel '{name}'. Available: {sorted(KERNELS)}")
    return KERNELS[name](inputs or {}, reference_rows or [], config or {})
