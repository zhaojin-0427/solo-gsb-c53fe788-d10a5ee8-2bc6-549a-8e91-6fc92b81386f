"""Execution-batch semantics and deviation early-warning validation.

Covers (without any HTTP layer):

* batch creation freezes input / thresholds / planned offsets;
* report revision protocol: first revision 1, strictly increasing,
  same-revision-same-value idempotence, same-revision-different-value and
  stale revision conflicts, concurrent submissions keep the highest revision;
* warning classification (reported / planned / overdue), the early-time
  rejection, incomplete flag, continuous-time conflict intervals projected
  onto ``t`` and first-conflict time;
* reloading a batch and re-evaluating gives identical results.

Run:  python tests/test_batches_warning.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.batch import (  # noqa: E402
    EarlyTimeReject,
    Report,
    evaluate_warning,
    review_warning,
    subtract_real_intervals,
)
from app.model import Waypoint, make_sortie  # noqa: E402
from app.storage import Store  # noqa: E402

FAILS = 0


def check(cond, msg):
    global FAILS
    if not cond:
        FAILS += 1
        print("FAIL:", msg)


def two_crossing_sorties():
    """A eastbound, B northbound, crossing at (100, 0), same altitude."""
    a = make_sortie("A", 1, 20, [
        Waypoint(0, 0, 20, 0), Waypoint(100, 0, 20, 10), Waypoint(200, 0, 20, 20)])
    b = make_sortie("B", 1, 20, [
        Waypoint(100, -100, 20, 0), Waypoint(100, 100, 20, 20)])
    return [a, b]


def two_touching_sorties():
    """A ends at (100, 0, 20) at its t=10; B starts there at its own t=0.

    Both cruise east at the same velocity, so with take-off difference exactly
    10 the two flight windows touch at ONE instant (global T=10 when A=0,
    B=10) where both occupy (100, 0, 20): a degenerate [T, T] conflict.
    """
    a = make_sortie("A", 1, 20, [Waypoint(0, 0, 20, 0), Waypoint(100, 0, 20, 10)])
    b = make_sortie("B", 1, 20, [Waypoint(100, 0, 20, 0), Waypoint(200, 0, 20, 10)])
    return [a, b]


def test_evaluation():
    sorties = two_crossing_sorties()
    plan = {"A": 0, "B": 10}  # staggered plan, conflict free
    w = evaluate_warning(sorties, plan, {}, 30, 10, 0)
    check(w["complete"] and not w["conflicts"], "plan itself should be conflict free")
    check(all(r["status"] == "planned" for r in w["sorties"]), "all planned at t=0")

    # B deviates to actual 0 (revision 1): simultaneous crossing conflict.
    reports = {"A": Report(0, 1), "B": Report(0, 1)}
    w = evaluate_warning(sorties, plan, reports, 30, 10, 0)
    check(len(w["conflicts"]) == 1, "expected one conflict interval after deviation")
    c = w["conflicts"][0]
    check(c["time_start"] < 10 < c["time_end"], "crossing instant inside interval")
    check(w["first_conflict_time"] == c["time_start"], "first conflict time matches")
    dev = {d["sortie_id"]: d["deviation_seconds"] for d in w["reported_deviations"]}
    check(dev == {"A": 0, "B": -10}, f"deviations wrong: {dev}")

    # Projection onto t clips the interval start and moves the witness.
    w8 = evaluate_warning(sorties, plan, reports, 30, 10, 8)
    c8 = w8["conflicts"][0]
    check(abs(c8["time_start"] - 8) < 1e-9, "interval start clipped at t")
    check(c8["time_end"] == c["time_end"], "interval end preserved")
    check(abs(c8["witness_time"] - 0.5 * (8 + c["time_end"])) < 1e-6,
          "witness recomputed at midpoint of clipped interval")
    check(abs(w8["first_conflict_time"] - 8) < 1e-9, "first conflict clipped to t")

    # After the interval ends there are no conflicts.
    check(not evaluate_warning(sorties, plan, reports, 30, 10, 13)["conflicts"],
          "no conflicts past the interval end")

    # t earlier than a reported actual take-off is rejected.
    try:
        evaluate_warning(sorties, plan,
                         {"A": Report(0, 1), "B": Report(9, 1)}, 30, 10, 3)
        check(False, "expected EarlyTimeReject")
    except EarlyTimeReject as exc:
        check([v["sortie_id"] for v in exc.violations] == ["B"],
              "violation names the future-reporting sortie")

    # Unreported with planned take-off == t is still planned; earlier is overdue.
    w10 = evaluate_warning(sorties, plan, {"A": Report(0, 1)}, 30, 10, 10)
    check({r["id"]: r["status"] for r in w10["sorties"]} ==
          {"A": "reported", "B": "planned"}, "take-off exactly at t counts as planned")
    check(w10["complete"], "prediction complete when nothing is overdue")
    w15 = evaluate_warning(sorties, plan, {}, 30, 10, 15)
    check(set(w15["overdue_sorties"]) == {"A", "B"}, "both overdue at t=15")
    check(w15["incomplete"] and not w15["conflicts"],
          "overdue excluded from prediction and result incomplete")


def test_store_revision_protocol():
    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "t.db"))
        sid = store.create_scenario({"sorties": []}, 30, 10)
        bid = store.insert_batch(sid, "b", {"sorties": [{"id": "A"}, {"id": "B"}]},
                                 30, 10, {"offsets": {"A": 1, "B": 2}})

        outcome, _ = store.submit_report(bid, "A", 5, 2)
        check(outcome == "invalid_first_revision", "first report revision must be 1")
        outcome, rep = store.submit_report(bid, "A", 5, 1)
        check(outcome == "inserted" and rep["actual_offset"] == 5, "first insert")
        outcome, rep = store.submit_report(bid, "A", 5, 1)
        check(outcome == "idempotent", "same revision same value idempotent")
        outcome, info = store.submit_report(bid, "A", 6, 1)
        check(outcome == "revision_value_conflict" and info["stored_actual_offset"] == 5,
              "same revision different value conflicts")
        outcome, _ = store.submit_report(bid, "A", 7, 2)
        check(outcome == "inserted", "strictly higher revision accepted")
        outcome, info = store.submit_report(bid, "A", 5, 1)
        check(outcome == "stale_revision" and info["current_revision"] == 2,
              "older revision after a newer one is stale")
        row = store.get_batch(bid)
        check(len(row["reports"]) == 1 and row["reports"][0]["revision"] == 2
              and row["reports"][0]["actual_offset"] == 7,
              "only the highest revision row survives")

        # Freeze: batch stores its own snapshot and plan.
        check(row["plan"]["offsets"] == {"A": 1, "B": 2}, "planned offsets frozen")
        check(row["safety_radius"] == 30 and row["safety_height"] == 10,
              "safety thresholds frozen")


def test_concurrent_keeps_highest():
    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "t.db"))
        sid = store.create_scenario({"sorties": []}, 30, 10)
        for trial in range(20):
            bid = store.insert_batch(sid, f"b{trial}", {"sorties": [{"id": "A"}]},
                                     30, 10, {"offsets": {"A": 0}})
            outcomes = []
            lock = threading.Lock()

            def put(off, rev):
                o, _ = store.submit_report(bid, "A", off, rev)
                with lock:
                    outcomes.append((rev, o))

            ts = [threading.Thread(target=put, args=(10 * r, r))
                  for r in (1, 2, 3, 4, 5, 6, 7, 8)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            row = store.get_batch(bid)
            check(len(row["reports"]) == 1, "single report row per sortie")
            check(row["reports"][0]["revision"] == 8
                  and row["reports"][0]["actual_offset"] == 80,
                  f"trial {trial}: highest revision must win, got {outcomes}")


def _mp_worker(path, bid, off, rev, barrier, queue):
    """Open the shared database in a *fresh* process and race one report."""
    store = Store(path)
    barrier.wait()
    outcome, info = store.submit_report(bid, "A", off, rev)
    queue.put((rev, outcome, info))


def test_multiprocess_shared_db():
    """Two OS processes sharing one SQLite file arbitrate at database level.

    With revision 1 stored, rev 3 and rev 2 are submitted simultaneously from
    two processes.  Whichever transaction wins, the final row must be the
    highest revision (3) and the loser must see its current revision.
    Concurrent first reports (both rev 1) follow the same-revision rules.
    """
    import multiprocessing as mp

    ctx = mp.get_context("fork")
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.db")
        setup = Store(path)
        sid = setup.create_scenario({"sorties": []}, 30, 10)

        def race(bid, offers):
            """offers: list of (revision, actual_offset) for racing processes."""
            barrier = ctx.Barrier(2)
            queue = ctx.Queue()
            procs = [
                ctx.Process(target=_mp_worker,
                            args=(path, bid, off, rev, barrier, queue))
                for rev, off in offers
            ]
            for p in procs:
                p.start()
            results = [queue.get(timeout=30) for _ in procs]
            for p in procs:
                p.join(10)
                check(p.exitcode == 0, "worker process finished cleanly")
            return results

        # --- revision 3 vs 2 on top of an existing revision 1 ---------------
        for trial in range(10):
            bid = setup.insert_batch(
                sid, f"race{trial}", {"sorties": [{"id": "A"}]},
                30, 10, {"offsets": {"A": 0}})
            outcome, _ = setup.submit_report(bid, "A", 10, 1)
            check(outcome == "inserted", "seed revision 1")
            results = race(bid, [(3, 30), (2, 20)])
            outcomes = {rev: (o, info) for rev, o, info in results}
            # Revision 3 is always accepted and the stored row is always 3/30.
            check(outcomes[3][0] == "inserted",
                  f"trial {trial}: revision 3 accepted, got {outcomes}")
            # Revision 2 may either lose outright to the committed 3 ...
            if outcomes[2][0] == "stale_revision":
                check(outcomes[2][1]["current_revision"] == 3,
                      f"trial {trial}: stale rev 2 reports current=3, got {outcomes}")
            # ... or serialize first (2 written over 1), in which case rev 3
            # then overwrites it; the invariant is that 2 can never be final.
            else:
                check(outcomes[2][0] == "inserted",
                      f"trial {trial}: rev 2 only allowed to serialize first, "
                      f"got {outcomes}")
            row = setup.get_batch(bid)
            check(len(row["reports"]) == 1
                  and row["reports"][0]["revision"] == 3
                  and row["reports"][0]["actual_offset"] == 30,
                  f"trial {trial}: stored row is the highest revision (3/30)")

        # --- concurrent first reports, same revision 1 ----------------------
        # Same value: one insert + one idempotent, exactly one row.
        bid = setup.insert_batch(
            sid, "first-same", {"sorties": [{"id": "A"}]},
            30, 10, {"offsets": {"A": 0}})
        results = race(bid, [(1, 5), (1, 5)])
        # Both carry revision 1; distinguish by outcome only.
        names = sorted(o for _, o, _ in results)
        check(names == ["idempotent", "inserted"],
              f"concurrent identical first reports: {results}")
        row = setup.get_batch(bid)
        check(len(row["reports"]) == 1 and row["reports"][0]["actual_offset"] == 5,
              "single row after concurrent identical first reports")

        # Different values: one insert + one same-revision value conflict.
        bid = setup.insert_batch(
            sid, "first-diff", {"sorties": [{"id": "A"}]},
            30, 10, {"offsets": {"A": 0}})
        results = race(bid, [(1, 5), (1, 6)])
        names = sorted(o for _, o, _ in results)
        check(names == ["inserted", "revision_value_conflict"],
              f"concurrent clashing first reports: {results}")
        row = setup.get_batch(bid)
        check(len(row["reports"]) == 1 and row["reports"][0]["revision"] == 1,
              "single row after concurrent clashing first reports")


def test_reload_consistency():
    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "t.db"))
        store.create_scenario({"sorties": []}, 30, 10)
        bid = store.insert_batch(1, "b", {"sorties": []}, 30, 10,
                                 {"offsets": {"A": 0, "B": 10}})
        store.submit_report(bid, "A", 0, 1)
        store.submit_report(bid, "B", 0, 1)
        sorties = two_crossing_sorties()
        reports1 = {r["sortie_id"]: Report(r["actual_offset"], r["revision"])
                    for r in store.get_batch(bid)["reports"]}
        w1 = evaluate_warning(sorties, {"A": 0, "B": 10}, reports1, 30, 10, 0)
        # Simulate a later reload from persistence.
        reports2 = {r["sortie_id"]: Report(r["actual_offset"], r["revision"])
                    for r in store.get_batch(bid)["reports"]}
        w2 = evaluate_warning(sorties, {"A": 0, "B": 10}, reports2, 30, 10, 0)
        check(w1 == w2, "reloaded batch evaluates identically")


def test_report_nodes_and_review():
    """Accepted writes create review nodes; rejected/idempotent ones don't.

    Each node restores the exact before/after report states; reviews replay
    both with the same t, intersecting conflict intervals only contribute
    their changed parts, and early-time rejection covers either side.
    """
    sorties = two_crossing_sorties()
    plan = {"A": 0, "B": 10}
    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "t.db"))
        sid = store.create_scenario({"sorties": []}, 30, 10)
        bid = store.insert_batch(sid, "b", {"sorties": [{"id": "A"}, {"id": "B"}]},
                                 30, 10, {"offsets": plan})
        nodes = store.list_report_nodes(bid)
        check(len(nodes) == 1 and nodes[0]["is_baseline"]
              and nodes[0]["seq"] == 1 and nodes[0]["before"] == [],
              "new batch starts with one empty baseline node")
        check(store.get_report_node(bid, nodes[0]["id"])["after"] == [],
              "baseline snapshot empty")

        # Accepted write -> node; idempotent retry / conflicts -> no node.
        store.submit_report(bid, "A", 5, 1)
        store.submit_report(bid, "A", 5, 1)                       # idempotent
        store.submit_report(bid, "A", 6, 1)                      # value conflict
        store.submit_report(bid, "A", 5, 1)                      # idempotent again
        store.submit_report(bid, "A", 2, 1)                      # value conflict
        nodes = store.list_report_nodes(bid)
        check(len(nodes) == 2 and not nodes[1]["is_baseline"]
              and nodes[1]["seq"] == 2,
              "only accepted writes create nodes")
        n1 = nodes[1]
        check(n1["before"] == [] and n1["after"]
              == [{"sortie_id": "A", "actual_offset": 5, "revision": 1}],
              "node snapshots bracket its write exactly")
        check(n1["trigger_sortie_id"] == "A"
              and n1["trigger_actual_offset"] == 5
              and n1["trigger_revision"] == 1,
              "node names the triggering sortie and revision")

        # Higher revision -> another node chaining on the previous after.
        store.submit_report(bid, "A", 7, 2)
        store.submit_report(bid, "A", 7, 1)                      # stale
        store.submit_report(bid, "B", 3, 2)                      # invalid first rev
        nodes = store.list_report_nodes(bid)
        check(len(nodes) == 3 and nodes[2]["before"] == n1["after"]
              and nodes[2]["after"]
              == [{"sortie_id": "A", "actual_offset": 7, "revision": 2}],
              "stale/invalid-first writes create no node; snapshots chain")
        check(store.get_report_node(bid, 99999) is None,
              "unknown node id is absent")

    # --- review: B joins its plan slot late, creating one added interval ---
    before = {"A": Report(0, 1)}
    after = {"A": Report(0, 1), "B": Report(0, 1)}
    r = review_warning(sorties, plan, before, after, 30, 10, 0,
                       trigger={"sortie_id": "B", "actual_offset": 0, "revision": 1})
    check(r["first_conflict_time_before"] is None
          and r["first_conflict_time_after"] is not None,
          "first conflict appears after the node")
    check(len(r["pair_changes"]) == 1
          and len(r["pair_changes"][0]["added"]) == 1
          and not r["pair_changes"][0]["removed"],
          "one added conflict section, nothing removed")
    check(r["node"]["sortie_id"] == "B" and r["node"]["revision"] == 1
          and r["node"]["baseline"] is False,
          "review names the triggering write")
    changed = {x["id"]: x for x in r["sortie_changes"]}
    check(changed["B"]["changed"] and changed["B"]["status_before"] == "planned"
          and changed["B"]["status_after"] == "reported"
          and not changed["A"]["changed"],
          "only the reporting sortie changes")
    check(r["overdue_added"] == [] and r["overdue_removed"] == [],
          "no overdue change here")

    # Identical before/after states -> no pair changes at all.
    r = review_warning(sorties, plan, before, before, 30, 10, 0,
                       trigger={"sortie_id": "A", "actual_offset": 0, "revision": 1})
    check(r["pair_changes"] == []
          and r["first_conflict_time_before"] == r["first_conflict_time_after"],
          "identical states show no change")

    # Baseline trigger -> baseline flag.
    r = review_warning(sorties, plan, {}, {}, 30, 10, 0, trigger=None)
    check(r["node"]["baseline"] is True and r["node"]["sortie_id"] is None,
          "baseline node flagged")


def test_review_overlap_only_changed_parts():
    """Intersecting before/after intervals surface only their differences."""
    # Pure interval subtraction.
    check(subtract_real_intervals([(5, 15)], [(10, 20)]) == [(5, 10)],
          "overlap keeps the earlier-only section")
    check(subtract_real_intervals([(5, 15)], [(0, 10)]) == [(10, 15)],
          "overlap keeps the later-only section")
    check(subtract_real_intervals([(0, 10)], [(3, 7)]) == [(0, 3), (7, 10)],
          "contained interval leaves both ends")
    check(subtract_real_intervals([(3, 4)], [(0, 10)]) == [],
          "fully covered interval has no changed part")
    check(subtract_real_intervals([(0, 5)], [(0, 5)]) == [],
          "identical interval is all overlap, no change")

    # Isolated instants (degenerate [p, p] intervals).
    check(subtract_real_intervals([(5, 5)], []) == [(5, 5)],
          "isolated instant on one side only is a changed point")
    check(subtract_real_intervals([(5, 5)], [(6, 9)]) == [(5, 5)],
          "instant disjoint from the other side is kept")
    check(subtract_real_intervals([(5, 5)], [(0, 3), (7, 9)]) == [(5, 5)],
          "instant in a gap between the other side's intervals is kept")
    check(subtract_real_intervals([(5, 5)], [(5, 5)]) == [],
          "instant shared by both sides is not a change")
    check(subtract_real_intervals([(5, 5)], [(5, 9)]) == [],
          "instant equal to the other side's left endpoint is shared")
    check(subtract_real_intervals([(5, 5)], [(1, 5)]) == [],
          "instant equal to the other side's right endpoint is shared")
    check(subtract_real_intervals([(5, 5)], [(0, 10)]) == [],
          "instant inside the other side's interval is covered")

    # Geometry: B deviates from actual 3 to actual 4 (plan 0).  Before and
    # after conflict windows with C/D overlap, so the review must report the
    # non-overlapping heads/tails and never the shared middle.
    import json
    payload = json.load(open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "examples", "crossroads.json")))
    s2 = []
    for s in payload["sorties"]:
        wps = [Waypoint(w["x"], w["y"], w["z"], w["t"]) for w in s["waypoints"]]
        s2.append(make_sortie(s["id"], s["weight"], s["window_hi"], wps, 0))
    plan2 = {"UAV-A": 4, "UAV-B": 0, "UAV-C": 2, "UAV-D": 6}
    before = {"UAV-A": Report(4, 1), "UAV-B": Report(3, 1), "UAV-C": Report(2, 1)}
    after = {"UAV-A": Report(4, 1), "UAV-B": Report(4, 2), "UAV-C": Report(2, 1)}
    wb = evaluate_warning(s2, plan2, before, 30, 10, 4)
    wa = evaluate_warning(s2, plan2, after, 30, 10, 4)
    b_iv = {(c["sortie_a"], c["sortie_b"]): (c["time_start"], c["time_end"])
            for c in wb["conflicts"]}
    a_iv = {(c["sortie_a"], c["sortie_b"]): (c["time_start"], c["time_end"])
            for c in wa["conflicts"]}
    r = review_warning(s2, plan2, before, after, 30, 10, 4,
                       trigger={"sortie_id": "UAV-B", "actual_offset": 4,
                                "revision": 2})
    check(r["first_conflict_time_before"] != r["first_conflict_time_after"],
          "first conflict time moves with the revision")
    by_pair = {(p["sortie_a"], p["sortie_b"]): p for p in r["pair_changes"]}
    check(set(by_pair) == set(b_iv) == set(a_iv),
          "every overlapping pair appears once")
    for key, p in by_pair.items():
        for seg in p["added"]:
            check(seg["change"] == "added"
                  and seg["time_start"] >= b_iv[key][1] - 1e-6,
                  f"added section starts only after the before interval {key}")
        for seg in p["removed"]:
            check(seg["change"] == "removed"
                  and seg["time_end"] <= a_iv[key][0] + 1e-6,
                  f"removed section ends before the after interval {key}")
        # Shared interior must not appear in either list.
        lo = max(b_iv[key][0], a_iv[key][0])
        hi = min(b_iv[key][1], a_iv[key][1])
        for seg in p["added"] + p["removed"]:
            check(not (seg["time_start"] >= lo - 1e-6
                       and seg["time_end"] <= hi + 1e-6),
                  f"overlapping middle of {key} is not marked as change")


def test_review_instant_conflict():
    """An accepted report makes two sorties meet at ONE instant only.

    With the review's current second t exactly at that instant the warning
    lists the degenerate [t, t] interval; the review must surface it as an
    added/removed point change instead of claiming the conflict set is
    unchanged, while an instant shared by both sides stays silent.
    """
    sorties = two_touching_sorties()
    plan = {"A": 0, "B": 0}
    # B reported at actual 10: its window [10, 20] touches A's [0, 10] at the
    # single instant T=10 where both sit at (100, 0, 20).
    before = {"A": Report(0, 1)}
    after = {"A": Report(0, 1), "B": Report(10, 1)}

    w_after = evaluate_warning(sorties, plan, after, 30, 10, 10)
    check(len(w_after["conflicts"]) == 1
          and w_after["conflicts"][0]["time_start"] == 10
          and w_after["conflicts"][0]["time_end"] == 10
          and w_after["first_conflict_time"] == 10,
          "warning lists the instantaneous conflict as [t, t]")
    check(not evaluate_warning(sorties, plan, before, 30, 10, 10)["conflicts"],
          "before the report there is no conflict at t")

    r = review_warning(sorties, plan, before, after, 30, 10, 10,
                       trigger={"sortie_id": "B", "actual_offset": 10,
                                "revision": 1})
    check(r["first_conflict_time_before"] is None
          and r["first_conflict_time_after"] == 10,
          "first conflict appears exactly at the meeting instant")
    check(len(r["pair_changes"]) == 1, "instantaneous conflict is a change")
    pc = r["pair_changes"][0]
    check((pc["sortie_a"], pc["sortie_b"]) == ("A", "B")
          and [(s["time_start"], s["time_end"]) for s in pc["added"]]
          == [(10, 10)] and not pc["removed"],
          f"added lists the isolated instant [10, 10], got {pc}")
    check(pc["added"][0]["witness_time"] == 10
          and pc["added"][0]["horizontal_distance"] == 0
          and pc["added"][0]["vertical_distance"] == 0,
          "witness sits at the meeting instant/point")

    # A correction (higher revision) moving B back to plan removes the point.
    corrected = {"A": Report(0, 1), "B": Report(0, 2)}
    r2 = review_warning(sorties, plan, after, corrected, 30, 10, 10,
                        trigger={"sortie_id": "B", "actual_offset": 0,
                                 "revision": 2})
    check(len(r2["pair_changes"]) == 1
          and [(s["time_start"], s["time_end"])
               for s in r2["pair_changes"][0]["removed"]] == [(10, 10)]
          and not r2["pair_changes"][0]["added"],
          "correcting B off the meeting instant removes [10, 10]")

    # The same isolated instant on both sides is not a change.
    r3 = review_warning(sorties, plan, after, after, 30, 10, 10, trigger=None)
    check(r3["pair_changes"] == [], "shared instant is not reported as change")

    # t one second past the instant: the point conflict is already over.
    check(not evaluate_warning(sorties, plan, after, 30, 10, 11)["conflicts"],
          "instantaneous conflict does not leak past its instant")


def test_review_rejects_on_either_side():
    sorties = two_crossing_sorties()
    plan = {"A": 0, "B": 10}
    # Before has a future take-off (9) while the correction moves back to 0:
    # t=3 is earlier than the BEFORE side's reported take-off -> whole review
    # rejected, no diff.
    try:
        review_warning(sorties, plan,
                       {"A": Report(9, 1)}, {"A": Report(0, 2)}, 30, 10, 3,
                       trigger={"sortie_id": "A", "actual_offset": 0, "revision": 2})
        check(False, "review must reject when t precedes a before-side take-off")
    except EarlyTimeReject as exc:
        check([v["sortie_id"] for v in exc.violations] == ["A"],
              "rejection names the violating sortie from either side")
    # And vice versa: after side in the future is rejected just as well.
    try:
        review_warning(sorties, plan,
                       {"A": Report(0, 1)}, {"A": Report(9, 2)}, 30, 10, 3,
                       trigger={"sortie_id": "A", "actual_offset": 9, "revision": 2})
        check(False, "review must reject when t precedes an after-side take-off")
    except EarlyTimeReject:
        pass


def test_review_overdue_change():
    sorties = two_crossing_sorties()
    plan = {"A": 0, "B": 10}
    # At t=5 nothing is reported: A (plan 0) overdue.  After A reports actual
    # 0 it becomes reported; B stays planned (plan 10 >= t).
    r = review_warning(sorties, plan, {}, {"A": Report(0, 1)}, 30, 10, 5,
                       trigger={"sortie_id": "A", "actual_offset": 0, "revision": 1})
    check(r["overdue_before"] == ["A"] and r["overdue_after"] == []
          and r["overdue_removed"] == ["A"] and r["overdue_added"] == [],
          "reporting an overdue sortie clears overdue on the after side")
    check(r["complete_before"] is False and r["complete_after"] is True,
          "completeness flips with the overdue set")


def test_upgrade_baseline_backfill():
    """A database created before nodes existed gets exactly one baseline per
    batch, capturing the latest state knowable at upgrade time."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.db")
        old = Store(path)
        sid = old.create_scenario({"sorties": []}, 30, 10)
        # Simulate a database written by a pre-feature release: insert the
        # batch and its report rows directly, bypassing the node table, then
        # delete any baseline so the batch has ZERO nodes (the exact shape an
        # upgrade finds on disk).
        import sqlite3
        import time
        with sqlite3.connect(path) as c:
            now = time.time()
            c.execute(
                "INSERT INTO batches(scenario_id,name,input_json,safety_radius,"
                "safety_height,plan_json,created_at) VALUES (?,?,?,?,?,?,?)",
                (sid, "old", '{"sorties":[{"id":"A"}]}', 30.0, 10.0,
                 '{"offsets":{"A":1}}', now))
            old_bid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute(
                "INSERT INTO batch_reports(batch_id,sortie_id,actual_offset,"
                "revision,reported_at) VALUES (?,?,?,?,?)",
                (old_bid, "A", 8, 2, now))
            c.execute("DELETE FROM batch_report_nodes")

        reopened = Store(path)  # runs the idempotent backfill
        nodes = reopened.list_report_nodes(old_bid)
        check(len(nodes) == 1 and nodes[0]["is_baseline"]
              and nodes[0]["before"] == nodes[0]["after"]
              == [{"sortie_id": "A", "actual_offset": 8, "revision": 2}],
              "upgraded batch gets one baseline at the newest known state, "
              "no fabricated earlier history")
        # Backfill is idempotent: another open must not duplicate baselines.
        Store(path)
        check(len(Store(path).list_report_nodes(old_bid)) == 1,
              "baseline backfill runs once")


def test_concurrent_nodes_follow_commit_order():
    """Racing accepted writes from threads and processes: one node per
    accepted write, seq contiguous, snapshots chain from the prior after, and
    replaying nodes reproduces the final report state.

    Under the revision protocol a lower revision is still rejected when a
    higher one committed first, so the accepted revision set is 1 plus the
    record highs in actual commit order.
    """
    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "t.db"))
        sid = store.create_scenario({"sorties": []}, 30, 10)
        bid = store.insert_batch(sid, "seq", {"sorties": [{"id": "A"}]},
                                 30, 10, {"offsets": {"A": 0}})
        # Strictly increasing submissions: every write accepted, node chain
        # reproduces the whole history in commit order.
        for rev in range(1, 9):
            o, _ = store.submit_report(bid, "A", 10 * rev, rev)
            check(o == "inserted", "sequential increasing revisions accepted")
        nodes = store.list_report_nodes(bid)
        check([n["seq"] for n in nodes] == list(range(1, 10)),
              "sequential chain: contiguous seq 1..9")
        acc = nodes[1:]
        check([n["trigger_revision"] for n in acc] == list(range(1, 9)),
              "sequential nodes follow commit order 1..8")
        for prev, cur in zip(nodes, nodes[1:]):
            check(prev["after"] == cur["before"], "sequential snapshots chain")
        check(nodes[-1]["after"]
              == [{"sortie_id": "A", "actual_offset": 80, "revision": 8}],
              "sequential replay ends at highest revision")

        # Truly concurrent revisions in ONE batch: every accepted write gets
        # exactly one node in commit order; rejected stale writes get none;
        # the chain always replays to the highest committed revision.
        for trial in range(10):
            bid = store.insert_batch(sid, f"b{trial}", {"sorties": [{"id": "A"}]},
                                     30, 10, {"offsets": {"A": 0}})
            barrier = threading.Barrier(8)
            results = []
            lock = threading.Lock()

            def put(rev):
                barrier.wait()
                o, _ = store.submit_report(bid, "A", 10 * rev, rev)
                with lock:
                    results.append((rev, o))

            ts = [threading.Thread(target=put, args=(r,)) for r in range(1, 9)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            nodes = store.list_report_nodes(bid)
            accepted = sorted(rev for rev, o in results if o == "inserted")
            check([n["seq"] for n in nodes] == list(range(1, len(nodes) + 1)),
                  f"trial {trial}: contiguous node seq")
            nodes_acc = nodes[1:]
            check(all(not n["is_baseline"] for n in nodes_acc), "no stray baselines")
            check(len(nodes_acc) == len(accepted),
                  f"trial {trial}: one node per accepted write "
                  f"({len(nodes_acc)} nodes vs {len(accepted)} accepts)")
            check(sorted(n["trigger_revision"] for n in nodes_acc) == accepted,
                  f"trial {trial}: node revisions match accepted writes")
            node_revs = [n["trigger_revision"] for n in nodes_acc]
            check(node_revs == sorted(node_revs) and node_revs[-1] == max(accepted),
                  f"trial {trial}: commit order ends at highest accepted, "
                  f"got {node_revs}")
            for prev, cur in zip(nodes, nodes[1:]):
                check(prev["after"] == cur["before"],
                      f"trial {trial}: snapshots chain at seq {cur['seq']}")
            top = max(accepted)
            check(nodes[-1]["after"]
                  == [{"sortie_id": "A", "actual_offset": 10 * top,
                       "revision": top}],
                  f"trial {trial}: replay ends at highest accepted revision")

    # Cross-process: two processes racing rev 2/3.  Rev 3 always wins; rev 2
    # is a node only when it serializes before rev 3.  Whatever the order,
    # reloaded nodes are identical and replay to the highest revision.
    import multiprocessing as mp
    ctx = mp.get_context("fork")
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.db")
        setup = Store(path)
        sid = setup.create_scenario({"sorties": []}, 30, 10)
        for trial in range(8):
            bid = setup.insert_batch(sid, f"mp{trial}", {"sorties": [{"id": "A"}]},
                                     30, 10, {"offsets": {"A": 0}})
            setup.submit_report(bid, "A", 10, 1)
            barrier = ctx.Barrier(2)
            queue = ctx.Queue()
            procs = [
                ctx.Process(target=_node_worker,
                            args=(path, bid, off, rev, barrier, queue))
                for off, rev in ((20, 2), (30, 3))]
            for p in procs:
                p.start()
            worker_outcomes = [queue.get(timeout=30) for _ in procs]
            for p in procs:
                p.join(10)
            nodes = setup.list_report_nodes(bid)
            revs = [n["trigger_revision"] for n in nodes[2:]]
            # rev 3 is always present; rev 2 only when it committed first.
            check(revs[-1] == 3 and sorted(set(revs)) == revs
                  and (revs == [3] or revs == [2, 3]),
                  f"mp{trial}: nodes follow commit order, highest=3, got {revs}")
            # baseline + seeded rev1 (one each) + one node per raced insert.
            check(len(nodes) == 2 + sum(1 for o in worker_outcomes
                                        if o == "inserted"),
                  f"mp{trial}: node count equals accepted writes, "
                  f"{worker_outcomes} vs {revs}")
            check(any(o == "stale_revision" for o in worker_outcomes)
                  or all(o == "inserted" for o in worker_outcomes),
                  f"mp{trial}: loser (if any) is stale and creates no node, "
                  f"got {worker_outcomes}")
            check(nodes[-1]["after"]
                  == [{"sortie_id": "A", "actual_offset": 30, "revision": 3}],
                  f"mp{trial}: final node snapshot is the highest revision")
            for prev, cur in zip(nodes, nodes[1:]):
                check(prev["after"] == cur["before"],
                      f"mp{trial}: cross-process snapshots chain")
            # Reload from a fresh Store and confirm identical replay.
            reloaded = Store(path).list_report_nodes(bid)
            check([(n["seq"], n["before"], n["after"]) for n in reloaded]
                  == [(n["seq"], n["before"], n["after"]) for n in nodes],
                  f"mp{trial}: reloaded nodes identical")


def _node_worker(path, bid, off, rev, barrier, queue):
    store = Store(path)
    barrier.wait()
    outcome, _ = store.submit_report(bid, "A", off, rev)
    queue.put(outcome)


def main():
    test_evaluation()
    test_store_revision_protocol()
    test_concurrent_keeps_highest()
    test_multiprocess_shared_db()
    test_reload_consistency()
    test_report_nodes_and_review()
    test_review_overlap_only_changed_parts()
    test_review_instant_conflict()
    test_review_rejects_on_either_side()
    test_review_overdue_change()
    test_upgrade_baseline_backfill()
    test_concurrent_nodes_follow_commit_order()
    print("failures:", FAILS)
    return FAILS


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
