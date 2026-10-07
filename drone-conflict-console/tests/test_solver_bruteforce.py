"""End-to-end solver validation against brute-force enumeration."""

from __future__ import annotations

import itertools
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.model import Waypoint, make_sortie  # noqa: E402
from app.solver import build_pair_constraints, run_solve, scan_schedule  # noqa: E402


def random_sorties(rng, n, max_window=6, max_segs=3):
    out = []
    for i in range(n):
        nseg = rng.randint(1, max_segs)
        t = 0
        wps = []
        x = rng.uniform(-5, 5)
        y = rng.uniform(-5, 5)
        z = rng.uniform(0, 8)
        wps.append(Waypoint(x, y, z, 0))
        for _ in range(nseg):
            dt = rng.randint(1, 4)
            t += dt
            x += rng.uniform(-4, 4)
            y += rng.uniform(-4, 4)
            z += rng.uniform(-2, 2)
            z = max(0.5, z)
            wps.append(Waypoint(round(x, 3), round(y, 3), round(z, 3), t))
        wh = rng.randint(0, max_window)
        w = rng.randint(1, 5)
        out.append(make_sortie(f"S{i:02d}", w, wh, wps))
    return out


def brute_force(sorties, pcs, locks):
    ids = [s.id for s in sorties]
    domains = {s.id: list(range(s.window_lo, s.window_hi + 1)) for s in sorties}
    for k, v in locks.items():
        domains[k] = [v]
    lookup = {(pc.sortie_a, pc.sortie_b): pc.intervals for pc in pcs}
    best = None
    best_seq = None
    for combo in itertools.product(*[domains[i] for i in ids]):
        off = dict(zip(ids, combo))
        ok = True
        for (a, b), ivs in lookup.items():
            d = off[b] - off[a]
            if any(lo <= d <= hi for lo, hi in ivs):
                ok = False
                break
        if ok:
            obj = sum(next(s.weight for s in sorties if s.id == i) * off[i] for i in ids)
            seq = tuple(off[i] for i in sorted(ids))
            if best is None or (obj, seq) < (best, best_seq):
                best = obj
                best_seq = seq
    return best, best_seq


def main(seed=2026, cases=60):
    rng = random.Random(seed)
    fails = 0
    n_infeas = 0
    for case in range(cases):
        n = rng.randint(2, 5)
        sorties = random_sorties(rng, n, max_window=5)
        R = rng.choice([1.0, 2.0, 3.0])
        H = rng.choice([1.0, 2.0, 3.0])
        pcs = build_pair_constraints(sorties, R, H)
        locks = {}
        if rng.random() < 0.4:
            s = rng.choice(sorties)
            locks[s.id] = rng.randint(s.window_lo, s.window_hi)
        res = run_solve(sorties, pcs, locks, time_limit=10)
        bf_obj, bf_seq = brute_force(sorties, pcs, locks)
        if not res.feasible:
            n_infeas += 1
            if bf_obj is not None:
                print(f"case {case}: solver says INFEASIBLE but brute force has {bf_obj}")
                fails += 1
                continue
            # verify cores: each core must be unsat by itself AND minimal
            for core in res.cores:
                ids = set(core["sorties"])
                sub = [s for s in sorties if s.id in ids]
                sub_pc = [pc for pc in pcs if pc.sortie_a in ids and pc.sortie_b in ids]
                sub_locks = {k: v for k, v in locks.items() if k in ids}
                bobj, _ = brute_force(sub, sub_pc, sub_locks)
                if bobj is not None:
                    print(f"case {case}: reported core {sorted(ids)} is actually feasible!")
                    fails += 1
                for drop in ids:
                    ids2 = ids - {drop}
                    sub2 = [s for s in sorties if s.id in ids2]
                    pc2 = [pc for pc in pcs if pc.sortie_a in ids2 and pc.sortie_b in ids2]
                    lk2 = {k: v for k, v in locks.items() if k in ids2}
                    o2, _ = brute_force(sub2, pc2, lk2)
                    if o2 is None and len(ids2) >= 2:
                        print(f"case {case}: core {sorted(ids)} not minimal: removing "
                              f"{drop} still infeasible")
                        fails += 1
        else:
            if bf_obj is None:
                print(f"case {case}: solver feasible but brute force infeasible")
                fails += 1
                continue
            ids_sorted = sorted(s.id for s in sorties)
            seq = tuple(res.offsets[i] for i in ids_sorted)
            if res.weighted_delay != bf_obj or seq != bf_seq:
                print(f"case {case}: solver obj={res.weighted_delay} seq={seq} "
                      f"BF obj={bf_obj} seq={bf_seq}")
                fails += 1
            # verify solved schedule really has no conflicts
            rec = scan_schedule(sorties, res.offsets, R, H)
            if rec:
                print(f"case {case}: solved schedule still has {len(rec)} conflicts!")
                fails += 1
            # verify locks honored
            for k, v in locks.items():
                if res.offsets[k] != v:
                    print(f"case {case}: lock {k}={v} violated, got {res.offsets[k]}")
                    fails += 1
    print(f"solver cases: {cases}, infeasible: {n_infeas}, failures: {fails}")
    return fails


if __name__ == "__main__":
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 2026
    sys.exit(1 if main(seed=seed) else 0)
