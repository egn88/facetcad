"""An exact analytic kernel for axis-aligned prismatic solids.

Why this exists
---------------

It is not a toy stub. It is a second, independent implementation of
:class:`~facet.application.ports.geometry.GeometryKernel` and it earns its
keep three ways:

1. **It proves the port is honest.** A port with one implementation is a guess.
   Any OCCT concept that leaked into the application layer would show up here as
   something the fake cannot possibly provide.
2. **It makes the naming engine testable at speed.** The parameter-sweep
   stability suite runs thousands of rebuilds; against OCCT that is minutes,
   against this it is under a second.
3. **It bisects failures.** Running a misbehaving model against both kernels
   answers "is this our naming layer or the kernel?" immediately.

How it works
------------

Solids are represented as an occupancy set over a non-uniform axis-aligned grid.
Boolean operations refine the grid and flip occupancy, which is exact — there is
no tessellation and no tolerance fudging. Faces are recovered by finding exposed
cell faces and merging them into connected components, so a pocket that cuts a
top face into two disconnected regions genuinely produces two faces, which is
exactly the split case the naming engine must handle.

Scope is deliberately narrow: axis-aligned rectangular profiles only. Anything
else is refused via the capability mechanism rather than approximated, because a
test kernel that quietly disagrees with the real one is worse than none.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

from facet.application.ports.geometry import (
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
    Ref,
    SolidHandle,
    SolidResult,
    Tessellation,
)
from facet.domain.errors import FeatureBuildError
from facet.domain.fingerprint import (
    CurveKind,
    EdgeFingerprint,
    FaceFingerprint,
    SurfaceKind,
)
from facet.domain.math3d import Vec3

_SNAP = 9
#: The two in-plane axes for each principal axis, always in ascending order.
_IN_PLANE: dict[int, tuple[int, int]] = {0: (1, 2), 1: (0, 2), 2: (0, 1)}
_AXIS_VECTORS = (Vec3(1, 0, 0), Vec3(0, 1, 0), Vec3(0, 0, 1))


def _snap(value: float) -> float:
    return round(value, _SNAP)


# --------------------------------------------------------------------------
# Internal representation
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Box:
    """A closed axis-aligned interval box, in world coordinates."""

    lo: tuple[float, float, float]
    hi: tuple[float, float, float]

    def contains_point(self, point: tuple[float, float, float]) -> bool:
        return all(self.lo[a] <= point[a] <= self.hi[a] for a in range(3))


@dataclass(frozen=True, slots=True)
class _PrismRecord:
    """The prism an operation swept, and how its boundary planes are named.

    This is what lets the adapter answer "which profile curve produced this
    face?" without storing a per-face map that would have to be remapped every
    time the grid refines.
    """

    feature: str
    box: _Box
    axis: int
    #: (axis, side, plane coordinate) -> profile curve id
    curve_planes: dict[tuple[int, int, float], str]
    start_plane: float
    end_plane: float


@dataclass(frozen=True, slots=True)
class _Rect:
    """A rectangle on an axis-aligned plane, in the plane's (u, v) coordinates."""

    u0: float
    u1: float
    v0: float
    v1: float

    @property
    def area(self) -> float:
        return (self.u1 - self.u0) * (self.v1 - self.v0)

    @property
    def center(self) -> tuple[float, float]:
        return ((self.u0 + self.u1) / 2, (self.v0 + self.v1) / 2)

    def contains(self, u: float, v: float) -> bool:
        return self.u0 <= u <= self.u1 and self.v0 <= v <= self.v1


@dataclass(frozen=True)
class _FaceComponent:
    """One connected planar face of the solid."""

    ref: Ref
    axis: int
    side: int
    plane: float
    rects: tuple[_Rect, ...]
    provenance: FaceProvenance

    @property
    def normal(self) -> Vec3:
        return _AXIS_VECTORS[self.axis] * float(self.side)

    def area(self) -> float:
        return sum(r.area for r in self.rects)

    def centroid_world(self) -> Vec3:
        total = self.area() or 1.0
        u = sum(r.center[0] * r.area for r in self.rects) / total
        v = sum(r.center[1] * r.area for r in self.rects) / total
        return self._to_world(u, v)

    def sample_world(self) -> Vec3:
        u, v = self.rects[0].center
        return self._to_world(u, v)

    def _to_world(self, u: float, v: float) -> Vec3:
        au, av = _IN_PLANE[self.axis]
        coords = [0.0, 0.0, 0.0]
        coords[self.axis] = self.plane
        coords[au] = u
        coords[av] = v
        return Vec3(*coords)

    def contains_world(self, point: Vec3) -> bool:
        coords = point.as_tuple()
        if abs(coords[self.axis] - self.plane) > 1e-9:
            return False
        au, av = _IN_PLANE[self.axis]
        return any(r.contains(coords[au], coords[av]) for r in self.rects)

    def fingerprint(self) -> FaceFingerprint:
        return FaceFingerprint(
            surface=SurfaceKind.PLANE,
            area=self.area(),
            centroid=self.centroid_world(),
            normal=self.normal,
        )


@dataclass
class _Solid:
    """An occupancy grid plus the history needed to attribute provenance."""

    handle: SolidHandle
    xs: list[float]
    ys: list[float]
    zs: list[float]
    occupied: set[tuple[int, int, int]]
    faces: tuple[_FaceComponent, ...] = ()
    prism: _PrismRecord | None = None

    def axis_coords(self, axis: int) -> list[float]:
        return (self.xs, self.ys, self.zs)[axis]

    def set_axis_coords(self, axis: int, coords: list[float]) -> None:
        if axis == 0:
            self.xs = coords
        elif axis == 1:
            self.ys = coords
        else:
            self.zs = coords

    def cell_bounds(self, cell: tuple[int, int, int]) -> _Box:
        i, j, k = cell
        return _Box(
            lo=(self.xs[i], self.ys[j], self.zs[k]),
            hi=(self.xs[i + 1], self.ys[j + 1], self.zs[k + 1]),
        )

    def cell_center(self, cell: tuple[int, int, int]) -> tuple[float, float, float]:
        bounds = self.cell_bounds(cell)
        return tuple((bounds.lo[a] + bounds.hi[a]) / 2 for a in range(3))  # type: ignore[return-value]

    def counts(self) -> tuple[int, int, int]:
        return (len(self.xs) - 1, len(self.ys) - 1, len(self.zs) - 1)


# --------------------------------------------------------------------------
# Grid refinement
# --------------------------------------------------------------------------


def _refine_axis(
    existing: list[float], additions: Iterable[float]
) -> tuple[list[float], list[list[int]]]:
    """Insert coordinates, returning new coords and old-cell -> new-cells mapping."""
    merged = sorted({_snap(c) for c in [*existing, *additions]})
    mapping: list[list[int]] = []
    for index in range(len(existing) - 1):
        lo, hi = _snap(existing[index]), _snap(existing[index + 1])
        start = merged.index(lo)
        stop = merged.index(hi)
        mapping.append(list(range(start, stop)))
    return merged, mapping


def _refine(solid: _Solid, box: _Box) -> None:
    """Refine the grid so ``box``'s faces fall on grid planes."""
    mappings: list[list[list[int]]] = []
    for axis in range(3):
        coords, mapping = _refine_axis(
            solid.axis_coords(axis), (box.lo[axis], box.hi[axis])
        )
        solid.set_axis_coords(axis, coords)
        mappings.append(mapping)

    refined: set[tuple[int, int, int]] = set()
    for i, j, k in solid.occupied:
        for ni, nj, nk in itertools.product(mappings[0][i], mappings[1][j], mappings[2][k]):
            refined.add((ni, nj, nk))
    solid.occupied = refined


# --------------------------------------------------------------------------
# Face extraction
# --------------------------------------------------------------------------


def _exposed_cell_faces(
    solid: _Solid,
) -> dict[tuple[int, int, float], list[tuple[int, int, _Rect]]]:
    """Group exposed cell faces by (axis, side, plane), keyed by in-plane indices."""
    grouped: dict[tuple[int, int, float], list[tuple[int, int, _Rect]]] = {}
    counts = solid.counts()
    coords = (solid.xs, solid.ys, solid.zs)

    for cell in solid.occupied:
        for axis in range(3):
            for side in (-1, 1):
                neighbour = list(cell)
                neighbour[axis] += side
                index = neighbour[axis]
                if 0 <= index < counts[axis] and tuple(neighbour) in solid.occupied:
                    continue
                plane_index = cell[axis] + (1 if side == 1 else 0)
                plane = _snap(coords[axis][plane_index])
                au, av = _IN_PLANE[axis]
                rect = _Rect(
                    u0=coords[au][cell[au]],
                    u1=coords[au][cell[au] + 1],
                    v0=coords[av][cell[av]],
                    v1=coords[av][cell[av] + 1],
                )
                grouped.setdefault((axis, side, plane), []).append(
                    (cell[au], cell[av], rect)
                )
    return grouped


def _connected_components(
    cell_faces: Sequence[tuple[int, int, _Rect]],
) -> list[list[_Rect]]:
    """Split coplanar cell faces into connected regions (4-neighbourhood)."""
    index_of = {(u, v): position for position, (u, v, _) in enumerate(cell_faces)}
    seen: set[int] = set()
    components: list[list[_Rect]] = []

    for position, (u, v, _) in enumerate(cell_faces):
        if position in seen:
            continue
        stack = [(u, v)]
        seen.add(position)
        member_rects: list[_Rect] = []
        while stack:
            cu, cv = stack.pop()
            member_rects.append(cell_faces[index_of[(cu, cv)]][2])
            for du, dv in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                neighbour = (cu + du, cv + dv)
                neighbour_index = index_of.get(neighbour)
                if neighbour_index is not None and neighbour_index not in seen:
                    seen.add(neighbour_index)
                    stack.append(neighbour)
        components.append(member_rects)
    return components


def _attribute(
    axis: int,
    side: int,
    plane: float,
    sample: Vec3,
    prism: _PrismRecord | None,
    previous: Sequence[_FaceComponent],
) -> FaceProvenance:
    """Decide where a face came from.

    Inheritance is checked **first**: a face that already existed and is still
    exposed keeps its identity even when it happens to be coplanar with the new
    operation's boundary. Attributing it to the new feature instead would be
    precisely the kind of silent renaming this project exists to prevent.
    """
    for candidate in previous:
        if candidate.contains_world(sample):
            return FaceProvenance(origin=Origin.INHERITED, parent=candidate.ref)

    if prism is not None:
        curve = prism.curve_planes.get((axis, side, plane))
        if curve is not None:
            return FaceProvenance(origin=Origin.SWEPT, curve=curve)
        if axis == prism.axis:
            if abs(plane - prism.start_plane) < 1e-9:
                return FaceProvenance(origin=Origin.CAP_START)
            if abs(plane - prism.end_plane) < 1e-9:
                return FaceProvenance(origin=Origin.CAP_END)
    return FaceProvenance(origin=Origin.UNKNOWN)


def _extract_faces(
    solid: _Solid, prism: _PrismRecord | None, previous: Sequence[_FaceComponent]
) -> tuple[_FaceComponent, ...]:
    components: list[_FaceComponent] = []
    grouped = _exposed_cell_faces(solid)

    for (axis, side, plane), cell_faces in sorted(grouped.items()):
        for rects in _connected_components(cell_faces):
            ordered = tuple(sorted(rects, key=lambda r: (r.u0, r.v0)))
            draft = _FaceComponent(
                ref="", axis=axis, side=side, plane=plane, rects=ordered,
                provenance=FaceProvenance(Origin.UNKNOWN),
            )
            provenance = _attribute(axis, side, plane, draft.sample_world(), prism, previous)
            components.append(replace(draft, provenance=provenance))

    # Refs are assigned in a canonical order so results are reproducible.
    components.sort(key=lambda c: (c.axis, c.side, c.plane, c.rects[0].u0, c.rects[0].v0))
    return tuple(
        replace(component, ref=f"f{index}") for index, component in enumerate(components)
    )


# --------------------------------------------------------------------------
# Edge extraction — exact intersection of perpendicular planar faces
# --------------------------------------------------------------------------


def _edges_between(a: _FaceComponent, b: _FaceComponent) -> list[tuple[float, float, int]]:
    """Intervals along the shared axis where two perpendicular faces meet."""
    if a.axis == b.axis:
        return []
    shared = ({0, 1, 2} - {a.axis, b.axis}).pop()

    intervals: list[tuple[float, float]] = []
    for rect_a in a.rects:
        span_a = _span_along(a, rect_a, shared)
        if not _plane_within(a, rect_a, b.axis, b.plane):
            continue
        for rect_b in b.rects:
            if not _plane_within(b, rect_b, a.axis, a.plane):
                continue
            span_b = _span_along(b, rect_b, shared)
            lo = max(span_a[0], span_b[0])
            hi = min(span_a[1], span_b[1])
            if hi - lo > 1e-9:
                intervals.append((lo, hi))

    return [(lo, hi, shared) for lo, hi in _merge_intervals(intervals)]


def _span_along(face: _FaceComponent, rect: _Rect, axis: int) -> tuple[float, float]:
    au = _IN_PLANE[face.axis][0]
    return (rect.u0, rect.u1) if axis == au else (rect.v0, rect.v1)


def _plane_within(face: _FaceComponent, rect: _Rect, axis: int, plane: float) -> bool:
    au = _IN_PLANE[face.axis][0]
    lo, hi = (rect.u0, rect.u1) if axis == au else (rect.v0, rect.v1)
    return lo - 1e-9 <= plane <= hi + 1e-9


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not intervals:
        return []
    merged: list[tuple[float, float]] = []
    for lo, hi in sorted(intervals):
        if merged and lo <= merged[-1][1] + 1e-9:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def _extract_edges(faces: Sequence[_FaceComponent]) -> list[EdgeRecord]:
    records: list[EdgeRecord] = []
    counter = 0
    for a, b in itertools.combinations(faces, 2):
        for lo, hi, axis in _edges_between(a, b):
            midpoint = [0.0, 0.0, 0.0]
            for other in (a, b):
                midpoint[other.axis] = other.plane
            midpoint[axis] = (lo + hi) / 2
            records.append(
                EdgeRecord(
                    ref=f"e{counter}",
                    faces=(a.ref, b.ref),
                    fingerprint=EdgeFingerprint(
                        curve=CurveKind.LINE,
                        length=hi - lo,
                        midpoint=Vec3(*midpoint),
                        direction=_AXIS_VECTORS[axis],
                    ),
                )
            )
            counter += 1
    return records


# --------------------------------------------------------------------------
# Profile handling
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _ProfileBox:
    """The world-space extent of a rectangular profile, plus per-edge curve ids."""

    lo: tuple[float, float]
    hi: tuple[float, float]
    axis: int
    #: (axis, side) -> curve id, for the four lateral planes
    curves: dict[tuple[int, int], str]


def _analyse_profile(profile: Profile, feature: str) -> _ProfileBox:
    """Reduce an axis-aligned rectangular profile to a box with named sides.

    Anything else is refused outright. Approximating a circle here would make
    the fake kernel disagree with OCCT, which would defeat its purpose.
    """
    if len(profile.curves) != 4 or any(c.type != CurveType.LINE for c in profile.curves):
        raise FeatureBuildError(
            feature=feature,
            reason=(
                "the analytic test kernel supports only axis-aligned rectangular "
                f"profiles; sketch '{profile.sketch}' has {len(profile.curves)} curve(s) "
                "of which not all are lines"
            ),
        )

    frame = profile.frame
    normal_axis = _principal_axis(frame.z_axis, feature, "sketch plane normal")

    world_points: list[Vec3] = []
    for curve in profile.curves:
        if curve.start is None or curve.end is None:
            raise FeatureBuildError(
                feature=feature, reason=f"curve '{curve.id}' is missing endpoints"
            )
        world_points.append(frame.point_at(curve.start))
        world_points.append(frame.point_at(curve.end))

    au, av = _IN_PLANE[normal_axis]
    us = [p.as_tuple()[au] for p in world_points]
    vs = [p.as_tuple()[av] for p in world_points]
    lo = (_snap(min(us)), _snap(min(vs)))
    hi = (_snap(max(us)), _snap(max(vs)))

    if hi[0] - lo[0] <= 0 or hi[1] - lo[1] <= 0:
        raise FeatureBuildError(
            feature=feature, reason=f"profile '{profile.sketch}.{profile.loop}' is degenerate"
        )

    curves: dict[tuple[int, int], str] = {}
    for curve in profile.curves:
        assert curve.start is not None and curve.end is not None
        start, end = frame.point_at(curve.start), frame.point_at(curve.end)
        for axis, (low, high) in ((au, (lo[0], hi[0])), (av, (lo[1], hi[1]))):
            a_coord = _snap(start.as_tuple()[axis])
            b_coord = _snap(end.as_tuple()[axis])
            if abs(a_coord - b_coord) > 1e-9:
                continue
            if abs(a_coord - low) < 1e-9:
                curves[(axis, -1)] = curve.id
            elif abs(a_coord - high) < 1e-9:
                curves[(axis, 1)] = curve.id

    if len(curves) != 4:
        raise FeatureBuildError(
            feature=feature,
            reason=(
                f"profile '{profile.sketch}.{profile.loop}' is not an axis-aligned "
                "rectangle; the analytic test kernel cannot name its side faces "
                "deterministically"
            ),
        )
    return _ProfileBox(lo=lo, hi=hi, axis=normal_axis, curves=curves)


def _principal_axis(direction: Vec3, feature: str, what: str) -> int:
    for axis, unit in enumerate(_AXIS_VECTORS):
        if abs(abs(direction.dot(unit)) - 1.0) < 1e-9:
            return axis
    raise FeatureBuildError(
        feature=feature,
        reason=f"the analytic test kernel requires an axis-aligned {what}",
    )


def _prism_box(profile_box: _ProfileBox, frame_origin: float, start: float, end: float) -> _Box:
    lo = [0.0, 0.0, 0.0]
    hi = [0.0, 0.0, 0.0]
    au, av = _IN_PLANE[profile_box.axis]
    lo[au], hi[au] = profile_box.lo[0], profile_box.hi[0]
    lo[av], hi[av] = profile_box.lo[1], profile_box.hi[1]
    lo[profile_box.axis], hi[profile_box.axis] = min(start, end), max(start, end)
    return _Box(lo=tuple(lo), hi=tuple(hi))  # type: ignore[arg-type]


def _prism_record(
    feature: str,
    profile_box: _ProfileBox,
    box: _Box,
    start: float,
    end: float,
    sense: int = 1,
) -> _PrismRecord:
    """Record which boundary plane each profile curve produced.

    ``sense`` is +1 for additive features and -1 for subtractive ones. For a pad,
    the curve at the profile's low-x edge yields a face whose outward normal
    points -x; for a pocket the surviving material sits on the other side, so the
    very same curve yields a face pointing +x. Without this flip a pocket's walls
    come back unattributed and lose their link to the sketch curve that made
    them.
    """
    curve_planes: dict[tuple[int, int, float], str] = {}
    for (axis, side), curve in profile_box.curves.items():
        plane = box.hi[axis] if side == 1 else box.lo[axis]
        curve_planes[(axis, side * sense, _snap(plane))] = curve
    return _PrismRecord(
        feature=feature,
        box=box,
        axis=profile_box.axis,
        curve_planes=curve_planes,
        start_plane=_snap(start),
        end_plane=_snap(end),
    )


# --------------------------------------------------------------------------
# The adapter
# --------------------------------------------------------------------------


class FakeKernel:
    """Analytic implementation of :class:`GeometryKernel` for axis-aligned solids."""

    def __init__(self) -> None:
        self._solids: dict[str, _Solid] = {}
        self._counter = 0

    # -- port identity -----------------------------------------------------

    @property
    def name(self) -> str:
        return "analytic"

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset(
            {
                Capability.PAD,
                Capability.POCKET,
                Capability.TESSELLATE,
                Capability.MESH_EXPORT,
            }
        )

    # -- operations --------------------------------------------------------

    def pad(self, request: PadRequest) -> SolidResult:
        profile_box = _analyse_profile(request.profile, request.feature)
        if request.length <= 0:
            raise FeatureBuildError(
                feature=request.feature, reason=f"pad length must be positive, got {request.length}"
            )

        base_offset = request.profile.frame.origin.as_tuple()[profile_box.axis]
        signed = request.length * (1 if request.direction >= 0 else -1)
        if request.midplane:
            start, end = base_offset - signed / 2, base_offset + signed / 2
        else:
            start, end = base_offset, base_offset + signed

        box = _prism_box(profile_box, base_offset, start, end)
        prism = _prism_record(request.feature, profile_box, box, start, end)

        solid = self._new_solid(box)
        solid.prism = prism
        solid.faces = _extract_faces(solid, prism, previous=())
        return self._result(solid, deleted=())

    def pocket(
        self,
        base: SolidHandle,
        request: PocketRequest,
        face_refs: Mapping[Ref, object] | None = None,
    ) -> SolidResult:
        source = self._lookup(base)
        profile_box = _analyse_profile(request.profile, request.feature)
        if request.depth <= 0 and not request.through_all:
            raise FeatureBuildError(
                feature=request.feature,
                reason=f"pocket depth must be positive, got {request.depth}",
            )

        base_offset = request.profile.frame.origin.as_tuple()[profile_box.axis]
        signed = request.depth * (1 if request.direction >= 0 else -1)
        if request.through_all:
            extent = self._extent(source, profile_box.axis)
            margin = max(extent[1] - extent[0], 1.0)
            start = base_offset + (margin if request.direction < 0 else -margin)
            end = base_offset + (-margin if request.direction < 0 else margin)
        else:
            start, end = base_offset, base_offset + signed

        tool = _prism_box(profile_box, base_offset, start, end)
        prism = _prism_record(request.feature, profile_box, tool, start, end, sense=-1)

        previous_faces = source.faces
        solid = self._clone(source)
        _refine(solid, tool)
        removed = {
            cell for cell in solid.occupied if tool.contains_point(solid.cell_center(cell))
        }
        if not removed:
            raise FeatureBuildError(
                feature=request.feature,
                reason=(
                    "the pocket removes no material — check its depth, direction and "
                    "position against the body it cuts"
                ),
            )
        solid.occupied -= removed
        if not solid.occupied:
            raise FeatureBuildError(
                feature=request.feature, reason="the pocket would remove the entire body"
            )

        solid.prism = prism
        solid.faces = _extract_faces(solid, prism, previous=previous_faces)

        surviving = {
            face.provenance.parent
            for face in solid.faces
            if face.provenance.origin == Origin.INHERITED
        }
        deleted = tuple(
            DeletedFace(ref=face.ref, reason="consumed")
            for face in previous_faces
            if face.ref not in surviving
        )
        return self._result(solid, deleted=deleted)

    def fuse(self, base: SolidHandle, addition: SolidHandle) -> SolidResult:
        source = self._lookup(base)
        other = self._lookup(addition)
        previous_faces = source.faces

        solid = self._clone(source)
        bounds = _Box(
            lo=(other.xs[0], other.ys[0], other.zs[0]),
            hi=(other.xs[-1], other.ys[-1], other.zs[-1]),
        )
        _refine(solid, bounds)
        for cell in list(itertools.product(*(range(n) for n in solid.counts()))):
            centre = solid.cell_center(cell)
            if any(other.cell_bounds(c).contains_point(centre) for c in other.occupied):
                solid.occupied.add(cell)

        solid.prism = other.prism
        solid.faces = _extract_faces(solid, other.prism, previous=previous_faces)
        return self._result(solid, deleted=())

    # -- queries -----------------------------------------------------------

    def tessellate(self, solid_handle: SolidHandle, tolerance: float = 0.1) -> Tessellation:
        solid = self._lookup(solid_handle)
        positions: list[float] = []
        normals: list[float] = []
        indices: list[int] = []
        ranges: list[FaceRange] = []

        for face in solid.faces:
            start = len(indices)
            for rect in face.rects:
                corners = _rect_corners(face, rect)
                base_index = len(positions) // 3
                for corner in corners:
                    positions.extend(corner.as_tuple())
                    normals.extend(face.normal.as_tuple())
                winding = (0, 1, 2, 0, 2, 3) if face.side > 0 else (0, 2, 1, 0, 3, 2)
                indices.extend(base_index + offset for offset in winding)
            ranges.append(FaceRange(ref=face.ref, start=start, count=len(indices) - start))

        edges = tuple(
            EdgePolyline(
                ref=record.ref,
                points=_edge_points(record),
            )
            for record in _extract_edges(solid.faces)
        )
        return Tessellation(
            positions=tuple(positions),
            normals=tuple(normals),
            indices=tuple(indices),
            face_ranges=tuple(ranges),
            edges=edges,
        )

    def bounding_box(self, solid_handle: SolidHandle) -> BoundingBox:
        solid = self._lookup(solid_handle)
        if not solid.occupied:
            return BoundingBox()
        boxes = [solid.cell_bounds(cell) for cell in solid.occupied]
        return BoundingBox(
            min=tuple(min(b.lo[a] for b in boxes) for a in range(3)),  # type: ignore[arg-type]
            max=tuple(max(b.hi[a] for b in boxes) for a in range(3)),  # type: ignore[arg-type]
        )

    def volume(self, solid_handle: SolidHandle) -> float:
        solid = self._lookup(solid_handle)
        total = 0.0
        for cell in solid.occupied:
            bounds = solid.cell_bounds(cell)
            total += (
                (bounds.hi[0] - bounds.lo[0])
                * (bounds.hi[1] - bounds.lo[1])
                * (bounds.hi[2] - bounds.lo[2])
            )
        return total

    def release(self, solid_handle: SolidHandle) -> None:
        self._solids.pop(solid_handle.id, None)

    # -- internals ---------------------------------------------------------

    def _new_solid(self, box: _Box) -> _Solid:
        self._counter += 1
        handle = SolidHandle(id=f"s{self._counter}", kernel=self.name)
        solid = _Solid(
            handle=handle,
            xs=[_snap(box.lo[0]), _snap(box.hi[0])],
            ys=[_snap(box.lo[1]), _snap(box.hi[1])],
            zs=[_snap(box.lo[2]), _snap(box.hi[2])],
            occupied={(0, 0, 0)},
        )
        self._solids[handle.id] = solid
        return solid

    def _clone(self, source: _Solid) -> _Solid:
        self._counter += 1
        handle = SolidHandle(id=f"s{self._counter}", kernel=self.name)
        solid = _Solid(
            handle=handle,
            xs=list(source.xs),
            ys=list(source.ys),
            zs=list(source.zs),
            occupied=set(source.occupied),
        )
        self._solids[handle.id] = solid
        return solid

    def _lookup(self, handle: SolidHandle) -> _Solid:
        try:
            return self._solids[handle.id]
        except KeyError:
            raise FeatureBuildError(
                feature="<kernel>", reason=f"unknown solid handle '{handle.id}'"
            ) from None

    def _extent(self, solid: _Solid, axis: int) -> tuple[float, float]:
        coords = solid.axis_coords(axis)
        return (coords[0], coords[-1])

    def _result(self, solid: _Solid, deleted: tuple[DeletedFace, ...]) -> SolidResult:
        return SolidResult(
            solid=solid.handle,
            faces=tuple(
                FaceRecord(ref=face.ref, provenance=face.provenance, fingerprint=face.fingerprint())
                for face in solid.faces
            ),
            edges=tuple(_extract_edges(solid.faces)),
            deleted=deleted,
        )


def _rect_corners(face: _FaceComponent, rect: _Rect) -> list[Vec3]:
    au, av = _IN_PLANE[face.axis]
    corners: list[Vec3] = []
    for u, v in ((rect.u0, rect.v0), (rect.u1, rect.v0), (rect.u1, rect.v1), (rect.u0, rect.v1)):
        coords = [0.0, 0.0, 0.0]
        coords[face.axis] = face.plane
        coords[au] = u
        coords[av] = v
        corners.append(Vec3(*coords))
    return corners


def _edge_points(record: EdgeRecord) -> tuple[float, ...]:
    fingerprint = record.fingerprint
    half = fingerprint.direction * (fingerprint.length / 2)
    start = fingerprint.midpoint - half
    end = fingerprint.midpoint + half
    return (*start.as_tuple(), *end.as_tuple())
