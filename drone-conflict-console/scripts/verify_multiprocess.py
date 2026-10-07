#!/usr/bin/env python3
"""Two-process shared-SQLite verification over real HTTP.

Assumes TWO server processes are running against the same SQLite database:

    BASE1 (default http://localhost:8000)  -> process 1
    BASE2 (default http://localhost:8001)  -> process 2, same DCC_DB_PATH

Easiest way (see README):

    docker compose --profile multiproc up --build
    python3 scripts/verify_multiprocess.py

or fully inside Docker Compose (the compose exit code is the script's):

    docker compose --profile verify up --build \
        --abort-on-container-exit --exit-code-from verify

The script imports the bundled example, solves/saves it, creates execution
batches and then exercises the revision protocol *across the two processes*:

* case A: revision 3 committed first, then revision 2 from the other process
  must be rejected as a stale revision whose ``current_revision`` is 3;
* case B: revision 2 committed first, revision 3 afterwards still updates;
* case C: revisions 2 and 3 fired truly simultaneously from both processes,
  repeated several times - the stored row must always end at revision 3;
* case D: two simultaneous first reports (revision 1, same value) - exactly
  one insert and one idempotent retry.
* case E (batch creation vs. /solve commit order): the batch route must
  arbitrate on the AUTHORITATIVE scenario row inside its write transaction,
  never on a pre-transaction read.  The script decides the commit order
  instead of leaving it to chance:

  - FORCED ORDER (commit barrier).  Every mutating route commits its SQLite
    write transaction BEFORE its HTTP response is sent, so a received
    response is a commit barrier: whatever is fired afterwards, from either
    process, arbitrates on committed state.  Awaiting the first request's
    response before issuing the second therefore FORCES a commit order -
    no randomness, no retry rounds:
      * unknown committed first  -> an unflagged batch request MUST be
        refused 409 ``latest_solve_unverified`` and leave no batch; only
        ``use_stored_plan=true`` with the OLD table's ORIGINAL thresholds
        (30/10) may still create (201, freezing the retained old table);
      * batch committed first    -> it keeps the old snapshot (30/10, old
        offsets) whatever the later solve commits; the committed unknown
        marker then governs only LATER requests (409, no new batch);
      * new feasible committed first -> the batch MUST freeze the NEW
        thresholds and offsets.  The new solve runs at 25/8 with a lock so
        its plan differs from the old table in BOTH thresholds AND offsets
        (freezing the stale old table cannot masquerade as correct);
      * proven infeasible committed first -> 400, no batch.
    If a solve does not produce the required outcome (unknown / feasible /
    infeasible) or a response violates the forced order's expectation, the
    script FAILS explicitly.

  - CONFIRMED ORDER (simultaneous race).  Both requests fired at the same
    instant; the commit order is then CONFIRMED from the responses (a 409
    proves the marker committed before the batch's in-transaction read, a
    201 proves the batch committed first; in the feasible race the frozen
    thresholds+offsets say which table was authoritative).  Each round is
    checked against the expectation of the order it confirms.  These rounds
    only exercise the write-lock contention path - coverage of the three
    orders comes from the forced cases above, never from repeating races.

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


def call(method, base, path, payload=None):
    """Return (http_status, parsed_json)."""
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def wait_healthy(base):
    for _ in range(60):
        try:
            status, _ = call("GET", base, "/api/health")
            if status == 200:
                return
        except OSError:
            pass
        import time
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

    # --- case E: batch creation vs. /solve commit-order arbitration --------
    # The batch route must arbitrate on the AUTHORITATIVE scenario row inside
    # its own write transaction.  Commit order is DECIDED here, not sampled:
    # a received HTTP response is a commit barrier (the route commits its
    # SQLite transaction before responding), so "await response, then fire
    # the next request from the other process" forces the order.  The two
    # feasible tables are distinguishable in BOTH thresholds and offsets, so
    # freezing the wrong one cannot pass.
    CROSSROADS = json.loads((ROOT / "examples" / "crossroads.json").read_text())
    # OLD table: crossroads solved at its own thresholds 30/10.
    OLD_R, OLD_H = 30.0, 10.0
    OLD_PLAN = {"UAV-A": 4, "UAV-B": 0, "UAV-C": 2, "UAV-D": 6}
    # NEW verified table: different thresholds AND (lock-forced) different
    # offsets - 25/8 with UAV-B locked at 5 solves to a plan that differs
    # from OLD_PLAN in every coordinate.
    NEW_R, NEW_H = 25.0, 8.0
    UNKNOWN_SOLVE = {"time_limit": 0.0001, "safety_radius": 45,
                     "safety_height": 12,
                     "locks": [{"sortie_id": "UAV-B", "offset": 5}]}
    NEW_FEASIBLE_SOLVE = {"time_limit": 20, "safety_radius": NEW_R,
                          "safety_height": NEW_H,
                          "locks": [{"sortie_id": "UAV-B", "offset": 5}]}
    INFEASIBLE_SOLVE = {"time_limit": 20, "safety_radius": 100000,
                        "safety_height": 100000}

    def fresh_old_table(tag):
        """Import crossroads and solve the verified OLD table (30/10)."""
        s_status, s_body = call("POST", BASE1, "/api/import", CROSSROADS)
        check(s_status == 200, f"{tag}: import (HTTP {s_status})")
        fsid = s_body["scenario_id"]
        st, body = call("POST", BASE1, f"/api/scenarios/{fsid}/solve",
                        {"time_limit": 20})
        sol = body.get("solution", {})
        check(st == 200 and sol.get("feasible")
              and sol.get("offsets") == OLD_PLAN
              and body.get("safety_radius") == OLD_R
              and body.get("safety_height") == OLD_H,
              f"{tag}: OLD table verified at 30/10 -> HTTP {st} "
              f"{sol.get('status')} {sol.get('offsets')}")
        return fsid

    def batch_ids(bsid):
        st, body = call("GET", BASE1, f"/api/batches?scenario_id={bsid}")
        assert st == 200, (st, body)
        return {b["id"] for b in body["batches"]}

    def solve_unknown(base, usid, tag):
        """Run the timed-out solve; its response IS the commit barrier.

        Fails explicitly if the run does not come back ``unknown`` - the
        follow-up batch arbitration is only meaningful against a committed
        marker.
        """
        st, body = call("POST", base, f"/api/scenarios/{usid}/solve",
                        UNKNOWN_SOLVE)
        sol = body.get("solution", {})
        unv = body.get("unverified_solve") or {}
        check(st == 200 and sol.get("status") == "unknown"
              and unv.get("safety_radius") == 45
              and unv.get("safety_height") == 12,
              f"{tag}: solve returned unknown, new 45/12 attempt persisted "
              f"-> HTTP {st} {sol.get('status')}")
        prior = body.get("prior_solution") or {}
        check(prior.get("offsets") == OLD_PLAN
              and prior.get("safety_radius") == OLD_R
              and prior.get("safety_height") == OLD_H,
              f"{tag}: old table retained with its ORIGINAL 30/10 -> "
              f"{prior.get('offsets')} R={prior.get('safety_radius')}")
        return body

    def solve_new_feasible(base, fsid, tag):
        """Run the NEW verified solve (25/8 + lock); response = commit barrier.

        Fails explicitly if the run is not feasible or its plan is NOT
        distinguishable from the old table - an indistinguishable "new"
        table would make the freeze checks below vacuous.
        """
        st, body = call("POST", base, f"/api/scenarios/{fsid}/solve",
                        NEW_FEASIBLE_SOLVE)
        sol = body.get("solution", {})
        plan = sol.get("offsets")
        check(st == 200 and sol.get("feasible") and isinstance(plan, dict),
              f"{tag}: new solve feasible -> HTTP {st} {sol.get('status')}")
        check(body.get("safety_radius") == NEW_R
              and body.get("safety_height") == NEW_H
              and isinstance(plan, dict) and plan != OLD_PLAN,
              f"{tag}: NEW table distinguishable (25/8, offsets {plan}) - "
              f"otherwise freezing the stale old table could not be detected")
        check("unverified_solve" not in body,
              f"{tag}: verified solve reports no unknown marker")
        return plan

    # E-U1: unknown commits FIRST (forced), then requests from the other
    # process.  The unflagged batch MUST be refused 409 and leave no batch.
    esid = fresh_old_table("E-U1")
    solve_unknown(BASE1, esid, "E-U1")
    check(call("GET", BASE2, f"/api/scenarios/{esid}")[1].get("unverified_solve")
          is not None,
          "E-U1: committed unknown marker visible from process 2")
    st, body = call("POST", BASE2, f"/api/scenarios/{esid}/batches",
                    {"name": "E-U1-unflagged"})
    check(st == 409 and body["detail"]["reason"] == "latest_solve_unverified"
          and body["detail"]["stored_plan_original_safety_radius"] == OLD_R
          and body["detail"]["stored_plan_original_safety_height"] == OLD_H,
          f"E-U1: unknown first -> unflagged batch 409 "
          f"latest_solve_unverified, got {st} {body}")
    check(batch_ids(esid) == set(), "E-U1: the 409 left no batch behind")
    st, body = call("POST", BASE2, f"/api/scenarios/{esid}/batches",
                    {"name": "E-U1-wrong-confirm", "use_stored_plan": True,
                     "confirmed_safety_radius": 45,
                     "confirmed_safety_height": 12})
    check(st == 409
          and body["detail"]["reason"] == "stored_plan_threshold_mismatch",
          f"E-U1: confirming the NEW 45/12 against the old table -> 409 "
          f"mismatch, got {st} {body}")
    check(batch_ids(esid) == set(), "E-U1: mismatch left no batch")
    st, body = call("POST", BASE2, f"/api/scenarios/{esid}/batches",
                    {"name": "E-U1-no-confirm", "use_stored_plan": True})
    check(st == 409 and body["detail"]["reason"]
          == "stored_plan_threshold_confirmation_required",
          f"E-U1: selection without threshold confirmation -> 409, got {st}")
    check(batch_ids(esid) == set(), "E-U1: missing confirmation left no batch")
    st, body = call("POST", BASE2, f"/api/scenarios/{esid}/batches",
                    {"name": "E-U1-explicit", "use_stored_plan": True,
                     "confirmed_safety_radius": 30,
                     "confirmed_safety_height": 10})
    check(st == 201 and body["safety_radius"] == OLD_R
          and body["safety_height"] == OLD_H
          and body["planned_offsets"] == OLD_PLAN,
          f"E-U1: explicit selection + OLD thresholds freezes the retained "
          f"old table -> {st} {body.get('planned_offsets')}")
    check(batch_ids(esid) == {body.get("batch_id")},
          "E-U1: exactly the explicit old-table batch exists")

    # E-B1: the batch commits FIRST (forced); the unknown solve commits after
    # and must neither retroactively change nor revoke the committed batch.
    esid = fresh_old_table("E-B1")
    st, body = call("POST", BASE2, f"/api/scenarios/{esid}/batches",
                    {"name": "E-B1-batch-first"})
    check(st == 201 and body["safety_radius"] == OLD_R
          and body["safety_height"] == OLD_H
          and body["planned_offsets"] == OLD_PLAN,
          f"E-B1: batch committed first froze the then-valid old table -> "
          f"{st} {body.get('planned_offsets')}")
    bid_b1 = body.get("batch_id")
    solve_unknown(BASE1, esid, "E-B1")  # commits AFTER the batch
    if bid_b1 is not None:
        for base in (BASE1, BASE2):
            gst, gbody = call("GET", base, f"/api/batches/{bid_b1}")
            check(gst == 200 and gbody["safety_radius"] == OLD_R
                  and gbody["safety_height"] == OLD_H
                  and gbody["planned_offsets"] == OLD_PLAN,
                  f"E-B1: later unknown solve did not touch the committed "
                  f"batch (via {base})")
    st, body = call("POST", BASE1, f"/api/scenarios/{esid}/batches",
                    {"name": "E-B1-after-marker"})
    check(st == 409 and body["detail"]["reason"] == "latest_solve_unverified",
          f"E-B1: marker governs LATER unflagged requests -> 409, got {st}")
    check(batch_ids(esid) == {bid_b1},
          "E-B1: refusal left only the race-won batch")

    # E-F1: a NEW verified feasible solve commits FIRST (forced); the batch
    # MUST freeze the NEW thresholds/offsets, never the stale old table.
    esid = fresh_old_table("E-F1")
    new_plan = solve_new_feasible(BASE1, esid, "E-F1")
    st, body = call("POST", BASE2, f"/api/scenarios/{esid}/batches",
                    {"name": "E-F1-after-new-feasible"})
    check(st == 201, f"E-F1: unflagged batch against the verified new table "
                     f"-> HTTP {st}")
    if st == 201:
        check(body["safety_radius"] == NEW_R
              and body["safety_height"] == NEW_H
              and body["planned_offsets"] == new_plan,
              f"E-F1: batch froze the NEW 25/8 table {new_plan}, not the "
              f"stale old one -> R={body['safety_radius']} "
              f"H={body['safety_height']} {body['planned_offsets']}")
        g1 = call("GET", BASE1, f"/api/batches/{body['batch_id']}")[1]
        g2 = call("GET", BASE2, f"/api/batches/{body['batch_id']}")[1]
        check(g1 == g2 and g1["planned_offsets"] == new_plan,
              "E-F1: new-table batch identical from both processes")

    # E-F2: marker stood, then a NEW feasible solve clears it (forced order):
    # unflagged creation recovers and freezes the NEW table.
    esid = fresh_old_table("E-F2")
    solve_unknown(BASE1, esid, "E-F2")
    st, body = call("POST", BASE2, f"/api/scenarios/{esid}/batches",
                    {"name": "E-F2-blocked"})
    check(st == 409 and body["detail"]["reason"] == "latest_solve_unverified",
          f"E-F2: marker standing blocks unflagged creation -> {st}")
    new_plan = solve_new_feasible(BASE2, esid, "E-F2")  # solved from process 2
    st, body = call("POST", BASE1, f"/api/scenarios/{esid}/batches",
                    {"name": "E-F2-recovered"})
    check(st == 201 and body["safety_radius"] == NEW_R
          and body["safety_height"] == NEW_H
          and body["planned_offsets"] == new_plan,
          f"E-F2: feasible re-solve cleared the marker; unflagged batch "
          f"froze the NEW table -> {st} {body.get('planned_offsets')}")

    # E-I1: a proven infeasibility commits FIRST (forced) -> 400, no batch.
    esid = fresh_old_table("E-I1")
    st, body = call("POST", BASE1, f"/api/scenarios/{esid}/solve",
                    INFEASIBLE_SOLVE)
    check(st == 200 and body["solution"]["status"] == "infeasible"
          and not body["solution"]["feasible"],
          f"E-I1: solve proved infeasible -> HTTP {st} "
          f"{body['solution'].get('status')}")
    st, body = call("POST", BASE2, f"/api/scenarios/{esid}/batches",
                    {"name": "E-I1-after-infeasible"})
    check(st == 400,
          f"E-I1: infeasible first -> batch refused 400, got {st} {body}")
    check(batch_ids(esid) == set(), "E-I1: refusal left no batch")

    # E-R: truly simultaneous pairs (barrier release).  The commit order is
    # NOT chosen here - it is CONFIRMED from the responses (both responses in
    # hand means both transactions committed), and each round is checked
    # against the expectation of the order it confirms.  These rounds
    # exercise the write-lock contention path; coverage of the three orders
    # comes from the forced cases above, never from repeating these races.
    def race_pair(call_a, call_b):
        """Fire two HTTP calls at the same instant; return both responses."""
        start = threading.Barrier(2)

        def one(fn):
            start.wait()
            return fn()

        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(one, [call_a, call_b]))

    confirmed = {"unknown-first": 0, "batch-first": 0,
                 "solve-first(new table)": 0, "batch-first(old table)": 0}

    # E-R1: unknown solve vs. UNFLAGGED batch, alternating process direction.
    for trial in range(4):
        esid = fresh_old_table(f"E-R1-{trial}")
        solve_base, batch_base = (BASE1, BASE2) if trial % 2 == 0 \
            else (BASE2, BASE1)
        s_resp, b_resp = race_pair(
            lambda: call("POST", solve_base, f"/api/scenarios/{esid}/solve",
                         UNKNOWN_SOLVE),
            lambda: call("POST", batch_base, f"/api/scenarios/{esid}/batches",
                         {"name": f"E-R1-{trial}"}))
        s_st, s_body = s_resp
        check(s_st == 200 and s_body["solution"]["status"] == "unknown",
              f"E-R1-{trial}: racing solve is unknown (HTTP {s_st})")
        b_st, b_body = b_resp
        if b_st == 409:
            # The batch's in-transaction read saw the committed marker.
            confirmed["unknown-first"] += 1
            check(b_body["detail"]["reason"] == "latest_solve_unverified"
                  and b_body["detail"]["stored_plan_original_safety_radius"]
                  == OLD_R
                  and b_body["detail"]["stored_plan_original_safety_height"]
                  == OLD_H,
                  f"E-R1-{trial}: confirmed unknown-first -> 409 with the "
                  f"old table's original thresholds, got {b_st} {b_body}")
            check(batch_ids(esid) == set(),
                  f"E-R1-{trial}: confirmed unknown-first -> no batch left")
        elif b_st == 201:
            # The batch transaction committed before the marker did.
            confirmed["batch-first"] += 1
            check(b_body["safety_radius"] == OLD_R
                  and b_body["safety_height"] == OLD_H
                  and b_body["planned_offsets"] == OLD_PLAN,
                  f"E-R1-{trial}: confirmed batch-first -> froze the "
                  f"then-valid old table -> {b_body.get('planned_offsets')}")
            # The marker is committed too (its response is in hand): the
            # NEXT unflagged request must be refused and leave no batch.
            st2, body2 = call("POST", batch_base,
                              f"/api/scenarios/{esid}/batches",
                              {"name": f"E-R1-{trial}-after"})
            check(st2 == 409
                  and body2["detail"]["reason"] == "latest_solve_unverified",
                  f"E-R1-{trial}: after the committed marker, unflagged -> "
                  f"409, got {st2}")
            check(batch_ids(esid) == {b_body["batch_id"]},
                  f"E-R1-{trial}: only the race-won batch exists")
        else:
            check(False, f"E-R1-{trial}: unexpected batch outcome "
                         f"{b_st} {b_body}")

    # E-R2: NEW feasible solve vs. unflagged batch.  Both orders are legal;
    # the frozen (R, H, offsets) triple confirms which table was authoritative
    # at the batch's commit - it must be EXACTLY one of the two committed
    # tables, never a mix and never a third value.
    for trial in range(3):
        esid = fresh_old_table(f"E-R2-{trial}")
        s_resp, b_resp = race_pair(
            lambda: call("POST", BASE1, f"/api/scenarios/{esid}/solve",
                         NEW_FEASIBLE_SOLVE),
            lambda: call("POST", BASE2, f"/api/scenarios/{esid}/batches",
                         {"name": f"E-R2-{trial}"}))
        s_st, s_body = s_resp
        sol = s_body.get("solution", {})
        check(s_st == 200 and sol.get("feasible"),
              f"E-R2-{trial}: racing solve feasible (HTTP {s_st})")
        new_plan = sol.get("offsets")
        check(s_body.get("safety_radius") == NEW_R
              and s_body.get("safety_height") == NEW_H
              and isinstance(new_plan, dict) and new_plan != OLD_PLAN,
              f"E-R2-{trial}: new table distinguishable (25/8, {new_plan})")
        b_st, b_body = b_resp
        check(b_st == 201, f"E-R2-{trial}: batch created against a verified "
                           f"table either way (HTTP {b_st}) {b_body}")
        if b_st == 201:
            frozen = (b_body["safety_radius"], b_body["safety_height"],
                      b_body["planned_offsets"])
            if frozen == (OLD_R, OLD_H, OLD_PLAN):
                confirmed["batch-first(old table)"] += 1
                order = "batch-first(old table)"
            elif frozen == (NEW_R, NEW_H, new_plan):
                confirmed["solve-first(new table)"] += 1
                order = "solve-first(new table)"
            else:
                order = None
            check(order is not None,
                  f"E-R2-{trial}: frozen snapshot is exactly one committed "
                  f"table, never a mix -> {frozen}")
            if order is not None:
                print(f"       E-R2-{trial}: confirmed order: {order}")
                g1 = call("GET", BASE1, f"/api/batches/{b_body['batch_id']}")[1]
                g2 = call("GET", BASE2, f"/api/batches/{b_body['batch_id']}")[1]
                check(g1 == g2,
                      f"E-R2-{trial}: race-won batch identical from both "
                      f"processes")

    # E-R3: unknown solve vs. EXPLICIT old-table selection (use_stored_plan +
    # OLD thresholds) - the one admissible path must create in EITHER order.
    for trial in range(2):
        esid = fresh_old_table(f"E-R3-{trial}")
        select = {"name": f"E-R3-{trial}", "use_stored_plan": True,
                  "confirmed_safety_radius": 30,
                  "confirmed_safety_height": 10}
        s_resp, b_resp = race_pair(
            lambda: call("POST", BASE1, f"/api/scenarios/{esid}/solve",
                         UNKNOWN_SOLVE),
            lambda: call("POST", BASE2, f"/api/scenarios/{esid}/batches",
                         select))
        check(s_resp[0] == 200
              and s_resp[1]["solution"]["status"] == "unknown",
              f"E-R3-{trial}: racing solve is unknown (HTTP {s_resp[0]})")
        b_st, b_body = b_resp
        check(b_st == 201 and b_body["safety_radius"] == OLD_R
              and b_body["safety_height"] == OLD_H
              and b_body["planned_offsets"] == OLD_PLAN,
              f"E-R3-{trial}: explicit old-table selection creates in either "
              f"commit order, freezing the retained table -> {b_st} "
              f"{b_body.get('planned_offsets')}")

    print(f"  info: simultaneous races confirmed commit orders -> {confirmed}")
    print("  info: the three orders are covered by the FORCED cases "
          "(E-U1/E-B1/E-F1) above; race rounds only confirm, never carry "
          "coverage.")

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
