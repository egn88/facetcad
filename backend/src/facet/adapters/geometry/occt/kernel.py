"""OpenCascade implementation of the geometry kernel port.

This is the production kernel. It lifts the analytic kernel's deliberate
restrictions: profiles may be arbitrary closed polygons (and arcs and circles),
on datum planes at any orientation.

The one subtlety worth knowing
------------------------------

OCCT keys its history maps on the **actual sub-shapes of the input shape**, not
on the objects you handed to a builder. ``BRepBuilderAPI_MakeWire`` copies and
may reverse edges, so asking ``prism.Generated(my_edge)`` silently returns
nothing for most edges — which would leave faces unattributed and destroy the
naming guarantee.

The fix is to explore the edges back out of the constructed profile face and
query history with *those*, then map each one to its sketch curve by comparing
midpoints. That geometric match happens exactly once, at construction, and its
result is a name that persists from then on. It is the only place in the system
where geometry decides identity, and it is safe there because the profile is
authored, not derived.

As everywhere else, this adapter reports **provenance only**. It does not know
that a swept face is a pad "side" but a pocket "wall"; that is decided in
:mod:`facet.application.naming`.
"""

from __future__ import annotations

import itertools
import pickle
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from OCP.BinTools import BinTools
from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakeWire,
)
from OCP.BRepFilletAPI import BRepFilletAPI_MakeChamfer, BRepFilletAPI_MakeFillet
from OCP.BRepGProp import BRepGProp
from OCP.BRepLProp import BRepLProp_SLProps
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
from OCP.GC import GC_MakeArcOfCircle
from OCP.GCPnts import GCPnts_QuasiUniformDeflection
from OCP.GeomAbs import GeomAbs_CurveType, GeomAbs_SurfaceType
from OCP.gp import gp_Ax2, gp_Ax3, gp_Circ, gp_Dir, gp_Pln, gp_Pnt, gp_Vec
from OCP.GProp import GProp_GProps
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS, TopoDS_Edge, TopoDS_Face, TopoDS_Shape

from facet.adapters.export.drawing import export_drawing
from facet.adapters.geometry.occt.booleans import boolean
from facet.adapters.geometry.occt.profiles import face_profile, section_profile
from facet.adapters.geometry.occt.threads import cut_thread
from facet.application.ports.geometry import (
    BlendRequest,
    BoundingBox,
    Capability,
    CurveType,
    DeletedFace,
    EdgePolyline,
    EdgeRecord,
    FaceProvenance,
    FaceRange,
    FaceRecord,
    Origin,
    PadRequest,
    PocketRequest,
    Profile,
    Profile2D,
    ProfileCurve,
    Ref,
    SolidHandle,
    SolidResult,
    Tessellation,
    ThreadRequest,
)
from facet.domain.errors import DocumentError, FeatureBuildError
from facet.domain.fingerprint import (
    CurveKind,
    EdgeFingerprint,
    FaceFingerprint,
    SurfaceKind,
)
from facet.domain.math3d import LINEAR_TOL, Frame, Vec2, Vec3

#: Tolerance for matching a built edge back to its authoring sketch curve (mm).
#: Loose enough to survive OCCT's internal reparameterisation, far tighter than
#: the distance between two distinct curves' midpoints in any real sketch.
_CURVE_MATCH_TOL = 1e-4
#: Coordinate quantisation for deterministic reference ordering.
_SORT_DIGITS = 6
#: Layout of a snapshot blob. Bumped when what goes into one changes shape, so
#: an older file is refused rather than half-understood.
_SNAPSHOT_FORMAT = 1

_SURFACE_KINDS = {
    GeomAbs_SurfaceType.GeomAbs_Plane: SurfaceKind.PLANE,
    GeomAbs_SurfaceType.GeomAbs_Cylinder: SurfaceKind.CYLINDER,
    GeomAbs_SurfaceType.GeomAbs_Cone: SurfaceKind.CONE,
    GeomAbs_SurfaceType.GeomAbs_Sphere: SurfaceKind.SPHERE,
    GeomAbs_SurfaceType.GeomAbs_Torus: SurfaceKind.TORUS,
}

_CURVE_KINDS = {
    GeomAbs_CurveType.GeomAbs_Line: CurveKind.LINE,
    GeomAbs_CurveType.GeomAbs_Circle: CurveKind.CIRCLE,
    GeomAbs_CurveType.GeomAbs_Ellipse: CurveKind.ELLIPSE,
    GeomAbs_CurveType.GeomAbs_BSplineCurve: CurveKind.BSPLINE,
}


# --------------------------------------------------------------------------
# Small shape helpers
# --------------------------------------------------------------------------


def _explore(shape: TopoDS_Shape, kind) -> list[TopoDS_Shape]:
    found: list[TopoDS_Shape] = []
    explorer = TopExp_Explorer(shape, kind)
    while explorer.More():
        found.append(explorer.Current())
        explorer.Next()
    return found


def _faces_of(shape: TopoDS_Shape) -> list[TopoDS_Face]:
    return [TopoDS.Face_s(s) for s in _explore(shape, TopAbs_FACE)]


def _edges_of(shape: TopoDS_Shape) -> list[TopoDS_Edge]:
    return [TopoDS.Edge_s(s) for s in _explore(shape, TopAbs_EDGE)]


def _unique_edges_of(shape: TopoDS_Shape) -> list[TopoDS_Edge]:
    """Each edge once.

    ``TopExp_Explorer`` visits a shared edge once per adjacent face, so a box
    yields 24 rather than 12. Fine when walking a single face, wrong when
    listing a solid's edges for display.
    """
    seen = _ShapeSet()
    unique: list[TopoDS_Edge] = []
    for edge in _edges_of(shape):
        if not seen.contains(edge):
            seen.add(edge)
            unique.append(edge)
    return unique


def _shape_list(items) -> list[TopoDS_Shape]:
    """The shapes in a TopTools_ListOfShape.

    ``list(items)`` is correct and 250x slower: OCP's iterator costs ~90us per
    item, against 0.5us for First(). Almost every list here holds one or two
    shapes -- an edge has two adjacent faces, a Modified() image is usually one
    -- so those two cases are read directly and only the rare longer list pays
    for the iterator.
    """
    size = items.Size()
    if size == 0:
        return []
    if size == 1:
        return [items.First()]
    if size == 2:
        return [items.First(), items.Last()]
    return list(items)


def _to_vec(point: gp_Pnt) -> Vec3:
    return Vec3(point.X(), point.Y(), point.Z())


def _surface_properties(face: TopoDS_Face) -> tuple[float, Vec3]:
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    return props.Mass(), _to_vec(props.CentreOfMass())


def _face_normal(face: TopoDS_Face) -> Vec3:
    """The **outward** normal, honouring face orientation.

    OCCT's surface normal ignores how the face sits in the solid; a REVERSED
    face points the other way. Getting this wrong would invert every cap sign,
    so it is applied here once rather than trusted to callers.
    """
    adaptor = BRepAdaptor_Surface(face)
    u = (adaptor.FirstUParameter() + adaptor.LastUParameter()) / 2
    v = (adaptor.FirstVParameter() + adaptor.LastVParameter()) / 2
    props = BRepLProp_SLProps(adaptor, u, v, 1, 1e-7)
    if not props.IsNormalDefined():
        return Vec3(0.0, 0.0, 1.0)
    normal = props.Normal()
    vector = Vec3(normal.X(), normal.Y(), normal.Z())
    if face.Orientation() == TopAbs_REVERSED:
        vector = -vector
    return vector


def _face_fingerprint(face: TopoDS_Face) -> FaceFingerprint:
    area, centroid = _surface_properties(face)
    adaptor = BRepAdaptor_Surface(face)
    return FaceFingerprint(
        surface=_SURFACE_KINDS.get(adaptor.GetType(), SurfaceKind.FREEFORM),
        area=area,
        centroid=centroid,
        normal=_face_normal(face),
    )


def _edge_fingerprint(edge: TopoDS_Edge) -> EdgeFingerprint:
    props = GProp_GProps()
    BRepGProp.LinearProperties_s(edge, props)
    curve = BRepAdaptor_Curve(edge)
    start, end = curve.FirstParameter(), curve.LastParameter()
    midpoint = _to_vec(curve.Value((start + end) / 2))
    chord = _to_vec(curve.Value(end)) - _to_vec(curve.Value(start))
    direction = chord.normalized() if chord.length() > 1e-12 else Vec3(0.0, 0.0, 1.0)
    return EdgeFingerprint(
        curve=_CURVE_KINDS.get(curve.GetType(), CurveKind.UNKNOWN),
        length=props.Mass(),
        midpoint=midpoint,
        direction=direction,
    )


def _edge_midpoint(edge: TopoDS_Edge) -> Vec3:
    curve = BRepAdaptor_Curve(edge)
    return _to_vec(curve.Value((curve.FirstParameter() + curve.LastParameter()) / 2))


class _ShapeSet:
    """Membership test over TopoDS shapes, using OCCT's own identity."""

    def __init__(self, shapes: Iterable[TopoDS_Shape] = ()) -> None:
        self._buckets: dict[int, list[TopoDS_Shape]] = {}
        for shape in shapes:
            self.add(shape)

    def add(self, shape: TopoDS_Shape) -> None:
        self._buckets.setdefault(hash(shape), []).append(shape)

    def contains(self, shape: TopoDS_Shape) -> bool:
        return any(other.IsSame(shape) for other in self._buckets.get(hash(shape), ()))


class _ShapeMap:
    """Dictionary keyed by TopoDS shape identity (``IsSame``)."""

    def __init__(self) -> None:
        self._buckets: dict[int, list[tuple[TopoDS_Shape, object]]] = {}

    def set(self, shape: TopoDS_Shape, value: object) -> None:
        self._buckets.setdefault(hash(shape), []).append((shape, value))

    def get(self, shape: TopoDS_Shape) -> object | None:
        for other, value in self._buckets.get(hash(shape), ()):
            if other.IsSame(shape):
                return value
        return None


# --------------------------------------------------------------------------
# Profile construction
# --------------------------------------------------------------------------


@dataclass
class _BuiltProfile:
    """A profile face, plus how to attribute the faces swept from it."""

    face: TopoDS_Face
    #: explored profile edge -> sketch curve id
    curve_of_edge: _ShapeMap = field(default_factory=_ShapeMap)


def _plane_of(frame: Frame) -> gp_Pln:
    axis = gp_Ax3(
        gp_Pnt(*frame.origin.as_tuple()),
        gp_Dir(*frame.z_axis.as_tuple()),
        gp_Dir(*frame.x_axis.as_tuple()),
    )
    return gp_Pln(axis)


def _world_point(frame: Frame, uv: Vec2) -> gp_Pnt:
    return gp_Pnt(*frame.point_at(uv).as_tuple())


def _make_edge(curve: ProfileCurve, frame: Frame, feature: str) -> TopoDS_Edge:
    if curve.type == CurveType.LINE:
        assert curve.start is not None and curve.end is not None
        # A zero-length line is what a sketch collapses to when a dimension is
        # driven to zero. OCCT answers with a bare StdFail_NotDone, which says
        # nothing about which curve or why, so it is caught here instead.
        if (curve.end - curve.start).length() <= LINEAR_TOL:
            raise FeatureBuildError(
                feature=feature,
                reason=(
                    f"curve '{curve.id}' has zero length — its two ends are at the "
                    "same point. Check the parameters its endpoints depend on."
                ),
            )
        return BRepBuilderAPI_MakeEdge(
            _world_point(frame, curve.start), _world_point(frame, curve.end)
        ).Edge()

    if curve.center is None or curve.radius <= 0:
        raise FeatureBuildError(
            feature=feature,
            reason=f"curve '{curve.id}' needs a centre and a positive radius",
        )
    centre = _world_point(frame, curve.center)
    axis = gp_Ax2(centre, gp_Dir(*frame.z_axis.as_tuple()), gp_Dir(*frame.x_axis.as_tuple()))
    circle = gp_Circ(axis, curve.radius)

    if curve.type == CurveType.CIRCLE:
        return BRepBuilderAPI_MakeEdge(circle).Edge()

    if curve.start is None or curve.end is None:
        raise FeatureBuildError(
            feature=feature, reason=f"arc '{curve.id}' is missing an endpoint"
        )
    arc = GC_MakeArcOfCircle(
        circle, _world_point(frame, curve.start), _world_point(frame, curve.end),
        curve.counter_clockwise,
    )
    if not arc.IsDone():
        raise FeatureBuildError(
            feature=feature, reason=f"arc '{curve.id}' could not be constructed"
        )
    return BRepBuilderAPI_MakeEdge(arc.Value()).Edge()


def _build_profile(profile: Profile, feature: str) -> _BuiltProfile:
    """Build a planar face from a sketch loop, retaining curve attribution."""
    if not profile.curves:
        raise FeatureBuildError(
            feature=feature, reason=f"profile '{profile.sketch}.{profile.loop}' has no curves"
        )

    # Keep each authored edge so its midpoint can identify the copy that ends up
    # in the wire. Reversal does not move a midpoint, so this is exact for every
    # curve type — no per-type trigonometry to get subtly wrong.
    authored: list[tuple[str, Vec3]] = []
    wire_builder = BRepBuilderAPI_MakeWire()
    for curve in profile.curves:
        edge = _make_edge(curve, profile.frame, feature)
        authored.append((curve.id, _edge_midpoint(edge)))
        wire_builder.Add(edge)
    if not wire_builder.IsDone():
        raise FeatureBuildError(
            feature=feature,
            reason=(
                f"profile '{profile.sketch}.{profile.loop}' does not form a closed wire; "
                "check that consecutive curves share endpoints"
            ),
        )

    face_builder = BRepBuilderAPI_MakeFace(_plane_of(profile.frame), wire_builder.Wire())
    if not face_builder.IsDone():
        raise FeatureBuildError(
            feature=feature,
            reason=(
                f"profile '{profile.sketch}.{profile.loop}' could not be turned into a face; "
                "it may be self-intersecting or not planar"
            ),
        )
    face = face_builder.Face()

    # Re-associate the *built* edges with their authoring curves. Querying
    # history with the edges we constructed would silently return nothing,
    # because MakeWire copies and reverses them.
    built = _BuiltProfile(face=face)
    for edge in _edges_of(face):
        midpoint = _edge_midpoint(edge)
        matches = [
            cid for cid, point in authored
            if (point - midpoint).length() <= _CURVE_MATCH_TOL
        ]
        if len(matches) != 1:
            raise FeatureBuildError(
                feature=feature,
                reason=(
                    f"could not attribute an edge of '{profile.sketch}.{profile.loop}' to "
                    f"exactly one sketch curve ({len(matches)} candidates). Two curves may "
                    "share a midpoint, which would make the derived face names ambiguous."
                ),
            )
        built.curve_of_edge.set(edge, matches[0])
    return built


# --------------------------------------------------------------------------
# Prisms and their provenance
# --------------------------------------------------------------------------


@dataclass
class _Prism:
    """A swept solid together with the provenance of each of its faces."""

    shape: TopoDS_Shape
    provenance: _ShapeMap


def _sweep(profile: _BuiltProfile, direction: Vec3, feature: str) -> _Prism:
    builder = BRepPrimAPI_MakePrism(profile.face, gp_Vec(*direction.as_tuple()))
    if not builder.IsDone():
        raise FeatureBuildError(feature=feature, reason="the extrusion failed")
    shape = builder.Shape()

    provenance = _ShapeMap()
    for edge in _edges_of(profile.face):
        curve_id = profile.curve_of_edge.get(edge)
        if curve_id is None:
            continue
        for generated in _shape_list(builder.Generated(edge)):
            provenance.set(
                TopoDS.Face_s(generated),
                FaceProvenance(origin=Origin.SWEPT, curve=str(curve_id)),
            )

    provenance.set(
        TopoDS.Face_s(builder.FirstShape()), FaceProvenance(origin=Origin.CAP_START)
    )
    provenance.set(
        TopoDS.Face_s(builder.LastShape()), FaceProvenance(origin=Origin.CAP_END)
    )
    return _Prism(shape=shape, provenance=provenance)


def _images_of(operation, face: TopoDS_Face, result_faces: _ShapeSet) -> list[TopoDS_Face]:
    """Which result faces a given input face became."""
    return _images_and_change(operation, face, result_faces)[0]


def _images_and_change(
    operation, face: TopoDS_Face, result_faces: _ShapeSet
) -> tuple[list[TopoDS_Face], bool]:
    """Which result faces an input face became, and whether it was touched.

    OCCT reports nothing for a face that passed through untouched, so an empty
    ``Modified`` list means "unchanged" rather than "gone" — the distinction
    that decides whether a tag survives or is retired. It decides one more thing
    here: an untouched face is the *same* surface with the same bounds, so its
    area, centroid and normal cannot have moved and need not be integrated
    again.
    """
    if operation.IsDeleted(face):
        return [], True
    modified = operation.Modified(face)
    if modified.Extent() == 0:
        return ([face] if result_faces.contains(face) else []), False

    images: list[TopoDS_Face] = []
    for shape in _shape_list(modified):
        image = TopoDS.Face_s(shape)
        if result_faces.contains(image):
            images.append(image)
    return images, True


# --------------------------------------------------------------------------
# Stored solids
# --------------------------------------------------------------------------


@dataclass
class _Solid:
    handle: SolidHandle
    shape: TopoDS_Shape
    #: ref -> face, so a later operation can report inherited parents
    faces: list[tuple[Ref, TopoDS_Face]] = field(default_factory=list)
    #: ref -> fingerprint, kept so a face that a later boolean does not touch is
    #: never measured twice. Surface integration is ~0.2ms a face, and a linear
    #: history re-measures every face on every feature -- which is what makes a
    #: 40-feature model cost O(N^2) rather than O(N).
    fingerprints: dict[Ref, FaceFingerprint] = field(default_factory=dict)
    #: edge ref -> (edge, the two face refs it separates)
    edges: dict[Ref, tuple[TopoDS_Edge, tuple[Ref, Ref]]] = field(default_factory=dict)
    #: The solid's volume, integrated at most once. A cut measures the body
    #: before and after to prove it removed something, and "after" is the next
    #: feature's "before" -- so without this every feature integrates the whole
    #: solid twice over. On a threaded body that is 145ms a time.
    _volume_cache: float | None = None
    #: How each face of *this* solid came to be, kept so a later fuse can report
    #: an added body's faces with their real origin rather than as anonymous
    #: inherited ones — a padded side is still a side, whichever body it joins.
    provenance: dict[Ref, FaceProvenance] = field(default_factory=dict)

    def edge_ref(self, edge: TopoDS_Edge) -> str:
        """The ref of a stored edge, by shape identity.

        Two faces meeting at an edge report the *same* ref, which is what lets a
        joint generator pair up the panels without matching their geometry.
        """
        for ref, (stored, _) in self.edges.items():
            if stored.IsSame(edge):
                return ref
        return ""


# --------------------------------------------------------------------------
# The adapter
# --------------------------------------------------------------------------


class OcctKernel:
    """OpenCascade implementation of :class:`GeometryKernel`."""

    def __init__(self) -> None:
        self._solids: dict[str, _Solid] = {}
        self._counter = 0

    @property
    def name(self) -> str:
        return "occt"

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset(
            {
                Capability.PAD,
                Capability.POCKET,
                Capability.TESSELLATE,
                Capability.MESH_EXPORT,
                Capability.BREP_EXPORT,
                Capability.FILLET,
                Capability.CHAMFER,
                Capability.FACE_PROFILE,
                Capability.DRAWING_EXPORT,
                Capability.THREAD,
                Capability.SNAPSHOT,
            }
        )

    # -- operations --------------------------------------------------------

    def pad(self, request: PadRequest) -> SolidResult:
        if request.length <= 0:
            raise FeatureBuildError(
                feature=request.feature,
                reason=f"pad length must be positive, got {request.length}",
            )
        profile = _build_profile(request.profile, request.feature)
        normal = request.profile.frame.z_axis * float(request.direction or 1)

        if request.midplane:
            base = _build_profile(
                _shifted(request.profile, normal * (-request.length / 2)), request.feature
            )
            prism = _sweep(base, normal * request.length, request.feature)
        else:
            prism = _sweep(profile, normal * request.length, request.feature)

        return self._store(prism.shape, lambda face: _lookup(prism.provenance, face))

    def pocket(
        self,
        base: SolidHandle,
        request: PocketRequest,
        face_refs: Mapping[Ref, object] | None = None,
    ) -> SolidResult:
        source = self._lookup_solid(base)
        if request.depth <= 0 and not request.through_all:
            raise FeatureBuildError(
                feature=request.feature,
                reason=f"pocket depth must be positive, got {request.depth}",
            )

        profile = _build_profile(request.profile, request.feature)
        normal = request.profile.frame.z_axis * float(request.direction or -1)

        if request.through_all:
            reach = _diagonal(source.shape) * 2 + 1.0
            start = _shifted(request.profile, normal * -reach)
            tool = _sweep(
                _build_profile(start, request.feature),
                normal * (reach * 2),
                request.feature,
            )
        else:
            tool = _sweep(profile, normal * request.depth, request.feature)

        operation = boolean(BRepAlgoAPI_Cut(), source.shape, tool.shape)
        if not operation.IsDone():
            raise FeatureBuildError(
                feature=request.feature, reason="the boolean cut failed in the kernel"
            )
        result = operation.Shape()
        result_faces = _ShapeSet(_faces_of(result))
        if not _faces_of(result):
            raise FeatureBuildError(
                feature=request.feature, reason="the pocket would remove the entire body"
            )

        provenance = _ShapeMap()
        known = _ShapeMap()
        surviving_refs: set[Ref] = set()

        for ref, face in source.faces:
            images, changed = _images_and_change(operation, face, result_faces)
            for image in images:
                provenance.set(image, FaceProvenance(origin=Origin.INHERITED, parent=ref))
                surviving_refs.add(ref)
                if not changed:
                    kept = source.fingerprints.get(ref)
                    if kept is not None:
                        known.set(image, kept)

        lateral = 0
        for face in _faces_of(tool.shape):
            origin = _lookup(tool.provenance, face)
            for image in _images_of(operation, face, result_faces):
                if origin.origin == Origin.SWEPT:
                    lateral += 1
                if provenance.get(image) is None:
                    provenance.set(image, origin)

        if not self._cut_took_material(lateral, source, result, tool.shape):
            raise FeatureBuildError(
                feature=request.feature,
                reason=(
                    "the pocket removes no material. The usual cause is direction: "
                    "a sketch on the plane a pad was grown *from* has the material "
                    "on the +normal side, so cutting -normal from it goes out into "
                    "space. Check the direction, then the depth and position."
                ),
            )

        deleted = tuple(
            DeletedFace(ref=ref, reason="consumed")
            for ref, _ in source.faces
            if ref not in surviving_refs
        )
        return self._store(
            result, lambda face: _lookup(provenance, face), deleted=deleted, known=known
        )

    def fuse(self, base: SolidHandle, addition: SolidHandle) -> SolidResult:
        """Union two solids.

        Disjoint bodies are a legitimate result: OCCT returns a compound holding
        both, which is exactly what a model with two separate pads should be.

        Faces surviving from ``base`` are reported as inherited, so their names
        carry forward. Faces from ``addition`` keep the provenance they were
        built with, so a newly padded side is still named after the sketch curve
        that swept it rather than becoming an anonymous inherited face.
        """
        first = self._lookup_solid(base)
        second = self._lookup_solid(addition)
        operation = boolean(BRepAlgoAPI_Fuse(), first.shape, second.shape)
        if not operation.IsDone():
            raise FeatureBuildError(feature="<fuse>", reason="the boolean union failed")
        result = operation.Shape()
        result_faces = _ShapeSet(_faces_of(result))

        provenance = _ShapeMap()
        known = _ShapeMap()
        surviving: set[Ref] = set()
        for ref, face in first.faces:
            images, changed = _images_and_change(operation, face, result_faces)
            for image in images:
                provenance.set(image, FaceProvenance(origin=Origin.INHERITED, parent=ref))
                surviving.add(ref)
                if not changed:
                    kept = first.fingerprints.get(ref)
                    if kept is not None:
                        known.set(image, kept)

        for ref, face in second.faces:
            origin = second.provenance.get(ref, FaceProvenance(origin=Origin.UNKNOWN))
            for image in _images_of(operation, face, result_faces):
                if provenance.get(image) is None:
                    provenance.set(image, origin)

        deleted = tuple(
            DeletedFace(ref=ref, reason="merged")
            for ref, _ in first.faces
            if ref not in surviving
        )
        return self._store(
            result, lambda face: _lookup(provenance, face), deleted=deleted, known=known
        )

    def fillet(self, base: SolidHandle, request: BlendRequest) -> SolidResult:
        return self._blend(base, request, chamfer=False)

    def chamfer(self, base: SolidHandle, request: BlendRequest) -> SolidResult:
        return self._blend(base, request, chamfer=True)

    def _blend(
        self, base: SolidHandle, request: BlendRequest, *, chamfer: bool
    ) -> SolidResult:
        """Round or bevel edges, keeping every face attributable afterwards.

        Blends are where OCCT most often gives up, and it does so by returning a
        self-intersecting or unfinished shape rather than by raising. Each
        outcome is checked explicitly and turned into a reason a user can act on,
        because "the fillet failed" with no radius or edge count is useless.
        """
        source = self._lookup_solid(base)
        kind = "chamfer" if chamfer else "fillet"
        if request.size <= 0:
            raise FeatureBuildError(
                feature=request.feature,
                reason=f"{kind} size must be positive, got {request.size:.6g}",
            )
        if not request.edges:
            raise FeatureBuildError(
                feature=request.feature, reason=f"the {kind} selected no edges"
            )

        builder: object
        if chamfer:
            maker = BRepFilletAPI_MakeChamfer(source.shape)
            for ref in request.edges:
                entry = source.edges.get(ref)
                if entry is None:
                    raise FeatureBuildError(
                        feature=request.feature, reason=f"unknown edge '{ref}'"
                    )
                maker.Add(request.size, entry[0])
            builder = maker
        else:
            maker_f = BRepFilletAPI_MakeFillet(source.shape)
            for ref in request.edges:
                entry = source.edges.get(ref)
                if entry is None:
                    raise FeatureBuildError(
                        feature=request.feature, reason=f"unknown edge '{ref}'"
                    )
                maker_f.Add(request.size, entry[0])
            builder = maker_f

        try:
            builder.Build()  # type: ignore[attr-defined]
        except Exception as error:  # OCCT raises Standard_Failure subclasses
            raise FeatureBuildError(
                feature=request.feature,
                reason=(
                    f"the {kind} of {request.size:.6g}mm on {len(request.edges)} edge(s) "
                    f"failed in the kernel: {error}. Blends fail when the size exceeds "
                    "the space available — try a smaller one, or fewer edges."
                ),
            ) from error

        if not builder.IsDone():  # type: ignore[attr-defined]
            raise FeatureBuildError(
                feature=request.feature,
                reason=(
                    f"the {kind} of {request.size:.6g}mm on {len(request.edges)} edge(s) "
                    "could not be completed. Blends fail when the size exceeds the space "
                    "available — try a smaller one, or fewer edges."
                ),
            )

        result = builder.Shape()  # type: ignore[attr-defined]
        if not _faces_of(result):
            raise FeatureBuildError(
                feature=request.feature, reason=f"the {kind} produced an empty solid"
            )

        result_faces = _ShapeSet(_faces_of(result))
        provenance = _ShapeMap()
        known = _ShapeMap()
        surviving: set[Ref] = set()

        for ref, face in source.faces:
            images, changed = _images_and_change(builder, face, result_faces)
            for image in images:
                provenance.set(image, FaceProvenance(origin=Origin.INHERITED, parent=ref))
                surviving.add(ref)
                if not changed:
                    kept = source.fingerprints.get(ref)
                    if kept is not None:
                        known.set(image, kept)

        # A blend face is named by the edge it replaced, and that edge is named
        # by the two faces it separated — so no new naming concept is needed.
        for ref in request.edges:
            entry = source.edges.get(ref)
            if entry is None:
                continue
            edge, adjacent = entry
            for shape in _shape_list(builder.Generated(edge)):  # type: ignore[attr-defined]
                face = TopoDS.Face_s(shape)
                if result_faces.contains(face) and provenance.get(face) is None:
                    provenance.set(
                        face, FaceProvenance(origin=Origin.BLEND, parents=adjacent)
                    )

        deleted = tuple(
            DeletedFace(ref=ref, reason="consumed")
            for ref, _ in source.faces
            if ref not in surviving
        )
        # OCCT also builds transition patches where blends meet at a corner, and
        # attributes them to a vertex rather than to an edge — so `Generated`
        # never claims them and they arrive here unattributed. They are reported
        # as BLEND_CORNER with the faces that bound them, which the naming
        # engine turns into a corner tag.
        stored = self._store(
            result,
            lambda face: _lookup(provenance, face)
            or FaceProvenance(origin=Origin.UNKNOWN),
            deleted=deleted,
            known=known,
        )
        return self._attribute_corners(stored, request.feature, kind)

    def _attribute_corners(
        self, result: SolidResult, feature: str, kind: str
    ) -> SolidResult:
        """Give every still-unattributed face the faces that surround it.

        Deliberately a second pass over the *stored* result: refs are assigned
        in canonical geometric order there, and adjacency is already computed
        for the edge records, so a corner's parents are stable across builds
        without any extra geometry.
        """
        unknown = {
            record.ref
            for record in result.faces
            if record.provenance.origin == Origin.UNKNOWN
        }
        if not unknown:
            return result

        neighbours: dict[Ref, list[Ref]] = {ref: [] for ref in unknown}
        for edge in result.edges:
            first, second = edge.faces
            for ref, other in ((first, second), (second, first)):
                # Only faces that *are* attributed can lend a name, so a corner
                # touching another corner does not depend on naming order.
                if ref in unknown and other not in unknown and other not in neighbours[ref]:
                    neighbours[ref].append(other)

        stranded = [ref for ref in unknown if len(neighbours[ref]) < 3]
        if stranded:
            raise FeatureBuildError(
                feature=feature,
                reason=(
                    f"the {kind} produced {len(stranded)} face(s) at a corner with fewer "
                    "than three named neighbours, so they cannot be identified. Widen "
                    "the selector to cover the whole run of edges meeting there."
                ),
            )

        faces = tuple(
            record
            if record.ref not in unknown
            else FaceRecord(
                ref=record.ref,
                provenance=FaceProvenance(
                    origin=Origin.BLEND_CORNER,
                    parents=tuple(neighbours[record.ref]),
                ),
                fingerprint=record.fingerprint,
            )
            for record in result.faces
        )
        stored = self._solids[result.solid.id]
        for record in faces:
            stored.provenance[record.ref] = record.provenance
            stored.fingerprints[record.ref] = record.fingerprint
        return SolidResult(
            solid=result.solid,
            faces=faces,
            edges=result.edges,
            deleted=result.deleted,
        )

    # -- queries -----------------------------------------------------------

    def tessellate(self, solid_handle: SolidHandle, tolerance: float = 0.1) -> Tessellation:
        solid = self._lookup_solid(solid_handle)
        BRepMesh_IncrementalMesh(solid.shape, tolerance, False, 0.5, True)

        positions: list[float] = []
        normals: list[float] = []
        indices: list[int] = []
        ranges: list[FaceRange] = []

        for ref, face in solid.faces:
            location = TopLoc_Location()
            triangulation = BRep_Tool.Triangulation_s(face, location)
            start = len(indices)
            if triangulation is not None:
                transform = location.Transformation()
                base_index = len(positions) // 3
                reversed_face = face.Orientation() == TopAbs_REVERSED
                normal = _face_normal(face)

                for node in range(1, triangulation.NbNodes() + 1):
                    point = triangulation.Node(node).Transformed(transform)
                    positions.extend((point.X(), point.Y(), point.Z()))
                    normals.extend(normal.as_tuple())

                for index in range(1, triangulation.NbTriangles() + 1):
                    a, b, c = triangulation.Triangle(index).Get()
                    # OCCT triangle winding follows the face's own orientation.
                    triangle = (a, c, b) if reversed_face else (a, b, c)
                    indices.extend(base_index + node - 1 for node in triangle)
            ranges.append(FaceRange(ref=ref, start=start, count=len(indices) - start))

        edges: list[EdgePolyline] = []
        for number, edge in enumerate(_unique_edges_of(solid.shape)):
            points = _discretise(edge, tolerance)
            if len(points) >= 6:
                edges.append(EdgePolyline(ref=f"e{number}", points=tuple(points)))

        return Tessellation(
            positions=tuple(positions),
            normals=tuple(normals),
            indices=tuple(indices),
            face_ranges=tuple(ranges),
            edges=tuple(edges),
        )

    def bounding_box(self, solid_handle: SolidHandle) -> BoundingBox:
        solid = self._lookup_solid(solid_handle)
        box = Bnd_Box()
        # AddOptimal rather than Add: the latter inflates the box by the shape's
        # tolerance, which would report a 6mm plate as 6.0000001mm thick.
        BRepBndLib.AddOptimal_s(solid.shape, box, True, False)
        if box.IsVoid():
            return BoundingBox()
        return BoundingBox(min=box.CornerMin().Coord(), max=box.CornerMax().Coord())

    def volume(self, solid_handle: SolidHandle) -> float:
        return self._volume_of(self._lookup_solid(solid_handle))

    def _volume_of(self, solid: _Solid) -> float:
        """A stored solid's volume, integrated once and remembered."""
        if solid._volume_cache is None:
            solid._volume_cache = _volume(solid.shape)
        return solid._volume_cache

    def release(self, solid_handle: SolidHandle) -> None:
        self._solids.pop(solid_handle.id, None)

    # -- STEP export -------------------------------------------------------

    def thread(self, base: SolidHandle, request: ThreadRequest) -> SolidResult:
        """Cut a helical thread form, internal or external.

        Every face the cut creates is reported as swept from the placement
        point, so a thread needs no naming rule of its own: the handler's
        vocabulary turns "swept" into the ``thread`` role and the tag reads
        ``m6/thread[plate.h1]`` — the same shape of name as a hole's wall.
        """
        source = self._lookup_solid(base)
        operation = cut_thread(source.shape, request)
        shape = operation.Shape()

        if not _faces_of(shape):
            raise FeatureBuildError(
                feature=request.feature, reason="the thread would remove the entire body"
            )
        faces = _faces_of(shape)
        result_faces = _ShapeSet(faces)

        provenance = _ShapeMap()
        known = _ShapeMap()
        surviving: set[Ref] = set()
        inherited = 0
        for ref, face in source.faces:
            images, changed = _images_and_change(operation, face, result_faces)
            for image in images:
                provenance.set(image, FaceProvenance(origin=Origin.INHERITED, parent=ref))
                surviving.add(ref)
                inherited += 1
                if not changed:
                    kept = source.fingerprints.get(ref)
                    if kept is not None:
                        known.set(image, kept)

        # Faces the source cannot account for are the helix the cut left behind.
        # A thread whose axis missed the material produces none, so this is the
        # same free proof a pocket gets from its tool's walls.
        if not self._cut_took_material(
            len(faces) - inherited, source, shape, _tool_of(operation)
        ):
            raise FeatureBuildError(
                feature=request.feature,
                reason=(
                    "the thread cuts nothing — check that its axis passes through "
                    "material and that the diameter matches the bore or shaft"
                ),
            )

        thread_face = FaceProvenance(origin=Origin.SWEPT, curve=request.curve)
        deleted = tuple(
            DeletedFace(ref=ref, reason="threaded")
            for ref, _ in source.faces
            if ref not in surviving
        )
        # `_lookup` substitutes UNKNOWN for a miss, which is truthy, so the
        # fallback has to test the map directly.
        def attribute(face: TopoDS_Face) -> FaceProvenance:
            found = provenance.get(face)
            return found if isinstance(found, FaceProvenance) else thread_face

        return self._store(shape, attribute, deleted=deleted, known=known)

    def face_profile(
        self, solid_handle: SolidHandle, ref: Ref, tolerance: float = 0.01
    ) -> Profile2D:
        """The cut path of one planar face, in that face's own plane."""
        solid = self._lookup_solid(solid_handle)
        for stored_ref, face in solid.faces:
            if stored_ref == ref:
                return face_profile(face, tolerance, label=ref, edge_refs=solid.edge_ref)
        raise FeatureBuildError(feature="face", reason=f"unknown face '{ref}'")

    def section_profile(
        self, solid_handle: SolidHandle, frame: Frame, tolerance: float = 0.01
    ) -> Profile2D:
        solid = self._lookup_solid(solid_handle)
        return section_profile(solid.shape, frame, tolerance, label="section")

    def export_drawing(
        self, solid_handle: SolidHandle, fmt: str, views: Sequence[str] = ()
    ) -> bytes:
        """Orthographic views, as flat sections through the solid.

        Sections rather than hidden-line projection: a section is exactly what a
        CNC or laser operator wants (the material actually present at that
        height), it is unambiguous, and it cannot silently drop a feature the
        way a mis-tuned HLR pass can.
        """
        solid = self._lookup_solid(solid_handle)
        box = self.bounding_box(solid_handle)
        centre = tuple((lo + hi) / 2.0 for lo, hi in zip(box.min, box.max, strict=True))

        wanted = tuple(views) or ("top",)
        profiles: list[Profile2D] = []
        for view in wanted:
            frame = _view_frame(view, centre)
            profiles.append(
                section_profile(solid.shape, frame, DRAWING_TOLERANCE, label=view)
            )
        return export_drawing(profiles, fmt, title=solid_handle.id)

    # -- snapshots ---------------------------------------------------------

    def snapshot(self, solid_handle: SolidHandle) -> bytes:
        """Serialise a stored solid, with everything needed to name it again.

        The geometry goes out through ``BinTools`` rather than ``BRepTools``. The
        ASCII form loses about 1e-12 on a coordinate, and refs here are assigned
        by sorting on centroid and area — that drift sits far below the sort's
        own rounding and would almost certainly never reorder anything, but
        "almost certainly" is the failure mode this project exists to remove. The
        binary form is exactly equal, half the size and four times faster.
        Measured on a 403-face body: 857kB, 7ms out, 10ms back, every fingerprint
        identical to the last bit.

        The refs and their provenance travel alongside, because a B-rep read off
        a disk has no history: nothing in the file says a face was swept from
        curve ``c1``. Storing the refs in canonical order rather than recomputing
        them also means the restore can *check* the ordering it derives instead
        of assuming it.

        Via a file because OCP's ``BytesIO`` overload does not round-trip — it
        writes a stream ``Read_s`` rejects with a bad point representation. The
        same reason ``export_brep`` below uses one.
        """
        solid = self._lookup_solid(solid_handle)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "solid.bin"
            if not BinTools.Write_s(solid.shape, str(path)):
                raise FeatureBuildError(
                    feature="<snapshot>", reason="the solid could not be serialised"
                )
            geometry = path.read_bytes()

        return pickle.dumps(
            {
                "format": _SNAPSHOT_FORMAT,
                "geometry": geometry,
                # In the order refs were assigned, which is the order the sort
                # below has to reproduce.
                "refs": [ref for ref, _ in solid.faces],
                "provenance": {ref: solid.provenance.get(ref) for ref, _ in solid.faces},
                "fingerprints": {ref: solid.fingerprints.get(ref) for ref, _ in solid.faces},
            },
            protocol=pickle.HIGHEST_PROTOCOL,
        )

    def restore(self, blob: bytes) -> SolidResult:
        """Register a solid from :meth:`snapshot`, with the refs it had.

        No geometry is rebuilt, but the *ordering* is, and then checked against
        what was stored. If the canonical sort over the restored shape does not
        reproduce the recorded fingerprints face for face, the names no longer
        describe this solid and the whole thing is refused — the alternative
        being a selector that quietly resolves to a different face.
        """
        try:
            state = pickle.loads(blob)
            geometry = state["geometry"]
            refs: list[Ref] = list(state["refs"])
            provenance: dict[Ref, FaceProvenance] = state["provenance"]
            recorded: dict[Ref, FaceFingerprint] = state["fingerprints"]
            version = state["format"]
        except Exception as error:
            raise FeatureBuildError(
                feature="<snapshot>", reason=f"the stored solid is unreadable: {error}"
            ) from error

        if version != _SNAPSHOT_FORMAT:
            raise FeatureBuildError(
                feature="<snapshot>",
                reason=f"stored in format {version!r}, this kernel writes {_SNAPSHOT_FORMAT}",
            )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "solid.bin"
            path.write_bytes(geometry)
            shape = TopoDS_Shape()
            try:
                BinTools.Read_s(shape, str(path))
            except Exception as error:  # OCCT raises Standard_Failure subclasses
                raise FeatureBuildError(
                    feature="<snapshot>",
                    reason=f"the stored solid could not be read back: {error}",
                ) from error

        faces = _faces_of(shape)
        if len(faces) != len(refs):
            raise FeatureBuildError(
                feature="<snapshot>",
                reason=(
                    f"the stored solid had {len(refs)} faces and read back with "
                    f"{len(faces)}"
                ),
            )

        ordered = [(face, _face_fingerprint(face)) for face in faces]
        ordered.sort(key=lambda item: _sort_key(item[1]))
        for ref, (_, fingerprint) in zip(refs, ordered, strict=True):
            if recorded.get(ref) != fingerprint:
                raise FeatureBuildError(
                    feature="<snapshot>",
                    reason=(
                        f"face '{ref}' read back in a different position; the stored "
                        "names do not describe this solid"
                    ),
                )

        return self._adopt(shape, ordered, refs, provenance)

    def _adopt(
        self,
        shape: TopoDS_Shape,
        ordered: list[tuple[TopoDS_Face, FaceFingerprint]],
        refs: Sequence[Ref],
        provenance: Mapping[Ref, FaceProvenance],
    ) -> SolidResult:
        """Register a verified restored shape under the refs it already had."""
        self._counter += 1
        handle = SolidHandle(id=f"s{self._counter}", kernel=self.name)
        stored = _Solid(handle=handle, shape=shape)
        self._solids[handle.id] = stored

        unknown = FaceProvenance(origin=Origin.UNKNOWN)
        records: list[FaceRecord] = []
        index_of = _ShapeMap()
        for ref, (face, fingerprint) in zip(refs, ordered, strict=True):
            origin = provenance.get(ref) or unknown
            stored.faces.append((ref, face))
            stored.fingerprints[ref] = fingerprint
            stored.provenance[ref] = origin
            index_of.set(face, ref)
            records.append(
                FaceRecord(ref=ref, provenance=origin, fingerprint=fingerprint)
            )

        edge_records = _edge_records(shape, index_of)
        for record, edge in edge_records:
            stored.edges[record.ref] = (edge, record.faces)

        return SolidResult(
            solid=handle,
            faces=tuple(records),
            edges=tuple(record for record, _ in edge_records),
        )

    def export_brep(self, solid_handle: SolidHandle, fmt: str) -> bytes:
        """Write STEP — the reason to use a B-rep kernel at all."""
        from OCP.IFSelect import IFSelect_ReturnStatus
        from OCP.Interface import Interface_Static
        from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

        if fmt not in ("step", "stp"):
            raise FeatureBuildError(feature="<export>", reason=f"unsupported format {fmt!r}")
        solid = self._lookup_solid(solid_handle)

        writer = STEPControl_Writer()
        Interface_Static.SetCVal_s("write.step.schema", "AP214IS")
        writer.Transfer(solid.shape, STEPControl_AsIs)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.step"
            if writer.Write(str(path)) != IFSelect_ReturnStatus.IFSelect_RetDone:
                raise FeatureBuildError(feature="<export>", reason="STEP export failed")
            return path.read_bytes()

    # -- internals ---------------------------------------------------------

    def _store(
        self,
        shape: TopoDS_Shape,
        attribute,
        deleted: tuple[DeletedFace, ...] = (),
        known: _ShapeMap | None = None,
    ) -> SolidResult:
        """Register a result, assigning refs in a canonical, reproducible order.

        ``known`` carries the fingerprints of faces the operation did not touch.
        They are reused rather than re-integrated: identical values by
        construction, so refs still sort the same way, and the measurement that
        dominated a long history disappears.
        """
        faces = _faces_of(shape)
        fingerprints = [
            (face, _reuse_or_measure(known, face)) for face in faces
        ]
        # Sorting by geometry rather than by OCCT's enumeration is what makes
        # refs identical across two builds of the same document.
        fingerprints.sort(key=lambda item: _sort_key(item[1]))

        self._counter += 1
        handle = SolidHandle(id=f"s{self._counter}", kernel=self.name)
        stored = _Solid(handle=handle, shape=shape)
        self._solids[handle.id] = stored

        records: list[FaceRecord] = []
        index_of = _ShapeMap()
        for index, (face, fingerprint) in enumerate(fingerprints):
            ref = f"f{index}"
            origin = attribute(face)
            stored.faces.append((ref, face))
            stored.fingerprints[ref] = fingerprint
            stored.provenance[ref] = origin
            index_of.set(face, ref)
            records.append(
                FaceRecord(ref=ref, provenance=origin, fingerprint=fingerprint)
            )

        edge_records = _edge_records(shape, index_of)
        for record, edge in edge_records:
            stored.edges[record.ref] = (edge, record.faces)

        return SolidResult(
            solid=handle,
            faces=tuple(records),
            edges=tuple(record for record, _ in edge_records),
            deleted=deleted,
        )

    def _cut_took_material(
        self, lateral: int, source: _Solid, after: TopoDS_Shape, tool: TopoDS_Shape
    ) -> bool:
        """Whether a cut took anything away.

        ``lateral`` is how many faces of the *result* came from the tool's swept
        sides — a number the provenance pass has already worked out. A tool that
        removed material left some of its own wall behind; a tool that only
        grazed a face left none, which is exactly the direction mistake this
        check exists to catch. A positive count is therefore proof, at no cost.

        Only when there is no such proof is the volume difference measured, and
        then it decides, exactly as it always did. Three whole-body integrations
        per cut were a fifth of a rebuild here, and on every cut that worked they
        were confirming what the history map had already said.
        """
        if lateral > 0:
            return True
        removed = self._volume_of(source) - _volume(after)
        return removed > _volume(tool) * _CUT_FRACTION

    def _lookup_solid(self, handle: SolidHandle) -> _Solid:
        try:
            return self._solids[handle.id]
        except KeyError:
            raise FeatureBuildError(
                feature="<kernel>", reason=f"unknown solid handle '{handle.id}'"
            ) from None


def _sort_key(fingerprint: FaceFingerprint) -> tuple:
    centroid = fingerprint.centroid
    return (
        round(centroid.x, _SORT_DIGITS),
        round(centroid.y, _SORT_DIGITS),
        round(centroid.z, _SORT_DIGITS),
        round(fingerprint.area, _SORT_DIGITS),
        fingerprint.surface,
    )


def _reuse_or_measure(known: _ShapeMap | None, face: TopoDS_Face) -> FaceFingerprint:
    if known is not None:
        kept = known.get(face)
        if isinstance(kept, FaceFingerprint):
            return kept
    return _face_fingerprint(face)


def _lookup(provenance: _ShapeMap, face: TopoDS_Face) -> FaceProvenance:
    found = provenance.get(face)
    return found if isinstance(found, FaceProvenance) else FaceProvenance(origin=Origin.UNKNOWN)


def _edge_records(
    shape: TopoDS_Shape, index_of: _ShapeMap
) -> list[tuple[EdgeRecord, TopoDS_Edge]]:
    """Report each edge as the pair of faces it separates, with the edge itself.

    The edge travels alongside its record so a later blend can act on exactly the
    edge the user selected, rather than re-finding it geometrically.
    """
    # Walked face by face rather than through MapShapesAndAncestors: that map
    # answers per edge with a TopTools_ListOfShape, and reading 409 two-item
    # lists out of OCP costs 70ms against 1.2ms for this. The result is the
    # same edges with the same adjacency.
    adjacency: dict[int, tuple[TopoDS_Edge, list[Ref]]] = {}
    order: list[int] = []
    for face in _faces_of(shape):
        ref = index_of.get(face)
        if not isinstance(ref, str):
            continue
        explorer = TopExp_Explorer(face, TopAbs_EDGE)
        while explorer.More():
            edge = TopoDS.Edge_s(explorer.Current())
            key = hash(edge)
            entry = adjacency.get(key)
            if entry is None:
                adjacency[key] = (edge, [ref])
                order.append(key)
            elif ref not in entry[1]:
                entry[1].append(ref)
            explorer.Next()

    records: list[tuple[EdgeRecord, TopoDS_Edge]] = []
    for key in order:
        edge, refs = adjacency[key]
        # A seam edge belongs to one face and carries no two-face identity.
        if len(refs) < 2:
            continue
        fingerprint = _edge_fingerprint(edge)
        for first, second in itertools.combinations(sorted(refs), 2):
            records.append(
                (
                    EdgeRecord(
                        ref=f"e{len(records)}",
                        faces=(first, second),
                        fingerprint=fingerprint,
                    ),
                    edge,
                )
            )
    return records


def _discretise(edge: TopoDS_Edge, tolerance: float) -> list[float]:
    curve = BRepAdaptor_Curve(edge)
    sampler = GCPnts_QuasiUniformDeflection(curve, max(tolerance, 1e-3))
    if not sampler.IsDone():
        return []
    points: list[float] = []
    for index in range(1, sampler.NbPoints() + 1):
        point = sampler.Value(index)
        points.extend((point.X(), point.Y(), point.Z()))
    return points


def _volume(shape: TopoDS_Shape) -> float:
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return props.Mass()


#: A cut has to remove at least this fraction of its own tool to count.
#: Well above OCCT's numerical noise, well below any feature a person means.
_CUT_FRACTION = 1e-6


def _tool_of(operation: BRepAlgoAPI_Cut) -> TopoDS_Shape:
    """The shape a boolean cut with, without building it a second time.

    Rebuilding a thread tool costs seconds, so it is read back off the operation
    rather than recomputed just to measure it.
    """
    return _shape_list(operation.Tools())[0]


def _removed_material(before: TopoDS_Shape, after: TopoDS_Shape, tool: TopoDS_Shape) -> bool:
    """Whether a cut actually took anything away.

    Compared against the *tool's* volume rather than the body's, and as a
    fraction rather than an absolute. Both matter.

    A tool that lies flush against a face — a pocket drilled the wrong way from
    a sketch on the surface it should have entered — removes nothing, but the
    boolean still imprints the tool's outline on that face and reports a volume
    a fraction of a cubic millimetre lighter. That noise scales with the body,
    so an absolute tolerance passes it on a large part and the feature 'succeeds'
    having only split a face in two. Measured on a 3833mm3 body: 1e-4mm3 of
    drift, against a 1e-9 threshold.

    The tool's own volume is the honest yardstick, because it is what the cut
    was supposed to remove.
    """
    removed = _volume(before) - _volume(after)
    return removed > _volume(tool) * _CUT_FRACTION


def _diagonal(shape: TopoDS_Shape) -> float:
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, True, False)
    if box.IsVoid():
        return 1.0
    low, high = box.CornerMin(), box.CornerMax()
    return max((_to_vec(high) - _to_vec(low)).length(), 1.0)


def _shifted(profile: Profile, offset: Vec3) -> Profile:
    """The same profile on a frame translated by ``offset``."""
    frame = profile.frame
    return Profile(
        sketch=profile.sketch,
        loop=profile.loop,
        frame=Frame(
            origin=frame.origin + offset,
            x_axis=frame.x_axis,
            y_axis=frame.y_axis,
            z_axis=frame.z_axis,
        ),
        curves=profile.curves,
    )


#: How finely a free-form section curve is approximated for a drawing (mm).
DRAWING_TOLERANCE = 0.01

#: Standard views, each a plane through the middle of the part. Named rather
#: than given as vectors so a document can ask for "front" and mean it.
_VIEW_NORMALS: dict[str, Vec3] = {
    "top": Vec3(0.0, 0.0, 1.0),
    "bottom": Vec3(0.0, 0.0, -1.0),
    "front": Vec3(0.0, -1.0, 0.0),
    "back": Vec3(0.0, 1.0, 0.0),
    "right": Vec3(1.0, 0.0, 0.0),
    "left": Vec3(-1.0, 0.0, 0.0),
}


def _view_frame(view: str, centre: tuple[float, float, float]) -> Frame:
    normal = _VIEW_NORMALS.get(view.strip().lower())
    if normal is None:
        raise DocumentError(
            reason=(
                f"unknown view {view!r}; choose from "
                f"{', '.join(sorted(_VIEW_NORMALS))}"
            ),
            path="export",
        )
    return Frame.from_origin_normal(Vec3(*centre), normal)
