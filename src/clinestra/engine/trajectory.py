"""
Trajectory: the course of one measure for one patient over time.

New in the library. A trajectory is a view rebuilt from stored scores; it is
never a source of truth and never triggers anything. Gaps stay gaps.
"""
from datetime import datetime
from typing import List, Optional

__all__ = ["compute_trajectory", "compare"]

_OPS = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b,
        ">=": lambda a, b: a >= b, "==": lambda a, b: a == b}


def compare(value: float, comparator: str, target: float) -> bool:
    if comparator not in _OPS:
        raise ValueError(f"comparator must be one of {sorted(_OPS)}")
    return _OPS[comparator](value, target)


def _slope_per_week(points):
    if len(points) < 2:
        return None
    t0 = points[0][0]
    xs = [(t - t0).total_seconds() / 604800.0 for t, _ in points]
    ys = [v for _, v in points]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den, 4)


def compute_trajectory(points: List[dict], comparator: Optional[str] = None, target: Optional[float] = None,
                       by: Optional[datetime] = None, now: Optional[datetime] = None, min_points: int = 2) -> dict:
    """Summarise a series of ``{"at": datetime|iso, "value": float, "source_id": str}`` points.

    With a goal (``comparator`` and ``target``) the status is one of
    ``met``, ``on_track``, ``behind``, ``missed`` or ``too_little_data``.
    """
    pts = []
    for p in points:
        at = p["at"] if isinstance(p["at"], datetime) else datetime.fromisoformat(str(p["at"]))
        if p.get("value") is None:
            continue
        pts.append((at, float(p["value"]), p.get("source_id")))
    pts.sort(key=lambda x: x[0])
    out = {"points": [{"at": a.isoformat(), "value": v, "source_id": s} for a, v, s in pts],
           "count": len(pts), "baseline": pts[0][1] if pts else None, "latest": pts[-1][1] if pts else None,
           "latest_at": pts[-1][0].isoformat() if pts else None,
           "change_from_baseline": round(pts[-1][1] - pts[0][1], 4) if pts else None,
           "slope_per_week": _slope_per_week([(a, v) for a, v, _ in pts]), "status": None}
    if comparator is None or target is None:
        return out
    if len(pts) < min_points:
        out["status"] = "too_little_data"
        return out
    if compare(pts[-1][1], comparator, target):
        out["status"] = "met"
        return out
    if by is not None and now is not None and now > by:
        out["status"] = "missed"
        return out
    slope = out["slope_per_week"] or 0.0
    toward = (slope < 0) if comparator in ("<", "<=") else (slope > 0) if comparator in (">", ">=") else False
    out["status"] = "on_track" if toward else "behind"
    return out
