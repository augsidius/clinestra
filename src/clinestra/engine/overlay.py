"""
Fork overlay: local adaptation without editing the published base.


A fork is an append-only list of events (output_added, output_suppressed,
rerank). Replaying them over a base result set is deterministic:
suppress, then add, then boost and sort, then pin.

Paradigm-agnostic fork-overlay replay.

Evidence Apps store fork edits as graph events
(output_added / output_suppressed / rerank). Running a base model/app with a
fork_id replays those events over the base result set: suppress → add → boost →
pin. This module is the single implementation both paradigms import.
"""


def apply_fork_overlay(rows, overlay_events, output_key, rank_by):
    """Replay fork events over a base result set for one output key.

    rows           : list[dict] base rows (mutated copy expected from caller)
    overlay_events : list[dict] resolved fork events (see resolve_fork_overlay)
    output_key     : the output these rows belong to
    rank_by        : the numeric column used for ordering (may be None)
    """
    events = [e for e in overlay_events if e.get("output_key") in [output_key, None]]

    suppressed = {e["target_node_id"] for e in events
                  if e.get("event_type") == "output_suppressed" and e.get("target_node_id")}
    rows = [r for r in rows if r.get("node_id") not in suppressed]

    score_col = rank_by or "association"
    for e in events:
        if e.get("event_type") == "output_added" and e.get("target_node_id"):
            rows.append({
                "node_id": e["target_node_id"],
                "name": e.get("target_name"),
                "node_label": e.get("target_label"),
                score_col: e.get("score") or 0.0,
                "fork_added": True,
                "rationale": e.get("rationale"),
            })

    boosts = {e["target_node_id"]: e.get("boost") for e in events
              if e.get("event_type") == "rerank" and e.get("boost")}
    pins = {e["target_node_id"]: e.get("rank_after") for e in events
            if e.get("event_type") == "rerank" and e.get("rank_after")}

    if rank_by:
        for r in rows:
            if r.get("node_id") in boosts and isinstance(r.get(rank_by), (int, float)):
                r[rank_by] = round(r[rank_by] * boosts[r["node_id"]], 4)
                r["fork_boosted"] = boosts[r["node_id"]]
        rows.sort(key=lambda r: r.get(rank_by) or 0, reverse=True)

    # Absolute pins applied last (1-based rank).
    for node_id, rank_after in sorted(pins.items(), key=lambda kv: kv[1]):
        idx = next((i for i, r in enumerate(rows) if r.get("node_id") == node_id), None)
        if idx is not None:
            row = rows.pop(idx)
            row["fork_pinned_rank"] = rank_after
            rows.insert(max(0, rank_after - 1), row)
    return rows


__all__ = ["apply_fork_overlay"]
