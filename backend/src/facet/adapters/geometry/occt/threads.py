"""Cutting ISO metric threads in OpenCascade.

Threads are the one place where the obvious construction is the wrong one, so
the reasoning is worth recording.

**Always cut, never fuse.** An external thread is a helical *groove* removed
from a cylinder at the major diameter, not a helical ridge added to a core. The
two describe the same solid; only the first is tractable. Fusing a ridge onto a
shaft was measured here at 2.3s for four turns and 151s for twenty-four — the
contact is tangential along the whole helix, which is the worst case for the
boolean. Cutting is transversal and scales linearly.

**One sweep per half turn.** A single pipe-shell along the whole helix produces
one lateral face that wraps around itself many times. OCCT accepts it, reports
it valid, and then silently subtracts nothing from it. Sweeping in short
segments gives every face a simple parameterisation, and the boolean behaves.

**Sweep one segment, screw it into place.** The segments are all the same solid
at different stations, because a helix is invariant under its own screw motion.
Building each one by sweeping a section at its own station is the same geometry
in principle and a trap in practice: a pipe shell positions the section by where
it sits relative to the *start of the spine*, so a section left on the +X axis
while the spine starts half a turn round comes out half a pitch out of place.
That produced not a thread but pairs of half-turn arcs stacked at one height,
stepping back twice per turn — with a plausible volume, the right tags and a
valid solid to show for it, which is why it survived every test here for so
long. Sweeping once at turn zero, where section and spine meet by construction,
and moving rigid copies along the axis cannot get that wrong.

**The segments are welded before they cut.** Left as a compound they abut on
shared section faces, and the cut leaves every one of those behind in the result
as a coincident pair — a membrane across the groove every half turn, which is
both wrong geometry and two faces the naming engine cannot order. Fusing first
dissolves those junctions while the shape is still a tool. Glued rather than
intersected, because the segments only ever touch and never overlap: that is a
promise OCCT can use, and it turns a 12s fuse into a 4s one.

Overlapping the segments instead would seem the simpler cure for the same
membranes, and is not one. A tool whose own solids interpenetrate is the case
where the cut reports success and subtracts nothing at all.

**The tool overlaps the material.** The cutting profile reaches past the surface
it is cutting into, so no face of the tool is ever coincident with a face of the
part. Coincident faces are the other reliable way to make a boolean lie.

Measured on this machine: an M12 x 14 thread costs about 0.1s to sweep, 4s to
weld and 2s to cut, scaling linearly with length. That is slow enough to matter,
which is why threads are cached per feature like everything else and why the
modelled form is opt-in.
"""

from __future__ import annotations

import math

from OCP.BOPAlgo import BOPAlgo_GlueEnum
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_Copy,
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakePolygon,
    BRepBuilderAPI_MakeWire,
    BRepBuilderAPI_Transform,
)
from OCP.BRepLib import BRepLib
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakePipeShell
from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
from OCP.Geom import Geom_CylindricalSurface
from OCP.Geom2d import Geom2d_Line
from OCP.gp import (
    gp_Ax1,
    gp_Ax2,
    gp_Ax3,
    gp_Dir,
    gp_Dir2d,
    gp_Pnt,
    gp_Pnt2d,
    gp_Trsf,
    gp_Vec,
)
from OCP.TopAbs import TopAbs_WIRE
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Shape
from OCP.TopTools import TopTools_ListOfShape

from facet.adapters.geometry.occt.booleans import boolean
from facet.application.ports.geometry import ThreadRequest
from facet.domain.errors import FeatureBuildError
from facet.domain.math3d import Frame, Vec3

#: How far the cutting tool reaches past the surface it cuts (mm). Large enough
#: that no face is ever coincident, small enough not to reach anything else.
OVERLAP = 0.5

#: Turns per swept segment. Half a turn keeps every face simply parameterised;
#: smaller would only add faces and time.
SEGMENT_TURNS = 0.5

#: Extra turns run on before and after the threaded length, so the thread does
#: not stop abruptly inside the material at either end.
RUNOUT_TURNS = 1.5


#: Designation, length and hand -- everything that changes the tool's shape, and
#: nothing that only moves it.
_ToolKey = tuple[float, float, float, float, bool, bool]

#: Unplaced tools, keyed on the numbers that decide their shape. A thread's
#: tool depends on its designation, its length and its hand — not on where it
#: is, which is a transform applied afterwards. So the four M8 cover screws on
#: a plate are one tool used four times.
#:
#: Worth having because this is the expensive half: on an export with every
#: thread modelled, building tools was 7.0s of a 21.9s rebuild and only three
#: of the seven were distinct.
#:
#: Bounded because a document with many distinct thread lengths would otherwise
#: accumulate one compound each. Oldest out first; a document has few.
_TOOL_CACHE: dict[_ToolKey, TopoDS_Shape] = {}
_TOOL_CACHE_LIMIT = 32


def _tool_key(request: ThreadRequest) -> _ToolKey:
    return (
        request.major,
        request.minor,
        request.pitch,
        request.length,
        request.internal,
        request.right_handed,
    )


def thread_tool(request: ThreadRequest) -> TopoDS_Shape:
    """The solid to subtract in order to leave ``request``'s thread behind."""
    if request.pitch <= 0:
        raise FeatureBuildError(
            feature=request.feature, reason=f"thread pitch must be positive, got {request.pitch:g}"
        )
    if request.length <= 3 * request.pitch:
        raise FeatureBuildError(
            feature=request.feature,
            reason=(
                f"a thread of {request.length:g}mm is too short for pitch "
                f"{request.pitch:g}: one pitch of plain lead-in is left at each end, "
                f"so at least {3 * request.pitch:g}mm is needed"
            ),
        )
    if request.minor <= 0:
        raise FeatureBuildError(
            feature=request.feature,
            reason=(
                f"an M{request.major:g} thread at pitch {request.pitch:g} has no material "
                "left at its root; check the designation"
            ),
        )

    frame = _axis_frame(request)
    key = _tool_key(request)
    cached = _TOOL_CACHE.get(key)
    if cached is not None:
        # Copied rather than shared. A boolean may attach internal
        # representations to its arguments, and two cuts reaching into one
        # compound is not a thing to find out about later.
        return _placed(BRepBuilderAPI_Copy(cached).Shape(), frame)

    shape = _build_tool(request)
    if len(_TOOL_CACHE) >= _TOOL_CACHE_LIMIT:
        del _TOOL_CACHE[next(iter(_TOOL_CACHE))]
    _TOOL_CACHE[key] = shape
    return _placed(BRepBuilderAPI_Copy(shape).Shape(), frame)


def _build_tool(request: ThreadRequest) -> TopoDS_Shape:
    """The tool about the world Z axis, before it is moved onto its own."""
    major_r = request.major / 2.0
    minor_r = request.minor / 2.0
    mid_r = (major_r + minor_r) / 2.0

    # Internal threads are cut from inside the bore outwards, external ones from
    # outside the shaft inwards. Only the profile differs; the helix is the same.
    if request.internal:
        outer, inner = major_r, minor_r - OVERLAP
    else:
        outer, inner = major_r + OVERLAP, minor_r

    segment = _sweep(
        _helix(mid_r, request.pitch, SEGMENT_TURNS, request.right_handed),
        _vee(outer, inner, request.pitch),
    )
    if segment is None:
        raise FeatureBuildError(
            feature=request.feature, reason="the thread form could not be swept"
        )

    turns = request.length / request.pitch + 2 * RUNOUT_TURNS
    segments = math.ceil(turns / SEGMENT_TURNS)
    pieces = [
        _screwed(
            segment,
            -RUNOUT_TURNS + index * SEGMENT_TURNS,
            request.pitch,
            request.right_handed,
        )
        for index in range(segments)
    ]
    return _clipped(_welded(pieces, request), request, outer)


def _welded(pieces: list[TopoDS_Shape], request: ThreadRequest) -> TopoDS_Shape:
    """Fuse the swept segments into one solid, dissolving the joins between them."""
    if len(pieces) == 1:
        return pieces[0]
    arguments = TopTools_ListOfShape()
    arguments.Append(pieces[0])
    tools = TopTools_ListOfShape()
    for piece in pieces[1:]:
        tools.Append(piece)
    fuse = BRepAlgoAPI_Fuse()
    fuse.SetArguments(arguments)
    fuse.SetTools(tools)
    # The segments meet on shared faces and nowhere else, which is precisely
    # what glueing asserts. Without it OCCT looks for intersections that are not
    # there, at three times the cost.
    fuse.SetGlue(BOPAlgo_GlueEnum.BOPAlgo_GlueShift)
    fuse.SetRunParallel(True)
    fuse.Build()
    if not fuse.IsDone():
        raise FeatureBuildError(
            feature=request.feature, reason="the thread form could not be joined up"
        )
    return fuse.Shape()


def _clipped(tool: TopoDS_Shape, request: ThreadRequest, outer: float) -> TopoDS_Shape:
    """Confine the tool to the threaded length, less a lead-in at each end.

    The run-on turns exist so the helix does not begin mid-flank, but left
    unclipped they cut past both ends — notching the face the hole enters
    through and the floor it stops at, and splitting both. A face unrelated to
    the thread must not change shape because the thread's pitch changed.

    Clipping flush is not enough either: a clip plane coincident with the entry
    face or the floor leaves coplanar fragments there, which splits those faces
    just as badly. So the band is inset by one pitch at each end, putting both
    clip planes strictly inside material. The part that results is also the
    honest one — a tapped hole has a plain lead-in and never threads to the very
    bottom of a blind hole.
    """
    lead = request.pitch
    band = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(0.0, 0.0, lead), gp_Dir(0, 0, 1)),
        outer + OVERLAP,
        max(request.length - 2 * lead, lead),
    ).Shape()
    common = boolean(BRepAlgoAPI_Common(), tool, band)
    if not common.IsDone():
        raise FeatureBuildError(
            feature=request.feature, reason="the thread form could not be trimmed to length"
        )
    return common.Shape()


def cut_thread(base: TopoDS_Shape, request: ThreadRequest) -> BRepAlgoAPI_Cut:
    """Subtract the thread form from ``base``.

    Returns the boolean operation rather than its shape: the caller needs the
    history map to attribute faces, and building the thread twice to get it
    would double the one genuinely expensive step.
    """
    operation = boolean(BRepAlgoAPI_Cut(), base, thread_tool(request))
    if not operation.IsDone():
        raise FeatureBuildError(
            feature=request.feature,
            reason=(
                "the thread could not be cut. Check that the axis passes through "
                "material and that the threaded length fits inside it."
            ),
        )
    return operation


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------


def _axis_frame(request: ThreadRequest) -> Frame:
    length = request.direction.length()
    if length < 1e-9:
        raise FeatureBuildError(
            feature=request.feature, reason="the thread axis has no direction"
        )
    return Frame.from_origin_normal(request.origin, request.direction * (1.0 / length))


def _helix(radius: float, pitch: float, turns: float, right: bool):
    """A helical edge on a cylinder about +Z, from the +X axis at ``z = 0``.

    On a cylinder the surface parameters are (angle, height), so a straight 2D
    line of slope ``pitch / 2pi`` is exactly a helix — no approximation, and no
    point sampling to go stale.

    It always starts on +X, where :func:`_vee` puts the section. Starting a spine
    anywhere else is what a pipe shell mistakes for an offset section.
    """
    surface = Geom_CylindricalSurface(gp_Ax3(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), radius)
    slope = pitch / (2.0 * math.pi)
    sense = 1.0 if right else -1.0
    line = Geom2d_Line(gp_Pnt2d(0.0, 0.0), gp_Dir2d(sense, slope))
    # The 2D line is parameterised by arc length in (u, v), so a turn is longer
    # than 2pi by the pitch's contribution.
    unit = 2.0 * math.pi * math.sqrt(1.0 + slope * slope)
    edge = BRepBuilderAPI_MakeEdge(line, surface, 0.0, turns * unit).Edge()
    BRepLib.BuildCurves3d_s(edge)
    return BRepBuilderAPI_MakeWire(edge).Wire()


def _vee(outer: float, inner: float, pitch: float):
    """The triangular thread section, in the plane containing the axis.

    On +X at ``z = 0``, which is where :func:`_helix` begins.
    """
    polygon = BRepBuilderAPI_MakePolygon()
    polygon.Add(gp_Pnt(outer, 0.0, -pitch / 2.0))
    polygon.Add(gp_Pnt(inner, 0.0, 0.0))
    polygon.Add(gp_Pnt(outer, 0.0, pitch / 2.0))
    polygon.Close()
    return BRepBuilderAPI_MakeFace(polygon.Wire()).Face()


def _screwed(shape: TopoDS_Shape, turns: float, pitch: float, right: bool) -> TopoDS_Shape:
    """``shape`` advanced ``turns`` along the helix it was built on.

    Rotation and rise together, in the proportion the pitch fixes: the motion the
    helix is invariant under, so a segment stays a segment of the same thread.
    """
    sense = 1.0 if right else -1.0
    rotation = gp_Trsf()
    rotation.SetRotation(
        gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), sense * 2.0 * math.pi * turns
    )
    rise = gp_Trsf()
    rise.SetTranslation(gp_Vec(0.0, 0.0, turns * pitch))
    return BRepBuilderAPI_Transform(shape, rise.Multiplied(rotation), True).Shape()


def _sweep(spine, section) -> TopoDS_Shape | None:
    pipe = BRepOffsetAPI_MakePipeShell(spine)
    # Keep the section upright along the helix rather than letting it twist with
    # the curve's own frame, which is what makes the flanks flat.
    pipe.SetMode(gp_Dir(0, 0, 1))
    explorer = TopExp_Explorer(section, TopAbs_WIRE)
    if not explorer.More():
        return None
    pipe.Add(TopoDS.Wire_s(explorer.Current()), False, False)
    pipe.Build()
    if not pipe.IsDone():
        return None
    pipe.MakeSolid()
    return pipe.Shape()


def _placed(shape: TopoDS_Shape, frame: Frame) -> TopoDS_Shape:
    """Move a shape built about the world Z axis onto the thread's own axis."""
    if frame.is_identity:
        return shape
    transform = gp_Trsf()
    transform.SetTransformation(
        gp_Ax3(
            gp_Pnt(*frame.origin.as_tuple()),
            _dir(frame.z_axis),
            _dir(frame.x_axis),
        ),
        gp_Ax3(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1), gp_Dir(1, 0, 0)),
    )
    return BRepBuilderAPI_Transform(shape, transform, True).Shape()


def _dir(vector: Vec3) -> gp_Dir:
    return gp_Dir(vector.x, vector.y, vector.z)
