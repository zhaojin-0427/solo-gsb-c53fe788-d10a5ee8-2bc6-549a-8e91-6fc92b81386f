#!/usr/bin/env python3
"""Two-process shared-SQLite verification over real HTTP.

Assumes TWO server processes are running against the same SQLite database:

    BASE1 (default http://localhost:8000)  -> process 1
    BASE2 (default http://localhost:8001)  -> process 2, same DCC_DB_PATH

Easiest way (see README):

    docker compose --profile multiproc up --build
    python3 scripts/verify_multiprocess.py

The script imports the bundled examples, solves/saves them, creates execution
batches and then exercises the revision protocol *across the two processes*:

* case A: revision 3 committed first, then revision 2 from the other process
  must be rejected as a stale revision whose ``current_revision`` is 3;
* case B: revision 2 committed first, revision 3 afterwards still updates;
* case C: revisions 2 and 3 fired truly simultaneously from both processes,
  repeated several times - the stored row must always end at revision 3;
* case D: two simultaneous first reports (revision 1, same value) - exactly
  one insert and one idempotent retry.
* case E (batch creation vs. /solve interleaving): a batch request races a
  /solve against the SAME scenario from the OTHER process.  The batch route
  arbitrates on the AUTHORITATIVE scenario row INSIDE its single
  ``BEGIN IMMEDIATE`` write transaction, never on a read taken before it.
  Unlike an unconstrained coin-flip race, every required commit order below is
  either FORCED (the racing solve provably has not committed when the batch
  request is answered) or CONFIRMED (the batch request is only sent once the
  solve's committed state is observable):

    E-unk-first : the ``unknown`` marker COMMITS FIRST.  The solve is started
      first and the unflagged batch is sent only AFTER the marker is visible
      from BOTH processes -> 409 ``latest_solve_unverified`` and NO batch.
      The explicit old-table path is then checked: ``use_stored_plan=true``
      with the retained OLD table's original thresholds creates (201, frozen
      old table); confirming the NEW attempted thresholds is 409
      ``stored_plan_threshold_mismatch``;
    E-batch-first: the batch COMMITS FIRST while the racing solve is still
      computing.  The solve is deliberately long (it provably runs the whole
      time limit and returns ``unknown``); the batch request, fired truly
      concurrently from the other process, is answered hundreds of ms BEFORE
      the solve even reaches its write transaction, so the returned 201
      logically proves the batch committed first - it must freeze the OLD
      snapshot (old R/H, old offsets).  After the solve eventually commits,
      a FOLLOW-UP unflagged request is 409 and leaves no extra batch;
    E-new-first: a NEW VERIFIED feasible solve COMMITS FIRST with DIFFERENT
      safety thresholds AND DIFFERENT offsets than the retained old table.
      The batch is sent only once the new table is visible from both
      processes -> 201 freezing the NEW R/H and the NEW offsets, never the
      stale old table; it is byte-identical when read back from either
      process;
    E-new-race: the same new-feasible solve races a batch while it is still
      computing; the batch wins (its write transaction is acquired while the
      solve is still inside CP-SAT), so it keeps the old snapshot - and a
      second batch sent after the new table committed freezes the new one.
      The two frozen batches are therefore DIFFERENT in exactly the order
      their commits observed the state, which is what catches a stale-freeze.

  The two feasible plans come from ``examples/dense_corridor.json`` (20 UAVs
  crossing one corridor): at (R,H)=(1,1) CP-SAT proves a unique optimal plan
  P0 within milliseconds; at (8,4), with a generous limit, it finds a
  DIFFERENT feasible plan P1 (different thresholds AND different offsets).
  If a recomputation does not return the expected status/plan, or a required
  commit order cannot be confirmed, this script FAILS that case explicitly -
  it never claims coverage from random extra rounds.
* review nodes: every accepted write above gets exactly one node (idempotent
  and rejected writes get none), visible in the same commit order from both
  processes; the node's before/after snapshots replay to the stored state and
  a review reloaded from either process returns identical first-conflict
  deltas; an unknown node returns 404.

After every case it reloads the batch (and its warning) from BOTH processes
and checks they show the highest revision's actual offset. Exits non-zero on
the first failed expectation.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE1 = os.environ.get("BASE1", "http://localhost:8000")
BASE2 = os.environ.get("BASE2", "http://localhost:8001")
ROOT = Path(__file__).resolve().parent.parent

FAILURES = 0


def check(cond, msg):
    global FAILURES
    print(("  ok  " if cond else " FAIL ") + msg)
    if not cond:
        FAILURES += 1


class ExpectationFailed(Exception):
    """A case-E expectation was not met (or an order could not be confirmed).

    Raised instead of a bare ``assert`` so the failure is reported as an
    explicit FAIL line with the reason - never as a traceback - and the other
    independent sub-cases still run.
    """


def call(method, base, path, payload=None, timeout=90.0):
    """Return (http_status, parsed_json)."""
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def wait_healthy(base):
    for _ in range(60):
        try:
            status, _ = call("GET", base, "/api/health", timeout=5)
            if status == 200:
                return
        except OSError:
            pass
        time.sleep(1)
    raise SystemExit(f"server not healthy: {base}")


def setup_scenario_and_plan():
    payload = json.loads((ROOT / "examples" / "crossroads.json").read_text())
    status, body = call("POST", BASE1, "/api/import", payload)
    check(status == 200, f"import scenario via {BASE1} (HTTP {status})")
    sid = body["scenario_id"]

    status, body = call("POST", BASE1, f"/api/scenarios/{sid}/solve",
                        {"time_limit": 20})
    check(status == 200 and body["solution"]["feasible"],
          f"solve a feasible plan (HTTP {status})")
    call("POST", BASE1, f"/api/scenarios/{sid}/save",
         {"name": "multiproc-check"})
    return sid


def new_batch(sid, name):
    status, body = call("POST", BASE1, f"/api/scenarios/{sid}/batches",
                        {"name": name})
    check(status == 201, f"create batch {name!r} (HTTP {status})")
    return body["batch_id"]


def report(base, bid, sortie, offset, revision):
    return call("PUT", base, f"/api/batches/{bid}/reports",
                {"sortie_id": sortie, "actual_offset": offset,
                 "revision": revision})


def stored_report(base, bid, sortie):
    status, body = call("GET", base, f"/api/batches/{bid}")
    assert status == 200
    rows = {r["sortie_id"]: r for r in body["reports"]}
    return rows.get(sortie)


def warning_row(base, bid, t):
    status, body = call("GET", base, f"/api/batches/{bid}/warning?t={t}")
    assert status == 200, (status, body)
    return {r["id"]: r for r in body["warning"]["sorties"]}["UAV-A"]


def report_nodes(base, bid):
    status, body = call("GET", base, f"/api/batches/{bid}/report-nodes")
    assert status == 200, (status, body)
    return body["nodes"]


def review(base, bid, node_id, t):
    return call("GET", base,
                f"/api/batches/{bid}/report-nodes/{node_id}/review?t={t}")


def race(bid, offers):
    """offers: list of (base, offset, revision); release at the same instant."""
    start = threading.Barrier(len(offers))

    def one(base, off, rev):
        start.wait()
        return report(base, bid, "UAV-A", off, rev)

    with ThreadPoolExecutor(max_workers=len(offers)) as pool:
        return list(pool.map(lambda a: one(*a), offers))


# ---------------------------------------------------------------------------
# Case E helpers: deterministic batch/solve interleavings over real HTTP.
# ---------------------------------------------------------------------------

# The dense-corridor fixture is deliberately tuned so that:
#   * (R,H)=(1,1)   -> a unique OPTIMAL plan P0 found in a few ms;
#   * (R,H)=(60,1)  with a short limit -> the feasibility probe runs the WHOLE
#                      limit and proves nothing (status "unknown"), so the
#                      request occupies ~UNKNOWN_LIMIT seconds BEFORE opening
#                      its write transaction;
#   * (R,H)=(8,4)   with a generous limit -> a DIFFERENT feasible plan P1
#                      (different thresholds and different offsets from P0),
#                      and the search also keeps the request busy for seconds
#                      before it commits (so a concurrent batch provably wins
#                      the write lock).
# The numbers were chosen with a large hardware margin; the script treats a
# mismatch as a hard failure instead of "covering it with another round".
DENSE = json.loads((ROOT / "examples" / "dense_corridor.json").read_text())
P0_R, P0_H = 1.0, 1.0
NEW_R, NEW_H = 8.0, 4.0
HARD_R, HARD_H = 60.0, 1.0
UNKNOWN_LIMIT = 1.2          # seconds the unknown solve stays inside CP-SAT
FEASIBLE_LIMIT = 3.0         # seconds the new-feasible solve keeps searching
UNKNOWN_MIN_ELAPSED = 0.9    # proof that the solve really ran the long probe
BATCH_MAX_ELAPSED = 0.3      # a batch answered this fast cannot have queued
# behind a solve still holding the write lock (the solve's write txn starts
# only AFTER its CP-SAT search returns).


def import_dense(base):
    status, body = call("POST", base, "/api/import", DENSE)
    if status != 200:
        raise ExpectationFailed(f"import dense_corridor via {base} failed: "
                                f"HTTP {status} {body}")
    return body["scenario_id"]


def solve_p0(sid, base=BASE1):
    """Seed the verified-feasible OLD table P0 (1/1, unique optimum)."""
    status, body = call("POST", base, f"/api/scenarios/{sid}/solve",
                        {"time_limit": 5, "safety_radius": P0_R,
                         "safety_height": P0_H})
    if not (status == 200 and body["solution"]["feasible"]):
        raise ExpectationFailed(
            f"OLD-table recompute at {P0_R}/{P0_H} did not return a feasible "
            f"plan: HTTP {status} {body.get('solution')}")
    sol = body["solution"]
    if sol["status"] != "optimal":
        raise ExpectationFailed(
            f"OLD-table plan expected status optimal, got {sol['status']}; "
            f"cannot certify its thresholds/offsets as the OLD snapshot")
    return sol["offsets"], (float(body["safety_radius"]),
                            float(body["safety_height"]))


def scenario_view(base, sid):
    status, body = call("GET", base, f"/api/scenarios/{sid}")
    if status != 200:
        raise ExpectationFailed(f"GET scenario {sid} via {base} failed: "
                                f"HTTP {status} {body}")
    return body


def wait_state(sid, predicate, what, bases=(BASE1, BASE2),
               timeout=30.0, interval=0.02):
    """Poll until ``predicate(view)`` is true from EVERY base.

    Polling the cheap list endpoint is avoided on purpose: GET
    /api/scenarios/{id} rebuilds the geometry, which still costs only a few
    milliseconds for the fixture and gives the exact committed columns.
    """
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        views = {b: scenario_view(b, sid) for b in bases}
        if all(predicate(v) for v in views.values()):
            return views
        last = views
        time.sleep(interval)
    summary = [(v.get("safety_radius"), v.get("safety_height"),
                (v.get("solution") or {}).get("status"))
               for v in last.values()]
    raise ExpectationFailed(
        f"state {what!r} not visible on all processes {list(bases)} within "
        f"{timeout}s (target commit order did not occur); "
        f"last (R,H,status) per process={summary}")


def marker_visible(v):
    return v.get("unverified_solve") is not None


def feasible_plan_visible(v, radius, height):
    sol = v.get("solution")
    return (v.get("unverified_solve") is None and sol is not None
            and sol.get("feasible") and float(v["safety_radius"]) == radius
            and float(v["safety_height"]) == height)


def batch_ids(sid, base=BASE1):
    status, body = call("GET", base,
                        f"/api/batches?scenario_id={sid}")
    if status != 200:
        raise ExpectationFailed(f"list batches via {base} failed: "
                                f"HTTP {status} {body}")
    return {b["id"]: b["name"] for b in body["batches"]}


def freezes_plan(bb, radius, height, offsets):
    return (float(bb["safety_radius"]) == radius
            and float(bb["safety_height"]) == height
            and {k: int(v) for k, v in bb["planned_offsets"].items()}
            == {k: int(v) for k, v in offsets.items()})


def main():
    wait_healthy(BASE1)
    wait_healthy(BASE2)
    print(f"both processes healthy: {BASE1} and {BASE2}")
    sid = setup_scenario_and_plan()
    # planned offset of UAV-A in the unique optimal plan is 4
    planned_a = 4

    # --- case A: rev 3 first, rev 2 second -> stale conflict ---------------
    bid_a = new_batch(sid, "caseA-3-then-2")
    check(report(BASE1, bid_a, "UAV-A", 7, 1)[0] == 200,
          "A: revision 1 accepted")
    check(report(BASE1, bid_a, "UAV-A", 9, 3)[0] == 200,
          "A: revision 3 accepted on process 1")
    status, body = report(BASE2, bid_a, "UAV-A", 8, 2)
    check(status == 409 and body["detail"]["reason"] == "stale_revision"
          and body["detail"]["current_revision"] == 3,
          f"A: revision 2 on process 2 -> 409 stale, current=3, got {status} {body}")

    # --- case B: rev 2 first, rev 3 second -> 3 still updates --------------
    bid_b = new_batch(sid, "caseB-2-then-3")
    check(report(BASE1, bid_b, "UAV-A", 7, 1)[0] == 200,
          "B: revision 1 accepted")
    check(report(BASE2, bid_b, "UAV-A", 8, 2)[0] == 200,
          "B: revision 2 accepted on process 2")
    status, body = report(BASE1, bid_b, "UAV-A", 9, 3)
    check(status == 200 and body["report"]["revision"] == 3,
          f"B: revision 3 on process 1 still updates (HTTP {status})")

    # Both orders must leave rev 3 / offset 9, visible from BOTH processes,
    # and the warning must use that actual offset (deviation = 9 - 4).
    def assert_final(case_name, case_bid):
        for base in (BASE1, BASE2):
            rep = stored_report(base, case_bid, "UAV-A")
            check(rep is not None and rep["revision"] == 3
                  and rep["actual_offset"] == 9,
                  f"{case_name}: {base} batch detail shows rev 3 / offset 9 -> {rep}")
        row = warning_row(BASE1, case_bid, 9)
        check(row["actual_offset"] == 9 and row["revision"] == 3
              and row["deviation_seconds"] == 9 - planned_a,
              f"{case_name}: warning uses rev 3 actual offset 9 -> {row}")

    assert_final("A", bid_a)
    assert_final("B", bid_b)

    # --- case C: simultaneous rev 2 vs rev 3, repeated ---------------------
    for trial in range(5):
        cb = new_batch(sid, f"caseC-race-{trial}")
        check(report(BASE1, cb, "UAV-A", 7, 1)[0] == 200,
              f"C{trial}: seed revision 1")
        results = race(cb, [(BASE1, 9, 3), (BASE2, 8, 2)])

        def is_for(rev, status, body):
            if status == 200:
                return body.get("report", {}).get("revision") == rev
            return body.get("detail", {}).get("received_revision") == rev

        r3 = [r for r in results if is_for(3, *r)]
        r2 = [r for r in results if is_for(2, *r)]
        check(len(r3) == 1 and len(r2) == 1 and r3[0][0] == 200,
              f"C{trial}: exactly one rev3 reply and it is accepted, got {results}")
        # rev2 either lost to a committed 3 (409 current=3) or serialized first
        # and was then overwritten by 3 - it must never be the final value.
        s2, b2 = r2[0]
        check(s2 == 200 or (b2["detail"]["reason"] == "stale_revision"
                            and b2["detail"]["current_revision"] == 3),
              f"C{trial}: rev2 allowed outcomes only, got {s2} {b2}")
        rep = stored_report(BASE1, cb, "UAV-A")
        check(rep["revision"] == 3 and rep["actual_offset"] == 9,
              f"C{trial}: final row always rev 3 / offset 9 -> {rep}")

    # --- case D: simultaneous first reports, same value --------------------
    db = new_batch(sid, "caseD-first-same")
    results = race(db, [(BASE1, 7, 1), (BASE2, 7, 1)])
    idem = sorted((s, b.get("idempotent")) for s, b in results)
    check(all(s == 200 for s, _ in idem)
          and {v for _, v in idem} == {False, True},
          f"D: concurrent identical first reports = 1 insert + 1 idempotent, "
          f"got {results}")
    rep = stored_report(BASE2, db, "UAV-A")
    check(rep is not None and rep["revision"] == 1 and rep["actual_offset"] == 7,
          f"D: single rev 1 / offset 7 row visible on process 2 -> {rep}")

    # --- case E: batch creation interleaving with a racing /solve ----------
    # Arbitration runs INSIDE the batch write transaction.  Each required
    # order is forced/confirmed, so a re-run does the SAME thing every time;
    # a recompute that comes back different or an order that cannot be proven
    # is reported as an explicit FAIL below - never as a traceback and never
    # papered over with extra random rounds.
    print("case E: deterministic batch/solve interleavings "
          "(examples/dense_corridor.json)")

    def expect(cond, msg):
        if not cond:
            raise ExpectationFailed(msg)

    def assert_old_snapshot(label, bb, p0_offsets):
        expect(freezes_plan(bb, P0_R, P0_H, p0_offsets),
               f"{label}: batch must keep the OLD table P0 at {P0_R}/{P0_H}, "
               f"got R/H={bb['safety_radius']}/{bb['safety_height']} "
               f"offsets={bb['planned_offsets']}")

    def fire_pair(fn_a, fn_b):
        """Release two HTTP calls at the same instant; return
        ((resp_a, elapsed_a), (resp_b, elapsed_b))."""
        barrier = threading.Barrier(2)

        def fire(fn):
            barrier.wait()
            t0 = time.perf_counter()
            out = fn()
            return out, time.perf_counter() - t0

        with ThreadPoolExecutor(max_workers=2) as pool:
            fa = pool.submit(fire, fn_a)
            fb = pool.submit(fire, fn_b)
            return fa.result(), fb.result()

    def run_subcase(name, fn):
        """Run one independent case-E sub-case; report FAIL instead of abort."""
        global FAILURES
        try:
            evidence = fn()
        except ExpectationFailed as exc:
            FAILURES += 1
            print(f" FAIL {name}: {exc}")
        except Exception as exc:  # never mask a broken run as a pass
            FAILURES += 1
            print(f" FAIL {name}: unexpected error {type(exc).__name__}: {exc}")
        else:
            tail = f" ({evidence})" if evidence else ""
            print(f"  ok  {name}: target commit order confirmed{tail}")

    def make_batch(base, sid, payload):
        return lambda: call("POST", base,
                            f"/api/scenarios/{sid}/batches", payload)

    def solve_unknown(base, sid):
        return lambda: call("POST", base, f"/api/scenarios/{sid}/solve",
                            {"time_limit": UNKNOWN_LIMIT,
                             "safety_radius": HARD_R, "safety_height": HARD_H})

    def solve_new_feasible(base, sid):
        return lambda: call("POST", base, f"/api/scenarios/{sid}/solve",
                            {"time_limit": FEASIBLE_LIMIT,
                             "safety_radius": NEW_R, "safety_height": NEW_H})

    # -------- E-unk-first: the unknown marker COMMITS FIRST -----------------
    def case_unknown_first():
        esid = import_dense(BASE1)
        p0, p0_rh = solve_p0(esid)
        expect(p0_rh == (P0_R, P0_H),
               f"OLD table stored at {P0_R}/{P0_H}, got {p0_rh}")
        before = batch_ids(esid)
        # Run the unknown solve to completion and WAIT until its marker is
        # committed and observable from BOTH processes before asking the other
        # process to create the batch.  The batch transaction therefore
        # necessarily starts AFTER the marker committed - the solve-first
        # order is CONFIRMED, not gambled on.
        us_status, us_body = solve_unknown(BASE1, esid)()
        expect(us_status == 200
               and us_body["solution"]["status"] == "unknown"
               and not us_body["solution"]["feasible"]
               and us_body["solution"].get("elapsed_seconds", 0)
               >= UNKNOWN_MIN_ELAPSED,
               f"racing solve must really return unknown after a long probe "
               f"-> {us_status} {us_body.get('solution')}")
        wait_state(esid, marker_visible, "unknown marker")
        # No-flag batch AFTER the committed marker: 409 and no new batch.
        st, bb = make_batch(BASE2, esid,
                            {"name": "E-unk-first-unflagged"})()
        expect(st == 409
               and bb["detail"]["reason"] == "latest_solve_unverified"
               and bb["detail"]["stored_plan_original_safety_radius"] == P0_R
               and bb["detail"]["stored_plan_original_safety_height"] == P0_H,
               f"unflagged batch after committed marker must be 409 "
               f"latest_solve_unverified, got {st} {bb}")
        expect(batch_ids(esid) == before,
               f"the 409 must leave NO batch behind, got "
               f"{batch_ids(esid)} vs {before}")
        # The one admissible marker-era path: explicit selection + OLD R/H.
        st, bb = make_batch(BASE1, esid, {
            "name": "E-unk-first-explicit", "use_stored_plan": True,
            "confirmed_safety_radius": P0_R,
            "confirmed_safety_height": P0_H})()
        expect(st == 201,
               f"explicit selection + OLD thresholds must create -> {st} {bb}")
        assert_old_snapshot("E-unk-first", bb, p0)
        st2, b2 = call("GET", BASE2, f"/api/batches/{bb['batch_id']}")
        expect(st2 == 200 and b2 == {**bb},
               "explicit old-table batch must be byte-identical from process 2")
        # Confirming the NEW attempted thresholds must still be refused.
        st, bb = make_batch(BASE2, esid, {
            "name": "E-unk-first-wrong", "use_stored_plan": True,
            "confirmed_safety_radius": HARD_R,
            "confirmed_safety_height": HARD_H})()
        expect(st == 409
               and bb["detail"]["reason"] == "stored_plan_threshold_mismatch",
               f"confirming the NEW thresholds must be 409 mismatch -> {st} {bb}")
        expect(batch_ids(esid, BASE2) == batch_ids(esid, BASE1),
               "batch set must be identical on both processes")
        return ("unknown marker visible on both processes before the batch; "
                "unflagged -> 409, no batch; explicit+1/1 -> 201 old table")

    # -------- E-batch-first: the batch COMMITS FIRST -----------------------
    def case_batch_first():
        esid = import_dense(BASE1)
        p0, _ = solve_p0(esid)
        before = batch_ids(esid)
        # The racing solve provably spends UNKNOWN_LIMIT seconds INSIDE
        # CP-SAT before it opens its write transaction.  Fire both requests at
        # the same instant; the batch (a fast write transaction) is answered
        # while the solve is still searching, so it necessarily acquires the
        # write lock and commits BEFORE the solve even tries to.
        (solve_resp, solve_elapsed), (batch_resp, batch_elapsed) = fire_pair(
            solve_unknown(BASE1, esid),
            make_batch(BASE2, esid, {"name": "E-batch-first"}))
        sstat, sbody = solve_resp
        expect(sstat == 200 and sbody["solution"]["status"] == "unknown"
               and not sbody["solution"]["feasible"]
               and sbody["solution"].get("elapsed_seconds", 0)
               >= UNKNOWN_MIN_ELAPSED,
               f"racing solve must be the long unknown probe -> {solve_resp}")
        expect(solve_elapsed >= UNKNOWN_MIN_ELAPSED
               and batch_elapsed <= BATCH_MAX_ELAPSED,
               f"batch must be answered while the solve still computes "
               f"(solve {solve_elapsed:.2f}s vs batch {batch_elapsed:.2f}s)")
        bstat, bbody = batch_resp
        expect(bstat == 201
               and solve_elapsed - batch_elapsed >= UNKNOWN_MIN_ELAPSED,
               f"batch 201 must be provably before the solve commit "
               f"(HTTP {bstat}, gap {solve_elapsed - batch_elapsed:.2f}s) "
               f"{bbody}")
        assert_old_snapshot("E-batch-first", bbody, p0)
        # Once the marker lands, a FOLLOW-UP unflagged batch is refused; the
        # race-winning batch is the only one that may exist.
        wait_state(esid, marker_visible, "unknown marker")
        st, fb = make_batch(BASE1, esid,
                            {"name": "E-batch-first-after"})()
        expect(st == 409
               and fb["detail"]["reason"] == "latest_solve_unverified",
               f"follow-up after the marker must be 409, got {st} {fb}")
        after = batch_ids(esid)
        expect(set(after) - set(before) == {bbody["batch_id"]},
               f"only the one race-winning batch may exist "
               f"(before={before} after={after})")
        st2, b2 = call("GET", BASE2, f"/api/batches/{bbody['batch_id']}")
        expect(st2 == 200 and freezes_plan(b2, P0_R, P0_H, p0),
               "winning batch must still freeze P0 when read from process 2")
        return (f"batch answered {solve_elapsed - batch_elapsed:.2f}s before "
                f"the solve commit (solve {solve_elapsed:.2f}s, batch "
                f"{batch_elapsed:.3f}s); 201 freezes old P0 1/1, follow-up 409")

    # -------- E-new-first: a NEW verified feasible plan COMMITS FIRST -------
    def case_new_first():
        esid = import_dense(BASE1)
        p0, _ = solve_p0(esid)
        fstat, fbody = solve_new_feasible(BASE1, esid)()
        expect(fstat == 200 and fbody["solution"]["feasible"],
               f"recompute at {NEW_R}/{NEW_H} must be feasible -> "
               f"{fstat} {fbody.get('solution')}")
        p1 = fbody["solution"]["offsets"]
        expect({k: int(v) for k, v in p1.items()}
               != {k: int(v) for k, v in p0.items()},
               "the NEW feasible plan must have offsets DIFFERENT from the OLD "
               "table, otherwise freezing the stale table could not be caught "
               f"(both {p1})")
        expect((NEW_R, NEW_H) != (P0_R, P0_H),
               "the NEW plan must use DIFFERENT safety thresholds")
        # Confirm the NEW table is committed and observable on BOTH processes
        # BEFORE sending the batch - this pins the solve-first order.
        views = wait_state(
            esid, lambda v: feasible_plan_visible(v, NEW_R, NEW_H),
            f"new feasible plan at {NEW_R}/{NEW_H}")
        expect(views[BASE2]["solution"]["offsets"] == p1,
               "process 2 must read the committed NEW offsets before the "
               "batch request")
        st, nb = make_batch(BASE2, esid, {"name": "E-new-first"})()
        expect(st == 201, f"batch must be created (HTTP {st}) {nb}")
        expect(freezes_plan(nb, NEW_R, NEW_H, p1),
               f"batch must freeze the NEW table {NEW_R}/{NEW_H} and the NEW "
               f"offsets, never the stale old table -> R/H="
               f"{nb['safety_radius']}/{nb['safety_height']} "
               f"offsets={nb['planned_offsets']}")
        g1 = call("GET", BASE1, f"/api/batches/{nb['batch_id']}")[1]
        g2 = call("GET", BASE2, f"/api/batches/{nb['batch_id']}")[1]
        expect(g1 == g2 == {**nb},
               "frozen new batch must be byte-identical from both processes")
        return (f"new feasible table {NEW_R}/{NEW_H} visible on both processes "
                f"before the batch; 201 freezes the NEW offsets (P1 != P0)")

    # ---- E-new-race: NEW feasible solve still computing; BATCH wins -------
    def case_new_race():
        esid = import_dense(BASE1)
        p0, _ = solve_p0(esid)
        # The new-feasible search keeps the request busy for FEASIBLE_LIMIT
        # seconds; the concurrent batch acquires the write lock first and
        # keeps P0.  A SECOND batch after the new table commits must freeze
        # P1/NEW R/H, so the two batches MUST differ in commit order.
        (solve_resp2, solve_elapsed2), (batch_resp2, batch_elapsed2) = \
            fire_pair(solve_new_feasible(BASE1, esid),
                      make_batch(BASE2, esid, {"name": "E-new-race-old"}))
        sstat2, sbody2 = solve_resp2
        expect(sstat2 == 200 and sbody2["solution"]["feasible"],
               f"racing recompute must return a feasible plan -> {solve_resp2}")
        p1 = sbody2["solution"]["offsets"]
        expect({k: int(v) for k, v in p1.items()}
               != {k: int(v) for k, v in p0.items()},
               "the racing feasible plan must differ from the old table")
        bstat2, bbody2 = batch_resp2
        expect(bstat2 == 201 and solve_elapsed2 - batch_elapsed2 >= 0.9
               and batch_elapsed2 <= BATCH_MAX_ELAPSED,
               f"batch must be answered before the long solve commits "
               f"(solve {solve_elapsed2:.2f}s vs batch {batch_elapsed2:.2f}s) "
               f"-> {bstat2} {bbody2}")
        assert_old_snapshot("E-new-race", bbody2, p0)
        wait_state(esid, lambda v: feasible_plan_visible(v, NEW_R, NEW_H),
                   "new feasible plan")
        st, nb2 = make_batch(BASE1, esid,
                             {"name": "E-new-race-new"})()
        expect(st == 201 and freezes_plan(nb2, NEW_R, NEW_H, p1),
               f"the batch AFTER the commit must freeze the NEW table -> "
               f"HTTP {st}, frozen "
               f"{None if st != 201 else (nb2['safety_radius'], nb2['safety_height'])}")
        expect(not freezes_plan(bbody2, NEW_R, NEW_H, p1)
               and freezes_plan(nb2, NEW_R, NEW_H, p1),
               "the race-won batch (OLD) and post-commit batch (NEW) must "
               "freeze DIFFERENT plans in their commit order")
        expect(call("GET", BASE2,
                    f"/api/batches/{bbody2['batch_id']}")[1] == {**bbody2},
               "the OLD race-won batch must stay unchanged on process 2")
        return (f"racing batch answered {solve_elapsed2 - batch_elapsed2:.2f}s "
                f"before the solve commits and freezes old P0; post-commit "
                f"batch freezes new P1 {NEW_R}/{NEW_H}; the two differ")

    for _name, _fn in (("E-unknown-first", case_unknown_first),
                       ("E-batch-first", case_batch_first),
                       ("E-new-first", case_new_first),
                       ("E-new-race", case_new_race)):
        run_subcase(_name, _fn)

    # --- review nodes across the two processes -----------------------------
    # Case A had accepted writes rev1 then rev3 (rev2 was rejected): the
    # baseline + exactly two trigger nodes, no node for the stale rev2.
    for base in (BASE1, BASE2):
        nodes = report_nodes(base, bid_a)
        triggers = [(n["trigger_sortie_id"], n["trigger_revision"],
                     n["trigger_actual_offset"]) for n in nodes if not n["is_baseline"]]
        check(nodes[0]["is_baseline"] and nodes[0]["seq"] == 1,
              f"A: node chain starts with baseline (via {base}) -> {nodes}")
        check(triggers == [("UAV-A", 1, 7), ("UAV-A", 3, 9)],
              f"A: one node per accepted write in commit order (via {base}) "
              f"-> {triggers}")

    # Case B's chain (rev1, rev2, rev3): reviewing the rev3 node at t=9 must
    # succeed from BOTH processes with identical before/after first-conflict
    # times and an idempotent reload.
    nodes_b = report_nodes(BASE1, bid_b)
    rev3_node = [n for n in nodes_b
                 if n["trigger_sortie_id"] == "UAV-A"
                 and n["trigger_revision"] == 3][0]
    reviews = {}
    for base in (BASE1, BASE2):
        status, body = review(base, bid_b, rev3_node["node_id"], 9)
        check(status == 200, f"review rev3 node via {base} (HTTP {status})")
        r = body["review"]
        check(r["node"]["sortie_id"] == "UAV-A" and r["node"]["revision"] == 3
              and r["node"]["actual_offset"] == 9,
              f"review names the triggering write (via {base})")
        check(r["warning_before"]["sorties"] and r["warning_after"]["sorties"],
              f"review restores full before/after states (via {base})")
        reviews[base] = (r["first_conflict_time_before"],
                         r["first_conflict_time_after"],
                         [(p["sortie_a"], p["sortie_b"],
                           tuple((x["change"], x["time_start"], x["time_end"])
                                 for x in p["added"] + p["removed"]))
                          for p in r["pair_changes"]])
    check(reviews[BASE1] == reviews[BASE2],
          f"review identical across processes / reloads -> {reviews}")

    # Re-review returns byte-identical content on repeat (determinism).
    s1, b1 = review(BASE1, bid_b, rev3_node["node_id"], 9)
    s2, b2 = review(BASE1, bid_b, rev3_node["node_id"], 9)
    check(s1 == s2 == 200 and b1 == b2, "repeated review is deterministic")

    # The raced case C: however rev2/rev3 committed, nodes exist only for the
    # writes that actually inserted, strictly ordered, final snapshot rev3.
    for trial in range(5):
        cb_id = new_batch(sid, f"caseC-node-{trial}")
        report(BASE1, cb_id, "UAV-A", 7, 1)
        race(cb_id, [(BASE1, 9, 3), (BASE2, 8, 2)])
        nodes = report_nodes(BASE1, cb_id)
        revs = [n["trigger_revision"] for n in nodes if not n["is_baseline"]]
        check(revs[0] == 1 and revs[-1] == 3
              and (revs == [1, 3] or revs == [1, 2, 3]),
              f"C{trial}: nodes follow actual commit order -> {revs}")
        if revs == [1, 3]:
            # rev2 lost the race: verify it really created no node (same as
            # its 409 stale response).
            check(any(n["trigger_revision"] == 2 for n in nodes) is False,
                  f"C{trial}: rejected stale rev2 left no node")
        # Every committed node replays from process 1 with both states.
        for n in nodes[1:]:
            status, body = review(BASE1, cb_id, n["node_id"], 9)
            check(status == 200
                  and "warning_before" in body.get("review", {})
                  and "warning_after" in body.get("review", {}),
                  f"C{trial}: node rev{n['trigger_revision']} review replays "
                  f"both states (HTTP {status})")
        rep = stored_report(BASE2, cb_id, "UAV-A")
        check(rep["revision"] == 3 and rep["actual_offset"] == 9,
              f"C{trial}: stored row still highest revision")

    # Unknown node -> 404; unknown batch -> 404.
    status, _ = review(BASE1, bid_b, 999999, 9)
    check(status == 404, f"unknown review node -> 404 (got {status})")
    status, _ = call("GET", BASE1, "/api/batches/999999/report-nodes")
    check(status == 404, f"unknown batch node list -> 404 (got {status})")

    print()
    if FAILURES:
        print(f"RESULT: {FAILURES} check(s) FAILED")
        sys.exit(1)
    print("RESULT: all two-process shared-DB checks passed")


if __name__ == "__main__":
    main()
