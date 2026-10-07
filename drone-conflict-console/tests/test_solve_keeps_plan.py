"""Route-level guard: a timed-out solve must not overwrite a known plan —
and must not *report* success either.

Uses a temporary SQLite store and monkeypatches ``app.solver.run_solve`` so
that a first (real, tiny) solve is followed by a re-solve whose probe timed
out (``status == "unknown"``).  The contract:

* the response's ``solution`` honestly reports THIS run as ``unknown``
  (no back-filled old table masquerading as a fresh success);
* the response keeps the requested (new) thresholds/constraints at top
  level — they were what the solver actually saw;
* the retained old table rides in ``prior_solution``, annotated with the
  original thresholds it was solved under, so it stays viewable/usable;
* the unknown state is PERSISTED as a marker (it survives a fresh Store
  reopen and a page GET), so batch routes distinguish "latest solve
  unverified" from a normally feasible scenario even via direct API calls;
* while the marker stands, a batch is created ONLY with an explicit
  ``use_stored_plan: true`` selection AND a confirmation of the old table's
  original thresholds; omitting the flag / false / a wrong or missing
  threshold confirmation rejects without creating a batch;
* renaming (POST /save) the retained old table does NOT lift the marker;
* a later feasible solve clears the marker and restores unmarked creation;
  a proven infeasibility replaces the stored result and still rejects;
  with no prior feasible plan the response is a plain unknown and batches
  stay rejected.

Run:  python tests/test_solve_keeps_plan.py
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import main as web  # noqa: E402
from app.solver import SolveResult  # noqa: E402

FAILS = 0


def check(cond, msg):
    global FAILS
    if not cond:
        FAILS += 1
        print("FAIL:", msg)
    else:
        print("ok:", msg)


CROSSROADS = {
    "safety_radius": 30,
    "safety_height": 10,
    "sorties": [
        {"id": "A", "weight": 1, "window_hi": 20,
         "waypoints": [
             {"x": 0, "y": 0, "z": 20, "t": 0},
             {"x": 200, "y": 0, "z": 20, "t": 20}]},
        {"id": "B", "weight": 1, "window_hi": 20,
         "waypoints": [
             {"x": 100, "y": -100, "z": 20, "t": 0},
             {"x": 100, "y": 100, "z": 20, "t": 20}]},
    ],
}


def main():
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "console.db")
    web.DB_PATH = db_path
    web.store = web.Store(db_path)

    with TestClient(web.app) as client:
        r = client.post("/api/import", json=CROSSROADS)
        check(r.status_code == 200, f"import ok ({r.status_code})")
        sid = r.json()["scenario_id"]

        # First solve: real solver, known feasible plan persisted.
        r = client.post(f"/api/scenarios/{sid}/solve", json={"time_limit": 20})
        check(r.status_code == 200, f"first solve ok ({r.status_code})")
        first = r.json()["solution"]
        check(first["feasible"], "first solve is feasible")
        check("prior_solution" not in r.json(),
              "plain successful solve carries no prior_solution field")
        saved_offsets = dict(first["offsets"])

        # Re-solve with NEW thresholds and a NEW lock whose probe times out:
        # nothing proven, nothing known — for the NEW constraints.
        original = web.run_solve
        seen = {}

        def fake_unknown(sorties, pcs, locks, **kwargs):
            seen["locks"] = dict(locks)
            seen["pcs"] = pcs
            # Mirror the real solver's unknown return (it echoes the locks).
            return SolveResult(feasible=False, status="unknown",
                               locked=dict(locks))

        web.run_solve = fake_unknown
        try:
            r = client.post(
                f"/api/scenarios/{sid}/solve",
                json={"time_limit": 20, "safety_radius": 45,
                      "safety_height": 12,
                      "locks": [{"sortie_id": "A", "offset": 3}]},
            )
        finally:
            web.run_solve = original
        check(r.status_code == 200, f"unknown solve still 200 ({r.status_code})")
        body = r.json()
        sol = body["solution"]
        check(sol["status"] == "unknown" and not sol["feasible"],
              "response solution honestly reports THIS run as unknown")
        check(seen.get("locks") == {"A": 3},
              "new locks were forwarded into the solver")
        check(sol["locked"] == {"A": 3},
              "response solution echoes this run's locks")
        check(body["safety_radius"] == 45 and body["safety_height"] == 12,
              "top-level thresholds stay the requested new ones (not reverted)")
        # The retained old table is carried separately, with its ORIGINAL
        # thresholds, so it stays viewable without masking the unverified run.
        prior = body.get("prior_solution")
        check(prior is not None, "retained old table exposed as prior_solution")
        check(prior and prior["feasible"] and prior["offsets"] == saved_offsets,
              "prior_solution carries the retained old offsets")
        check(prior and prior["safety_radius"] == 30
              and prior["safety_height"] == 10,
              "prior_solution is annotated with the old table's original "
              "thresholds")

        # SQLite still holds the old plan under the old thresholds.
        r = client.get(f"/api/scenarios/{sid}")
        got = r.json()
        persisted = got["solution"]
        check(persisted["feasible"] and persisted["offsets"] == saved_offsets,
              "unknown re-solve never overwrote the stored plan")
        check(got["safety_radius"] == 30 and got["safety_height"] == 10,
              "stored thresholds unchanged by the unknown re-solve")
        # The unknown marker is persisted: GET (page reload / direct API)
        # reports the unverified latest run and the retained old table under
        # prior_solution with its original thresholds.
        check(got.get("unverified_solve") is not None
              and got["unverified_solve"]["status"] == "unknown",
              "GET exposes the persisted unverified (unknown) latest solve")
        check(got["unverified_solve"]["safety_radius"] == 45
              and got["unverified_solve"]["safety_height"] == 12
              and got["unverified_solve"]["locks"] == {"A": 3},
              "persisted unverified marker keeps the attempted R/H and locks")
        check(got.get("prior_solution") is not None
              and got["prior_solution"]["offsets"] == saved_offsets
              and got["prior_solution"]["safety_radius"] == 30
              and got["prior_solution"]["safety_height"] == 10,
              "GET carries the retained old table with its original thresholds")

        # The marker also survives a fresh Store reopen (e.g. container
        # restart / a second process sharing the file).
        web.store = web.Store(db_path)
        reopened_row = web.store.get_scenario(sid)
        check(reopened_row.get("unverified_solve") is not None
              and reopened_row["unverified_solve"]["safety_radius"] == 45,
              "unknown marker survives reopening the SQLite store")

        # --- Batch adjudication while the latest solve is unverified ------
        # Omitting use_stored_plan: still trusting the old table silently is
        # exactly the bypass being fixed -> reject, no batch created.
        before = set(b["id"] for b in web.store.list_batches(sid))
        r = client.post(f"/api/scenarios/{sid}/batches", json={"name": "bypass"})
        check(r.status_code == 409
              and r.json()["detail"]["reason"] == "latest_solve_unverified",
              f"batch without explicit old-table selection rejected 409 ({r.status_code})")
        after = set(b["id"] for b in web.store.list_batches(sid))
        check(before == after, "rejected request created no batch")
        # Explicit false is rejected identically.
        r = client.post(f"/api/scenarios/{sid}/batches",
                        json={"name": "bypass2", "use_stored_plan": False})
        check(r.status_code == 409
              and r.json()["detail"]["reason"] == "latest_solve_unverified",
              "use_stored_plan=false rejected while the marker stands")
        # Explicit selection without the threshold confirmation is rejected.
        r = client.post(f"/api/scenarios/{sid}/batches",
                        json={"name": "no-confirm", "use_stored_plan": True})
        check(r.status_code == 409
              and r.json()["detail"]["reason"]
              == "stored_plan_threshold_confirmation_required",
              "explicit selection without original-threshold confirmation rejected")
        # Confirming thresholds that do not match the stored old table is
        # rejected (e.g. the NEW attempted thresholds).
        r = client.post(f"/api/scenarios/{sid}/batches",
                        json={"name": "wrong-rh", "use_stored_plan": True,
                              "confirmed_safety_radius": 45,
                              "confirmed_safety_height": 12})
        check(r.status_code == 409
              and r.json()["detail"]["reason"]
              == "stored_plan_threshold_mismatch",
              "confirming the new attempted thresholds is a mismatch -> 409")
        # Only explicit selection AND a matching confirmation of the OLD
        # table's original thresholds freezes the old inputs/offsets/R/H.
        r = client.post(
            f"/api/scenarios/{sid}/batches",
            json={"name": "kept-explicit", "use_stored_plan": True,
                  "confirmed_safety_radius": 30,
                  "confirmed_safety_height": 10})
        check(r.status_code == 201,
              f"explicit selection with matching thresholds freezes old table ({r.status_code})")
        check(r.json()["planned_offsets"] == saved_offsets,
              "batch frozen on the retained old offsets")
        check(r.json()["safety_radius"] == 30 and r.json()["safety_height"] == 10,
              "batch frozen on the old table's original thresholds")
        # Creating the batch does NOT clear the marker.
        web.store = web.Store(db_path)
        check(web.store.get_scenario(sid).get("unverified_solve") is not None,
              "batch creation does not clear the unknown marker")

        # Renaming/saving the retained old table must not lift the restriction.
        r = client.post(f"/api/scenarios/{sid}/save", json={"name": "renamed-old"})
        check(r.status_code == 200, "renaming the old table succeeds")
        web.store = web.Store(db_path)
        row = web.store.get_scenario(sid)
        check(row["name"] == "renamed-old"
              and row.get("unverified_solve") is not None
              and row["result"]["offsets"] == saved_offsets
              and row["safety_radius"] == 30,
              "rename neither clears the marker nor rewrites the old table/R/H")
        r = client.post(f"/api/scenarios/{sid}/batches", json={"name": "after-rename"})
        check(r.status_code == 409
              and r.json()["detail"]["reason"] == "latest_solve_unverified",
              "renaming the old table does not restore unmarked batch creation")

        # A fresh feasible solve saves as usual and CLEARS the marker: plain
        # creation (no flag) works again, exactly like before the unknown.
        r = client.post(f"/api/scenarios/{sid}/solve", json={"time_limit": 20})
        check(r.status_code == 200 and r.json()["solution"]["feasible"],
              "new feasible solve after unknown saves as usual")
        check("prior_solution" not in r.json()
              and "unverified_solve" not in r.json(),
              "feasible re-solve carries no prior_solution / unverified marker")
        r = client.post(f"/api/scenarios/{sid}/batches", json={"name": "fresh"})
        check(r.status_code == 201 and r.json()["planned_offsets"] == saved_offsets,
              "marker cleared: normal batch creation without the flag restored")

        # A genuine infeasibility proof still replaces the stored result.
        infeas = SolveResult(feasible=False, status="infeasible",
                             cores=[{"sorties": ["A", "B"], "size": 2,
                                     "mutex_offsets": []}],
                             truncated=False)
        web.run_solve = lambda *a, **k: infeas  # noqa: E731
        r = client.post(f"/api/scenarios/{sid}/solve", json={"time_limit": 20})
        web.run_solve = original
        check(r.status_code == 200 and not r.json()["solution"]["feasible"],
              "proven infeasible replaces the stored result (distinct from unknown)")
        check("prior_solution" not in r.json()
              and "unverified_solve" not in r.json(),
              "infeasible run carries no prior_solution / unverified marker")
        r = client.get(f"/api/scenarios/{sid}")
        check(r.json()["solution"]["status"] == "infeasible"
              and r.json().get("unverified_solve") is None,
              "infeasible proof persisted and marker cleared")
        r = client.post(f"/api/scenarios/{sid}/batches", json={"name": "infeas"})
        check(r.status_code == 400,
              "proven infeasible -> batch creation still rejected (no 409 path)")

        # No prior feasible plan at all: a bare unknown, nothing to fall back
        # to — the response is a plain unknown without prior_solution.
        r = client.post("/api/import", json=CROSSROADS)
        sid2 = r.json()["scenario_id"]
        web.run_solve = fake_unknown
        try:
            r = client.post(f"/api/scenarios/{sid2}/solve",
                            json={"time_limit": 20})
        finally:
            web.run_solve = original
        body2 = r.json()
        check(body2["solution"]["status"] == "unknown"
              and not body2["solution"]["feasible"],
              "unknown with no prior plan reports unknown")
        check(body2.get("prior_solution") is None,
              "no prior feasible plan -> no prior_solution field")
        r = client.post(f"/api/scenarios/{sid2}/batches", json={"name": "nope"})
        check(r.status_code == 400,
              "no feasible plan stored -> batch creation still rejected")

    print(f"route guard checks, failures: {FAILS}")
    return FAILS


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
