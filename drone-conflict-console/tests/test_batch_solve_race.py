"""Deterministic two-process race: batch creation vs. a committing solve.

The stale-adjudication bug this guards against:

* POST /api/scenarios/{sid}/batches used to read the feasible old table and
  the (absent) unknown marker OUTSIDE its write transaction and only opened a
  new transaction to insert the batch;
* if POST /api/scenarios/{sid}/solve committed in the gap - either an
  ``unknown`` run with new locks/thresholds (persisted marker) or a NEW
  verified feasible/infeasible table - the batch insert still ran on the
  pre-transaction read:
    - the committed unknown marker wrongly admitted a request that never
      selected the old table (an unverified old table frozen as if verified);
    - a newly committed feasible table was ignored and the stale old table
      wrongly frozen.

The fix reads the AUTHORITATIVE scenario row (feasible table, marker and
stored thresholds) INSIDE the batch's own ``BEGIN IMMEDIATE`` transaction and
binds the old-table threshold confirmation and the snapshot freeze to that
read.  These tests make the interleaving deterministic by holding a raw write
transaction open in one process (the committing /solve), blocking the batch
transaction of a second forked process on SQLite's reserved lock, then
committing:

* unknown committed first -> only ``use_stored_plan=true`` with the OLD
  thresholds may still create; the unflagged request is refused, no batch;
* new feasible committed first -> the batch freezes the NEW snapshot, never
  the stale old table;
* proven infeasible / no table committed first -> refused, no batch;
* the opposite order (batch commits first, solve second) keeps the snapshot
  valid at batch time and the solve then persists on top.

Run:  python tests/test_batch_solve_race.py
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.storage import BatchRejected, Store  # noqa: E402

FAILS = 0


def check(cond, msg):
    global FAILS
    if not cond:
        FAILS += 1
        print("FAIL:", msg)
    else:
        print("ok:", msg)


def feasible_result(offsets):
    return {
        "feasible": True,
        "status": "optimal",
        "weighted_delay": sum(offsets.values()),
        "offsets": dict(offsets),
        "locked": {},
        "elapsed_seconds": 0.01,
        "solved_conflicts": [],
        "cores": [],
        "truncated": False,
    }


# The retained OLD table is always (R=30, H=10) with offsets {A: 1}; the
# racing NEW verified table is (R=45, H=12) with offsets {A: 9}.
OLD_R, OLD_H, OLD_OFF = 30.0, 10.0, {"A": 1}
NEW_R, NEW_H, NEW_OFF = 45.0, 12.0, {"A": 9}
INPUT_JSON = json.dumps({"sorties": [{"id": "A"}]})
UNKNOWN_ATTEMPT = json.dumps({
    "status": "unknown", "feasible": False,
    "safety_radius": NEW_R, "safety_height": NEW_H, "locks": {"A": 3}})


def _new_raw_conn(path):
    return sqlite3.connect(path, timeout=10, isolation_level=None)


def _seed(path) -> int:
    """A scenario holding a verified feasible OLD table, no marker."""
    store = Store(path)
    sid = store.create_scenario({"sorties": [{"id": "A"}]}, OLD_R, OLD_H)
    store.save_result(sid, feasible_result(OLD_OFF), OLD_R, OLD_H)
    return sid


def _batch_worker(path, sid, ready, gate, queue, **kwargs):
    # Construct the Store (its own WAL/migration write transactions) BEFORE
    # the parent holds a write lock, then wait for the gate; only then race
    # into the batch transaction.
    store = Store(path)
    ready.set()
    gate.wait()
    try:
        bid = store.create_batch(sid, "raced", **kwargs)
        queue.put(("ok", bid))
    except BatchRejected as exc:
        queue.put(("rejected", exc.status_code, exc.reason))
    except Exception as exc:  # a lock failure must not masquerade as success
        queue.put(("error", repr(exc)))


def _run_with_pending_commit(path, sid, pending_sql, pending_args,
                             batch_kwargs, hold=0.6):
    """Fork a batch process while THIS process holds an uncommitted write.

    The child finishes its Store initialization and blocks on ``gate``; once
    it is ready THIS process opens the pending write transaction (so the
    child's init cannot deadlock on it), releases the gate and keeps the
    transaction open for ``hold`` seconds.  The child's ``BEGIN IMMEDIATE``
    queues on SQLite's write lock until the pending solve is committed here,
    so the child's in-transaction authoritative read is guaranteed to happen
    AFTER the pending solve committed.
    """
    ctx = mp.get_context("fork")
    ready, gate = ctx.Event(), ctx.Event()
    queue = ctx.Queue()
    child = ctx.Process(target=_batch_worker,
                        args=(path, sid, ready, gate, queue),
                        kwargs=batch_kwargs)
    child.start()
    ready.wait(30)
    raw = _new_raw_conn(path)
    raw.execute("BEGIN IMMEDIATE")
    raw.execute(pending_sql, pending_args)
    gate.set()
    time.sleep(hold)  # child is queued on the write lock during this window
    raw.commit()
    raw.close()
    outcome = queue.get(timeout=30)
    child.join(10)
    check(child.exitcode == 0, f"child finished cleanly -> {outcome}")
    return outcome


def _pending_unknown(sid):
    return (
        "UPDATE scenarios SET latest_solve_status='unknown', "
        "unverified_solve_json=? WHERE id=?",
        (UNKNOWN_ATTEMPT, sid),
    )


def _pending_feasible(sid):
    return (
        "UPDATE scenarios SET result_json=?, solved_at=?, safety_radius=?, "
        "safety_height=?, latest_solve_status=NULL, unverified_solve_json=NULL "
        "WHERE id=?",
        (json.dumps(feasible_result(NEW_OFF)), time.time(),
         NEW_R, NEW_H, sid),
    )


def _pending_infeasible(sid):
    res = json.dumps({"feasible": False, "status": "infeasible",
                      "offsets": {}, "cores": []})
    return (
        "UPDATE scenarios SET result_json=?, solved_at=?, "
        "latest_solve_status=NULL, unverified_solve_json=NULL WHERE id=?",
        (res, time.time(), sid),
    )


def _pending_no_table(sid):
    return (
        "UPDATE scenarios SET result_json=NULL, solved_at=NULL, "
        "latest_solve_status=NULL, unverified_solve_json=NULL WHERE id=?",
        (sid,),
    )


def _pending_rename(sid):
    return "UPDATE scenarios SET name=? WHERE id=?", ("renamed", sid)


def test_unknown_commits_first():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.db")
        sid = _seed(path)

        # Unflagged request that read "feasible, no marker" before the gap but
        # reaches its write transaction only AFTER the unknown marker
        # committed: it must be refused 409 and leave no batch.
        out = _run_with_pending_commit(
            path, sid, *_pending_unknown(sid), {"use_stored_plan": False})
        check(out[0] == "rejected" and out[1] == 409
              and out[2] == "latest_solve_unverified",
              f"unknown committed first: unflagged batch refused 409 -> {out}")
        check(Store(path).list_batches(sid) == [],
              "unknown committed first: refusal left no batch")

        # Explicit old-table selection that confirms the OLD thresholds still
        # succeeds (the marker was there before the batch's authoritative
        # read) and freezes the OLD table/OLD thresholds.
        out = _run_with_pending_commit(
            path, sid, *_pending_unknown(sid),
            {"use_stored_plan": True, "confirmed_radius": OLD_R,
             "confirmed_height": OLD_H})
        check(out[0] == "ok", f"explicit old-table selection still creates -> {out}")
        if out[0] == "ok":
            b = Store(path).get_batch(out[1])
            check(b["safety_radius"] == OLD_R and b["safety_height"] == OLD_H
                  and b["plan"]["offsets"] == OLD_OFF,
                  "marker-first explicit batch freezes the old table/old R/H")

        # Selecting the old table but confirming the NEW attempted thresholds
        # is a mismatch; confirming without the flag path is likewise refused.
        sid = _seed(path)
        out = _run_with_pending_commit(
            path, sid, *_pending_unknown(sid),
            {"use_stored_plan": True, "confirmed_radius": NEW_R,
             "confirmed_height": NEW_H})
        check(out[0] == "rejected" and out[1] == 409
              and out[2] == "stored_plan_threshold_mismatch",
              f"new thresholds confirmed against old table -> mismatch 409 -> {out}")
        check(not Store(path).list_batches(sid), "mismatch left no batch")

        sid = _seed(path)
        out = _run_with_pending_commit(
            path, sid, *_pending_unknown(sid),
            {"use_stored_plan": True})
        check(out[0] == "rejected" and out[1] == 409
              and out[2] == "stored_plan_threshold_confirmation_required",
              f"selection without threshold confirmation -> 409 -> {out}")
        check(not Store(path).list_batches(sid), "missing confirmation left no batch")


def test_feasible_commits_first():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.db")
        sid = _seed(path)
        # The request had the OLD table in mind (no flag needed pre-gap), but
        # a NEW verified feasible solve commits first.  The batch must freeze
        # the NEW snapshot/thresholds/offsets read inside the transaction -
        # never the stale old table.
        out = _run_with_pending_commit(
            path, sid, *_pending_feasible(sid), {"use_stored_plan": False})
        check(out[0] == "ok", f"new feasible committed first: batch created -> {out}")
        if out[0] == "ok":
            b = Store(path).get_batch(out[1])
            check(b["safety_radius"] == NEW_R and b["safety_height"] == NEW_H
                  and b["plan"]["offsets"] == NEW_OFF,
                  f"batch freezes the NEW committed table, got "
                  f"R={b['safety_radius']} H={b['safety_height']} "
                  f"offsets={b['plan']['offsets']}")
            check(len(Store(path).list_report_nodes(out[1])) == 1,
                  "race-won batch still gets its baseline node atomically")


def test_infeasible_and_no_table_commit_first():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.db")

        sid = _seed(path)
        out = _run_with_pending_commit(
            path, sid, *_pending_infeasible(sid), {"use_stored_plan": False})
        check(out[0] == "rejected" and out[1] == 400,
              f"proven infeasible committed first -> 400 no batch -> {out}")
        check(not Store(path).list_batches(sid), "infeasible-first left no batch")

        sid = _seed(path)
        out = _run_with_pending_commit(
            path, sid, *_pending_no_table(sid), {"use_stored_plan": False})
        check(out[0] == "rejected" and out[1] == 400,
              f"no feasible table committed first -> 400 no batch -> {out}")
        check(not Store(path).list_batches(sid), "no-table-first left no batch")

        # A plain rename committing in the gap changes nothing: the retained
        # old table was and remains feasible, the batch is created.
        sid = _seed(path)
        out = _run_with_pending_commit(
            path, sid, *_pending_rename(sid), {"use_stored_plan": False})
        check(out[0] == "ok", f"rename in the gap must not block the batch -> {out}")


# ---------------------------------------------------------------------------
# Opposite order: the batch commits FIRST.  It keeps the snapshot valid then;
# the solve that follows persists afterwards without touching the batch.
# ---------------------------------------------------------------------------


def _solve_after_batch_worker(path, sid, ready, gate, queue, kind):
    # Initialize before the parent's batch transaction holds the write lock.
    store = Store(path)
    ready.set()
    gate.wait()
    if kind == "unknown":
        store.mark_solve_unknown(sid, json.loads(UNKNOWN_ATTEMPT))
        queue.put(("unknown",))
    else:
        store.save_result(sid, feasible_result(NEW_OFF), NEW_R, NEW_H)
        queue.put(("feasible",))


def test_batch_commits_first_then_solve():
    ctx = mp.get_context("fork")
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.db")
        main_store = Store(path)

        def race_batch_first(sid, kind):
            ready, gate = ctx.Event(), ctx.Event()
            queue = ctx.Queue()
            child = ctx.Process(target=_solve_after_batch_worker,
                                args=(path, sid, ready, gate, queue, kind))
            child.start()
            ready.wait(30)
            gen = main_store._write_tx()
            c = gen.__enter__()
            try:
                row = c.execute(
                    "SELECT input_json,safety_radius,safety_height,result_json,"
                    "latest_solve_status FROM scenarios WHERE id=?",
                    (sid,)).fetchone()
                result = json.loads(row["result_json"])
                bid = main_store._insert_batch_locked(
                    c, sid, f"batch-before-{kind}",
                    json.loads(row["input_json"]),
                    row["safety_radius"], row["safety_height"],
                    {"offsets": dict(result["offsets"])})
                gate.set()
                time.sleep(0.6)  # child solve queued on our write lock
                gen.__exit__(None, None, None)
            except BaseException:
                gen.__exit__(True, None, None)
                raise
            child.join(10)
            check(child.exitcode == 0, f"{kind}: child solve finished cleanly")
            queue.get(timeout=30)
            return bid

        # Case 1: batch first on OLD table, then a NEW feasible solve: the
        # batch keeps the old snapshot; the scenario advances to the new one.
        sid1 = _seed(path)
        bid = race_batch_first(sid1, "feasible")
        b = Store(path).get_batch(bid)
        check(b is not None and b["safety_radius"] == OLD_R
              and b["safety_height"] == OLD_H
              and b["plan"]["offsets"] == OLD_OFF,
              f"feasible: batch committed first keeps its then-valid OLD "
              f"snapshot -> {b and (b['safety_radius'], b['plan']['offsets'])}")
        sc = Store(path).get_scenario(sid1)
        check(sc["safety_radius"] == NEW_R and sc["result"]["offsets"] == NEW_OFF
              and sc.get("unverified_solve") is None,
              "feasible solve last: scenario now holds the NEW table, no marker")

        # Case 2: FRESH scenario, batch first on the OLD verified table, then
        # an UNKNOWN solve.  The batch keeps the old snapshot while the marker
        # then stands for subsequent requests.
        sid2 = _seed(path)
        bid = race_batch_first(sid2, "unknown")
        b = Store(path).get_batch(bid)
        check(b is not None and b["safety_radius"] == OLD_R
              and b["safety_height"] == OLD_H
              and b["plan"]["offsets"] == OLD_OFF,
              f"unknown: batch committed first keeps its then-valid OLD "
              f"snapshot -> {b and (b['safety_radius'], b['plan']['offsets'])}")
        check(len(Store(path).list_batches(sid2)) == 1,
              "exactly the one race-won batch exists for this scenario")
        try:
            Store(path).create_batch(sid2, "after-unknown")
            check(False, "later unflagged batch must be refused")
        except BatchRejected as exc:
            check(exc.status_code == 409
                  and exc.reason == "latest_solve_unverified",
                  "unknown solve last: later unflagged batch refused 409")
        check(len(Store(path).list_batches(sid2)) == 1,
              "refusal leaves the race-won batch count unchanged")
        bid2 = Store(path).create_batch(
            sid2, "explicit-after-unknown", use_stored_plan=True,
            confirmed_radius=OLD_R, confirmed_height=OLD_H)
        b2 = Store(path).get_batch(bid2)
        check(b2["safety_radius"] == OLD_R and b2["plan"]["offsets"] == OLD_OFF,
              "marker stands: explicit selection still freezes retained table")



def main():
    test_unknown_commits_first()
    test_feasible_commits_first()
    test_infeasible_and_no_table_commit_first()
    test_batch_commits_first_then_solve()
    print("batch/solve race checks, failures:", FAILS)
    return FAILS


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
