"""Flattening OCCT geometry into 2D profiles.

This is the CNC and laser side of the adapter: a planar face becomes the curves
you would actually cut, expressed in that face's own plane.

Two rules run through the module.

**The frame is derived from the plane, never from OCCT's parameterisation.**
A ``gp_Pln`` carries an arbitrary location and X direction — arbitrary in the
sense that OCCT is free to choose differently on the next build. Instead the
origin is taken as the point of the plane closest to the world origin, and the
X axis from the same deterministic rule the domain uses elsewhere. So the same
face exports the same coordinates every time, which is what makes a re-cut after
a parameter change land on the previous part.

**Arcs are recognised, not approximated.** Only genuinely free-form curves get
discretised, and then with a stated deflection.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace
from itertools import pairwise

from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
from OCP.BRepTools import BRepTools, BRepTools_WireExplorer
from OCP.GCPnts import GCPnts_QuasiUniformDeflection
from OCP.GeomAbs import GeomAbs_CurveType, GeomAbs_SurfaceType
from OCP.gp import gp_Dir, gp_Pln, gp_Pnt
from OCP.TopAbs import TopAbs_EDGE, TopAbs_REVERSED, TopAbs_WIRE
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Edge, TopoDS_Face, TopoDS_Shape

from facet.application.ports.geometry import (
    Arc2D,
    Curve2D,
    Line2D,
    Loop2D,
    Profile2D,
)
from facet.domain.errors import FeatureBuildError
from facet.domain.math3d import Frame, Vec3

#: Below this a coordinate difference is noise, not a distinct point.
_TOL = 1e-7


def face_profile(
    face: TopoDS_Face,
    tolerance: float,
    label: str = "",
    edge_refs: Callable[[TopoDS_Edge], str] | None = None,
) -> Profile2D:
    """Flatten a planar face into its own plane.

    Refuses a curved face rather than projecting it, because the projection of a
    cylinder is not a cut path and silently producing one would be worse than
    saying so.
    """
    adaptor = BRepAdaptor_Surface(face)
    if adaptor.GetType() != GeomAbs_SurfaceType.GeomAbs_Plane:
        raise FeatureBuildError(
            feature=label or "face",
            reason=(
                "only a planar face can be flattened into a cut path; this one is "
                f"{_surface_name(adaptor.GetType())}. Take a section instead."
            ),
        )

    frame = _plane_frame(adaptor.Plane())
    outer = BRepTools.OuterWire_s(face)

    loops: list[Loop2D] = []
    explorer = TopExp_Explorer(face, TopAbs_WIRE)
    while explorer.More():
        wire = TopoDS.Wire_s(explorer.Current())
        curves = _wire_curves(wire, frame, tolerance, edge_refs)
        if curves:
            loops.append(Loop2D(curves=tuple(curves), outer=wire.IsSame(outer)))
        explorer.Next()

    # Outer first: a cutter that reads the file top to bottom should see the
    # part before the holes in it.
    loops.sort(key=lambda loop: not loop.outer)
    return Profile2D(loops=tuple(loops), frame=frame, label=label)


def section_profile(
    shape: TopoDS_Shape, frame: Frame, tolerance: float, label: str = ""
) -> Profile2D:
    """The outline where ``frame``'s plane cuts ``shape``.

    Unlike a face profile the result is a bag of edges rather than ordered
    loops — a section can cut a solid into several disjoint islands, and
    pretending to know their order would be an invention.
    """
    plane = gp_Pln(
        gp_Pnt(*frame.origin.as_tuple()),
        _dir(frame.z_axis),
    )
    face = BRepBuilderAPI_MakeFace(plane).Face()

    section = BRepAlgoAPI_Section(shape, face, False)
    section.ComputePCurveOn1(True)
    section.Approximation(True)
    section.Build()
    if not section.IsDone():
        raise FeatureBuildError(
            feature=label or "section", reason="the section could not be computed"
        )

    curves: list[Curve2D] = []
    explorer = TopExp_Explorer(section.Shape(), TopAbs_EDGE)
    while explorer.More():
        curves.extend(_edge_curves(TopoDS.Edge_s(explorer.Current()), frame, tolerance))
        explorer.Next()

    if not curves:
        raise FeatureBuildError(
            feature=label or "section",
            reason="the section plane does not pass through the solid",
        )
    return Profile2D(loops=(Loop2D(curves=tuple(curves)),), frame=frame, label=label)


# --------------------------------------------------------------------------
# Frames
# --------------------------------------------------------------------------


def _plane_frame(plane: gp_Pln) -> Frame:
    """A reproducible frame for a plane.

    The origin is the plane's closest point to the world origin and the X axis
    comes from the domain's deterministic rule, so nothing here depends on how
    OCCT happened to parameterise the surface.
    """
    a, b, c, d = plane.Coefficients()
    normal = Vec3(a, b, c)
    length = normal.length()
    if length < _TOL:
        raise FeatureBuildError(feature="face", reason="degenerate plane")
    normal = normal * (1.0 / length)
    origin = normal * (-d / length)
    return Frame.from_origin_normal(origin, normal)


def _dir(vector: Vec3) -> gp_Dir:
    return gp_Dir(vector.x, vector.y, vector.z)


# --------------------------------------------------------------------------
# Curves
# --------------------------------------------------------------------------


def _wire_curves(
    wire,
    frame: Frame,
    tolerance: float,
    edge_refs: Callable[[TopoDS_Edge], str] | None = None,
) -> list[Curve2D]:
    """Walk a wire in connection order, so the path is cuttable as written."""
    curves: list[Curve2D] = []
    walker = BRepTools_WireExplorer(wire)
    while walker.More():
        edge = walker.Current()
        ref = edge_refs(edge) if edge_refs else ""
        curves.extend(_with_edge(_edge_curves(edge, frame, tolerance), ref))
        walker.Next()
    return curves


def _with_edge(curves: list[Curve2D], ref: str) -> list[Curve2D]:
    """Stamp each run with the model edge it came from."""
    if not ref:
        return curves
    return [replace(curve, edge=ref) for curve in curves]


def _edge_curves(edge: TopoDS_Edge, frame: Frame, tolerance: float) -> list[Curve2D]:
    adaptor = BRepAdaptor_Curve(edge)
    reversed_edge = edge.Orientation() == TopAbs_REVERSED
    kind = adaptor.GetType()
    if kind == GeomAbs_CurveType.GeomAbs_Line:
        start = _flat(adaptor.Value(adaptor.FirstParameter()), frame)
        end = _flat(adaptor.Value(adaptor.LastParameter()), frame)
        if reversed_edge:
            start, end = end, start
        if _same(start, end):
            return []
        return [Line2D(start=start, end=end)]

    if kind == GeomAbs_CurveType.GeomAbs_Circle:
        arc = _circle_curve(adaptor, frame, reversed_edge)
        return [arc] if arc is not None else []

    return _discretise(adaptor, frame, tolerance, reversed_edge)


def _circle_curve(
    adaptor: BRepAdaptor_Curve, frame: Frame, reversed_edge: bool
) -> Curve2D | None:
    """A circle or arc, kept exact.

    The sweep direction is read off a sampled midpoint rather than from the
    circle's axis: the axis tells you how OCCT stored it, the midpoint tells you
    where the arc actually goes, and only the second survives a face being
    reversed.
    """
    first, last = adaptor.FirstParameter(), adaptor.LastParameter()
    circle = adaptor.Circle()
    centre = _flat(circle.Location(), frame)
    radius = circle.Radius()
    if radius < _TOL:
        return None

    start = _flat(adaptor.Value(first), frame)
    end = _flat(adaptor.Value(last), frame)
    middle = _flat(adaptor.Value((first + last) / 2.0), frame)

    start_angle = _angle(centre, start)
    end_angle = _angle(centre, end)
    mid_angle = _angle(centre, middle)

    if _same(start, end):
        # A closed circle: no sweep to determine, and the writers special-case it.
        return Arc2D(
            centre=centre, radius=radius, start_angle=start_angle, end_angle=start_angle
        )

    ccw = _passes_through(start_angle, mid_angle, end_angle)
    if reversed_edge:
        start_angle, end_angle = end_angle, start_angle
        ccw = not ccw
    return Arc2D(
        centre=centre,
        radius=radius,
        start_angle=start_angle,
        end_angle=end_angle,
        ccw=ccw,
    )


def _discretise(
    adaptor: BRepAdaptor_Curve, frame: Frame, tolerance: float, reversed_edge: bool
) -> list[Curve2D]:
    """Approximate a free-form curve, with the deflection stated by the caller."""
    sampler = GCPnts_QuasiUniformDeflection(adaptor, max(tolerance, 1e-4))
    if not sampler.IsDone() or sampler.NbPoints() < 2:
        return []
    points = [_flat(sampler.Value(i), frame) for i in range(1, sampler.NbPoints() + 1)]
    if reversed_edge:
        points.reverse()
    return [
        Line2D(start=a, end=b)
        for a, b in pairwise(points)
        if not _same(a, b)
    ]


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _flat(point: gp_Pnt, frame: Frame) -> tuple[float, float]:
    local = frame.to_local(Vec3(point.X(), point.Y(), point.Z()))
    return (local.x, local.y)


def _angle(centre: tuple[float, float], point: tuple[float, float]) -> float:
    return math.degrees(math.atan2(point[1] - centre[1], point[0] - centre[0])) % 360.0


def _passes_through(start: float, middle: float, end: float) -> bool:
    """Whether going counter-clockwise from ``start`` to ``end`` meets ``middle``."""
    return (middle - start) % 360.0 <= (end - start) % 360.0


def _same(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return abs(a[0] - b[0]) <= _TOL and abs(a[1] - b[1]) <= _TOL


_SURFACE_NAMES = {
    GeomAbs_SurfaceType.GeomAbs_Cylinder: "cylindrical",
    GeomAbs_SurfaceType.GeomAbs_Cone: "conical",
    GeomAbs_SurfaceType.GeomAbs_Sphere: "spherical",
    GeomAbs_SurfaceType.GeomAbs_Torus: "toroidal",
}


def _surface_name(kind) -> str:
    return _SURFACE_NAMES.get(kind, "curved")
