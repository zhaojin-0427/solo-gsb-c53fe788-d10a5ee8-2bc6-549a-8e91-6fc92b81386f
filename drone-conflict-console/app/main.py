"""FastAPI application: import, analyse, solve and persist drone scenarios."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .model import Waypoint, make_sortie
from .schemas import (
    CreateBatchRequest,
    ImportPayload,
    LockEntry,
    ReportRequest,
    SaveRequest,
    SolveRequest,
)
from .solver import (
    PairConstraint,
    build_pair_constraints,
    run_solve,
    scan_schedule,
)
from .batch import EarlyTimeReject, Report, evaluate_warning, review_warning
from .workzone import InvalidWorkZoneQuery, evaluate_workzone
from .storage import BatchRejected, Store

DB_PATH = os.environ.get("DCC_DB_PATH", "data/console.db")
store: Store | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global store
    store = Store(DB_PATH)
    yield


app = FastAPI(title="无人机航迹冲突消解台", version="1.0", lifespan=lifespan)


def _build_sorties(payload: ImportPayload | dict):
    if isinstance(payload, ImportPayload):
        data = payload
    else:
        data = ImportPayload.model_validate(payload)
    sorties = []
    for s in data.sorties:
        wps = [Waypoint(w.x, w.y, w.z, w.t) for w in s.waypoints]
        sorties.append(make_sortie(s.id, s.weight, s.window_hi, wps, 0))
    return sorties, data.safety_radius, data.safety_height


def _pair_constraints_dto(pcs: list[PairConstraint]) -> list[dict]:
    return [
        {"sortie_a": pc.sortie_a, "sortie_b": pc.sortie_b,
         "forbidden_offsets": [[lo, hi] for lo, hi in pc.intervals]}
        for pc in pcs
    ]


def _scenario_summary(sid: int, sorties, safety_radius, safety_height,
                      pcs, solve_result=None, earliest_conflicts=None) -> dict:
    out = {
        "scenario_id": sid,
        "safety_radius": safety_radius,
        "safety_height": safety_height,
        "sorties": [
            {
                "id": s.id,
                "weight": s.weight,
                "window_lo": s.window_lo,
                "window_hi": s.window_hi,
                "flight_end": s.flight_end,
                "waypoints": [
                    {"x": w.x, "y": w.y, "z": w.z, "t": w.t} for w in s.waypoints
                ],
            }
            for s in sorties
        ],
        "pair_constraints": _pair_constraints_dto(pcs),
        "earliest_conflicts": earliest_conflicts,
    }
    if solve_result is not None:
        out["solution"] = solve_result
    return out


@app.post("/api/import")
def import_scenario(payload: ImportPayload):
    sorties, R, H = _build_sorties(payload)
    sid = store.create_scenario(payload.model_dump(), R, H)
    pcs = build_pair_constraints(sorties, R, H)
    earliest = scan_schedule(sorties, {s.id: 0 for s in sorties}, R, H)
    return _scenario_summary(sid, sorties, R, H, pcs, earliest_conflicts=earliest)


@app.get("/api/scenarios")
def list_scenarios():
    return {"scenarios": store.list_scenarios()}


@app.get("/api/scenarios/{sid}")
def get_scenario(sid: int):
    row = store.get_scenario(sid)
    if row is None:
        raise HTTPException(404, "scenario not found")
    payload = ImportPayload.model_validate(row["input"])
    sorties, _, _ = _build_sorties(payload)
    R = row["safety_radius"]
    H = row["safety_height"]
    pcs = build_pair_constraints(sorties, R, H)
    earliest = scan_schedule(sorties, {s.id: 0 for s in sorties}, R, H)
    out = _scenario_summary(sid, sorties, R, H, pcs,
                            solve_result=row["result"],
                            earliest_conflicts=earliest)
    _attach_unverified(row, out)
    return out


@app.delete("/api/scenarios/{sid}")
def delete_scenario(sid: int):
    if not store.delete_scenario(sid):
        raise HTTPException(404, "scenario not found")
    return {"deleted": sid}


def _load(sid: int):
    row = store.get_scenario(sid)
    if row is None:
        raise HTTPException(404, "scenario not found")
    payload = ImportPayload.model_validate(row["input"])
    sorties, _, _ = _build_sorties(payload)
    return sorties, row, payload


def _attach_unverified(row: dict, out: dict) -> None:
    """Expose a persisted ``unknown`` marker on a scenario response.

    When the most recent solve proved nothing while SQLite retains a feasible
    old table, the response describes that unproven run under
    ``unverified_solve`` (its attempted thresholds/locks) and carries the
    retained old table under ``prior_solution`` with the ORIGINAL thresholds
    stored alongside it - whether the response comes straight from /solve or
    from a later GET / page reload, so the page and direct API apply the same
    adjudication.  Note the top-level thresholds stay at the stored old values
    here: GET reopens the retained snapshot.
    """
    unverified = row.get("unverified_solve")
    prior = row.get("result")
    if unverified is not None:
        out["unverified_solve"] = unverified
        if prior and prior.get("feasible"):
            out["prior_solution"] = {
                **prior,
                "safety_radius": row["safety_radius"],
                "safety_height": row["safety_height"],
            }


@app.post("/api/scenarios/{sid}/solve")
def solve_scenario(sid: int, req: SolveRequest):
    sorties, row, payload = _load(sid)
    R = req.safety_radius if req.safety_radius is not None else row["safety_radius"]
    H = req.safety_height if req.safety_height is not None else row["safety_height"]
    if R <= 0 or H <= 0:
        raise HTTPException(400, "safety parameters must be positive")
    known = {s.id: s for s in sorties}
    locks: dict[str, int] = {}
    for lk in req.locks:
        if lk.sortie_id not in known:
            raise HTTPException(400, f"unknown sortie id in locks: {lk.sortie_id}")
        s = known[lk.sortie_id]
        if not (s.window_lo <= lk.offset <= s.window_hi):
            raise HTTPException(
                400,
                f"locked offset {lk.offset} for {lk.sortie_id} outside window "
                f"[{s.window_lo}, {s.window_hi}]",
            )
        locks[lk.sortie_id] = lk.offset

    pcs = build_pair_constraints(sorties, R, H)
    res = run_solve(sorties, pcs, locks, time_limit=req.time_limit)

    # A time-out that proved nothing must never destroy a previously proven
    # result in SQLite (a feasible old table, or even a proven infeasibility).
    # The response still reports THIS run honestly: ``solution`` carries the
    # unknown status and the requested (new) thresholds/constraints, while a
    # retained feasible old table is exposed separately under
    # ``prior_solution`` with the original thresholds it was solved under, and
    # the unverified state is persisted (see below) for the batch route.
    prior = row.get("result")
    keep_prior = res.status == "unknown" and bool(prior and prior.get("feasible"))

    earliest = scan_schedule(sorties, {s.id: 0 for s in sorties}, R, H)
    solved_conflicts = []
    if res.feasible:
        solved_conflicts = scan_schedule(sorties, res.offsets, R, H)
        if solved_conflicts:
            raise HTTPException(500, "internal error: solved schedule still conflicts")

    result = {
        "feasible": res.feasible,
        "status": res.status,
        "weighted_delay": res.weighted_delay,
        "offsets": res.offsets,
        "locked": res.locked,
        "elapsed_seconds": round(res.elapsed_seconds, 4),
        "solved_conflicts": solved_conflicts,
        "cores": res.cores,
        "truncated": res.truncated,
    }
    # Description of THIS run's (unproven) attempt, persisted so the unknown
    # marker survives restarts/reloads and so direct API clients see exactly
    # what the page saw.
    unverified_attempt = {
        "status": "unknown",
        "feasible": False,
        "safety_radius": R,
        "safety_height": H,
        "locks": dict(locks),
        "elapsed_seconds": result["elapsed_seconds"],
    }
    if res.status == "unknown":
        # SQLite keeps the previously proven table untouched (and an unknown
        # run never overwrites a proven infeasibility either); the response
        # still reports this run as unknown.  The fact that the LATEST solve
        # is unverified is itself persisted (``mark_solve_unknown``) - it does
        # not rely on this HTTP response being the last thing the client saw,
        # and is not lifted by renaming/saving the old table.  It is cleared
        # only by a later verified solve (feasible, or a proven infeasible).
        store.mark_solve_unknown(sid, unverified_attempt)
    else:
        store.save_result(sid, result, R, H)
    out = _scenario_summary(sid, sorties, R, H, pcs,
                            solve_result=result,
                            earliest_conflicts=earliest)
    if keep_prior:
        # The retained old table rides separately, annotated with its original
        # thresholds, together with the persisted unverified-run marker.
        out["unverified_solve"] = unverified_attempt
        out["prior_solution"] = {
            **prior,
            "safety_radius": row["safety_radius"],
            "safety_height": row["safety_height"],
        }
    elif res.status == "unknown":
        out["unverified_solve"] = unverified_attempt
    return out


@app.post("/api/scenarios/{sid}/save")
def save_scenario(sid: int, req: SaveRequest):
    sorties, row, payload = _load(sid)
    if row["result"] is None:
        raise HTTPException(400, "solve the scenario before saving a plan")
    # Saving/renaming only names the plan already persisted in SQLite; it must
    # NOT rewrite the result or clear an ``unknown`` marker.  Renaming the
    # retained old table therefore cannot lift the batch-creation restriction.
    store.rename_scenario(sid, req.name)
    return {"scenario_id": sid, "name": req.name, "saved": True}


# ---------------------------------------------------------------------------
# Execution batches: frozen plan + actual-take-off reports + warning
# ---------------------------------------------------------------------------


def _build_batch(row: dict):
    payload = ImportPayload.model_validate(row["input"])
    sorties, _, _ = _build_sorties(payload)
    reports = {
        r["sortie_id"]: Report(r["actual_offset"], r["revision"])
        for r in row["reports"]
    }
    return sorties, reports


def _batch_summary(row: dict) -> dict:
    return {
        "batch_id": row["id"],
        "scenario_id": row["scenario_id"],
        "name": row["name"],
        "safety_radius": row["safety_radius"],
        "safety_height": row["safety_height"],
        "created_at": row["created_at"],
        "planned_offsets": row["plan"]["offsets"],
        "reports": [
            {"sortie_id": r["sortie_id"], "actual_offset": r["actual_offset"],
             "revision": r["revision"], "reported_at": r["reported_at"]}
            for r in row["reports"]
        ],
    }


@app.post("/api/scenarios/{sid}/batches", status_code=201)
def create_batch(sid: int, req: CreateBatchRequest):
    # The arbitration must not run on state read outside the write
    # transaction: with several processes sharing the SQLite file a /solve can
    # commit an ``unknown`` marker (new locks/thresholds proved nothing) or a
    # new verified feasible table in the gap between such a read and the
    # insert.  ``create_batch`` therefore re-reads the AUTHORITATIVE scenario
    # row inside its BEGIN IMMEDIATE transaction and makes the decision and
    # the insert one atomic unit:
    #
    # * no feasible table at the authoritative moment (none ever solved, or a
    #   freshly committed proven infeasibility) -> 400, no batch;
    # * latest solve is the persisted ``unknown`` marker while an OLD feasible
    #   table is retained -> created ONLY with an explicit
    #   ``use_stored_plan: true`` selection AND a confirmation equal to the
    #   old table's ORIGINAL thresholds; omitted flag / false, a missing
    #   confirmation or a mismatching one -> 409, no batch.  A marker
    #   committed in the gap is therefore seen and enforced; a request that
    #   did not select the old table can never pass;
    # * otherwise the snapshot, thresholds and planned offsets frozen are the
    #   ones committed at the decision moment - a new verified feasible solve
    #   committed in the gap freezes the NEW table rather than a stale one;
    # * if the batch transaction commits first, it keeps the snapshot that was
    #   valid then; the interleaving solve arbitrates later on its own
    #   transaction and can neither retroactively change nor revoke the batch.
    #
    # Renaming/saving the old table does not clear the marker; a later
    # feasible solve (or a proven infeasibility) does.  Status codes, response
    # fields and already-created batches are unchanged.
    try:
        bid = store.create_batch(
            sid, req.name,
            use_stored_plan=req.use_stored_plan,
            confirmed_radius=req.confirmed_safety_radius,
            confirmed_height=req.confirmed_safety_height)
    except BatchRejected as exc:
        if exc.status_code == 404:
            raise HTTPException(404, "scenario not found")
        if exc.status_code == 400:
            # Keep the historical plain-message 400 for "no feasible plan".
            raise HTTPException(400, exc.fields.get(
                "message",
                "a feasible plan must be solved before creating an execution batch"))
        raise HTTPException(409, detail={"reason": exc.reason, **exc.fields})
    batch = store.get_batch(bid)
    return _batch_summary(batch)


@app.get("/api/batches")
def list_batches(scenario_id: int | None = None):
    return {"batches": store.list_batches(scenario_id)}


@app.get("/api/batches/{bid}")
def get_batch(bid: int):
    row = store.get_batch(bid)
    if row is None:
        raise HTTPException(404, "batch not found")
    return _batch_summary(row)


@app.delete("/api/batches/{bid}")
def delete_batch(bid: int):
    if not store.delete_batch(bid):
        raise HTTPException(404, "batch not found")
    return {"deleted": bid}


@app.put("/api/batches/{bid}/reports")
def put_report(bid: int, req: ReportRequest):
    row = store.get_batch(bid)
    if row is None:
        raise HTTPException(404, "batch not found")
    known = {s["id"] for s in row["input"]["sorties"]}
    if req.sortie_id not in known:
        raise HTTPException(404, f"sortie not found in batch: {req.sortie_id}")
    # Atomic compare-and-set: identical retries are idempotent, same revision
    # with a different value or an older revision conflicts, and concurrent
    # reports leave only the highest revision stored.
    outcome, info = store.submit_report(
        bid, req.sortie_id, req.actual_offset, req.revision)
    if outcome in ("idempotent", "inserted"):
        return {"batch_id": bid, "report": info, "accepted": True,
                "idempotent": outcome == "idempotent"}
    if outcome == "invalid_first_revision":
        raise HTTPException(409, detail={
            "reason": "invalid_first_revision",
            "sortie_id": req.sortie_id,
            "received_revision": req.revision,
            "expected_revision": 1,
        })
    if outcome == "revision_value_conflict":
        raise HTTPException(409, detail={
            "reason": "revision_value_conflict",
            "sortie_id": req.sortie_id,
            "revision": req.revision,
            "stored_actual_offset": info["stored_actual_offset"],
            "received_actual_offset": req.actual_offset,
        })
    raise HTTPException(409, detail={
        "reason": "stale_revision",
        "sortie_id": req.sortie_id,
        "received_revision": req.revision,
        "current_revision": info["current_revision"],
    })


@app.get("/api/batches/{bid}/warning")
def batch_warning(bid: int, t: int):
    row = store.get_batch(bid)
    if row is None:
        raise HTTPException(404, "batch not found")
    if t < 0:
        raise HTTPException(400, "t must be a non-negative integer second")
    sorties, reports = _build_batch(row)
    try:
        result = evaluate_warning(
            sorties, row["plan"]["offsets"], reports,
            row["safety_radius"], row["safety_height"], t,
        )
    except EarlyTimeReject as exc:
        raise HTTPException(
            400,
            detail={
                "reason": "t_before_reported_takeoff",
                "t": t,
                "violations": exc.violations,
            },
        )
    summary = _batch_summary(row)
    return {**summary, "warning": result}


@app.get("/api/batches/{bid}/workzone")
def batch_workzone(bid: int, cx: float, cy: float, radius: float,
                   z_lo: float, z_hi: float, t: int, T: int, d: int):
    """Safe integer start window for a temporary cylindrical work zone."""
    row = store.get_batch(bid)
    if row is None:
        raise HTTPException(404, "batch not found")
    sorties, reports = _build_batch(row)
    try:
        result = evaluate_workzone(
            sorties, row["plan"]["offsets"], reports,
            cx, cy, radius, z_lo, z_hi, t, T, d)
    except InvalidWorkZoneQuery as exc:
        raise HTTPException(400, detail={"reason": exc.reason, **exc.fields})
    except EarlyTimeReject as exc:
        raise HTTPException(
            400,
            detail={
                "reason": "t_before_reported_takeoff",
                "t": t,
                "violations": exc.violations,
            },
        )
    summary = _batch_summary(row)
    return {**summary, "workzone": result}


def _reports_from_snapshot(snapshot: list[dict]) -> dict[str, Report]:
    return {
        item["sortie_id"]: Report(item["actual_offset"], item["revision"])
        for item in snapshot
    }


def _node_dto(node: dict) -> dict:
    return {
        "node_id": node["id"],
        "seq": node["seq"],
        "created_at": node["created_at"],
        "is_baseline": node["is_baseline"],
        "trigger_sortie_id": node["trigger_sortie_id"],
        "trigger_actual_offset": node["trigger_actual_offset"],
        "trigger_revision": node["trigger_revision"],
    }


@app.get("/api/batches/{bid}/report-nodes")
def list_report_nodes(bid: int):
    row = store.get_batch(bid)
    if row is None:
        raise HTTPException(404, "batch not found")
    return {"batch_id": bid, "nodes": [_node_dto(n) for n in store.list_report_nodes(bid)]}


@app.get("/api/batches/{bid}/report-nodes/{node_id}/review")
def review_report_node(bid: int, node_id: int, t: int):
    row = store.get_batch(bid)
    if row is None:
        raise HTTPException(404, "batch not found")
    if t < 0:
        raise HTTPException(400, "t must be a non-negative integer second")
    node = store.get_report_node(bid, node_id)
    if node is None:
        raise HTTPException(404, "report node not found")
    sorties, _ = _build_batch(row)
    before = _reports_from_snapshot(node["before"])
    after = _reports_from_snapshot(node["after"])
    trigger = None
    if not node["is_baseline"]:
        trigger = {
            "sortie_id": node["trigger_sortie_id"],
            "actual_offset": node["trigger_actual_offset"],
            "revision": node["trigger_revision"],
        }
    try:
        result = review_warning(
            sorties, row["plan"]["offsets"], before, after,
            row["safety_radius"], row["safety_height"], t, trigger)
    except EarlyTimeReject as exc:
        raise HTTPException(
            400,
            detail={
                "reason": "t_before_reported_takeoff",
                "t": t,
                "violations": exc.violations,
            },
        )
    return {
        "batch_id": bid,
        "node": {**_node_dto(node),
                 "sortie_id": node["trigger_sortie_id"],
                 "actual_offset": node["trigger_actual_offset"],
                 "revision": node["trigger_revision"]},
        "review": result,
    }


@app.get("/api/health")
def health():
    return {"ok": True}


# Static front-end (served last so /api/* wins).
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/app", StaticFiles(directory=STATIC_DIR, html=True), name="app")


@app.get("/")
def root():
    return FileResponse(STATIC_DIR / "index.html")
