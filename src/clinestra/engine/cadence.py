"""
Cadence: turn a repeating schedule into facts (due, overdue, missed, done).

Previously a schedule was a free-text string such
as ``every_4wk`` or a ``repeat_frequency`` in days on a care action. Here it is
something the engine can compute with. It never sends, orders or moves
anything; it only reports.
"""
from datetime import datetime, timedelta, timezone
from typing import List, Optional

__all__ = ["occurrences", "schedule_status"]


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def occurrences(anchor: datetime, every_days: int, until: Optional[datetime], now: datetime,
                baseline: bool = True, max_count: int = 500) -> List[datetime]:
    """Due dates from ``anchor`` up to ``now`` (inclusive) or ``until``."""
    anchor, now = _aware(anchor), _aware(now)
    end = min(now, _aware(until)) if until else now
    out = []
    t = anchor if baseline else anchor + timedelta(days=every_days)
    while t <= end and len(out) < max_count:
        out.append(t)
        t = t + timedelta(days=every_days)
    return out


def schedule_status(anchor: datetime, every_days: int, completions: List[datetime], now: datetime,
                    grace_days: int = 3, until: Optional[datetime] = None, baseline: bool = True) -> dict:
    """Match completions to due dates.

    A completion counts for an occurrence when it falls in the window from the
    due date to the next due date. An occurrence is *overdue* while inside its
    grace period with no completion, *missed* once the grace period has passed.
    """
    if every_days <= 0:
        raise ValueError("every_days must be positive")
    now = _aware(now)
    done = sorted(_aware(c) for c in completions)
    rows = []
    for due in occurrences(anchor, every_days, until, now, baseline):
        window_end = due + timedelta(days=every_days)
        hit = next((c for c in done if due - timedelta(days=grace_days) <= c < window_end), None)
        if hit:
            done.remove(hit)
            state = "done"
        elif now <= due + timedelta(days=grace_days):
            state = "due" if now < due + timedelta(days=1) else "overdue"
        else:
            state = "missed"
        rows.append({"due": due.isoformat(), "state": state, "completed_at": hit.isoformat() if hit else None})
    last = rows[-1]["due"] if rows else None
    nxt = _aware(anchor) if not rows else datetime.fromisoformat(last) + timedelta(days=every_days)
    if until and nxt > _aware(until):
        nxt = None
    counts = {k: sum(1 for r in rows if r["state"] == k) for k in ("done", "due", "overdue", "missed")}
    return {"occurrences": rows, "counts": counts, "next_due": nxt.isoformat() if nxt else None,
            "adherence": round(counts["done"] / len(rows), 3) if rows else None}
