"""Execution batches, deviation early-warning and warning-change reviews.

A *batch* freezes one saved feasible plan: the input trajectories (snapshot),
the safety thresholds (R, H) and the planned take-off offsets.  Batch state
then only changes through per-sortie actual-take-off reports carrying a
strictly increasing revision number.

Given a non-negative current time ``t`` the warning evaluation classifies
every sortie:

* **reported** — an actual take-off offset has been reported; it flies at the
  reported time;
* **planned** — not reported yet, but its planned take-off is not earlier
  than ``t``: it is still predictable and flies at the planned time;
* **overdue** — not reported and its planned take-off is already in the past
  (strictly earlier than ``t``): excluded from prediction and the result is
  flagged incomplete.

Conflicts reuse the continuous-time detector in :mod:`app.geometry` on the
effective (reported / planned) schedule and are reported for instants
``>= t``.  The evaluation is a pure function of the frozen batch and the
stored reports, so reloading a batch and re-evaluating always gives the same
result.

A *warning-change review* (:func:`review_warning`) replays the complete
report state immediately before and after one accepted report node (both
restored from SQLite snapshots) against the SAME current second ``t``:

* conflicts are diffed per sortie pair - where the before/after intervals
  intersect only the non-overlapping parts are shown as ``added`` /
  ``removed``; an identical interval shows no change; an isolated instant
  (a degenerate ``[T, T]`` interval, e.g. two sorties meeting only at one
  segment endpoint) counts as added / removed when only one side has it,
  while an instant shared by both sides is not a change;
* first conflict times on both sides and the overdue set difference are
  reported;
* if ``t`` is earlier than ANY reported actual take-off on EITHER side the
  whole review is rejected - no difference is returned.
"""

from __future__ import annotations

from .geometry import EPS, conflicts_between_sorties

REPORTED = "reported"
PLANNED = "planned"
OVERDUE = "overdue"

#: Continuous-time intervals closer than this are treated as touching and are
#: merged; positive-length interval-diff slivers no longer than this are
#: boundary instants shared by both sides, not "changed" parts.  A degenerate
#: (isolated-instant) interval is still a genuine change when the other side
#: does not cover the instant at all.
_INTERVAL_EPS = 1e-6


class Report:
    __slots__ = ("actual_offset", "revision")

    def __init__(self, actual_offset: int, revision: int):
        self.actual_offset = actual_offset
        self.revision = revision

    def __eq__(self, other):
        return (isinstance(other, Report)
                and self.actual_offset == other.actual_offset
                and self.revision == other.revision)

    def __repr__(self):
        return f"Report(actual_offset={self.actual_offset}, revision={self.revision})"


class EarlyTimeReject(Exception):
    """Raised when ``t`` is earlier than a reported actual take-off."""

    def __init__(self, t: int, violations: list[dict]):
        self.t = t
        self.violations = violations
        super().__init__(f"t={t} is earlier than a reported actual take-off")


# ---------------------------------------------------------------------------
# Real interval set algebra (closed intervals, continuous time)
# ---------------------------------------------------------------------------


def merge_real_intervals(intervals: list[tuple[float, float]]
                         ) -> list[tuple[float, float]]:
    """Union of closed real intervals, merging overlaps/touching endpoints."""
    ivs = sorted((float(a), float(b)) for a, b in intervals if b >= a - EPS)
    merged: list[list[float]] = []
    for a, b in ivs:
        if merged and a <= merged[-1][1] + _INTERVAL_EPS:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def subtract_real_intervals(xs: list[tuple[float, float]],
                            ys: list[tuple[float, float]]
                            ) -> list[tuple[float, float]]:
    """Parts of merged intervals ``xs`` not covered by the closed set ``ys``.

    Both inputs are merged interval lists.  A boundary instant shared with a
    ``ys`` interval is not emitted as a changed part (positive-length pieces
    keep their closed hull; slivers no longer than ``_INTERVAL_EPS`` are
    dropped), so strictly-overlapping before/after conflict intervals yield
    only their genuinely different sections.

    A degenerate ``xs`` interval is an *isolated instant* (e.g. two sorties
    meeting only at one segment endpoint): it is emitted as a point interval
    when no ``ys`` interval covers it (within the same tolerance), so an
    instantaneous conflict present on just one side surfaces as added /
    removed, while an instant shared by both sides stays silent.
    """
    out: list[tuple[float, float]] = []
    for a, b in xs:
        if b - a <= _INTERVAL_EPS:
            # Isolated instant: a changed part only when the other side does
            # not cover it at all; an instant shared with ``ys`` (including
            # one merely touching at a shared endpoint) is not a change.
            p = 0.5 * (a + b)
            if not any(c <= p + _INTERVAL_EPS and d >= p - _INTERVAL_EPS
                       for c, d in ys):
                out.append((min(a, b), max(a, b)))
            continue
        cur = a
        for c, d in ys:
            if d < cur - EPS or c > b + EPS:
                continue
            cs, ce = max(cur, c), min(b, d)
            if cs > ce + EPS:
                continue
            if cs - cur > _INTERVAL_EPS:
                out.append((cur, cs))
            cur = max(cur, ce)
            if cur >= b - EPS:
                break
        if b - cur > _INTERVAL_EPS:
            out.append((cur, b))
    return out


# ---------------------------------------------------------------------------
# Warning evaluation
# ---------------------------------------------------------------------------


def _conflict_dto(sorties_by_id, takeoff_a, takeoff_b, rec, t, change=None):
    """Project a conflict record onto ``[t, ...]`` and refresh its witness."""
    start = max(float(t), rec.time_start)
    end = rec.time_end
    return _segment_dto(sorties_by_id, rec.sortie_a, rec.sortie_b,
                        {rec.sortie_a: takeoff_a, rec.sortie_b: takeoff_b},
                        start, end, t, change)


def _segment_dto(sorties_by_id, sortie_a, sortie_b, takeoffs, start, end, t,
                 change=None):
    """DTO for an arbitrary continuous-time conflict section [start, end]."""
    start = max(float(t), float(start))
    end = float(end)
    length = end - start
    witness = start if length <= 1e-9 else 0.5 * (start + end)
    pa = sorties_by_id[sortie_a].position(witness - takeoffs[sortie_a])
    pb = sorties_by_id[sortie_b].position(witness - takeoffs[sortie_b])
    dx, dy = pa[0] - pb[0], pa[1] - pb[1]
    dto = {
        "sortie_a": sortie_a,
        "sortie_b": sortie_b,
        "time_start": round(start, 6),
        "time_end": round(end, 6),
        "witness_time": round(witness, 6),
        "a_pos": [round(v, 6) for v in pa],
        "b_pos": [round(v, 6) for v in pb],
        "horizontal_distance": round((dx * dx + dy * dy) ** 0.5, 6),
        "vertical_distance": round(abs(pa[2] - pb[2]), 6),
    }
    if change is not None:
        dto["change"] = change
    return dto


def _reported_violations(reports: dict[str, Report], t: int) -> list[dict]:
    return [
        {"sortie_id": sid, "actual_takeoff": rep.actual_offset}
        for sid, rep in sorted(reports.items())
        if t < rep.actual_offset
    ]


def classify_sorties(sorties, planned_offsets: dict[str, int],
                     reports: dict[str, Report], t: int
                     ) -> tuple[dict[str, str], dict[str, int]]:
    """Classify every sortie at current second ``t`` and resolve take-offs.

    Returns ``(statuses, effective)`` where ``statuses`` maps a sortie id to
    reported / planned / overdue and ``effective`` maps the *participating*
    (non-overdue) sorties to their effective take-off offset.  Raises
    :class:`EarlyTimeReject` when ``t`` precedes a reported actual take-off.

    A planned take-off exactly at ``t`` still counts as predictable
    (``planned_offset >= t``); an unreported sortie whose planned take-off is
    strictly earlier than ``t`` is overdue.
    """
    violations = _reported_violations(reports, t)
    if violations:
        raise EarlyTimeReject(t, violations)

    statuses: dict[str, str] = {}
    effective: dict[str, int] = {}
    for s in sorties:
        rep = reports.get(s.id)
        if rep is not None:
            statuses[s.id] = REPORTED
            effective[s.id] = rep.actual_offset
        elif planned_offsets[s.id] >= t:
            statuses[s.id] = PLANNED
            effective[s.id] = planned_offsets[s.id]
        else:
            statuses[s.id] = OVERDUE
    return statuses, effective


def build_sortie_rows(sorties, planned_offsets: dict[str, int],
                      reports: dict[str, Report],
                      statuses: dict[str, str],
                      effective: dict[str, int]) -> tuple[list[dict], list[dict]]:
    """Per-sortie status rows plus a compact reported-deviation list."""
    sortie_rows = []
    deviations = []
    for s in sorties:
        sid = s.id
        status = statuses[sid]
        rep = reports.get(sid)
        planned = planned_offsets[sid]
        row = {
            "id": sid,
            "status": status,
            "planned_offset": planned,
            "actual_offset": rep.actual_offset if rep is not None else None,
            "revision": rep.revision if rep is not None else None,
            "takeoff_offset": effective.get(sid),
            "deviation_seconds": None,
        }
        if rep is not None:
            deviation = rep.actual_offset - planned
            row["deviation_seconds"] = deviation
            deviations.append({
                "sortie_id": sid,
                "planned_offset": planned,
                "actual_offset": rep.actual_offset,
                "revision": rep.revision,
                "deviation_seconds": deviation,
            })
        sortie_rows.append(row)
    return sortie_rows, deviations


def _evaluate(sorties, planned_offsets: dict[str, int],
              reports: dict[str, Report], safety_radius: float,
              safety_height: float, t: int):
    """Core evaluation.

    Returns ``(warning_dto, raw_conflicts, statuses, effective)`` where
    ``raw_conflicts`` holds unrounded ``(record, takeoff_a, takeoff_b)``
    triples restricted to instants ``>= t`` (for interval diffing).

    Raises :class:`EarlyTimeReject` when ``t`` precedes a reported take-off.
    """
    statuses, effective = classify_sorties(sorties, planned_offsets, reports, t)

    by_id = {s.id: s for s in sorties}
    # Continuous-time conflicts on the effective schedule, restricted to the
    # participating (non-overdue) sorties and to instants at or after t.
    raw_conflicts: list[tuple] = []
    conflicts: list[dict] = []
    for i, sa in enumerate(sorties):
        if statuses[sa.id] == OVERDUE:
            continue
        for sb in sorties[i + 1:]:
            if statuses[sb.id] == OVERDUE:
                continue
            eff_a, eff_b = effective[sa.id], effective[sb.id]
            recs = conflicts_between_sorties(
                sa, eff_a, sb, eff_b, safety_radius, safety_height)
            for rec in recs:
                if rec.time_end < t - EPS:
                    continue
                raw_conflicts.append((rec, eff_a, eff_b))
                conflicts.append(_conflict_dto(by_id, eff_a, eff_b, rec, t))
    conflicts.sort(key=lambda r: (r["time_start"], r["sortie_a"], r["sortie_b"]))
    first_conflict_time = conflicts[0]["time_start"] if conflicts else None

    sortie_rows, deviations = build_sortie_rows(
        sorties, planned_offsets, reports, statuses, effective)

    overdue_ids = [row["id"] for row in sortie_rows if row["status"] == OVERDUE]
    dto = {
        "t": t,
        "complete": not overdue_ids,
        "incomplete": bool(overdue_ids),
        "overdue_sorties": overdue_ids,
        "sorties": sortie_rows,
        "conflicts": conflicts,
        "first_conflict_time": first_conflict_time,
        "reported_deviations": deviations,
    }
    return dto, raw_conflicts, statuses, effective


def evaluate_warning(sorties, planned_offsets: dict[str, int],
                     reports: dict[str, Report], safety_radius: float,
                     safety_height: float, t: int) -> dict:
    """Compute the deviation warning for a frozen batch at current time ``t``.

    Raises :class:`EarlyTimeReject` when ``t`` precedes a reported take-off.
    """
    dto, _, _, _ = _evaluate(sorties, planned_offsets, reports,
                             safety_radius, safety_height, t)
    return dto


# ---------------------------------------------------------------------------
# Warning-change review around one accepted report node
# ---------------------------------------------------------------------------


def _pair_intervals(raw_conflicts, t) -> dict[tuple[str, str], list[tuple[float, float]]]:
    grouped: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for rec, _, _ in raw_conflicts:
        key = (rec.sortie_a, rec.sortie_b)
        grouped.setdefault(key, []).append(
            (max(float(t), rec.time_start), rec.time_end))
    return {key: merge_real_intervals(ivs) for key, ivs in grouped.items()}


def review_warning(sorties, planned_offsets: dict[str, int],
                   before_reports: dict[str, Report],
                   after_reports: dict[str, Report],
                   safety_radius: float, safety_height: float, t: int,
                   trigger: dict | None = None) -> dict:
    """Diff the continuous-time warning just before/after one accepted node.

    Both states are evaluated with the SAME ``t``.  If ``t`` precedes a
    reported actual take-off on either side, :class:`EarlyTimeReject` is
    raised and no difference is computed.  ``trigger`` describes the accepted
    write (``{"sortie_id", "actual_offset", "revision"}``) or is ``None`` for
    a baseline node.
    """
    # Reject against EITHER side: union of violations, deduplicated by
    # (sortie, actual take-off) with a stable sortie-id order.
    seen: set[tuple[str, int]] = set()
    violations: list[dict] = []
    for reports in (before_reports, after_reports):
        for v in _reported_violations(reports, t):
            key = (v["sortie_id"], v["actual_takeoff"])
            if key not in seen:
                seen.add(key)
                violations.append(v)
    violations.sort(key=lambda v: v["sortie_id"])
    if violations:
        raise EarlyTimeReject(t, violations)

    w_before, raw_before, _, eff_before = _evaluate(
        sorties, planned_offsets, before_reports,
        safety_radius, safety_height, t)
    w_after, raw_after, _, eff_after = _evaluate(
        sorties, planned_offsets, after_reports,
        safety_radius, safety_height, t)

    by_id = {s.id: s for s in sorties}
    iv_before = _pair_intervals(raw_before, t)
    iv_after = _pair_intervals(raw_after, t)

    pair_changes: list[dict] = []
    for key in sorted(set(iv_before) | set(iv_after)):
        before_ivs = iv_before.get(key, [])
        after_ivs = iv_after.get(key, [])
        # Added = post-node sections not already conflicting before the node;
        # removed = pre-node sections no longer conflicting afterwards.
        # Subtraction clips overlaps away, so intersecting intervals only
        # contribute their actually-changed parts.
        added_ivs = subtract_real_intervals(after_ivs, before_ivs)
        removed_ivs = subtract_real_intervals(before_ivs, after_ivs)
        if not added_ivs and not removed_ivs:
            continue
        a_name, b_name = key
        added = [
            _segment_dto(by_id, a_name, b_name, eff_after, lo, hi, t, "added")
            for lo, hi in added_ivs]
        removed = [
            _segment_dto(by_id, a_name, b_name, eff_before, lo, hi, t, "removed")
            for lo, hi in removed_ivs]
        added.sort(key=lambda d: d["time_start"])
        removed.sort(key=lambda d: d["time_start"])
        pair_changes.append({
            "sortie_a": a_name,
            "sortie_b": b_name,
            "added": added,
            "removed": removed,
        })
    pair_changes.sort(key=lambda p: (
        min([d["time_start"] for d in p["added"] + p["removed"]]),
        p["sortie_a"], p["sortie_b"]))

    rows_before = {r["id"]: r for r in w_before["sorties"]}
    rows_after = {r["id"]: r for r in w_after["sorties"]}
    sortie_changes = []
    for s in sorties:
        rb, ra = rows_before[s.id], rows_after[s.id]
        changed = (
            rb["status"] != ra["status"]
            or rb["takeoff_offset"] != ra["takeoff_offset"]
            or rb["revision"] != ra["revision"])
        sortie_changes.append({
            "id": s.id,
            "changed": changed,
            "status_before": rb["status"],
            "status_after": ra["status"],
            "takeoff_offset_before": rb["takeoff_offset"],
            "takeoff_offset_after": ra["takeoff_offset"],
            "revision_before": rb["revision"],
            "revision_after": ra["revision"],
            "deviation_seconds_before": rb["deviation_seconds"],
            "deviation_seconds_after": ra["deviation_seconds"],
        })

    overdue_before = set(w_before["overdue_sorties"])
    overdue_after = set(w_after["overdue_sorties"])
    node_desc = {
        "sortie_id": None,
        "actual_offset": None,
        "revision": None,
        "baseline": True,
    }
    if trigger is not None:
        node_desc = {
            "sortie_id": trigger.get("sortie_id"),
            "actual_offset": trigger.get("actual_offset"),
            "revision": trigger.get("revision"),
            "baseline": trigger.get("sortie_id") is None,
        }
    return {
        "t": t,
        "node": node_desc,
        "first_conflict_time_before": w_before["first_conflict_time"],
        "first_conflict_time_after": w_after["first_conflict_time"],
        "complete_before": w_before["complete"],
        "complete_after": w_after["complete"],
        "overdue_before": w_before["overdue_sorties"],
        "overdue_after": w_after["overdue_sorties"],
        "overdue_added": sorted(overdue_after - overdue_before),
        "overdue_removed": sorted(overdue_before - overdue_after),
        "pair_changes": pair_changes,
        "sortie_changes": sortie_changes,
        "warning_before": w_before,
        "warning_after": w_after,
    }
