"""Sketches: explicit parametric coordinates, no constraint solver.

Every point is computed directly from parameters. There is no solver, no
degrees-of-freedom analysis, and no "sketch is over-constrained" state — which
also means no solver branch can flip between rebuilds and silently mirror a
profile.

The trade is that the sheet does the trigonometry. For a table-driven,
keyboard-first workflow that is the intended direction: a point's position is
readable as a formula rather than as the output of a numerical solve.

Curve ids are load-bearing. Each one is the root of the tag for every face swept
from it, so renaming a curve renames faces — which is why curves are named
rather than indexed.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .errors import DocumentError, DuplicateIdError, UnknownReferenceError
from .math3d import Frame, Vec2, Vec3
from .parameters import ResolvedParameters
from .values import Value, dependencies_of_many, resolve, resolve_vec2

#: An arc's two endpoints must agree on the radius to within this (mm).
_ARC_RADIUS_TOL = 1e-6


class CurveKinds:
    LINE = "line"
    ARC = "arc"
    CIRCLE = "circle"


@dataclass(frozen=True)
class SketchPoint:
    id: str
    at: tuple[Value, Value]

    def resolve(self, parameters: ResolvedParameters, sketch: str) -> Vec2:
        return resolve_vec2(self.at, parameters, where=f"sketches.{sketch}.points.{self.id}")


@dataclass(frozen=True)
class SketchCurve:
    """One curve of a sketch. ``id`` becomes part of every tag derived from it.

    Arcs reference named points exactly as lines do — ``start``, ``end`` and
    ``center`` — so a loop closes by construction rather than by the author
    computing endpoints that happen to meet. The radius is derived from the
    centre, and the two ends must agree on it; a mismatch is reported rather
    than quietly averaged.

    A circle is closed on its own, so it takes a centre and an explicit radius
    and forms a loop by itself.
    """

    id: str
    type: str = CurveKinds.LINE
    start: str = ""
    end: str = ""
    center: str = ""
    radius: Value = 0.0
    #: Arc sweep direction. Explicit, like every other direction in the system.
    clockwise: bool = False

    def __post_init__(self) -> None:
        if not self.id.isidentifier():
            raise DocumentError(reason=f"curve id {self.id!r} must be an identifier")
        if self.type not in (CurveKinds.LINE, CurveKinds.ARC, CurveKinds.CIRCLE):
            raise DocumentError(
                reason=(
                    f"curve '{self.id}' has unknown type {self.type!r}; "
                    f"expected one of line, arc, circle"
                )
            )
        required = {
            CurveKinds.LINE: ("start", "end"),
            CurveKinds.ARC: ("start", "end", "center"),
            CurveKinds.CIRCLE: ("center",),
        }[self.type]
        missing = [field for field in required if not getattr(self, field)]
        if missing:
            raise DocumentError(
                reason=(
                    f"{self.type} '{self.id}' is missing {', '.join(missing)}"
                )
            )
        if self.type == CurveKinds.CIRCLE and (self.start or self.end):
            raise DocumentError(
                reason=(
                    f"circle '{self.id}' is closed already, so it must not have "
                    "start or end points"
                )
            )

    @property
    def is_closed(self) -> bool:
        """True when this curve forms a loop on its own."""
        return self.type == CurveKinds.CIRCLE

    def point_refs(self) -> tuple[str, ...]:
        return tuple(r for r in (self.start, self.end, self.center) if r)


@dataclass(frozen=True)
class ResolvedCurve:
    """A sketch curve with every value evaluated, ready for the kernel."""

    curve: SketchCurve
    start: Vec2 | None = None
    end: Vec2 | None = None
    center: Vec2 | None = None
    radius: float = 0.0

    @property
    def id(self) -> str:
        return self.curve.id

    @property
    def type(self) -> str:
        return self.curve.type

    @property
    def clockwise(self) -> bool:
        return self.curve.clockwise

    def polyline(self, frame: Frame, tolerance: float = 0.2) -> list[Vec3]:
        """This curve as world-space points, ready to draw.

        Straight segments need two points; curved ones are sampled finely
        enough that the chord error stays under ``tolerance``, so a small hole
        does not render as a triangle.
        """
        if self.type == CurveKinds.LINE:
            assert self.start is not None and self.end is not None
            return [frame.point_at(self.start), frame.point_at(self.end)]

        assert self.center is not None
        segments = _arc_segments(self.radius, tolerance)

        if self.type == CurveKinds.CIRCLE:
            start_angle, sweep = 0.0, 2 * math.pi
        else:
            assert self.start is not None and self.end is not None
            start_angle = _angle_of(self.center, self.start)
            end_angle = _angle_of(self.center, self.end)
            sweep = end_angle - start_angle
            # Normalise into the direction the document asked for, so a
            # clockwise arc is not drawn the long way round.
            if self.clockwise and sweep > 0:
                sweep -= 2 * math.pi
            elif not self.clockwise and sweep < 0:
                sweep += 2 * math.pi

        points: list[Vec3] = []
        for step in range(segments + 1):
            angle = start_angle + sweep * (step / segments)
            points.append(
                frame.point_at(
                    Vec2(
                        self.center.x + self.radius * math.cos(angle),
                        self.center.y + self.radius * math.sin(angle),
                    )
                )
            )
        return points


@dataclass(frozen=True)
class SketchLoop:
    """A closed sequence of curves, usable as a feature profile."""

    id: str
    curves: tuple[str, ...]

    def __post_init__(self) -> None:
        # A circle is a loop on its own, so the minimum count depends on the
        # curve types and is checked in Sketch.validate() where they are known.
        if not self.curves:
            raise DocumentError(reason=f"loop '{self.id}' has no curves")


@dataclass(frozen=True)
class Sketch:
    """A set of named points, curves and closed loops on one datum plane."""

    id: str
    plane: str
    points: tuple[SketchPoint, ...] = ()
    curves: tuple[SketchCurve, ...] = ()
    loops: tuple[SketchLoop, ...] = ()

    def __post_init__(self) -> None:
        if not self.id.isidentifier():
            raise DocumentError(reason=f"sketch id {self.id!r} must be an identifier")
        _reject_duplicates("sketch point", (p.id for p in self.points))
        _reject_duplicates("sketch curve", (c.id for c in self.curves))
        _reject_duplicates("sketch loop", (loop.id for loop in self.loops))

    # -- lookup ------------------------------------------------------------

    def point(self, identifier: str) -> SketchPoint:
        found = next((p for p in self.points if p.id == identifier), None)
        if found is None:
            raise UnknownReferenceError(
                kind="sketch point", identifier=identifier, referenced_by=self.id
            )
        return found

    def curve(self, identifier: str) -> SketchCurve:
        found = next((c for c in self.curves if c.id == identifier), None)
        if found is None:
            raise UnknownReferenceError(
                kind="sketch curve", identifier=identifier, referenced_by=self.id
            )
        return found

    def loop(self, identifier: str) -> SketchLoop:
        found = next((loop for loop in self.loops if loop.id == identifier), None)
        if found is None:
            raise UnknownReferenceError(
                kind="sketch loop", identifier=identifier, referenced_by=self.id
            )
        return found

    def parameter_dependencies(self) -> frozenset[str]:
        values: list[Value] = []
        for point in self.points:
            values.extend(point.at)
        for curve in self.curves:
            values.append(curve.radius)
        return dependencies_of_many(values)

    def validate(self) -> None:
        """Check every internal reference resolves, before any geometry is built."""
        point_ids = {p.id for p in self.points}
        for curve in self.curves:
            for reference in curve.point_refs():
                if reference not in point_ids:
                    raise UnknownReferenceError(
                        kind="sketch point", identifier=reference, referenced_by=curve.id
                    )
        curve_ids = {c.id for c in self.curves}
        for loop in self.loops:
            for reference in loop.curves:
                if reference not in curve_ids:
                    raise UnknownReferenceError(
                        kind="sketch curve", identifier=reference, referenced_by=loop.id
                    )

    # -- resolution --------------------------------------------------------

    def resolve_points(self, parameters: ResolvedParameters) -> dict[str, Vec2]:
        return {point.id: point.resolve(parameters, self.id) for point in self.points}

    def resolve_all_curves(self, parameters: ResolvedParameters) -> list[ResolvedCurve]:
        """Every curve, whether or not it belongs to a loop.

        Drawing a sketch has to show what is actually there, including curves
        the author has not yet joined into a loop — that is often exactly what
        they are trying to see.
        """
        self.validate()
        points = self.resolve_points(parameters)
        resolved: list[ResolvedCurve] = []
        for curve in self.curves:
            try:
                resolved.append(self._resolve_curve(curve, points, parameters))
            except DocumentError:
                # A single malformed curve must not hide the rest of the sketch.
                continue
        return resolved

    def resolve_loop(self, loop_id: str, parameters: ResolvedParameters) -> list[ResolvedCurve]:
        """Resolve a loop's curves into evaluated geometry, in loop order."""
        self.validate()
        loop = self.loop(loop_id)
        points = self.resolve_points(parameters)

        resolved = [
            self._resolve_curve(self.curve(curve_id), points, parameters)
            for curve_id in loop.curves
        ]
        _check_loop_shape(resolved, self.id, loop_id)
        return resolved

    def _resolve_curve(
        self,
        curve: SketchCurve,
        points: Mapping[str, Vec2],
        parameters: ResolvedParameters,
    ) -> ResolvedCurve:
        where = f"sketches.{self.id}.curves.{curve.id}"

        if curve.type == CurveKinds.LINE:
            return ResolvedCurve(
                curve=curve, start=points[curve.start], end=points[curve.end]
            )

        if curve.type == CurveKinds.CIRCLE:
            radius = resolve(curve.radius, parameters, where=f"{where}.radius")
            if radius <= 0:
                raise DocumentError(
                    reason=f"circle '{curve.id}' needs a positive radius, got {radius:.6g}",
                    path=where,
                )
            return ResolvedCurve(curve=curve, center=points[curve.center], radius=radius)

        # Arc: the radius comes from the centre, and both ends must agree on it.
        centre = points[curve.center]
        start, end = points[curve.start], points[curve.end]
        radius = (start - centre).length()
        drift = abs((end - centre).length() - radius)
        if radius <= 0:
            raise DocumentError(
                reason=f"arc '{curve.id}' has its start point on its centre",
                path=where,
            )
        if drift > _ARC_RADIUS_TOL:
            raise DocumentError(
                reason=(
                    f"arc '{curve.id}' is inconsistent: its start is {radius:.6g}mm from "
                    f"the centre but its end is {(end - centre).length():.6g}mm. Both ends "
                    "must lie on the same circle."
                ),
                path=where,
            )
        return ResolvedCurve(curve=curve, start=start, end=end, center=centre, radius=radius)

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, object]:
        return {
            "plane": self.plane,
            "points": {p.id: list(p.at) for p in self.points},
            "curves": [_curve_to_dict(c) for c in self.curves],
            "loops": [{"id": loop.id, "curves": list(loop.curves)} for loop in self.loops],
        }

    @staticmethod
    def from_dict(identifier: str, data: Mapping[str, object]) -> Sketch:
        raw_points = data.get("points") or {}
        if not isinstance(raw_points, Mapping):
            raise DocumentError(
                reason="points must be a mapping of id to [u, v]",
                path=f"sketches.{identifier}.points",
            )
        points = tuple(
            SketchPoint(id=str(pid), at=_pair(value, identifier, str(pid)))
            for pid, value in raw_points.items()
        )

        raw_curves = data.get("curves") or []
        if not isinstance(raw_curves, Sequence):
            raise DocumentError(
                reason="curves must be a list", path=f"sketches.{identifier}.curves"
            )
        curves = tuple(_curve_from_dict(c, identifier) for c in raw_curves)

        raw_loops = data.get("loops") or []
        loops = tuple(
            SketchLoop(id=str(loop["id"]), curves=tuple(str(c) for c in loop["curves"]))
            for loop in raw_loops  # type: ignore[union-attr]
        )
        return Sketch(
            id=identifier,
            plane=str(data.get("plane", "xy")),
            points=points,
            curves=curves,
            loops=loops,
        )


def _curve_to_dict(curve: SketchCurve) -> dict[str, object]:
    data: dict[str, object] = {"id": curve.id, "type": curve.type}
    if curve.start:
        data["start"] = curve.start
    if curve.end:
        data["end"] = curve.end
    if curve.center:
        data["center"] = curve.center
    if curve.radius:
        data["radius"] = curve.radius
    if curve.clockwise:
        data["clockwise"] = True
    return data


def _curve_from_dict(raw: object, sketch: str) -> SketchCurve:
    if not isinstance(raw, Mapping):
        raise DocumentError(reason="curve must be an object", path=f"sketches.{sketch}.curves")
    return SketchCurve(
        id=str(raw["id"]),
        type=str(raw.get("type", CurveKinds.LINE)),
        start=str(raw.get("start", "")),
        end=str(raw.get("end", "")),
        center=str(raw.get("center", "")),
        radius=raw.get("radius", 0.0),  # type: ignore[arg-type]
        clockwise=bool(raw.get("clockwise", False)),
    )


def _pair(raw: object, sketch: str, point: str) -> tuple[Value, Value]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise DocumentError(
            reason="a point must be [u, v]", path=f"sketches.{sketch}.points.{point}"
        )
    return (raw[0], raw[1])  # type: ignore[return-value]


def _reject_duplicates(kind: str, identifiers: object) -> None:
    seen: set[str] = set()
    for identifier in identifiers:  # type: ignore[union-attr]
        if identifier in seen:
            raise DuplicateIdError(kind=kind, identifier=identifier)
        seen.add(identifier)


def _check_loop_shape(resolved: Sequence[ResolvedCurve], sketch: str, loop: str) -> None:
    """A profile must close, or the swept solid is undefined."""
    closed = [c for c in resolved if c.curve.is_closed]
    if closed:
        if len(resolved) != 1:
            raise DocumentError(
                reason=(
                    f"loop '{loop}' mixes the closed curve '{closed[0].id}' with "
                    f"{len(resolved) - 1} other curve(s); a circle forms a loop by itself"
                ),
                path=f"sketches.{sketch}.loops.{loop}",
            )
        return

    if len(resolved) < 3:
        raise DocumentError(
            reason=(
                f"loop '{loop}' needs at least 3 curves to enclose an area, has "
                f"{len(resolved)}"
            ),
            path=f"sketches.{sketch}.loops.{loop}",
        )

    for index, current in enumerate(resolved):
        following = resolved[(index + 1) % len(resolved)]
        end, next_start = current.end, following.start
        assert end is not None and next_start is not None
        if (end - next_start).length() > 1e-6:
            raise DocumentError(
                reason=(
                    f"loop '{loop}' is not closed: curve '{current.id}' ends at "
                    f"({end.x:.4g}, {end.y:.4g}) but '{following.id}' starts at "
                    f"({next_start.x:.4g}, {next_start.y:.4g})"
                ),
                path=f"sketches.{sketch}.loops.{loop}",
            )


def sketch_frame(sketch: Sketch, frames: Mapping[str, Frame]) -> Frame:
    """The datum frame a sketch lives on. Sketches attach to datums only."""
    frame = frames.get(sketch.plane)
    if frame is None:
        raise UnknownReferenceError(
            kind="datum", identifier=sketch.plane, referenced_by=sketch.id
        )
    return frame


def _angle_of(centre: Vec2, point: Vec2) -> float:
    return math.atan2(point.y - centre.y, point.x - centre.x)


def _arc_segments(radius: float, tolerance: float) -> int:
    """Enough segments that the chord sag stays within ``tolerance``."""
    if radius <= tolerance:
        return 8
    step = 2 * math.acos(max(-1.0, min(1.0, 1 - tolerance / radius)))
    return max(8, min(180, math.ceil(2 * math.pi / max(step, 1e-6))))
