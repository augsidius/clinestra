from datetime import datetime, timedelta, timezone

import pytest

from clinestra.engine import (UnsafeExpression, apply_fork_overlay, compute_trajectory, parse_case_mtes,
                              run_kernel, safe_eval, schedule_status)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_safe_eval_allows_arithmetic_and_conditionals():
    assert safe_eval("weight * 0.5 if age < 65 else weight * 0.25", {"weight": 70, "age": 70}) == 17.5
    assert safe_eval("sqrt(x) + max(1, 2)", {"x": 16}) == 6


@pytest.mark.parametrize("expr", ["__import__('os')", "open('x')", "x.__class__", "[i for i in range(3)]"])
def test_safe_eval_refuses_anything_else(expr):
    with pytest.raises(UnsafeExpression):
        safe_eval(expr, {"x": 1})


def test_weighted_point_sum_bands_and_safety_item():
    bands = [{"min": 0, "max": 4, "band_label": "Minimal"}, {"min": 5, "max": 27, "band_label": "Not minimal"}]
    r = run_kernel("weighted_point_sum", {"item_1": 1, "item_9": 1}, bands, {"max_score": 27, "safety_items": ["item_9"]})
    assert r["total_score"] == 2 and r["band_label"] == "Minimal" and r["flagged"] is True


def test_logistic_risk_and_boolean_gate():
    r = run_kernel("logistic_risk", {"smoker": 1}, [{"intercept": -2.0}, {"predictor": "smoker", "beta": 1.0}])
    assert 0.26 < r["probability"] < 0.27
    g = run_kernel("boolean_gate", {"sbp": 190}, [{"gate_id": "htn", "expression": "sbp >= 180", "verdict": "defer"}])
    assert g["verdict"] == "defer"


def test_dose_band_with_renal_adjustment():
    rows = [{"min_weight": 0, "max_weight": 200, "min_egfr": 60, "dose_mg": 1000},
            {"min_weight": 0, "max_weight": 200, "max_egfr": 59.9, "dose_mg": 500, "renal_band": "renally_adjusted"}]
    assert run_kernel("weight_banded_dose", {"weight_kg": 70, "egfr": 40}, rows)["dose_mg"] == 500


def test_unknown_kernel():
    with pytest.raises(KeyError):
        run_kernel("nope", {})


def test_fork_overlay_order_suppress_add_boost_pin():
    rows = [{"node_id": "a", "association": 0.9}, {"node_id": "b", "association": 0.5}, {"node_id": "c", "association": 0.4}]
    events = [{"event_type": "output_suppressed", "target_node_id": "a"},
              {"event_type": "output_added", "target_node_id": "d", "score": 0.1},
              {"event_type": "rerank", "target_node_id": "c", "boost": 2.0},
              {"event_type": "rerank", "target_node_id": "d", "rank_after": 1}]
    out = apply_fork_overlay(rows, events, "x", "association")
    assert [r["node_id"] for r in out] == ["d", "c", "b"]


def test_mtes_keeps_absences_and_colons():
    t = parse_case_mtes("SYM|fever|fever|presence:absent~OBS|bp|bp|value:120:80")
    assert t[0]["presence"] == "absent" and t[1]["properties_dict"]["value"] == "120:80"
    assert parse_case_mtes("garbage") == []


def test_schedule_status_done_overdue_missed():
    st = schedule_status(T0, 14, [T0 + timedelta(days=1)], now=T0 + timedelta(days=30), grace_days=3)
    assert [o["state"] for o in st["occurrences"]] == ["done", "missed", "overdue"]


def test_trajectory_status():
    pts = [{"at": T0, "value": 18}, {"at": T0 + timedelta(weeks=2), "value": 12}, {"at": T0 + timedelta(weeks=4), "value": 8}]
    t = compute_trajectory(pts, "<", 5)
    assert t["status"] == "on_track" and t["slope_per_week"] < 0 and t["baseline"] == 18
    assert compute_trajectory(pts[:1], "<", 5)["status"] == "too_little_data"
    assert compute_trajectory(pts + [{"at": T0 + timedelta(weeks=6), "value": 4}], "<", 5)["status"] == "met"
