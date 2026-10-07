"""3D poly-line trajectory model (metres / relative seconds).

A :class:`Sortie` owns a fixed piecewise-linear trajectory sampled at strictly
increasing waypoint times.  A sortie may only be *shifted in time*: the whole
trajectory moves by an integer take-off offset ``d`` (seconds relative to its
earliest take-off time, which is always ``0`` by convention).

Position along a trajectory::

    P(t) = p_0 + V * t            for global (post-shift) trajectory time t

Waypoint times are used to locate which segment contains a given time, while
the constant per-segment velocity makes the position evaluation exact.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Waypoint:
    x: float
    y: float
    z: float
    t: int  # seconds from the (unshifted) take-off instant, >= 0, strictly incr.


@dataclass
class Segment:
    """A linear segment traversed for global (unshifted) trajectory time in [t0,t1)."""

    p0: tuple[float, float, float]
    v: tuple[float, float, float]
    t0: float
    t1: float

    @property
    def duration(self) -> float:
        return self.t1 - self.t0

    def position(self, t: float) -> tuple[float, float, float]:
        return (
            self.p0[0] + self.v[0] * (t - self.t0),
            self.p0[1] + self.v[1] * (t - self.t0),
            self.p0[2] + self.v[2] * (t - self.t0),
        )


@dataclass
class Sortie:
    id: str
    weight: int  # positive integer priority weight
    window_lo: int  # earliest take-off (relative seconds), always 0 on import
    window_hi: int  # latest take-off (integer seconds)
    waypoints: list[Waypoint]
    segments: list[Segment] = field(default_factory=list)

    @property
    def flight_end(self) -> float:
        return self.waypoints[-1].t

    def position(self, t: float) -> tuple[float, float, float]:
        """Position at global trajectory time ``t`` (NaN-ish behaviour outside range)."""
        seg = self._segment_at(t)
        return seg.position(t)

    def _segment_at(self, t: float) -> Segment:
        segs = self.segments
        lo, hi = 0, len(segs) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if t > segs[mid].t1:
                lo = mid + 1
            else:
                hi = mid
        return segs[lo]


def build_segments(waypoints: list[Waypoint]) -> list[Segment]:
    segs: list[Segment] = []
    for a, b in zip(waypoints, waypoints[1:]):
        dt = b.t - a.t  # > 0 guaranteed by validation
        v = ((b.x - a.x) / dt, (b.y - a.y) / dt, (b.z - a.z) / dt)
        segs.append(Segment((a.x, a.y, a.z), v, float(a.t), float(b.t)))
    return segs


def make_sortie(
    sortie_id: str,
    weight: int,
    window_hi: int,
    waypoints: list[Waypoint],
    window_lo: int = 0,
) -> Sortie:
    return Sortie(
        id=sortie_id,
        weight=weight,
        window_lo=window_lo,
        window_hi=window_hi,
        waypoints=list(waypoints),
        segments=build_segments(waypoints),
    )
