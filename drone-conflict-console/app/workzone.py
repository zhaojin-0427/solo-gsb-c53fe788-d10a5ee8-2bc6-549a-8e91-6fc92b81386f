"""Temporary work-zone start-window queries for an execution batch.

A vertical *cylindrical protection zone* (horizontal centre ``(cx, cy)``,
positive radius ``r``, closed altitude band ``[z_lo, z_hi]``) must host one
work action of positive integer duration ``d`` seconds.  Given the current
second ``t`` and a deadline second ``T``, the action may start at an integer
second ``s`` in ``[t, T - d]`` and then occupies the closed time interval
``[s, s + d]``.

The schedule is restored exactly as for the deviation warning
(:mod:`app.batch`):

* **reported** sorties fly at their latest reported actual take-off;
* unreported sorties whose **planned** take-off is not earlier than ``t`` fly
  at the planned take-off;
* unreported sorties whose planned take-off is already strictly earlier than
  ``t`` are **overdue**: their trajectory is unknowable, so the query is
  *unconfirmable* and no safe start is offered.

As in the warning, ``t`` earlier than ANY reported actual take-off rejects the
whole query.

Occupancy is computed in continuous time by analytic geometry - never by
per-second sampling.  For each participating sortie's every linear segment we
intersect the segment's flight window with the times at which its position is
inside the closed cylinder (``horizontal <= r`` AND ``z_lo <= z <= z_hi``).
Touching the cylinder boundary, sitting exactly at a waypoint instant, or
sharing an endpoint with the action interval all count as occupancy, so an
integer start ``s`` is forbidden whenever the closed action interval
``[s, s + d]`` intersects any occupancy instant.

Boundary arithmetic is conservative (small EPS tolerance): a start that only
mathematically grazes an occupancy boundary is reported as forbidden.

The evaluation is a pure function of the frozen batch and the stored reports;
re-running after a revision correction or reloading the batch gives the same
answer.
"""

from __future__ import annotations

import math

from .batch import (
    EarlyTimeReject,
    Report,
    build_sortie_rows,
    classify_sorties,
    merge_real_intervals,
)
from .geometry import EPS, merge_intervals

#: Two real numbers closer than this are treated as equal when deciding whether
#: an integer boundary is included (safe side).
_NUM_EPS = 1e-7


class InvalidWorkZoneQuery(ValueError):
    """A work-zone query parameter is outside its legal domain."""

    def __init__(self, reason: str, **fields):
        self.reason = reason
        self.fields = fields
        super().__init__(reason)


def validate_workzone_query(cx: float, cy: float, radius: float,
                            z_lo: float, z_hi: float,
                            t: int, T: int, d: int) -> None:
    """Validate the user-supplied work-zone parameters.

    Raises :class:`InvalidWorkZoneQuery` with a machine-readable ``reason``.
    """
    if not (radius > 0):
        raise InvalidWorkZoneQuery("radius_not_positive", radius=radius)
    if not (z_lo < z_hi):
        raise InvalidWorkZoneQuery(
            "invalid_altitude_band", z_lo=z_lo, z_hi=z_hi)
    if not isinstance(t, int) or t < 0:
        raise InvalidWorkZoneQuery("t_negative", t=t)
    if not isinstance(T, int):
        raise InvalidWorkZoneQuery("T_not_integer", T=T)
    if not isinstance(d, int) or d <= 0:
        raise InvalidWorkZoneQuery("duration_not_positive", d=d)
    if T < t:
        raise InvalidWorkZoneQuery("deadline_before_t", t=t, T=T)


# ---------------------------------------------------------------------------
# Continuous-time cylinder occupancy of one piecewise-linear trajectory
# ---------------------------------------------------------------------------


def _quadratic_roots(a: float, b: float, c: float) -> list[float]:
    """Real roots of ``a q^2 + b q + c = 0`` (``a`` may be ~0)."""
    if abs(a) <= 1e-14:
        if abs(b) <= 1e-14:
            return []
        return [-c / b]
    disc = b * b - 4 * a * c
    if disc < -EPS:
        return []
    disc = max(disc, 0.0)
    sq = math.sqrt(disc)
    return [(-b - sq) / (2 * a), (-b + sq) / (2 * a)]


def _inside(px: float, py: float, pz: float, cx: float, cy: float,
            radius: float, z_lo: float, z_hi: float) -> bool:
    """Closed-cylinder test (boundary counts as occupied, EPS safe side)."""
    if pz < z_lo - EPS or pz > z_hi + EPS:
        return False
    dx, dy = px - cx, py - cy
    return dx * dx + dy * dy <= radius * radius + EPS


def cylinder_occupancy_segments(sortie, takeoff: float,
                                cx: float, cy: float, radius: float,
                                z_lo: float, z_hi: float
                                ) -> list[tuple[float, float]]:
    """Continuous global-time intervals in which one sortie is in the cylinder.

    The position along each segment is affine in time; the flight window is
    partitioned at every cylinder-boundary crossing (both the two horizontal
    planes ``z = z_lo/z_hi`` and the quadratic horizontal-distance crossings
    ``|p_xy - c| = r``), each open cell is classified by its midpoint and each
    boundary instant is tested on its own, so tangencies and waypoint instants
    are never missed.  Endpoints of overlapping/touching sections are merged.
    """
    r2 = radius * radius
    sections: list[tuple[float, float]] = []
    for seg in sortie.segments:
        a0 = seg.t0 + takeoff
        a1 = seg.t1 + takeoff
        # Position at global time u (u in [a0, a1]):
        #   P(u) = p0 + v * (u - a0) = P0 + v * u,  P0 = p0 - v*a0
        p0x = seg.p0[0] - seg.v[0] * a0
        p0y = seg.p0[1] - seg.v[1] * a0
        p0z = seg.p0[2] - seg.v[2] * a0
        vx, vy, vz = seg.v

        cuts = {a0, a1}
        # Horizontal-plane crossings pz(u) = z_lo / z_hi.
        if abs(vz) > 1e-14:
            for level in (z_lo, z_hi):
                cuts.add((level - p0z) / vz)
        # Horizontal disk crossings |(p0x + vx*u, p0y + vy*u) - c|^2 = r^2.
        d0x, d0y = p0x - cx, p0y - cy
        for u in _quadratic_roots(
                vx * vx + vy * vy,
                2 * (d0x * vx + d0y * vy),
                d0x * d0x + d0y * d0y - r2):
            cuts.add(u)
        cuts = sorted(u for u in cuts if a0 - EPS <= u <= a1 + EPS)
        cuts = [min(a1, max(a0, u)) for u in cuts]
        uniq: list[float] = []
        for u in cuts:
            if not uniq or abs(u - uniq[-1]) > 1e-9:
                uniq.append(u)

        def pos(u: float):
            return (p0x + vx * u, p0y + vy * u, p0z + vz * u)

        for lo, hi in zip(uniq, uniq[1:]):
            if hi - lo <= 1e-11:
                continue
            um = 0.5 * (lo + hi)
            xm, ym, zm = pos(um)
            if _inside(xm, ym, zm, cx, cy, radius, z_lo, z_hi):
                sections.append((lo, hi))
        # Boundary instants (crossings, waypoints) count when they lie inside.
        for u in uniq:
            x, y, z = pos(u)
            if _inside(x, y, z, cx, cy, radius, z_lo, z_hi):
                if not any(lo - EPS <= u <= hi + EPS for lo, hi in sections):
                    sections.append((u, u))
    return merge_real_intervals(sections)


# ---------------------------------------------------------------------------
# Integer start seconds forbidden by one continuous occupancy interval
# ---------------------------------------------------------------------------


def forbidden_starts(occ_lo: float, occ_hi: float, d: int) -> tuple[int, int] | None:
    """Inclusive integer interval of starts ``s`` with ``[s, s+d] ∩ occupancy``.

    For a closed occupancy interval ``[a, b]`` the closed action interval
    ``[s, s + d]`` intersects it exactly when ``s <= b`` and ``s + d >= a``,
    i.e. ``a - d <= s <= b``.  Touching counts (closed sets), and the EPS
    tolerance keeps an integer that only grazes the boundary on the forbidden
    (safe) side.
    """
    lo = math.ceil(occ_lo - d - _NUM_EPS)
    hi = math.floor(occ_hi + _NUM_EPS)
    if lo > hi:
        return None
    return lo, hi


def subtract_integer_intervals(lo: int, hi: int,
                               forbidden: list[tuple[int, int]]
                               ) -> list[tuple[int, int]]:
    """Parts of the inclusive integer interval ``[lo, hi]`` not forbidden."""
    if lo > hi:
        return []
    safe: list[tuple[int, int]] = []
    cur = lo
    for a, b in forbidden:
        if b < cur:
            continue
        if a > hi:
            break
        if a > cur:
            safe.append((cur, min(a - 1, hi)))
        cur = max(cur, b + 1)
        if cur > hi:
            break
    if cur <= hi:
        safe.append((cur, hi))
    return safe


# ---------------------------------------------------------------------------
# Work-zone start-window evaluation
# ---------------------------------------------------------------------------


def _occupancy_dto(sortie_id: str, status: str, takeoff: int | None,
                   intervals: list[tuple[float, float]]) -> dict:
    return {
        "sortie_id": sortie_id,
        "status": status,
        "takeoff_offset": takeoff,
        "occupied_intervals": [
            {"time_start": round(a, 6), "time_end": round(b, 6)}
            for a, b in intervals
        ],
    }


def evaluate_workzone(sorties, planned_offsets: dict[str, int],
                      reports: dict[str, Report],
                      cx: float, cy: float, radius: float,
                      z_lo: float, z_hi: float,
                      t: int, T: int, d: int) -> dict:
    """Safe integer start window for one temporary work action.

    Raises :class:`InvalidWorkZoneQuery` for illegal parameters and
    :class:`EarlyTimeReject` when ``t`` precedes a reported actual take-off.
    """
    validate_workzone_query(cx, cy, radius, z_lo, z_hi, t, T, d)

    statuses, effective = classify_sorties(sorties, planned_offsets, reports, t)
    sortie_rows, _ = build_sortie_rows(
        sorties, planned_offsets, reports, statuses, effective)
    overdue_ids = [row["id"] for row in sortie_rows if row["status"] == "overdue"]

    cand_lo, cand_hi = t, T - d
    # Continuous cylinder occupancy of every participating sortie, clipped to
    # the decision horizon [t, T] (an action starting no later than T-d never
    # reaches past T; occupancy outside [t, T] cannot matter).
    occupancy_rows: list[dict] = []
    all_intervals: list[tuple[float, float]] = []
    for s in sorties:
        sid = s.id
        if statuses[sid] == "overdue":
            occupancy_rows.append(_occupancy_dto(sid, "overdue", None, []))
            continue
        takeoff = effective[sid]
        ivs = cylinder_occupancy_segments(
            s, takeoff, cx, cy, radius, z_lo, z_hi)
        ivs = merge_real_intervals(
            [(max(float(t), a), min(float(T), b)) for a, b in ivs
             if b >= t - EPS and a <= T + EPS])
        all_intervals.extend(ivs)
        occupancy_rows.append(_occupancy_dto(sid, statuses[sid], takeoff, ivs))

    confirmable = not overdue_ids
    safe_intervals: list[list[int]] = []
    earliest = None
    forbidden_merged: list[tuple[int, int]] = []

    if confirmable and cand_lo <= cand_hi:
        forbidden: list[tuple[int, int]] = []
        for a, b in merge_real_intervals(all_intervals):
            fb = forbidden_starts(a, b, d)
            if fb is not None:
                forbidden.append(fb)
        forbidden_merged = merge_intervals(forbidden)
        safe = subtract_integer_intervals(cand_lo, cand_hi, forbidden_merged)
        safe_intervals = [[a, b] for a, b in safe]
        earliest = safe[0][0] if safe else None

    return {
        "zone": {
            "center_x": cx, "center_y": cy, "radius": radius,
            "z_lo": z_lo, "z_hi": z_hi,
        },
        "t": t,
        "T": T,
        "duration": d,
        "candidate_window": {"start_lo": cand_lo, "start_hi": cand_hi,
                             "has_candidates": cand_lo <= cand_hi},
        "confirmable": confirmable,
        "incomplete": bool(overdue_ids),
        "overdue_sorties": overdue_ids,
        "sorties": sortie_rows,
        "occupancy": occupancy_rows,
        "forbidden_start_intervals": [[a, b] for a, b in forbidden_merged],
        "safe_start_intervals": safe_intervals,
        "earliest_safe_start": earliest,
    }
