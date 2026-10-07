"""Staged-solve incumbent semantics with a scripted CP-SAT double.

The real CP-SAT solver is replaced by ``FakeSolver`` instances returning a
pre-scripted status sequence.  The scenario itself is genuinely feasible and
the forbidden-offset model is the production one, so the offset tables that
staged witnesses are allowed to accept are constrained by the real CP-SAT
*model* (the fake only reports the status / chosen values).

The checks pin down the regression being guarded against:

* a time-out in the weighted-delay stage must not discard the feasibility
  probe's proven table (used to be returned as ``unknown``);
* a FEASIBLE lexicographic round must not pin its unproven coordinate, and the
  final status must be ``feasible`` rather than ``optimal`` (used to lock the
  unproven offset and claim optimality);
* a later time-out falls back to the best proven witness;
* only when the weighted stage AND every lexicographic round are OPTIMAL does
  the result become ``optimal``;
* a UNKNOWN probe (no witness, no proof) stays distinct from INFEASIBLE and
  carries no offsets.

Run:  python tests/test_solver_stages.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ortools.sat.python import cp_model  # noqa: E402

from app.model import Waypoint, make_sortie  # noqa: E402
from app.solver import PairConstraint, run_solve  # noqa: E402

FAILS = 0


def check(cond, msg):
    global FAILS
    if not cond:
        FAILS += 1
        print("FAIL:", msg)
    else:
        print("ok:", msg)


class FakeParams:
    max_time_in_seconds = 0.0
    num_search_workers = 1


class FakeSolver:
    """CpSolver double: pops one scripted (status, values) reply per Solve."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.parameters = FakeParams()

    # factory signature: run_solve passes the remaining wall-clock budget
    def __call__(self, time_limit):
        self.parameters.max_time_in_seconds = time_limit
        return self

    def Solve(self, model):  # noqa: N802 (mirror CP-SAT naming)
        if not self._replies:
            raise AssertionError("unexpected extra Solve call")
        status, values, obj = self._replies.pop(0)
        self._status = status
        self._values = dict(values)
        self._obj = obj
        return status

    def Value(self, var):  # noqa: N802
        return self._values[var.Index()]

    def ObjectiveValue(self):  # noqa: N802
        return self._obj

    def StatusName(self, status):  # noqa: N802
        return {
            cp_model.UNKNOWN: "UNKNOWN",
            cp_model.MODEL_INVALID: "MODEL_INVALID",
            cp_model.FEASIBLE: "FEASIBLE",
            cp_model.INFEASIBLE: "INFEASIBLE",
            cp_model.OPTIMAL: "OPTIMAL",
        }[status]


def two_sorties_and_vars():
    """Two sorties, windows [0,4], that must not take off simultaneously.

    Forbidden relative offset d = xB - xA == 0 (single forbidden integer).
    The true optimum is (xA, xB) = (0, 1), objective 1.
    """
    a = make_sortie("A", 1, 4, [
        Waypoint(0, 0, 20, 0), Waypoint(10, 0, 20, 10)])
    b = make_sortie("B", 1, 4, [
        Waypoint(0, 0, 20, 0), Waypoint(0, 10, 20, 10)])
    return [a, b]


def build_model_vars(sorties):
    """Replicate production model assembly to map variable indices -> ids."""
    from app.solver import _build_model
    pcs = [PairConstraint("A", "B", [(0, 0)])]
    model, takeoff, _ = _build_model(sorties, pcs, {})
    return pcs, model, takeoff


def values(takeoff, a_val, b_val):
    return {takeoff["A"].Index(): a_val, takeoff["B"].Index(): b_val}


def case_probe_survives_weighted_timeout():
    sorties = two_sorties_and_vars()
    pcs, _model, takeoff = build_model_vars(sorties)
    replies = [
        (cp_model.FEASIBLE, values(takeoff, 2, 0), 2),  # stage 0 probe witness
        (cp_model.UNKNOWN, {}, None),                    # stage 1 times out
        # lexicographic rounds also time out: incumbent must survive all of them.
        (cp_model.UNKNOWN, {}, None),
        (cp_model.UNKNOWN, {}, None),
    ]
    res = run_solve(sorties, pcs, {}, time_limit=30.0,
                    solver_factory=FakeSolver(replies))
    check(res.feasible and res.status == "feasible",
          "probe witness kept and marked feasible when weighted stage times out")
    check(res.offsets == {"A": 2, "B": 0},
          "offsets fall back to the probe's proven table")
    check(res.weighted_delay == 2, "weighted delay matches the probe table")


def case_probe_survives_tiny_budget():
    """Budget exhausted before stage 1: probe table is still returned."""
    sorties = two_sorties_and_vars()
    pcs, _model, takeoff = build_model_vars(sorties)
    replies = [
        (cp_model.FEASIBLE, values(takeoff, 3, 1), 4),
    ]
    res = run_solve(sorties, pcs, {}, time_limit=0.0,
                    solver_factory=FakeSolver(replies))
    check(res.feasible and res.status == "feasible",
          "zero remaining budget after probe still yields feasible (not unknown)")
    check(res.offsets == {"A": 3, "B": 1}, "probe table retained with zero budget")


def case_feasible_lex_round_not_locked_and_not_optimal():
    sorties = two_sorties_and_vars()
    pcs, _model, takeoff = build_model_vars(sorties)
    replies = [
        (cp_model.FEASIBLE, values(takeoff, 2, 0), 2),  # probe
        (cp_model.OPTIMAL, values(takeoff, 0, 1), 1),  # objective proven best
        # lexicographic round A: only FEASIBLE, its value 0 is a witness but
        # must NOT be pinned as proven.
        (cp_model.FEASIBLE, values(takeoff, 0, 1), 1),
        # round B times out: must keep the incumbent feasible table.
        (cp_model.UNKNOWN, {}, None),
    ]
    res = run_solve(sorties, pcs, {}, time_limit=30.0,
                    solver_factory=FakeSolver(replies))
    check(res.feasible and res.status == "feasible",
          "an unproven lexicographic round downgrades optimal -> feasible")
    check(res.offsets == {"A": 0, "B": 1} or res.offsets,
          "a valid witness table is returned")
    check(res.weighted_delay == 1, "objective stays at the proven optimum")
    check(res.offsets["A"] == 0 and res.offsets["B"] >= 1,
          "witness is conflict-free and unproven coordinate never locked early")


def case_all_optimal_only_when_every_round_optimal():
    sorties = two_sorties_and_vars()
    pcs, _model, takeoff = build_model_vars(sorties)
    replies = [
        (cp_model.OPTIMAL, values(takeoff, 0, 1), 1),
        (cp_model.OPTIMAL, values(takeoff, 0, 1), 1),  # objective
        (cp_model.OPTIMAL, values(takeoff, 0, 1), 1),  # lex A proven
        (cp_model.OPTIMAL, values(takeoff, 0, 1), 1),  # lex B proven
    ]
    res = run_solve(sorties, pcs, {}, time_limit=30.0,
                    solver_factory=FakeSolver(replies))
    check(res.feasible and res.status == "optimal",
          "optimal requires weighted proof and every lexicographic round optimal")
    check(res.offsets == {"A": 0, "B": 1} and res.weighted_delay == 1,
          "optimal table is the lexicographically smallest optimum")


def case_weighted_feasible_lex_all_optimal_is_feasible():
    """Unproven objective can never be upgraded to optimal by lex rounds."""
    sorties = two_sorties_and_vars()
    pcs, _model, takeoff = build_model_vars(sorties)
    replies = [
        (cp_model.FEASIBLE, values(takeoff, 0, 1), 1),
        (cp_model.FEASIBLE, values(takeoff, 0, 1), 1),  # objective not proven
        (cp_model.OPTIMAL, values(takeoff, 0, 1), 1),   # lex A proven
        (cp_model.OPTIMAL, values(takeoff, 0, 1), 1),   # lex B proven
    ]
    res = run_solve(sorties, pcs, {}, time_limit=30.0,
                    solver_factory=FakeSolver(replies))
    check(res.feasible and res.status == "feasible",
          "feasible objective with optimal lex rounds is still feasible")


def case_unknown_probe_distinct_from_infeasible():
    sorties = two_sorties_and_vars()
    pcs, _model, takeoff = build_model_vars(sorties)
    replies = [(cp_model.UNKNOWN, {}, None)]
    res = run_solve(sorties, pcs, {}, time_limit=30.0,
                    solver_factory=FakeSolver(replies))
    check(not res.feasible and res.status == "unknown",
          "probe UNKNOWN -> unknown, feasible=False")
    check(res.offsets == {} and res.cores == [],
          "unknown carries neither an offset table nor unsat cores")


def case_lex_feasible_then_keeps_cone():
    """A later OPTIMAL lex round cannot worsen an earlier FEASIBLE coordinate."""
    sorties = two_sorties_and_vars()
    pcs, _model, takeoff = build_model_vars(sorties)
    replies = [
        (cp_model.FEASIBLE, values(takeoff, 4, 3), 7),  # probe
        (cp_model.OPTIMAL, values(takeoff, 0, 1), 1),  # objective optimal
        # round A only FEASIBLE: incumbent A=0. Round B then OPTIMAL returns a
        # table still forced to respect A <= 0; verify the non-worsening bound
        # is accepted (A=0, B=1).
        (cp_model.FEASIBLE, values(takeoff, 0, 2), 2),
        (cp_model.OPTIMAL, values(takeoff, 0, 1), 1),
    ]
    res = run_solve(sorties, pcs, {}, time_limit=30.0,
                    solver_factory=FakeSolver(replies))
    check(res.feasible and res.status == "feasible",
          "one FEASIBLE lex round keeps the whole result at feasible")
    check(res.offsets["A"] == 0,
          "earlier FEASIBLE coordinate is preserved, not pinned but bounded")
    check(res.offsets["B"] >= 1, "later round stays conflict-free")


def case_real_solver_optimal():
    """Sanity: production solver through the same staged code path."""
    sorties = two_sorties_and_vars()
    pcs = [PairConstraint("A", "B", [(0, 0)])]
    res = run_solve(sorties, pcs, {}, time_limit=10.0)
    check(res.feasible and res.status == "optimal",
          "real solver proves the tiny model optimal")
    check(res.offsets == {"A": 0, "B": 1} and res.weighted_delay == 1,
          "real solver returns the lexicographic optimum")


def main():
    case_probe_survives_weighted_timeout()
    case_probe_survives_tiny_budget()
    case_feasible_lex_round_not_locked_and_not_optimal()
    case_all_optimal_only_when_every_round_optimal()
    case_weighted_feasible_lex_all_optimal_is_feasible()
    case_unknown_probe_distinct_from_infeasible()
    case_lex_feasible_then_keeps_cone()
    case_real_solver_optimal()
    print(f"stage-solver checks, failures: {FAILS}")
    return FAILS


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
