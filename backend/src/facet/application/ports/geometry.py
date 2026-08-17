"""The geometry kernel port.

The single most important boundary in the system. Everything above it reasons
about *named* geometry; everything below it reasons about shapes.

The contract that makes the naming engine kernel-agnostic
---------------------------------------------------------

A kernel adapter **reports provenance; it never assigns names.** It answers
"this face was swept from profile curve ``c1``" or "this face survived from
input face ``r7``". It does not know that in a pocket a swept face is called a
*wall* while in a pad it is called a *side* — that is a domain decision, made in
:mod:`facet.application.naming`.

That split is what lets a B-rep kernel, a mesh kernel and the analytic test
kernel all feed one naming engine, and it is why ``domain`` never imports a
kernel type.

References are opaque strings assigned by the adapter and meaningful only within
one result. The caller supplies the refs of input faces, so an adapter echoes
back what it was given rather than inventing identity.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from facet.domain.fingerprint import EdgeFingerprint, FaceFingerprint
from facet.domain.math3d import Frame, Vec2, Vec3

# --------------------------------------------------------------------------
# Capabilities — substitutability without fat interfaces
# --------------------------------------------------------------------------


class Capability:
    """What a kernel can do.

    Declared rather than discovered, so a use case can fail with a clear message
    up front instead of a kernel raising ``NotImplementedError`` halfway through
    a rebuild. A kernel that supports less is still a substitutable kernel.
    """

    PAD = "pad"
    POCKET = "pocket"
    TESSELLATE = "tessellate"
    MESH_EXPORT = "mesh_export"
    BREP_EXPORT = "brep_export"
    DRAWING_EXPORT = "drawing_export"
    FILLET = "fillet"
    CHAMFER = "chamfer"
    REVOLVE = "revolve"
    FACE_PROFILE = "face_profile"
    """Can flatten a planar face into 2D curves — the cut path for CNC."""
    THREAD = "thread"
    """Can cut a helical thread form."""
    SNAPSHOT = "snapshot"
    """Can write a solid to bytes and read back one with the *same* face refs.

    The second half is the whole requirement. Any kernel can serialise a shape;
    what makes a snapshot usable is that restoring it assigns identical refs, so
    the names stored against them still point at the same geometry. A kernel
    whose round trip perturbs a centroid enough to reorder the canonical sort
    must not declare this.
    """


# --------------------------------------------------------------------------
# Handles
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SolidHandle:
    """An opaque reference to a solid living inside a kernel adapter.

    Deliberately not the shape itself: keeping kernel objects out of the
    application layer is what allows the OCCT adapter to run in a subprocess.
    """

    id: str
    kernel: str = ""


#: An opaque, per-result identifier for a face or edge.
Ref = str


# --------------------------------------------------------------------------
# Profiles — the geometric input to a feature
# --------------------------------------------------------------------------


class CurveType:
    LINE = "line"
    ARC = "arc"
    CIRCLE = "circle"


@dataclass(frozen=True, slots=True)
class ProfileCurve:
    """One curve of a closed profile loop, in sketch-plane (u, v) coordinates.

    ``id`` is the sketch-local curve name and is the root of every tag derived
    from this curve, which is why it travels with the geometry rather than being
    matched up afterwards by index.
    """

    id: str
    type: str
    start: Vec2 | None = None
    end: Vec2 | None = None
    center: Vec2 | None = None
    radius: float = 0.0
    start_angle: float = 0.0
    end_angle: float = 0.0
    counter_clockwise: bool = True


@dataclass(frozen=True, slots=True)
class Profile:
    """A closed loop of curves on a datum plane."""

    sketch: str
    loop: str
    frame: Frame
    curves: tuple[ProfileCurve, ...]

    def curve_ids(self) -> tuple[str, ...]:
        return tuple(c.id for c in self.curves)


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PadRequest:
    """Extrude a profile into new material.

    ``direction`` is +1 or -1 along the profile frame's normal and is always
    explicit — never inferred from face orientation. That is the rule which
    removes the relative-direction failures that plague face-attached sketches.
    """

    feature: str
    profile: Profile
    length: float
    direction: int = 1
    midplane: bool = False


@dataclass(frozen=True, slots=True)
class BlendRequest:
    """Round or bevel a set of edges.

    ``edges`` are refs from the solid being modified, which the application
    obtains by resolving an edge selector — so what reaches the kernel is
    already the user's stated intent, resolved against the current model.
    """

    feature: str
    edges: tuple[Ref, ...]
    #: Fillet radius, or chamfer setback.
    size: float


@dataclass(frozen=True, slots=True)
class ThreadRequest:
    """Cut an ISO metric thread form along an axis.

    Always expressed as a *cut*, whether the thread is internal or external:
    an external thread is a helical groove taken out of a cylinder at the major
    diameter, an internal one is a helical ridge taken out of a tapped bore.
    Cutting is the only formulation OCCT handles quickly and reliably here —
    fusing a helical ridge onto a shaft degrades super-linearly and starts
    failing outright past about a dozen turns.
    """

    feature: str
    #: A point on the axis, at the start of the threaded length.
    origin: Vec3
    #: Unit vector the thread advances along.
    direction: Vec3
    major: float
    pitch: float
    length: float
    internal: bool = True
    right_handed: bool = True
    #: Sketch curve or point id every thread face is attributed to, so the tag
    #: reads like a hole's: ``m6/thread[plate.h1]``.
    curve: str = "thread"

    @property
    def minor(self) -> float:
        """ISO 68-1 basic minor diameter: nominal minus 1.0825 x pitch."""
        return self.major - 1.0825 * self.pitch


@dataclass(frozen=True, slots=True)
class PocketRequest:
    """Remove material by extruding a profile and subtracting it."""

    feature: str
    profile: Profile
    depth: float
    direction: int = -1
    through_all: bool = False


# --------------------------------------------------------------------------
# Provenance — what the kernel reports back
# --------------------------------------------------------------------------


class Origin:
    """How a resulting face came to exist, in kernel-neutral terms."""

    SWEPT = "swept"
    """Swept from a profile curve; ``curve`` names which one."""
    CAP_START = "cap_start"
    """The cap at the start of the sweep (against the direction)."""
    CAP_END = "cap_end"
    """The cap at the end of the sweep (along the direction)."""
    INHERITED = "inherited"
    """Survived from an input face; ``parent`` is that face's ref."""
    BLEND = "blend"
    """A fillet or chamfer face; ``parents`` are the two faces its edge separated.

    This is why edges never needed their own naming scheme: a blend is named by
    the edge it replaced, and that edge is named by its two adjacent faces.
    """
    THREAD = "thread"
    """A helical thread face; ``parent`` is the bore or shaft it was cut into.

    A thread makes hundreds of faces and none of them is individually
    interesting, so they all share one tag and are told apart by ordinal. What
    a document selects is the thread as a whole, ``m6/thread[*]``, which stays
    right however many faces the current pitch and length happen to produce.
    """
    BLEND_CORNER = "blend_corner"
    """A blend transition patch; ``parents`` are the faces that bound it.

    Where two blends meet, the kernel emits a patch that came from a vertex
    rather than from any one edge, so it has no two-face name. The adapter
    reports which already-attributed faces surround it and the naming engine
    turns that set into a corner tag — still provenance, never a guess.
    """
    UNKNOWN = "unknown"
    """The kernel could not attribute this face. Always a diagnosable bug."""


@dataclass(frozen=True, slots=True)
class FaceProvenance:
    origin: str
    curve: str | None = None
    parent: Ref | None = None
    #: For a blend: the refs of the two faces whose shared edge was rounded.
    parents: tuple[Ref, ...] = ()

    def describe(self) -> str:
        match self.origin:
            case Origin.SWEPT:
                return f"swept from curve '{self.curve}'"
            case Origin.CAP_START:
                return "start cap of the sweep"
            case Origin.CAP_END:
                return "end cap of the sweep"
            case Origin.INHERITED:
                return f"inherited from input face '{self.parent}'"
            case Origin.BLEND:
                return f"blended along the edge between {' and '.join(self.parents)}"
            case _:
                return "unattributed"


@dataclass(frozen=True, slots=True)
class FaceRecord:
    """A face in a kernel result: opaque ref, provenance, world-space geometry.

    The fingerprint is in **world** coordinates. The naming engine converts it
    into the owning feature's local frame, because that conversion needs the
    frame and the kernel has no business knowing which frame owns what.
    """

    ref: Ref
    provenance: FaceProvenance
    fingerprint: FaceFingerprint


@dataclass(frozen=True, slots=True)
class EdgeRecord:
    """An edge, reported as the refs of the two faces it separates."""

    ref: Ref
    faces: tuple[Ref, Ref]
    fingerprint: EdgeFingerprint


@dataclass(frozen=True, slots=True)
class DeletedFace:
    """An input face consumed by the operation, with the reason it vanished."""

    ref: Ref
    reason: str = "consumed"


@dataclass(frozen=True)
class SolidResult:
    """The output of a kernel operation: a shape plus complete provenance."""

    solid: SolidHandle
    faces: tuple[FaceRecord, ...] = ()
    edges: tuple[EdgeRecord, ...] = ()
    deleted: tuple[DeletedFace, ...] = ()

    def face(self, ref: Ref) -> FaceRecord | None:
        return next((f for f in self.faces if f.ref == ref), None)


# --------------------------------------------------------------------------
# Tessellation for the viewer
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FaceRange:
    """Which triangles belong to which face — the basis of click-to-select."""

    ref: Ref
    start: int
    count: int


@dataclass(frozen=True, slots=True)
class EdgePolyline:
    """An exact edge curve, so the viewport draws CAD edges rather than a mesh."""

    ref: Ref
    points: tuple[float, ...]


@dataclass(frozen=True)
class Tessellation:
    positions: tuple[float, ...] = ()
    normals: tuple[float, ...] = ()
    indices: tuple[int, ...] = ()
    face_ranges: tuple[FaceRange, ...] = ()
    edges: tuple[EdgePolyline, ...] = ()

    @property
    def triangle_count(self) -> int:
        return len(self.indices) // 3

    @property
    def vertex_count(self) -> int:
        return len(self.positions) // 3


@dataclass(frozen=True)
class BoundingBox:
    min: tuple[float, float, float] = (0.0, 0.0, 0.0)
    max: tuple[float, float, float] = (0.0, 0.0, 0.0)

    def to_dict(self) -> dict[str, object]:
        return {"min": list(self.min), "max": list(self.max)}


# --------------------------------------------------------------------------
# Flattened 2D geometry — the CNC and laser side
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Line2D:
    """A straight run in a face's own plane."""

    start: tuple[float, float]
    end: tuple[float, float]
    #: Ref of the model edge this run came from, when the adapter knows it.
    #: Two panels that share an edge report the *same* ref, which is what lets a
    #: joint generator interlock them without matching their geometry.
    edge: str = ""


@dataclass(frozen=True, slots=True)
class Arc2D:
    """A circular arc in a face's own plane.

    Kept as an arc rather than flattened to a polyline because a CNC or laser
    controller cuts a real arc far better than it cuts a chain of chords, and
    because the file stays small enough to read.
    """

    centre: tuple[float, float]
    radius: float
    start_angle: float
    """Degrees, counter-clockwise from the frame's x axis."""
    end_angle: float
    ccw: bool = True
    #: Ref of the model edge this arc came from, when the adapter knows it.
    edge: str = ""

    @property
    def full_circle(self) -> bool:
        return abs((self.end_angle - self.start_angle) % 360.0) < 1e-9


Curve2D = Line2D | Arc2D


@dataclass(frozen=True)
class Loop2D:
    """One closed boundary of a face. Inner loops are holes."""

    curves: tuple[Curve2D, ...] = ()
    outer: bool = True


@dataclass(frozen=True)
class Profile2D:
    """A planar face flattened into its own plane.

    ``frame`` records where the plane sits in the model, so a drawing can say
    what it is a view of, and so the same profile can be lifted back into 3D.
    """

    loops: tuple[Loop2D, ...] = ()
    frame: Frame = field(default_factory=Frame.world)
    label: str = ""

    @property
    def is_empty(self) -> bool:
        return not any(loop.curves for loop in self.loops)


# --------------------------------------------------------------------------
# The port
# --------------------------------------------------------------------------


@runtime_checkable
class GeometryKernel(Protocol):
    """Everything the application needs from a geometry engine.

    Structural (``Protocol``) rather than nominal, so adapters never import a
    base class and the dependency arrow keeps pointing inwards.
    """

    @property
    def name(self) -> str:
        """Short adapter name, used in error messages."""
        ...

    @property
    def capabilities(self) -> frozenset[str]:
        """Which :class:`Capability` values this kernel supports."""
        ...

    def pad(self, request: PadRequest) -> SolidResult:
        """Create new material by extruding a closed profile."""
        ...

    def pocket(
        self,
        base: SolidHandle,
        request: PocketRequest,
        face_refs: Mapping[Ref, object] | None = None,
    ) -> SolidResult:
        """Subtract an extruded profile from ``base``.

        Faces surviving from ``base`` are reported with
        :data:`Origin.INHERITED` and the ref they had in the base result, so the
        naming engine can carry their tags forward.
        """
        ...

    def fuse(self, base: SolidHandle, addition: SolidHandle) -> SolidResult:
        """Union two solids, reporting provenance for both sides."""
        ...

    def tessellate(self, solid: SolidHandle, tolerance: float = 0.1) -> Tessellation:
        """Triangulate for display, retaining face attribution and exact edges."""
        ...

    def bounding_box(self, solid: SolidHandle) -> BoundingBox:
        ...

    def volume(self, solid: SolidHandle) -> float:
        ...

    def release(self, solid: SolidHandle) -> None:
        """Drop a solid the engine no longer needs to retain."""
        ...


@runtime_checkable
class ThreadKernel(Protocol):
    """Cutting threads. Separate from the kernel port so a kernel need not."""

    def thread(self, base: SolidHandle, request: ThreadRequest) -> SolidResult:
        ...


@runtime_checkable
class BlendKernel(Protocol):
    """Rounding and bevelling, kept out of the core port.

    A mesh kernel can pad and pocket perfectly well without being able to blend,
    and folding these into :class:`GeometryKernel` would force every adapter to
    implement methods it has no business implementing. Guarded by
    :data:`Capability.FILLET` and :data:`Capability.CHAMFER`.
    """

    def fillet(self, base: SolidHandle, request: BlendRequest) -> SolidResult:
        """Round the given edges.

        Blends are the most failure-prone operation in any B-rep kernel; an
        adapter is expected to raise :class:`FeatureBuildError` with a usable
        reason rather than returning a damaged solid.
        """
        ...

    def chamfer(self, base: SolidHandle, request: BlendRequest) -> SolidResult:
        """Bevel the given edges."""
        ...


@runtime_checkable
class MeshExporter(Protocol):
    """Separate from the kernel port so a kernel need not implement it (ISP)."""

    def export_mesh(self, solid: SolidHandle, fmt: str, tolerance: float = 0.05) -> bytes:
        ...


@runtime_checkable
class BrepExporter(Protocol):
    def export_brep(self, solid: SolidHandle, fmt: str) -> bytes:
        ...


@runtime_checkable
class DrawingExporter(Protocol):
    def export_drawing(
        self, solid: SolidHandle, fmt: str, views: Sequence[str] = ()
    ) -> bytes:
        ...


@runtime_checkable
class ProfileExtractor(Protocol):
    """Flattens a planar face into 2D curves.

    Separate from the kernel port (ISP): a kernel that only makes solids is
    still a usable kernel, and this is what the CNC and laser paths need.
    """

    def face_profile(
        self, solid: SolidHandle, ref: Ref, tolerance: float = 0.01
    ) -> Profile2D:
        ...

    def section_profile(
        self, solid: SolidHandle, frame: Frame, tolerance: float = 0.01
    ) -> Profile2D:
        """The outline where ``frame``'s plane cuts the solid."""
        ...


@runtime_checkable
class SolidSnapshots(Protocol):
    """Puts a built solid somewhere it survives this process, and gets it back.

    Separate from the kernel port for the usual reason: a kernel that cannot do
    this is still a kernel, and the recompute engine simply rebuilds.

    The contract is about *identity*, not bytes. ``restore`` must return a result
    whose refs are the ones the original had, because the names the caller stored
    are keyed on them. That makes the blob opaque and entirely the adapter's
    business: a shape read off a disk has no history to be asked about, so
    whatever an adapter needs beyond the geometry — provenance, the ordering it
    assigned — it has to put in there itself.

    An adapter should verify what it reads rather than trust it, and raise if it
    cannot. Rebuilding is always correct; a snapshot is only ever an
    optimisation, and one that cannot prove itself must not be handed back.
    """

    def snapshot(self, solid: SolidHandle) -> bytes:
        """Serialise a stored solid, completely. Raises if the handle is unknown."""
        ...

    def restore(self, blob: bytes) -> SolidResult:
        """Register a solid from ``snapshot`` output, with its original refs.

        Raises rather than returning something approximate if the blob does not
        read back as the solid it described.
        """
        ...


@dataclass(frozen=True)
class KernelInfo:
    """Describes a kernel to the API, so clients can adapt to what is available."""

    name: str
    version: str = ""
    capabilities: frozenset[str] = field(default_factory=frozenset)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "capabilities": sorted(self.capabilities),
        }
