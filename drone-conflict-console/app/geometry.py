"""Analytic conflict detection for two linearly-moving 3D trajectories.

Conflict rule (checked continuously over overlapping flight time, segment
endpoints included): the horizontal (XY) distance is strictly less than the
safety radius *and* the vertical (Z) distance is strictly less than the
safety height.

Two routines are provided:

* :func:`forbidden_offsets_pair` — for two segments, the exact set of integer
  relative take-off offsets ``d = t0_B - t0_A`` that create a conflict.
  Geometry is a convex (s, r) parameter space (s, r in [0, 1] are the local
  segment parameters) clipped by the vertical strips, then each polygon edge is
  intersected with the ellipsoid horizontal-distance region.  The feasible
  region is convex, so its projection on the d axis is a single interval.

* :func:`conflicts_fixed_schedule` — given concrete take-off times, every
  conflicting time interval is reported with a witness instant, for map and
  timeline rendering of the earliest-take-off scenario and verification of a
  solved schedule.

Floating point: inequalities are "strict" mathematically; a tolerance EPS is
used so that values within EPS of a boundary are treated as conflicting
(conservative, safe-side).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .model import Segment

EPS = 1e-7  # metres / seconds tolerance; boundary treated as conflict (safe side)


@dataclass
class ConflictRecord:
    sortie_a: str
    sortie_b: str
    time_start: float  # global clock (take-off A reference for analytic calls)
    time_end: float
    witness_time: float
    a_pos: tuple[float, float, float]
    b_pos: tuple[float, float, float]
    horizontal_distance: float
    vertical_distance: float


# ---------------------------------------------------------------------------
# small vector helpers
# ---------------------------------------------------------------------------


def _strict_inside_strip(value: float, bound: float) -> bool:
    return value < bound - EPS


# ---------------------------------------------------------------------------
# Half-plane clipping in (u, v) space
# ---------------------------------------------------------------------------
# Vertices carry a vstrict flag bookkeeping; strictness is in fact recomputed
# from coordinates at every candidate point, so the flag is only cosmetic.
#
# The polygon lives in (u, v) coordinates: u, v in [0, 1] are the fractions
# along segment A / segment B at the common global time.  The relative take-off
# offset d enters only through the constant term g.


def _vertical_diff_coeffs(seg_a: Segment, seg_b: Segment):
    """Z difference  dz(u, v) = c0 + cu*u + cv*v."""
    la = seg_a.duration
    lb = seg_b.duration
    c0 = seg_a.p0[2] - seg_b.p0[2]
    cu = la * seg_a.v[2]
    cv = -lb * seg_b.v[2]
    return c0, cu, cv


def _horizontal_sq_coeffs(seg_a: Segment, seg_b: Segment):
    """Horizontal squared distance h(u,v) = |g + alpha u - beta v|_xy^2.

    Returns constants ``g, alpha, beta`` (2-vectors) so callers may evaluate.
    """
    la = seg_a.duration
    lb = seg_b.duration
    g = (seg_a.p0[0] - seg_b.p0[0], seg_a.p0[1] - seg_b.p0[1])
    alpha = (la * seg_a.v[0], la * seg_a.v[1])
    beta = (lb * seg_b.v[0], lb * seg_b.v[1])
    return g, alpha, beta


def _eval_h(g, alpha, beta, u, v):
    dx = g[0] + alpha[0] * u - beta[0] * v
    dy = g[1] + alpha[1] * u - beta[1] * v
    return dx * dx + dy * dy


def _clip_polygon(poly, a, b, c):
    """Sutherland–Hodgman clip against half-plane  a*u + b*v + c <= EPS.

    Vertices carry a ``vstrict`` flag ("strictly inside both vertical strips")
    which is propagated: intersection points inherit the flag only when they
    lie strictly inside *this* line (they never do — they sit on it — so they
    inherit False; that is safe because strictness is recomputed per point).
    """
    if not poly:
        return poly
    out = []
    n = len(poly)
    for i in range(n):
        u1, v1, _ = poly[i - 1] if i > 0 else poly[n - 1]
        u2, v2, _ = poly[i]
        f1 = a * u1 + b * v1 + c
        f2 = a * u2 + b * v2 + c
        inside1 = f1 <= EPS
        inside2 = f2 <= EPS
        if inside2:
            if not inside1:
                denom = f1 - f2
                t = f1 / denom if abs(denom) > 1e-15 else 0.0
                t = min(1.0, max(0.0, t))
                uu = u1 + (u2 - u1) * t
                vv = v1 + (v2 - v1) * t
                out.append((uu, vv, False))
            out.append((u2, v2, poly[i][2]))
        elif inside1:
            denom = f1 - f2
            t = f1 / denom if abs(denom) > 1e-15 else 0.0
            t = min(1.0, max(0.0, t))
            uu = u1 + (u2 - u1) * t
            vv = v1 + (v2 - v1) * t
            out.append((uu, vv, False))
    return out


def _dedup(poly):
    out = []
    for p in poly:
        if not out or abs(p[0] - out[-1][0]) > 1e-10 or abs(p[1] - out[-1][1]) > 1e-10:
            out.append(p)
    if len(out) >= 2 and abs(out[0][0] - out[-1][0]) <= 1e-10 and abs(out[0][1] - out[-1][1]) <= 1e-10:
        out.pop()
    return out


def _polygon_signed_area2(poly) -> float:
    s = 0.0
    n = len(poly)
    for i in range(n):
        u1, v1, _ = poly[i]
        u2, v2, _ = poly[(i + 1) % n]
        s += u1 * v2 - u2 * v1
    return s


def _point_in_polygon(poly, u: float, v: float) -> bool:
    """Inside test for the convex polygon from Sutherland–Hodgman.

    Orientation (CW/CCW) depends on the order of clip planes; it is detected
    from the signed area instead of being assumed.
    """
    if len(poly) < 3:
        return False
    orient = 1.0 if _polygon_signed_area2(poly) >= 0 else -1.0
    n = len(poly)
    for i in range(n):
        u1, v1, _ = poly[i]
        u2, v2, _ = poly[(i + 1) % n]
        cross = (u2 - u1) * (v - v1) - (v2 - v1) * (u - u1)
        if orient * cross < -1e-8:
            return False
    return True


# ---------------------------------------------------------------------------
# Forbidden integer offsets between two segments
# ---------------------------------------------------------------------------


def _polygon_vertical_strict(zc0, zcu, zcv, H, u, v) -> bool:
    """Strict vertical strip interior: |dz| < H (with EPS safe side)."""
    dz = zc0 + zcu * u + zcv * v
    return abs(dz) < H - EPS


def _edge_quadratic_roots(a: float, b: float, c: float) -> list[float]:
    """Real roots of a q^2 + b q + c = 0 (a may be ~0)."""
    if abs(a) <= 1e-14:
        if abs(b) <= 1e-14:
            return []
        return [-c / b]
    disc = b * b - 4 * a * c
    if disc < -EPS:
        return []
    disc = max(disc, 0.0)
    sq = math.sqrt(disc)
    return [(-b - sq) / (2 * a), (-b + sq) / (2 * a)]


def _line_ellipse_intersections(g, alpha, beta, R2, a, b, c):
    """Intersections of line a*u + b*v + c = 0 with ellipsoid h(u,v) = R2.

    h = |g + alpha u - beta v|^2.  Parametrize the line as
    P = P0 + t * e where e is a unit direction of the line, then solve the
    resulting quadratic in t.
    """
    if abs(a) >= abs(b) and abs(a) > 1e-14:
        # point on line with v = 0: u = -c/a
        p = (-c / a, 0.0)
    elif abs(b) > 1e-14:
        p = (0.0, -c / b)
    else:
        return []
    norm = math.hypot(a, b)
    e = (b / norm, -a / norm)
    dx0 = g[0] + alpha[0] * p[0] - beta[0] * p[1]
    dy0 = g[1] + alpha[1] * p[0] - beta[1] * p[1]
    wx = alpha[0] * e[0] - beta[0] * e[1]
    wy = alpha[1] * e[0] - beta[1] * e[1]
    A = wx * wx + wy * wy
    B = 2 * (dx0 * wx + dy0 * wy)
    C = dx0 * dx0 + dy0 * dy0 - R2
    roots = _edge_quadratic_roots(A, B, C)
    return [(p[0] + e[0] * t, p[1] + e[1] * t) for t in roots]


def _edge_h_roots(g, alpha, beta, H, u1, v1, u2, v2):
    """Sub-intervals of polygon edge parameter q in [0,1] where h < H.

    Along the edge u(q) = u1 + du*q, v(q) = v1 + dv*q, the horizontal
    difference D(q) is affine in q, so h(q) = |D(q)|^2 is quadratic.
    """
    d0x = g[0] + alpha[0] * u1 - beta[0] * v1
    d0y = g[1] + alpha[1] * u1 - beta[1] * v1
    du = u2 - u1
    dv = v2 - v1
    ax = alpha[0] * du - beta[0] * dv
    ay = alpha[1] * du - beta[1] * dv
    # |D(q)|^2 = c0 + c1 q + c2 q^2 + c3 q^3 + c4 q^4
    c0 = d0x * d0x + d0y * d0y
    c1 = 2 * (d0x * ax + d0y * ay)
    c2 = ax * ax + ay * ay
    # D is affine in q (u,v linear in q), so h is actually quadratic.
    # (alpha u(q) - beta v(q) is affine: alpha*(u1+du q) - beta*(v1+dv q).)
    assert True
    # roots of c2 q^2 + c1 q + (c0 - H) = 0
    roots = [0.0, 1.0]
    disc = c1 * c1 - 4 * c2 * (c0 - H)
    if c2 > 1e-14:
        if disc >= -EPS:
            disc = max(disc, 0.0)
            sq = math.sqrt(disc)
            roots.append((-c1 - sq) / (2 * c2))
            roots.append((-c1 + sq) / (2 * c2))
    else:
        # nearly constant / linear
        if abs(c1) > 1e-14:
            roots.append((H - c0) / c1)
        # if c0 < H the whole edge qualifies (handled by value tests below)
    roots = sorted(r for r in roots if -1e-9 <= r <= 1 + 1e-9)
    clamped = []
    for r in roots:
        r = min(1.0, max(0.0, r))
        if not clamped or abs(r - clamped[-1]) > 1e-9:
            clamped.append(r)
    # decide sign on each sub-interval via midpoint
    intervals = []
    for a, b in zip(clamped, clamped[1:]):
        if b - a <= 1e-11:
            continue
        qm = 0.5 * (a + b)
        hm = c0 + c1 * qm + c2 * qm * qm
        if hm < H - EPS or abs(hm - H) <= EPS:
            intervals.append((a, b))
    return intervals, (c0, c1, c2)


def forbidden_offset_interval_segments(
    seg_a: Segment,
    seg_b: Segment,
    safety_radius: float,
    safety_height: float,
) -> tuple[float, float, bool, bool] | None:
    """Real-valued interval of d = t0_B - t0_A offsets causing a conflict.

    Returns ``(d_lo, d_hi, lo_closed, hi_closed)`` or ``None`` when the two
    segments can never conflict for any offset.

    Geometry (u, v in [0,1], fraction along each segment at the same global
    time)::

        delta(u, v) = g + la*u*VA - lb*v*VB          (g includes offset d)
        conflict iff |delta_xy| < R and |delta_z| < H
    """
    la = seg_a.duration
    lb = seg_b.duration
    R2 = safety_radius * safety_radius

    zc0, zcu, zcv = _vertical_diff_coeffs(seg_a, seg_b)
    H = safety_height

    # Start from unit square, clip with the two vertical strip inequalities:
    #   -H < zc0 + zcu u + zcv v < H   (strict; EPS applied inside clip).
    poly = [(0.0, 0.0, False), (1.0, 0.0, False), (1.0, 1.0, False), (0.0, 1.0, False)]
    poly = _clip_polygon(poly, zcu, zcv, zc0 - H)        # dz <= H
    poly = _clip_polygon(poly, -zcu, -zcv, -zc0 - H)     # -dz <= H  (dz >= -H)
    poly = _dedup(poly)
    if len(poly) < 3:
        # Degenerate (zero-area) polygon: still test its vertices below.
        if not poly:
            return None

    g, alpha, beta = _horizontal_sq_coeffs(seg_a, seg_b)

    # d(u, v): offset implied by aligning global time at these segment points:
    #   t0a + la*u = d + t0b + lb*v  =>  d = la*u - lb*v + (t0a - t0b)
    t0a, t0b = seg_a.t0, seg_b.t0
    da, db = la, -lb
    d_const = t0a - t0b

    candidates: list[tuple[float, bool]] = []  # (d, attainable-with-strict-conflict)

    def add_candidate(u, v):
        # vertical strip strictness at the point itself
        vstrict = _polygon_vertical_strict(zc0, zcu, zcv, H, u, v)
        h = _eval_h(g, alpha, beta, u, v)
        hstrict = h < R2 - EPS
        if hstrict and vstrict:
            candidates.append((da * u + db * v + d_const, True))
        elif h <= R2 + EPS and abs(zc0 + zcu * u + zcv * v) <= H + EPS:
            candidates.append((da * u + db * v + d_const, False))

    # 1) polygon vertices.  A vertex on a strip boundary (|dz| = H) with
    #    h < R2 still yields d as a strict limit when the polygon interior
    #    next to the vertex enters the strict strip; test that explicitly.
    centroid = (sum(p[0] for p in poly) / len(poly),
                sum(p[1] for p in poly) / len(poly)) if len(poly) >= 3 else None
    for u, v, _ in poly:
        add_candidate(u, v)
        if centroid is not None:
            hh = _eval_h(g, alpha, beta, u, v)
            if hh < R2 - EPS:
                for step in (1e-4, 1e-5, 1e-6):
                    nu = u + (centroid[0] - u) * step
                    nv = v + (centroid[1] - v) * step
                    if _polygon_vertical_strict(zc0, zcu, zcv, H, nu, nv) and \
                            _eval_h(g, alpha, beta, nu, nv) < R2 - EPS:
                        candidates.append((da * u + db * v + d_const, True))
                        break

    # 1b) The feasible (u,v) domain is the vertical polygon INTERSECTED with
    #     the unit square.  Its boundary therefore includes square edges that
    #     can lie in the polygon interior; scan all four square edges too.
    square_edges = [(0.0, 0.0, 1.0, 0.0),
                    (1.0, 0.0, 1.0, 1.0),
                    (1.0, 1.0, 0.0, 1.0),
                    (0.0, 1.0, 0.0, 0.0)]
    for u1, v1, u2, v2 in square_edges:
        intervals, coefs = _edge_h_roots(g, alpha, beta, R2, u1, v1, u2, v2)
        c0, c1, c2 = coefs
        for qa, qb in intervals:
            for q in (qa, qb):
                uu = u1 + (u2 - u1) * q
                vv = v1 + (v2 - v1) * q
                # On a square edge this is a true domain boundary: a point
                # with h = R2 strictly inside the vertical strip is a strict
                # d-limit (approach from inside the square).
                if _polygon_vertical_strict(zc0, zcu, zcv, H, uu, vv) and \
                        _point_in_polygon(poly, uu, vv):
                    candidates.append((da * uu + db * vv + d_const, True))
                else:
                    add_candidate(uu, vv)
            # Interior points of the h < R2 sub-interval that are strict in
            # both senses (these dominate the interval's d span directly).
            qm = 0.5 * (qa + qb)
            um = u1 + (u2 - u1) * qm
            vm = v1 + (v2 - v1) * qm
            hm = c0 + c1 * qm + c2 * qm * qm
            if hm < R2 - EPS and _polygon_vertical_strict(zc0, zcu, zcv, H, um, vm) \
                    and _point_in_polygon(poly, um, vm):
                candidates.append((da * um + db * vm + d_const, True))
        # Also collect raw h = R2 roots (the interval endpoints may coincide
        # with square corners already; interior roots split the edge).
        for q in _edge_quadratic_roots(c2, c1, c0 - R2):
            if 1e-9 < q < 1 - 1e-9:
                uu = u1 + (u2 - u1) * q
                vv = v1 + (v2 - v1) * q
                if _polygon_vertical_strict(zc0, zcu, zcv, H, uu, vv) and \
                        _point_in_polygon(poly, uu, vv):
                    candidates.append((da * uu + db * vv + d_const, True))

    # 2) polygon edges: h < R2 sub-intervals with a strict strip neighbour.
    n = len(poly)
    for i in range(n):
        u1, v1, _ = poly[i]
        u2, v2, _ = poly[(i + 1) % n]
        intervals, coefs = _edge_h_roots(g, alpha, beta, R2, u1, v1, u2, v2)
        c0, c1, c2 = coefs
        for qa, qb in intervals:
            for q in (qa, qb):
                uu = u1 + (u2 - u1) * q
                vv = v1 + (v2 - v1) * q
                add_candidate(uu, vv)
            qm = 0.5 * (qa + qb)
            um = u1 + (u2 - u1) * qm
            vm = v1 + (v2 - v1) * qm
            hm = c0 + c1 * qm + c2 * qm * qm
            if hm < R2 - EPS and _polygon_vertical_strict(zc0, zcu, zcv, H, um, vm):
                candidates.append((da * um + db * vm + d_const, True))

    # 2b) Intersections of the two strip-boundary lines dz = +/-H with the
    #     ellipsoid h = R2, restricted to the unit square.  These are the
    #     "lens" extremities: the strict feasible region (|dz| < H, h < R2)
    #     approaches them from the polygon interior, so d is a strict limit.
    for hh in (H, -H):
        for uu, vv in _line_ellipse_intersections(g, alpha, beta, R2,
                                                  zcu, zcv, zc0 - hh):
            if -EPS <= uu <= 1 + EPS and -EPS <= vv <= 1 + EPS and \
                    _point_in_polygon(poly, uu, vv):
                candidates.append((da * uu + db * vv + d_const, True))

    # 3) Interior tangency points of the ellipsoid along the d direction.
    #    When the disk {(u,v): h <= R2} lies (partly) inside the vertical
    #    polygon, the extrema of d over the strict feasible region may occur at
    #    a boundary point of the disk whose normal is parallel to grad d.
    #    h = |g + alpha u - beta v|^2.  With D = [alpha -beta] (2x2 columns)
    #    and q = [u, v],  D^T D (q - q0) = +/- R/|w| * w  where
    #    w = (da, db) = (la, -lb) and q0 = centre of the ellipsoid.
    a11 = alpha[0] * alpha[0] + alpha[1] * alpha[1]
    a12 = -(alpha[0] * beta[0] + alpha[1] * beta[1])
    a22 = beta[0] * beta[0] + beta[1] * beta[1]
    det = a11 * a22 - a12 * a12
    # q0 solves D(q - q0) = 0: alpha u - beta v = -g.  Solve the 2x2 system
    # a11 u + a12 v = -alpha.g ;  a12 u + a22 v = beta.g
    rhs1 = -(alpha[0] * g[0] + alpha[1] * g[1])
    rhs2 = beta[0] * g[0] + beta[1] * g[1]
    if det > 1e-12:
        u0 = (rhs1 * a22 - a12 * rhs2) / det
        v0 = (a11 * rhs2 - a12 * rhs1) / det
        wlen2 = da * da + db * db
        if wlen2 > 1e-12:
            # y = A^{-1} w ; tangency displacement x = +/- R y / sqrt(w.y)
            y1 = (a22 * da - a12 * db) / det
            y2 = (-a12 * da + a11 * db) / det
            denom2 = da * y1 + db * y2
            if denom2 > 1e-12:
                scale = safety_radius / math.sqrt(denom2)
                for sign in (1.0, -1.0):
                    uu = u0 + sign * scale * y1
                    vv = v0 + sign * scale * y2
                    # Tangency point on h = R2.  When it lies strictly inside
                    # the vertical strip and the square/polygon, d is the
                    # strict supremum/infimum of d over the open disk
                    # h < R2 (approach along the interior normal), so it is an
                    # attainable strict limit even though h == R2 itself.
                    if -EPS <= uu <= 1 + EPS and -EPS <= vv <= 1 + EPS and \
                            _polygon_vertical_strict(zc0, zcu, zcv, H, uu, vv) and \
                            _point_in_polygon(poly, uu, vv):
                        candidates.append((da * uu + db * vv + d_const, True))

    strict_candidates = [c for c in candidates if c[1]]
    if not strict_candidates:
        # Only boundary contacts (|dz| == H or h == R2) exist; under strict
        # inequalities these are not conflicts.
        return None

    d_lo = min(c[0] for c in strict_candidates)
    d_hi = max(c[0] for c in strict_candidates)
    lo_closed = any(abs(c[0] - d_lo) <= 1e-7 for c in strict_candidates)
    hi_closed = any(abs(c[0] - d_hi) <= 1e-7 for c in strict_candidates)
    return d_lo, d_hi, lo_closed, hi_closed


def integer_forbidden_interval(real_interval) -> tuple[int, int] | None:
    """Convert the real (closed/open) d interval to the contained integer set."""
    d_lo, d_hi, lo_closed, hi_closed = real_interval
    import math as _m

    if lo_closed:
        lo = int(_m.ceil(d_lo - 1e-7))
    else:
        lo = int(_m.floor(d_lo + 1e-7)) + 1
    if hi_closed:
        hi = int(_m.floor(d_hi + 1e-7))
    else:
        hi = int(_m.ceil(d_hi - 1e-7)) - 1
    if lo > hi:
        return None
    return lo, hi


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Union of inclusive integer intervals, merging overlaps/adjacencies."""
    if not intervals:
        return []
    intervals = sorted(intervals)
    merged = [list(intervals[0])]
    for lo, hi in intervals[1:]:
        if lo <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [(a, b) for a, b in merged]


def segment_time_envelope(seg_a: Segment, seg_b: Segment) -> tuple[float, float]:
    """Range of d = t0B - t0A for which segment time windows overlap at all."""
    # overlap of [t0a, t1a] with [d + t0b, d + t1b]
    return seg_a.t0 - seg_b.t1, seg_a.t1 - seg_b.t0



# ---------------------------------------------------------------------------
# Fixed-schedule conflict detection (continuous time, endpoints included)
# ---------------------------------------------------------------------------


def _quadratic_roots(a: float, b: float, c: float) -> list[float]:
    if abs(a) <= 1e-14:
        if abs(b) <= 1e-14:
            return []
        return [-c / b]
    disc = b * b - 4 * a * c
    if disc < -EPS:
        return []
    disc = max(disc, 0.0)
    sq = math.sqrt(disc)
    return [(-b - sq) / (2 * a), (-b + sq) / (2 * a)]


def conflicts_between_sorties(
    sortie_a,
    takeoff_a,
    sortie_b,
    takeoff_b,
    safety_radius: float,
    safety_height: float,
) -> list[ConflictRecord]:
    """Conflicting global-time intervals for two concretely-scheduled sorties.

    Both segment interiors and every segment endpoint are checked: the overlap
    window is partitioned at all safety-boundary crossings, each open cell is
    classified by its midpoint and every boundary instant is tested on its
    own.
    """
    R2 = safety_radius * safety_radius
    H = safety_height
    records: list[ConflictRecord] = []

    for seg_a0 in sortie_a.segments:
        a0 = seg_a0.t0 + takeoff_a
        a1 = seg_a0.t1 + takeoff_a
        for seg_b0 in sortie_b.segments:
            b0 = seg_b0.t0 + takeoff_b
            b1 = seg_b0.t1 + takeoff_b
            lo = max(a0, b0)
            hi = min(a1, b1)
            if lo > hi + EPS:
                continue
            L = hi - lo

            # Position difference as affine function of tau = T - lo in [0, L]:
            #   P_A(T) - P_B(T) = d0 + w * tau
            dax = seg_a0.p0[0] - seg_a0.v[0] * (a0 - lo)
            day = seg_a0.p0[1] - seg_a0.v[1] * (a0 - lo)
            daz = seg_a0.p0[2] - seg_a0.v[2] * (a0 - lo)
            dbx = seg_b0.p0[0] - seg_b0.v[0] * (b0 - lo)
            dby = seg_b0.p0[1] - seg_b0.v[1] * (b0 - lo)
            dbz = seg_b0.p0[2] - seg_b0.v[2] * (b0 - lo)
            dx0, dy0, dz0 = dax - dbx, day - dby, daz - dbz
            wx = seg_a0.v[0] - seg_b0.v[0]
            wy = seg_a0.v[1] - seg_b0.v[1]
            wz = seg_a0.v[2] - seg_b0.v[2]

            def h_bad(tau):
                dx = dx0 + wx * tau
                dy = dy0 + wy * tau
                return dx * dx + dy * dy < R2 - EPS

            def v_bad(tau):
                return abs(dz0 + wz * tau) < H - EPS

            def bad(tau):
                return h_bad(tau) and v_bad(tau)

            cuts = {0.0, L}
            # vertical boundary crossings |dz| = H
            for val in (H - dz0, -H - dz0):
                if abs(wz) > 1e-14:
                    cuts.add(val / wz)
            # horizontal boundary crossings h = R2
            for r in _quadratic_roots(
                wx * wx + wy * wy,
                2 * (dx0 * wx + dy0 * wy),
                dx0 * dx0 + dy0 * dy0 - R2,
            ):
                cuts.add(r)
            cuts = sorted(min(L, max(0.0, t)) for t in cuts)
            cuts = [t for t in cuts if -EPS <= t <= L + EPS]
            # dedup
            uniq = []
            for t in cuts:
                if not uniq or abs(t - uniq[-1]) > 1e-9:
                    uniq.append(t)
            cuts = uniq

            for i, (p, q) in enumerate(zip(cuts, cuts[1:])):
                if q - p <= 1e-9:
                    continue
                if bad(0.5 * (p + q)):
                    records.append(_make_record(sortie_a, sortie_b, seg_a0, seg_b0,
                                                a0, b0, lo, p, q,
                                                dx0, dy0, dz0, wx, wy, wz))
            for t in cuts:
                if bad(t):
                    inside_existing = any(
                        r.sortie_a == sortie_a.id and r.sortie_b == sortie_b.id
                        and r.time_start - EPS <= lo + t <= r.time_end + EPS
                        for r in records
                    )
                    if not inside_existing:
                        records.append(_make_record(sortie_a, sortie_b, seg_a0, seg_b0,
                                                    a0, b0, lo, t, t,
                                                    dx0, dy0, dz0, wx, wy, wz))

    return _merge_records(records)


def _make_record(sortie_a, sortie_b, seg_a, seg_b, a0, b0, lo, p, q,
                 dx0, dy0, dz0, wx, wy, wz):
    tm = 0.5 * (p + q)
    T = lo + tm
    pa = (
        seg_a.p0[0] + seg_a.v[0] * (T - a0),
        seg_a.p0[1] + seg_a.v[1] * (T - a0),
        seg_a.p0[2] + seg_a.v[2] * (T - a0),
    )
    pb = (
        seg_b.p0[0] + seg_b.v[0] * (T - b0),
        seg_b.p0[1] + seg_b.v[1] * (T - b0),
        seg_b.p0[2] + seg_b.v[2] * (T - b0),
    )
    ddx = dx0 + wx * tm
    ddy = dy0 + wy * tm
    ddz = dz0 + wz * tm
    return ConflictRecord(
        sortie_a=sortie_a.id,
        sortie_b=sortie_b.id,
        time_start=lo + p,
        time_end=lo + q,
        witness_time=T,
        a_pos=pa,
        b_pos=pb,
        horizontal_distance=math.hypot(ddx, ddy),
        vertical_distance=abs(ddz),
    )


def _merge_records(records: list[ConflictRecord]) -> list[ConflictRecord]:
    records.sort(key=lambda r: (r.sortie_a, r.sortie_b, r.time_start))
    out: list[ConflictRecord] = []
    for rec in records:
        if out and (
            rec.sortie_a == out[-1].sortie_a
            and rec.sortie_b == out[-1].sortie_b
            and rec.time_start <= out[-1].time_end + 1e-6
        ):
            out[-1].time_end = max(out[-1].time_end, rec.time_end)
        else:
            out.append(rec)
    return out
