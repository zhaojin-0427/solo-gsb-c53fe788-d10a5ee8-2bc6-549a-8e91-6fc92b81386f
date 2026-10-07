"""Temporary work-zone start-window semantics (continuous-time cylinder).

Covers:

* analytic cylinder occupancy (boundary / waypoint / vertical-band edge
  cases) with an independent dense-sampling oracle;
* closed action interval [s, s+d] intersection rules, including touching at
  the occupancy/action endpoints (touching excludes the start);
* no per-second sampling: forbidden start intervals come from real geometry;
* reported sorties fly at their actual take-off, unreported planned ones at
  plan, overdue sorties make the query unconfirmable with no safe start;
* t earlier than a reported actual take-off rejects the whole query;
* illegal parameters (T < t, non-positive radius/duration, z_lo >= z_hi,
  t < 0) are rejected;
* empty candidate window and genuinely-no-safe-start cases return an empty
  safe interval / null earliest;
* re-query after a revision correction and after reloading from SQLite are
  byte-identical; the existing warning/review responses stay compatible.

Run:  python tests/test_workzone_window.py
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.batch import EarlyTimeReject, Report, evaluate_warning  # noqa: E402
from app.model import Waypoint, make_sortie  # noqa: E402
from app.storage import Store  # noqa: E402
from app.workzone import (  # noqa: E402
    InvalidWorkZoneQuery,
    cylinder_occupancy_segments,
    evaluate_workzone,
    forbidden_starts,
    subtract_integer_intervals,
    validate_workzone_query,
)

FAILS = 0


def check(cond, msg):
    global FAILS
    if not cond:
        FAILS += 1
        print("FAIL:", msg)


def eastbound(sid="A", x0=0, x1=100, z=20, dur=10):
    return make_sortie(sid, 1, 30, [
        Waypoint(x0, 0, z, 0), Waypoint(x1, 0, z, dur)])


# ---------------------------------------------------------------------------
# Parameter validation
# ---------------------------------------------------------------------------


def test_validation():
    ok = dict(cx=0, cy=0, radius=10, z_lo=10, z_hi=30, t=0, T=10, d=4)
    validate_workzone_query(**ok)  # no raise
    bad_cases = [
        dict(radius=0), dict(radius=-1),
        dict(z_lo=30, z_hi=30), dict(z_lo=31, z_hi=30),
        dict(t=-1), dict(T=-5),
        dict(T=3, t=4), dict(d=0), dict(d=-2),
    ]
    for bad in bad_cases:
        kw = {**ok, **bad}
        try:
            validate_workzone_query(**kw)
            check(False, f"must reject {bad}")
        except InvalidWorkZoneQuery:
            pass


# ---------------------------------------------------------------------------
# Geometry vs a dense sampling oracle
# ---------------------------------------------------------------------------


def _sampled_occupancy(sortie, takeoff, cx, cy, radius, z_lo, z_hi,
                       step=1e-4):
    """Reference occupancy: a very dense midpoint/endpoint scan."""
    inside_times = []
    for seg in sortie.segments:
        a0 = seg.t0 + takeoff
        a1 = seg.t1 + takeoff
        n = max(2, int((a1 - a0) / step) + 1)
        for i in range(n + 1):
            u = a0 + (a1 - a0) * i / n
            p = seg.position(u - takeoff)
            if (z_lo - 1e-9 <= p[2] <= z_hi + 1e-9
                    and (p[0] - cx) ** 2 + (p[1] - cy) ** 2
                    <= radius * radius + 1e-9):
                inside_times.append(u)
    if not inside_times:
        return []
    # Group into contiguous clusters.
    groups = [[inside_times[0]]]
    for u in inside_times[1:]:
        if u - groups[-1][-1] <= step * 3:
            groups[-1].append(u)
        else:
            groups.append([u])
    return [(g[0], g[-1]) for g in groups]


def test_occupancy_matches_oracle():
    import random
    random.seed(42)
    cases = []
    # Straight/climbing/turning sorties with varied cylinders.
    cases.append(([Waypoint(0, 0, 20, 0), Waypoint(100, 0, 20, 10)],
                  50, 0, 10, 10, 30))
    cases.append(([Waypoint(0, 0, 0, 0), Waypoint(100, 0, 40, 10)],
                  50, 0, 12, 10, 30))            # climbs through the band
    cases.append(([Waypoint(0, 0, 20, 0), Waypoint(60, 60, 20, 10),
                   Waypoint(120, 0, 20, 20)],
                  60, 30, 20, 15, 25))           # turns near the disk
    cases.append(([Waypoint(0, 0, 20, 0), Waypoint(100, 0, 20, 10)],
                  50, 0, 0.001, 10, 30))        # needle radius, through axis
    for _ in range(40):
        wps = []
        x = y = z = 0.0
        tt = 0
        for k in range(random.randint(2, 4)):
            wps.append(Waypoint(x, y, z, tt))
            x += random.uniform(-40, 120)
            y += random.uniform(-60, 60)
            z += random.uniform(-15, 25)
            tt += random.randint(3, 9)
        cases.append((wps, random.uniform(-20, 120), random.uniform(-40, 40),
                      random.uniform(2, 30), random.uniform(-5, 25),
                      random.uniform(20, 45)))
    for wps, cx, cy, r, zlo, zhi in cases:
        s = make_sortie("S", 1, 30, wps)
        takeoff = random.randint(0, 5)
        got = cylinder_occupancy_segments(s, takeoff, cx, cy, r, zlo, zhi)
        ref = _sampled_occupancy(s, takeoff, cx, cy, r, zlo, zhi)
        check(len(got) == len(ref),
              f"interval count {got} vs oracle {ref} for {wps[:1]}...")
        for (a, b), (ra, rb) in zip(got, ref):
            check(abs(a - ra) < 5e-4 and abs(b - rb) < 5e-4,
                  f"occupancy endpoints [{a},{b}] vs oracle [{ra},{rb}]")


def test_boundary_touches_count():
    # The trajectory passes exactly at distance r and exactly at z=z_lo/z_hi
    # at isolated instants: boundary contact must be a (possibly point-like)
    # occupancy interval.
    # Eastbound at z=20 through (50,0): radius 10 -> enters at 4, exits 6;
    # band [20,20] is illegal (z_lo < z_hi required), so use band edge z_lo=20
    # with the track exactly on the lower plane.
    s = eastbound()
    ivs = cylinder_occupancy_segments(s, 0, 50, 0, 10, 20, 30)
    check(ivs == [(4.0, 6.0)], f"lower-band edge on the track counts: {ivs}")
    # Radius tangent: a parallel line exactly r away grazes the disk once.
    s2 = make_sortie("G", 1, 30, [Waypoint(0, 10, 20, 0), Waypoint(100, 10, 20, 10)])
    ivs2 = cylinder_occupancy_segments(s2, 0, 50, 0, 10, 10, 30)
    check(len(ivs2) == 1 and abs(ivs2[0][0] - ivs2[0][1]) < 1e-7
          and abs(ivs2[0][0] - 5) < 1e-7,
          f"grazing the cylinder wall at one instant yields [5,5]: {ivs2}")
    # A waypoint exactly on the boundary occupies at least that instant.
    s3 = make_sortie("W", 1, 30, [Waypoint(0, 0, 20, 0), Waypoint(40, 0, 20, 4)])
    ivs3 = cylinder_occupancy_segments(s3, 0, 50, 0, 10, 10, 30)
    check(any(abs(b - 4) < 1e-7 for a, b in ivs3),
          f"segment end at the disk edge is occupied: {ivs3}")


# ---------------------------------------------------------------------------
# Forbidden integer starts
# ---------------------------------------------------------------------------


def test_forbidden_starts_closed():
    # Occupancy [4, 6], duration 4: [s, s+4] meets [4,6] for s in [0, 6].
    check(forbidden_starts(4, 6, 4) == (0, 6), "closed intersection range")
    # Touching at one endpoint counts: s+d == a -> s = a-d included; s == b
    # included.
    check(forbidden_starts(5, 5, 4) == (1, 5),
          "point occupancy [5,5] forbids s=1 ([1,5] touches) through s=5")
    check(forbidden_starts(4.0, 6.0, 2) == (2, 6), "endpoint grazing included")
    # Integer boundary computed from irrational roots must keep the touching
    # integer on the safe side (EPS).
    lo, hi = forbidden_starts(6.0 - 1e-12, 6.0, 4)
    check(lo <= 2 and hi >= 6, "float jitter at an integer boundary is safe")
    check(subtract_integer_intervals(0, 10, [(2, 4), (7, 7)])
          == [(0, 1), (5, 6), (8, 10)], "integer subtraction leaves the gaps")
    check(subtract_integer_intervals(5, 4, []) == [], "empty domain -> empty")
    check(subtract_integer_intervals(0, 3, [(0, 3)]) == [],
          "fully covered domain -> no safe starts")


def test_no_integer_sampling():
    """A short occupancy strictly between two integers still forbids both
    adjacent integer starts via the closed action sweep - sampling at integer
    seconds would miss it."""
    # Occupancy only on (4.5, 4.6); d=1: s=4 -> [4,5] hits; s=3 -> [3,4]
    # does NOT (4 < 4.5); so forbidden = {4}.
    check(forbidden_starts(4.5, 4.6, 1) == (4, 4),
          "sub-second occupancy between samples still excludes s=4 only")


# ---------------------------------------------------------------------------
# End-to-end evaluation on frozen batches
# ---------------------------------------------------------------------------


def test_safe_window_basic():
    # Eastbound A at z=20 crosses cylinder (50,0), r=10 at global [4,6].
    sorties = [eastbound("A")]
    plan = {"A": 0}
    # t=0, T=12, d=4 -> candidates [0,8]; forbidden s in [0,6]; safe [7,8].
    r = evaluate_workzone(sorties, plan, {}, 50, 0, 10, 10, 30, 0, 12, 4)
    check(r["confirmable"] and not r["incomplete"], "fresh batch confirmable")
    check(r["candidate_window"] == {"start_lo": 0, "start_hi": 8,
                                    "has_candidates": True},
          f"candidate window wrong: {r['candidate_window']}")
    check(r["safe_start_intervals"] == [[7, 8]],
          f"safe starts: {r['safe_start_intervals']}")
    check(r["earliest_safe_start"] == 7, "earliest safe start is 7")
    check(r["forbidden_start_intervals"] == [[0, 6]],
          f"forbidden: {r['forbidden_start_intervals']}")
    occ = {o["sortie_id"]: o["occupied_intervals"] for o in r["occupancy"]}
    check(occ["A"] == [{"time_start": 4.0, "time_end": 6.0}],
          f"occupancy dto: {occ['A']}")


def test_touching_action_endpoint_excludes_start():
    sorties = [eastbound("A")]
    # Occupancy [4,6], d=2: s=2 gives [2,4] touching at 4 -> forbidden;
    # s=6 gives [6,8] touching at 6 -> forbidden; safe starts among [0,8]
    # are {0,1,7,8}.
    r = evaluate_workzone(sorties, {"A": 0}, {}, 50, 0, 10, 10, 30, 0, 10, 2)
    check(r["safe_start_intervals"] == [[0, 1], [7, 8]],
          f"touching endpoints excluded: {r['safe_start_intervals']}")
    check(r["earliest_safe_start"] == 0, "earliest 0")


def test_no_safe_start_and_empty_candidates():
    sorties = [eastbound("A")]
    # Long action covering the whole horizon: the track is inside sometime.
    r = evaluate_workzone(sorties, {"A": 0}, {}, 50, 0, 10, 10, 30, 0, 12, 12)
    check(r["candidate_window"]["has_candidates"]
          and r["safe_start_intervals"] == []
          and r["earliest_safe_start"] is None,
          "genuinely no safe start -> empty interval, null earliest")
    # T < t+d -> no candidate seconds at all.
    r2 = evaluate_workzone(sorties, {"A": 0}, {}, 50, 0, 10, 10, 30, 5, 6, 4)
    check(not r2["candidate_window"]["has_candidates"]
          and r2["candidate_window"] == {"start_lo": 5, "start_hi": 2,
                                         "has_candidates": False}
          and r2["safe_start_intervals"] == []
          and r2["earliest_safe_start"] is None,
          "T-d < t: empty candidate window, null earliest")
    # Cylinder nowhere near the track: everything is safe.
    r3 = evaluate_workzone(sorties, {"A": 0}, {}, 900, 900, 5, 10, 30, 0, 10, 2)
    check(r3["safe_start_intervals"] == [[0, 8]]
          and r3["earliest_safe_start"] == 0
          and r3["forbidden_start_intervals"] == [],
          "far-away cylinder never occupied")


def test_reported_and_planned_and_overdue():
    sorties = [
        make_sortie("A", 1, 30, [Waypoint(0, 0, 20, 0), Waypoint(100, 0, 20, 10)]),
        make_sortie("B", 1, 30, [Waypoint(0, 0, 20, 0), Waypoint(100, 0, 20, 10)]),
    ]
    # Same track line for both; plan A=0, B=8. At t=5: A reported at 0 flies;
    # B unreported with plan 8 >= 5 flies at 8.
    reports = {"A": Report(0, 1)}
    r = evaluate_workzone(sorties, {"A": 0, "B": 8}, reports,
                          50, 0, 10, 10, 30, 5, 20, 2)
    check(r["confirmable"], "reported + planned -> confirmable")
    statuses = {s["id"]: s["status"] for s in r["sorties"]}
    check(statuses == {"A": "reported", "B": "planned"}, statuses)
    # A occupies [4,6] (clipped to >=t=5 -> [5,6]); B at takeoff 8 occupies
    # [12,14]. Forbidden starts for d=2: from [5,6]: s in [3,6]->clipped
    # candidate [5..18] -> {5,6}; from [12,14]: s in [10,14].
    check(r["safe_start_intervals"] == [[7, 9], [15, 18]],
          f"union of both sorties' occupancy: {r['safe_start_intervals']}")

    # Now an unreported C with plan 2 at t=5 is overdue -> unconfirmable.
    sorties.append(
        make_sortie("C", 1, 30, [Waypoint(0, 0, 20, 0), Waypoint(100, 0, 20, 10)]))
    r2 = evaluate_workzone(sorties, {"A": 0, "B": 8, "C": 2}, reports,
                           50, 0, 10, 10, 30, 5, 20, 2)
    check(r2["overdue_sorties"] == ["C"] and not r2["confirmable"]
          and r2["incomplete"]
          and r2["safe_start_intervals"] == []
          and r2["earliest_safe_start"] is None,
          "overdue -> unconfirmable, no safe start provided")
    over = {o["sortie_id"]: o for o in r2["occupancy"]}
    check(over["C"]["status"] == "overdue"
          and over["C"]["occupied_intervals"] == []
          and over["C"]["takeoff_offset"] is None,
          "overdue sortie contributes no occupancy")
    # Participating sorties' occupancy is still reported for context.
    check(over["A"]["occupied_intervals"] == [{"time_start": 5.0, "time_end": 6.0}],
          "reported occupancy clipped to t")


def test_early_takeoff_rejects():
    sorties = [eastbound("A")]
    try:
        evaluate_workzone(sorties, {"A": 0}, {"A": Report(9, 1)},
                          50, 0, 10, 10, 30, 3, 20, 2)
        check(False, "t before a reported actual take-off must reject")
    except EarlyTimeReject as exc:
        check([v["sortie_id"] for v in exc.violations] == ["A"],
              "violation reported")


def test_illegal_parameters():
    sorties = [eastbound("A")]
    def expect_reason(**kw):
        try:
            evaluate_workzone(sorties, {"A": 0}, {}, **kw)
            return None
        except InvalidWorkZoneQuery as exc:
            return exc.reason
    base = dict(cx=50, cy=0, radius=10, z_lo=10, z_hi=30, t=0, T=10, d=2)
    check(expect_reason(**{**base, "radius": 0}) == "radius_not_positive",
          "radius 0 rejected")
    check(expect_reason(**{**base, "z_lo": 30, "z_hi": 20})
          == "invalid_altitude_band", "inverted band rejected")
    check(expect_reason(**{**base, "T": 3, "t": 4}) == "deadline_before_t",
          "T < t rejected")
    check(expect_reason(**{**base, "t": -1}) == "t_negative", "t<0 rejected")
    check(expect_reason(**{**base, "d": 0}) == "duration_not_positive",
          "d=0 rejected")


def test_deadline_clipping():
    # Occupancy past T must not forbid a start whose action ends by T-d <= T.
    sorties = [eastbound("A")]  # occupies [4,6]
    # t=0, T=5, d=1 -> candidates [0,4]. Occupancy clipped to [4,5]: s=3 gives
    # [3,4] touching 4 -> forbidden; s=4 -> [4,5] intersects -> forbidden.
    r = evaluate_workzone(sorties, {"A": 0}, {}, 50, 0, 10, 10, 30, 0, 5, 1)
    check(r["safe_start_intervals"] == [[0, 2]],
          f"occupancy clipped at T: {r['safe_start_intervals']}")


def test_vertical_band_filter():
    # Climbing sortie passes altitude [10,30] only part of the time.
    s = make_sortie("A", 1, 30, [
        Waypoint(0, 0, 0, 0), Waypoint(100, 0, 40, 10)])
    # over (50,0) at u=5, z=20 -> in band; r=10 gives horizontal times [4,6],
    # z between 10 and 30 for u in [2.5, 7.5] -> occupancy stays [4,6].
    r = evaluate_workzone([s], {"A": 0}, {}, 50, 0, 10, 10, 30, 0, 12, 2)
    check(r["safe_start_intervals"] == [[0, 1], [7, 10]],
          f"vertical band intersects horizontal window: {r['safe_start_intervals']}")
    # Band entirely above the trajectory near the cylinder -> never occupied.
    r2 = evaluate_workzone([s], {"A": 0}, {}, 50, 0, 10, 30.001, 40, 0, 12, 2)
    check(r2["safe_start_intervals"] == [[0, 10]]
          and not r2["occupancy"][0]["occupied_intervals"],
          "band above the passage altitude -> no occupancy")


def test_revision_and_reload_consistency():
    """After a correction, re-query is identical; reload from SQLite is too."""
    sorties = [eastbound("A")]
    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "t.db"))
        sid = store.create_scenario({"sorties": []}, 30, 10)
        bid = store.insert_batch(sid, "b", {"sorties": [{"id": "A"}]},
                                 30, 10, {"offsets": {"A": 0}})
        # Deviation: A actually takes off at 2 -> occupancy shifts to [6,8].
        store.submit_report(bid, "A", 2, 1)
        reps1 = {r["sortie_id"]: Report(r["actual_offset"], r["revision"])
                 for r in store.get_batch(bid)["reports"]}
        q1 = evaluate_workzone(sorties, {"A": 0}, reps1,
                               50, 0, 10, 10, 30, 2, 12, 2)
        # Occupancy [6,8] clipped to t=2; forbidden starts [4,8], candidates
        # [2,10] -> safe [2,3] and [9,10].
        check(q1["safe_start_intervals"] == [[2, 3], [9, 10]],
              f"reported delay shifts occupancy: {q1['safe_start_intervals']}")
        # Correct back to plan with revision 2.
        store.submit_report(bid, "A", 0, 2)
        reps2 = {r["sortie_id"]: Report(r["actual_offset"], r["revision"])
                 for r in store.get_batch(bid)["reports"]}
        q2 = evaluate_workzone(sorties, {"A": 0}, reps2,
                               50, 0, 10, 10, 30, 2, 12, 2)
        q2b = evaluate_workzone(sorties, {"A": 0}, reps2,
                                50, 0, 10, 10, 30, 2, 12, 2)
        check(q2 == q2b, "identical inputs -> identical results")
        # Occupancy [4,6] clipped to t=2; forbidden [2,6] -> safe [7,10].
        check(q2["safe_start_intervals"] == [[7, 10]],
              "correction restores the planned-occupancy safe window")
        # Reload from the database (fresh read) and re-evaluate.
        reloaded = store.get_batch(bid)
        reps3 = {r["sortie_id"]: Report(r["actual_offset"], r["revision"])
                 for r in reloaded["reports"]}
        q3 = evaluate_workzone(sorties, reloaded["plan"]["offsets"], reps3,
                               50, 0, 10, 10, 30, 2, 12, 2)
        check(q3 == q2, "reload + re-query identical")

        # Existing warning response is unaffected by the new feature.
        w = evaluate_warning(sorties, {"A": 0}, reps3, 30, 10, 0)
        check(set(w) == {"t", "complete", "incomplete", "overdue_sorties",
                         "sorties", "conflicts", "first_conflict_time",
                         "reported_deviations"},
              "warning DTO shape unchanged")


def test_extra_geometry_edges():
    # Vertical climb: horizontal pinned at the centre, only the z band limits
    # occupancy (z goes 0 -> 40 over 10 s; band [10,30] -> u in [2.5, 7.5]).
    climb = make_sortie("V", 1, 30, [
        Waypoint(50, 0, 0, 0), Waypoint(50, 0, 40, 10)])
    check(cylinder_occupancy_segments(climb, 0, 50, 0, 10, 10, 30)
          == [(2.5, 7.5)], "climb through the altitude band")
    # Flight entirely above / below the band never occupies.
    high = make_sortie("H", 1, 30, [
        Waypoint(50, 0, 100, 0), Waypoint(60, 0, 100, 10)])
    check(cylinder_occupancy_segments(high, 0, 55, 0, 10, 0, 50) == [],
          "above-band trajectory never occupies")
    low = make_sortie("L", 1, 30, [
        Waypoint(50, 0, -100, 0), Waypoint(60, 0, -100, 10)])
    check(cylinder_occupancy_segments(low, 0, 55, 0, 10, 0, 50) == [],
          "below-band trajectory never occupies")
    # Two pieces join through a waypoint inside the disk (each segment is
    # 4 m/s; disk r=10 -> 2.5 s each side of the t=5 waypoint).
    turn = make_sortie("M", 1, 30, [
        Waypoint(30, 0, 20, 0), Waypoint(50, 0, 20, 5),
        Waypoint(70, 0, 20, 10)])
    check(cylinder_occupancy_segments(turn, 0, 50, 0, 10, 10, 30)
          == [(2.5, 7.5)], "occupancy merges through an interior waypoint")
    check(cylinder_occupancy_segments(turn, 7, 50, 0, 10, 10, 30)
          == [(9.5, 14.5)], "waypoint occupancy shifts with the take-off")
    # Two sorties: one never comes near -> only the other forbids starts.
    a = eastbound("A")
    far = make_sortie("B", 1, 30, [
        Waypoint(0, 100, 20, 0), Waypoint(100, 100, 20, 10)])
    r = evaluate_workzone([a, far], {"A": 0, "B": 0}, {},
                          50, 0, 10, 10, 30, 0, 12, 1)
    check(r["safe_start_intervals"] == [[0, 2], [7, 11]],
          f"distant sortie adds no occupancy: {r['safe_start_intervals']}")
    # Single-candidate window: the only start second intersects -> empty.
    r3 = evaluate_workzone([a], {"A": 0}, {}, 50, 0, 10, 10, 30, 0, 5, 5)
    check(r3["candidate_window"]["has_candidates"]
          and r3["safe_start_intervals"] == []
          and r3["earliest_safe_start"] is None,
          "the only candidate second is unsafe -> empty window, null earliest")
    # Single safe candidate far from any track.
    r4 = evaluate_workzone([a], {"A": 0}, {}, 500, 500, 5, 10, 30, 0, 5, 5)
    check(r4["safe_start_intervals"] == [[0, 0]]
          and r4["earliest_safe_start"] == 0, "single isolated safe candidate")


def main():
    test_validation()
    test_occupancy_matches_oracle()
    test_boundary_touches_count()
    test_forbidden_starts_closed()
    test_no_integer_sampling()
    test_safe_window_basic()
    test_touching_action_endpoint_excludes_start()
    test_no_safe_start_and_empty_candidates()
    test_reported_and_planned_and_overdue()
    test_early_takeoff_rejects()
    test_illegal_parameters()
    test_deadline_clipping()
    test_vertical_band_filter()
    test_extra_geometry_edges()
    test_revision_and_reload_consistency()
    print("failures:", FAILS)
    return FAILS


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
