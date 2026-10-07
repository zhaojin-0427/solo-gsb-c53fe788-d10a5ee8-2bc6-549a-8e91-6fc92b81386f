"""Conflict model assembly and CP-SAT scheduling.

Pipeline:

1. For every sortie pair and every segment pair, the analytic geometry in
   :mod:`app.geometry` yields the real interval of relative take-off offsets
   ``d = t0_B - t0_A`` that produce a conflict.  The intervals are converted
   to integer sets and merged into the pair's forbidden-offset table.

2. A CP-SAT model chooses one integer take-off offset ``x_i`` per sortie
   (earliest take-off is 0, latest is the sortie's window end), disjunctively
   avoiding every forbidden interval.  The solve runs in three stages that
   share one wall-clock deadline:

   a. feasibility probe (all sorties participating);
   b. weighted-total-delay minimisation with respect to earliest take-off;
   c. lexicographic minimisation of the offset sequence ordered by sortie ID,
      never worsening the incumbent objective.

   A proven feasible table from any stage is kept as the incumbent and is
   replaced only by a witness satisfying every constraint proven earlier; a
   later stage returning UNKNOWN (time-out) therefore falls back to the
   incumbent instead of discarding it.  The result is ``optimal`` only when
   the weighted stage and *all* lexicographic rounds are proven optimal, and
   ``feasible`` when a valid table exists but its proof did not finish.  A
   FEASIBLE lexicographic round never pins its coordinate as if proven.  A
   probe that finds neither a witness nor a proof returns ``unknown``,
   distinct from a proven ``infeasible``.

3. On infeasibility, every *sortie* carries a participation boolean (assumed
   true); each pair's avoidance clause is enabled only when both sorties
   participate.  CP-SAT then returns an unsat core over these booleans, i.e.
   an inclusion-minimal ("包含关系极小") infeasible sortie set.  Further cores
   are found by branching: force one sortie of a known core out of the
   scenario and solve again; the positive literals of every such core are
   genuine inclusion-minimal infeasible sets of the original problem.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from .geometry import (
    conflicts_between_sorties,
    forbidden_offset_interval_segments,
    integer_forbidden_interval,
    merge_intervals,
)


@dataclass
class PairConstraint:
    sortie_a: str
    sortie_b: str
    intervals: list[tuple[int, int]]  # merged forbidden d = tB - tA intervals


@dataclass
class SolveResult:
    feasible: bool
    status: str  # optimal | feasible | infeasible | unknown | invalid
    offsets: dict[str, int] = field(default_factory=dict)
    weighted_delay: int | None = None
    cores: list[dict] = field(default_factory=list)  # minimal unsat sets
    pair_constraints: list[PairConstraint] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    locked: dict[str, int] = field(default_factory=dict)
    solved_conflicts: list[dict] = field(default_factory=list)
    truncated: bool = False


# ---------------------------------------------------------------------------
# Build pair constraints from geometry
# ---------------------------------------------------------------------------


def build_pair_constraints(sorties, safety_radius: float, safety_height: float) -> list[PairConstraint]:
    out: list[PairConstraint] = []
    n = len(sorties)
    for i in range(n):
        sa = sorties[i]
        for j in range(i + 1, n):
            sb = sorties[j]
            raw: list[tuple[int, int]] = []
            for sega in sa.segments:
                for segb in sb.segments:
                    interval = forbidden_offset_interval_segments(
                        sega, segb, safety_radius, safety_height
                    )
                    if interval is None:
                        continue
                    ii = integer_forbidden_interval(interval)
                    if ii is not None:
                        raw.append(ii)
            if not raw:
                continue
            d_min = sb.window_lo - sa.window_hi
            d_max = sb.window_hi - sa.window_lo
            clipped = [
                (max(lo, d_min), min(hi, d_max))
                for lo, hi in raw
                if hi >= d_min and lo <= d_max
            ]
            clipped = [(lo, hi) for lo, hi in clipped if lo <= hi]
            merged = merge_intervals(clipped)
            if merged:
                out.append(PairConstraint(sa.id, sb.id, merged))
    return out


# ---------------------------------------------------------------------------
# Fixed-schedule conflict scan
# ---------------------------------------------------------------------------


def scan_schedule(sorties, offsets: dict[str, int], safety_radius: float,
                  safety_height: float) -> list[dict]:
    records: list[dict] = []
    for i in range(len(sorties)):
        sa = sorties[i]
        for j in range(i + 1, len(sorties)):
            sb = sorties[j]
            recs = conflicts_between_sorties(
                sa, offsets.get(sa.id, 0),
                sb, offsets.get(sb.id, 0),
                safety_radius, safety_height,
            )
            for r in recs:
                records.append(_record_to_dict(r))
    records.sort(key=lambda r: (r["time_start"], r["sortie_a"], r["sortie_b"]))
    return records


def _record_to_dict(r) -> dict:
    return {
        "sortie_a": r.sortie_a,
        "sortie_b": r.sortie_b,
        "time_start": round(r.time_start, 6),
        "time_end": round(r.time_end, 6),
        "witness_time": round(r.witness_time, 6),
        "a_pos": [round(v, 6) for v in r.a_pos],
        "b_pos": [round(v, 6) for v in r.b_pos],
        "horizontal_distance": round(r.horizontal_distance, 6),
        "vertical_distance": round(r.vertical_distance, 6),
    }


# ---------------------------------------------------------------------------
# CP-SAT model
# ---------------------------------------------------------------------------


def _build_model(sorties, pair_constraints: list[PairConstraint], locks: dict[str, int]):
    model = cp_model.CpModel()
    takeoff: dict[str, cp_model.IntVar] = {}
    participate: dict[str, cp_model.IntVar] = {}
    for s in sorties:
        takeoff[s.id] = model.NewIntVar(s.window_lo, s.window_hi, f"to_{s.id}")
        participate[s.id] = model.NewBoolVar(f"in_{s.id}")
        if s.id in locks:
            model.Add(takeoff[s.id] == int(locks[s.id]))

    for pc in pair_constraints:
        xa, xb = takeoff[pc.sortie_a], takeoff[pc.sortie_b]
        pa, pb = participate[pc.sortie_a], participate[pc.sortie_b]
        for k, (lo, hi) in enumerate(pc.intervals):
            b_after = model.NewBoolVar(f"after_{pc.sortie_a}_{pc.sortie_b}_{k}")
            b_before = model.NewBoolVar(f"before_{pc.sortie_a}_{pc.sortie_b}_{k}")
            model.Add(xb - xa >= hi + 1).OnlyEnforceIf(b_after)
            model.Add(xb - xa <= lo - 1).OnlyEnforceIf(b_before)
            # pair constraint active only while both sorties participate
            model.AddBoolOr([b_after, b_before]).OnlyEnforceIf(pa, pb)
    return model, takeoff, participate


def _new_solver(time_limit: float) -> cp_model.CpSolver:
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit)
    solver.parameters.num_search_workers = 8
    return solver


def run_solve(sorties, pair_constraints: list[PairConstraint],
              locks: dict[str, int] | None = None,
              time_limit: float = 30.0, max_cores: int = 40,
              solver_factory=_new_solver) -> SolveResult:
    """Staged CP-SAT solve that never lets a later time-out destroy an
    already-proven feasible take-off table.

    Stages (all sharing the single wall-clock budget ``time_limit``):

    0. feasibility probe with every sortie participating;
    1. weighted-delay minimization;
    2. lexicographic tie-break over sortie-ID-ordered offsets.

    The *incumbent* is the best witness seen so far.  It is replaced only by a
    witness produced under a model containing every constraint already proven
    (optimal objective value, previously fixed coordinates) and no worse on the
    stage metric.  A stage returning ``UNKNOWN`` therefore falls back to the
    incumbent instead of discarding it.  ``optimal`` is claimed only when the
    weighted objective AND every lexicographic round are proven optimal; a
    proven feasible table with an unfinished proof yields ``feasible``.  A
    genuine ``INFEASIBLE`` probe still returns ``infeasible`` with cores,
    distinct from an ``unknown`` probe that found neither a witness nor a
    proof.
    """
    locks = dict(locks or {})
    t_start = time.perf_counter()
    deadline = t_start + float(time_limit)
    model, takeoff, participate = _build_model(sorties, pair_constraints, locks)
    idx_to_id = {v.Index(): sid for sid, v in participate.items()}

    def remaining() -> float:
        return max(0.0, deadline - time.perf_counter())

    def make_solver():
        # ``solver_factory`` is injectable for deterministic tests (scripted
        # status sequence); production always yields a real cp_model.CpSolver.
        return solver_factory(remaining())

    weights = {s.id: s.weight for s in sorties}
    ids = sorted(takeoff)

    def witness(solver) -> tuple[dict[str, int], int]:
        off = {sid: solver.Value(takeoff[sid]) for sid in ids}
        return off, sum(weights[sid] * v for sid, v in off.items())

    # ------------------------------------------------------------------
    # Stage 0: feasibility probe (all sorties participating).
    # ------------------------------------------------------------------
    model.AddAssumptions([participate[sid] for sid in ids])
    solver = make_solver()
    status = solver.Solve(model)

    if status == cp_model.INFEASIBLE:
        cores, truncated = _enumerate_cores(
            solver, model, participate, idx_to_id, pair_constraints,
            max_cores, t_start, time_limit,
        )
        return SolveResult(
            feasible=False,
            status="infeasible",
            cores=cores,
            pair_constraints=pair_constraints,
            elapsed_seconds=time.perf_counter() - t_start,
            locked=locks,
            truncated=truncated,
        )
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return SolveResult(
            feasible=False,
            status=solver.StatusName(status).lower(),
            pair_constraints=pair_constraints,
            elapsed_seconds=time.perf_counter() - t_start,
            locked=locks,
        )

    # Proven feasible table from the probe; kept for the lifetime of the call.
    offsets, best_obj = witness(solver)
    objective = sum(weights[sid] * v for sid, v in takeoff.items())

    # ------------------------------------------------------------------
    # Stage 1: minimise weighted total delay.
    # ------------------------------------------------------------------
    model.Minimize(objective)
    if remaining() > 0:
        solver = make_solver()
        status = solver.Solve(model)
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            cand, cand_obj = witness(solver)
            if cand_obj <= best_obj:
                offsets, best_obj = cand, cand_obj
        # UNKNOWN / invalid: the probe witness remains the incumbent.
        objective_optimal = status == cp_model.OPTIMAL

        # ----------------------------------------------------------------
        # Stage 2: lexicographically smallest offset sequence in sortie ID
        # order, without worsening the incumbent objective.
        #
        # The objective bound keeps every later witness inside the proven /
        # incumbent cone.  When the weighted optimum is proven the bound pins
        # the exact optimum; otherwise it only rejects strictly worse tables.
        model.Add(objective <= best_obj)
        lex_optimal = objective_optimal
        for sid in ids:
            if remaining() <= 0:
                lex_optimal = False
                break
            model.Minimize(takeoff[sid])
            solver = make_solver()
            st = solver.Solve(model)
            if st == cp_model.OPTIMAL:
                # Coordinate proven minimal: fix it for all later rounds.
                val = solver.Value(takeoff[sid])
                model.Add(takeoff[sid] == val)
                cand, cand_obj = witness(solver)
                if cand_obj <= best_obj:
                    offsets, best_obj = cand, cand_obj
            elif st == cp_model.FEASIBLE:
                # Witness is valid but NOT proven minimal for this coordinate;
                # never fix it as if proven.  Tighten only with the
                # non-worsening bound, adopt the witness if it improves the
                # lexicographic sequence, and let later rounds continue.
                cand, cand_obj = witness(solver)
                cand_seq = tuple(cand[k] for k in ids)
                inc_seq = tuple(offsets[k] for k in ids)
                if cand_obj <= best_obj and cand_seq <= inc_seq:
                    offsets, best_obj = cand, cand_obj
                model.Add(takeoff[sid] <= offsets[sid])
                lex_optimal = False
            else:
                # UNKNOWN (time-out) / MODEL_INVALID: keep the incumbent and
                # carry only the non-worsening bound for this coordinate, so a
                # later round cannot degrade the sequence.
                model.Add(takeoff[sid] <= offsets[sid])
                lex_optimal = False
    else:
        objective_optimal = False
        lex_optimal = False

    return SolveResult(
        feasible=True,
        status="optimal" if (objective_optimal and lex_optimal) else "feasible",
        offsets=offsets,
        weighted_delay=best_obj,
        pair_constraints=pair_constraints,
        elapsed_seconds=time.perf_counter() - t_start,
        locked=locks,
    )


def _extract_core_ids(solver, idx_to_id) -> tuple[list[str], list[str]]:
    """Return (participating ids, forced-out ids) of the current unsat core."""
    lits = solver.SufficientAssumptionsForInfeasibility()
    inside, outside = [], []
    for lit in lits:
        sid = idx_to_id.get(abs(lit))
        if sid is None:
            continue
        (inside if lit > 0 else outside).append(sid)
    return sorted(inside), sorted(outside)


def _enumerate_cores(solver, model, participate, idx_to_id, pair_constraints,
                     max_cores, t_start, time_limit) -> tuple[list[dict], bool]:
    found: list[frozenset[str]] = []
    queue: deque[frozenset[str]] = deque()

    first, _ = _extract_core_ids(solver, idx_to_id)
    if not first:
        return [], False
    found.append(frozenset(first))
    queue.append(frozenset(first))

    deadline = t_start + time_limit
    while queue and len(found) < max_cores and time.perf_counter() < deadline:
        core = queue.popleft()
        # Branch: drop each member of this core one at a time; a fresh
        # infeasibility yields a different minimal core not containing it.
        members = sorted(core)
        for dropped in members:
            if len(found) >= max_cores or time.perf_counter() >= deadline:
                break
            assm = []
            for sid, var in participate.items():
                assm.append(var.Not() if sid == dropped else var)
            model.ClearAssumptions()
            model.AddAssumptions(assm)
            st = solver.Solve(model)
            if st != cp_model.INFEASIBLE:
                continue
            inside, _outside = _extract_core_ids(solver, idx_to_id)
            if not inside:
                continue
            key = frozenset(inside)
            if key not in found:
                found.append(key)
                queue.append(key)

    model.ClearAssumptions()
    model.AddAssumptions([participate[sid] for sid in sorted(participate)])

    pair_index = {(pc.sortie_a, pc.sortie_b): pc.intervals for pc in pair_constraints}
    cores_out = []
    for key in sorted(found, key=lambda s: (len(s), sorted(s))):
        ids = sorted(key)
        mutex = []
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                ivs = pair_index.get((a, b))
                if ivs:
                    mutex.append({
                        "sortie_a": a,
                        "sortie_b": b,
                        "forbidden_offsets": [[lo, hi] for lo, hi in ivs],
                    })
        cores_out.append({"sorties": ids, "size": len(ids), "mutex_offsets": mutex})
    truncated = bool(queue) or len(found) >= max_cores
    return cores_out, truncated
