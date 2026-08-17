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
segments and cutting against the compound gives every face a simple
parameterisation, and the boolean behaves.

**The tool overlaps the material.** The cutting profile reaches past the surface
it is cutting into, so no face of the tool is ever coincident with a face of the
part. Coincident faces are the other reliable way to make a boolean lie.

Measured on this machine: an M6 x 12 thread costs about 0.1s to sweep and 3s to
cut, scaling linearly with length. That is slow enough to matter, which is why
threads are cached per feature like everything else and why the modelled form
is opt-in.
"""

from __future__ import annotations

import math

from OCP.BRep import BRep_Builder
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut
from OCP.BRepBuilderAPI import (
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
from OCP.gp import gp_Ax2, gp_Ax3, gp_Dir, gp_Dir2d, gp_Pnt, gp_Pnt2d, gp_Trsf
from OCP.TopAbs import TopAbs_WIRE
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Compound, TopoDS_Shape

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
    major_r = request.major / 2.0
    minor_r = request.minor / 2.0
    mid_r = (major_r + minor_r) / 2.0

    # Internal threads are cut from inside the bore outwards, external ones from
    # outside the shaft inwards. Only the profile differs; the helix is the same.
    if request.internal:
        outer, inner = major_r, minor_r - OVERLAP
    else:
        outer, inner = major_r + OVERLAP, minor_r

    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)

    turns = request.length / request.pitch + 2 * RUNOUT_TURNS
    segments = math.ceil(turns / SEGMENT_TURNS)
    made = 0
    for index in range(segments):
        start = -RUNOUT_TURNS + index * SEGMENT_TURNS
        spine = _helix(mid_r, request.pitch, start, SEGMENT_TURNS, request.right_handed)
        section = _vee(outer, inner, request.pitch, start * request.pitch)
        piece = _sweep(spine, section)
        if piece is not None:
            builder.Add(compound, piece)
            made += 1

    if made == 0:
        raise FeatureBuildError(
            feature=request.feature, reason="the thread form could not be swept"
        )
    return _placed(_clipped(compound, request, outer), frame)


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


def _helix(radius: float, pitch: float, start_turn: float, turns: float, right: bool):
    """A helical edge on a cylinder about +Z, built in the thread's own frame.

    On a cylinder the surface parameters are (angle, height), so a straight 2D
    line of slope ``pitch / 2pi`` is exactly a helix — no approximation, and no
    point sampling to go stale.
    """
    surface = Geom_CylindricalSurface(gp_Ax3(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), radius)
    slope = pitch / (2.0 * math.pi)
    sense = 1.0 if right else -1.0
    line = Geom2d_Line(gp_Pnt2d(0.0, 0.0), gp_Dir2d(sense, slope))
    # The 2D line is parameterised by arc length in (u, v), so a turn is longer
    # than 2pi by the pitch's contribution.
    unit = 2.0 * math.pi * math.sqrt(1.0 + slope * slope)
    edge = BRepBuilderAPI_MakeEdge(
        line, surface, start_turn * unit, (start_turn + turns) * unit
    ).Edge()
    BRepLib.BuildCurves3d_s(edge)
    return BRepBuilderAPI_MakeWire(edge).Wire()


def _vee(outer: float, inner: float, pitch: float, height: float):
    """The triangular thread section, in the plane containing the axis."""
    polygon = BRepBuilderAPI_MakePolygon()
    polygon.Add(gp_Pnt(outer, 0.0, height - pitch / 2.0))
    polygon.Add(gp_Pnt(inner, 0.0, height))
    polygon.Add(gp_Pnt(outer, 0.0, height + pitch / 2.0))
    polygon.Close()
    return BRepBuilderAPI_MakeFace(polygon.Wire()).Face()


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
