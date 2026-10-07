"""Cross-validation of analytic conflict geometry.

Two fully independent implementations are compared against dense sampling:

* method A: (u, v)-polygon clipping / ellipsoid-edge intersection
            (app.geometry.forbidden_offset_interval_segments)
* method B: fixed-schedule scan over integer offsets
            (app.geometry.conflicts_between_sorties)
* method C: brute-force dense temporal sampling (independent of A and B)
"""

from __future__ import annotations

import itertools
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.geometry import (  # noqa: E402
    EPS,
    conflicts_between_sorties,
    forbidden_offset_interval_segments,
    integer_forbidden_interval,
    merge_intervals,
)
from app.model import Segment, Waypoint, build_segments, make_sortie  # noqa: E402


def dense_forbidden(seg_a: Segment, seg_b: Segment, R: float, H: float,
                    offsets, dt: float = 0.005) -> set[int]:
    """Independent dense-sampling oracle for forbidden integer offsets."""
    out = set()
    for d in offsets:
        lo = max(seg_a.t0, d + seg_b.t0)
        hi = min(seg_a.t1, d + seg_b.t1)
        if lo > hi + 1e-9:
            continue
        n = max(2, int(math.ceil((hi - lo) / dt)) + 1)
        bad = False
        for k in range(n + 1):
            t = lo + (hi - lo) * k / n
            pa = seg_a.position(t)
            pb = seg_b.position(t - d)
            if math.hypot(pa[0] - pb[0], pa[1] - pb[1]) < R - EPS and abs(pa[2] - pb[2]) < H - EPS:
                bad = True
                break
        if bad:
            out.add(int(d))
    return out


def random_segment(rng: random.Random, dur_range=(1.0, 6.0), pos_range=(-8.0, 8.0),
                   vel_range=(-4.0, 4.0)) -> Segment:
    dur = rng.uniform(*dur_range)
    p0 = tuple(rng.uniform(*pos_range) for _ in range(3))
    v = tuple(rng.uniform(*vel_range) for _ in range(3))
    t0 = rng.uniform(0.0, 3.0)
    return Segment(p0, v, t0, t0 + dur)


def method_a_set(seg_a, seg_b, R, H, offsets):
    interval = forbidden_offset_interval_segments(seg_a, seg_b, R, H)
    if interval is None:
        return set()
    ii = integer_forbidden_interval(interval)
    if ii is None:
        return set()
    lo, hi = merge_intervals([ii])[0]
    return {d for d in offsets if lo <= d <= hi}


def method_b_set(seg_a, seg_b, R, H, offsets):
    sa = make_sortie("A", 1, 100, [Waypoint(0, 0, 0, int(seg_a.t0))] * 1)  # replaced below
    # Build single-segment sorties directly.
    sa = _single_segment_sortie("A", seg_a)
    sb = _single_segment_sortie("B", seg_b)
    bad = set()
    for d in offsets:
        recs = conflicts_between_sorties(sa, 0, sb, d, R, H)
        if recs:
            bad.add(int(d))
    return bad


def _single_segment_sortie(sid, seg):
    wp = [
        Waypoint(seg.p0[0], seg.p0[1], seg.p0[2], int(round(seg.t0))),
        Waypoint(
            seg.p0[0] + seg.v[0] * seg.duration,
            seg.p0[1] + seg.v[1] * seg.duration,
            seg.p0[2] + seg.v[2] * seg.duration,
            int(round(seg.t1)),
        ),
    ]
    # times must be strictly increasing integers; emulate by rebuilding segs
    s = make_sortie(sid, 1, 100, wp)
    # override the segment with the exact float one (same relative base)
    base = seg.t0 - int(round(seg.t0))
    s.segments = [Segment(seg.p0, seg.v, seg.t0, seg.t1)]
    return s


def run(trials: int = 400, seed: int = 7):
    rng = random.Random(seed)
    failures = 0
    for trial in range(trials):
        sa = random_segment(rng)
        sb = random_segment(rng)
        R = rng.choice([0.5, 1.0, 2.0, 3.0, 5.0])
        H = rng.choice([0.5, 1.0, 2.0, 3.0, 5.0])
        d_lo = int(math.floor(sa.t0 - sb.t1)) - 1
        d_hi = int(math.ceil(sa.t1 - sb.t0)) + 1
        offsets = list(range(d_lo, d_hi + 1))
        if not offsets:
            continue
        oracle = dense_forbidden(sa, sb, R, H, offsets)
        a_set = method_a_set(sa, sb, R, H, offsets)
        b_set = method_b_set(sa, sb, R, H, offsets)
        if a_set != oracle or b_set != oracle:
            failures += 1
            if failures <= 10:
                print(f"--- mismatch trial {trial} R={R} H={H}")
                print(" segA", sa)
                print(" segB", sb)
                print(" oracle", sorted(oracle))
                print(" A     ", sorted(a_set), "missing", sorted(oracle - a_set), "extra", sorted(a_set - oracle))
                print(" B     ", sorted(b_set), "missing", sorted(oracle - b_set), "extra", sorted(b_set - oracle))
    print(f"random segment trials: {trials}, failures: {failures}")
    return failures


def grazing_trials():
    """Deliberately engineer tangential/boundary cases."""
    rng = random.Random(123)
    failures = 0
    # Two aircraft moving parallel at exactly R apart -> never conflict (strict)
    seg_a = Segment((0, 0, 0), (1, 0, 0), 0, 5)
    seg_b = Segment((0, 10, 0), (1, 0, 0), 0, 5)
    offsets = range(-6, 7)
    R, H = 10.0, 5.0
    o = dense_forbidden(seg_a, seg_b, R, H, offsets)
    a = method_a_set(seg_a, seg_b, R, H, offsets)
    b = method_b_set(seg_a, seg_b, R, H, offsets)
    # with strict inequality and EPS=1e-7, distance exactly 10 => no conflict
    print("grazing parallel (dist exactly R): oracle", sorted(o), "A", sorted(a), "B", sorted(b))

    # crossing paths at fixed point at exact time for one integer offset
    seg_a = Segment((0, 0, 0), (2, 0, 0), 0, 5)
    seg_b = Segment((10, 0, 0), (-2, 0, 0), 0, 5)
    R, H = 0.5, 3.0
    offsets = range(-6, 7)
    o = dense_forbidden(seg_a, seg_b, R, H, offsets)
    a = method_a_set(seg_a, seg_b, R, H, offsets)
    b = method_b_set(seg_a, seg_b, R, H, offsets)
    print("head-on crossing: oracle", sorted(o), "A", sorted(a), "B", sorted(b))
    if o != a or o != b:
        failures += 1

    # vertical separation exactly H -> no conflict
    seg_a = Segment((0, 0, 0), (1, 0, 0), 0, 5)
    seg_b = Segment((0, 0, 5), (1, 0, 0), 0, 5)
    R, H = 10.0, 5.0
    o = dense_forbidden(seg_a, seg_b, R, H, offsets)
    a = method_a_set(seg_a, seg_b, R, H, offsets)
    b = method_b_set(seg_a, seg_b, R, H, offsets)
    print("vertical exactly H: oracle", sorted(o), "A", sorted(a), "B", sorted(b))
    if o != a or o != b:
        failures += 1
    print("grazing failures:", failures)
    return failures


if __name__ == "__main__":
    f1 = run()
    f2 = grazing_trials()
    sys.exit(1 if f1 + f2 else 0)
