"""Reading the plane of a named face back out of the document.

A face tag says how the face came to be: ``pad_1/cap+`` is the far cap of
feature ``pad_1``, which pads a sketch, and that sketch sits on a datum. Every
term of that sentence is already written down, so the plane the face lies in can
be *derived* rather than measured — the sketch's datum, offset along its normal
by the feature's own length or depth **exactly as the document states it**.

Carrying the offset symbolically is the whole point. It travels as
``"plate_t * 2"``, never as ``12``, so a datum built from the proposal is still
computed from parameters and other datums and the rule in
:mod:`facet.domain.datum` holds precisely as if the user had typed it. No
geometry is consulted anywhere below: this reads the document, not the solid, so
nothing picked ever enters a datum.

A face need not be parallel to its sketch to be answerable. A side or a wall is
swept *along* the sketch's normal, so its plane is the one through the curve and
that normal — still every term written down, since the curve's endpoints are
sketch points and sketch points are parameters.

What is left is curved: a face swept from an arc or a circle is a cylinder, as is
a fillet; a thread flank is a helix; a chamfer sits at an angle to both faces it
bevels between and its plane depends on more than the sheet states; a blend corner
is a patch between three faces. Those are refused, and the refusal names the flat
faces alongside that *can* be derived — because a plane offered confidently and
wrongly is worse than no plane at all, and the reader has to be left in no doubt
that a fallback plane is not the face they picked.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from facet.domain.datum import DatumPlane
from facet.domain.document import Document
from facet.domain.errors import FacetCADError
from facet.domain.features import FeatureSpec
from facet.domain.math3d import ANGULAR_TOL, Frame, Vec3
from facet.domain.parameters import ResolvedParameters
from facet.domain.sketch import CurveKinds, Sketch, SketchCurve
from facet.domain.tags import CurveRef, EdgeTag, FaceTag, Roles
from facet.domain.values import Value
from facet.domain.values import resolve as resolve_value

# The '+normal' / '-normal' vocabulary is stated once, by the handler that
# builds the feature. A second reading of it here would be free to drift.
from .features import _direction

#: How near two planes must be to count as the same one (mm). Loose enough to
#: absorb the float noise of composing a parent frame, far tighter than any
#: distance a person would call a separate plane.
COINCIDENT_TOLERANCE = 1e-6

#: How each feature type reads in a sentence: "pad 'lid' pads sketch 'outline'".
_VERBS = {"pad": "pads", "pocket": "cuts", "hole": "drills", "thread": "taps"}

#: Types that remove material from their sketch plane inwards. Their default
#: direction is ``-normal``, matching the handlers in :mod:`.features`.
_CUTTERS = ("pocket", "hole", "thread")

#: Why a role can never be parallel to the sketch that produced it. Stated per
#: role rather than as one blanket refusal: "it is curved" and "it stands on
#: edge" are different facts, and the caller has to explain one of them to a
#: person before asking them to place the datum by hand.
_NOT_PARALLEL = {
    Roles.COUNTERBORE: (
        "a counterbore wall is swept along the sketch's normal, so it stands on edge to "
        "the sketch plane rather than parallel to it"
    ),
    Roles.FILLET: (
        "a fillet is a cylinder, so it lies in no plane at all. The two flat faces it "
        "rounds between can each be derived — pick one of those instead"
    ),
    Roles.CHAMFER: (
        "a chamfer is cut at an angle to both faces it bevels between, and its plane "
        "depends on geometry the sheet does not state. The two faces it bevels between "
        "can each be derived — pick one of those instead"
    ),
    Roles.CORNER: (
        "a blend corner is a patch between three or more faces and lies in no plane of "
        "its own"
    ),
    Roles.THREAD: "a thread flank is helical, so it lies in no plane at all",
}

#: Role -> id fragment. '+' and '-' are not identifier characters, so the sign is
#: spelled out rather than dropped: cap+ and cap- are opposite planes and must
#: never collapse onto one name.
_ROLE_WORDS = {
    Roles.CAP_POS: "cap_pos",
    Roles.CAP_NEG: "cap_neg",
    Roles.FLOOR: "floor",
    Roles.CEILING: "ceiling",
    Roles.COUNTERBORE_FLOOR: "cbore_floor",
}


@dataclass(frozen=True)
class DatumProposal:
    """A datum that would lie on the plane of a named face.

    ``datum`` is a payload ready to PUT; ``existing`` names a datum already on
    that plane so the caller can reuse it instead of adding another.
    """

    ok: bool
    datum: Mapping[str, object] | None = None
    existing: str | None = None
    explanation: str = ""
    reason: str | None = None
    #: A clicked world point, expressed on the derived plane. ``None`` when no
    #: point was given, or when the sheet cannot resolve.
    at: tuple[float, float] | None = None
    #: The face's extent in its own coordinates, symbolically — so a hole can be
    #: centred on it with an expression that survives a change of dimensions.
    size: Mapping[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "datum": dict(self.datum) if self.datum is not None else None,
            "existing": self.existing,
            "explanation": self.explanation,
            "reason": self.reason,
            "at": {"u": self.at[0], "v": self.at[1]} if self.at is not None else None,
            "size": dict(self.size) if self.size is not None else None,
        }


def propose_datum_for_face(
    document: Document,
    tag_text: str,
    point: tuple[float, float, float] | None = None,
) -> DatumProposal:
    """Derive the plane of ``tag_text`` from the document alone.

    ``point`` is a world point — a click on the face — and comes back as ``at``,
    its coordinates *on the derived plane*. Reading them off the parent instead
    is a trap: a cap's datum is parallel to its parent so the two agree, but a
    side or a chamfer stands on edge to it and the numbers are then coordinates
    on the wrong plane. They look plausible, which is what makes them dangerous.
    """
    # Fragments of a split face are coplanar with the face they came from, so
    # the ordinal says nothing about the plane and is dropped before anything
    # else looks at the tag.
    tag = FaceTag.parse(tag_text).without_ordinal()
    spec = document.feature(tag.feature)

    try:
        plane = _plane_of(spec, tag, document)
    except _NotDerivable as refusal:
        return DatumProposal(ok=False, reason=refusal.reason)
    except FacetCADError as error:
        # A feature that cannot state its own depth, or names a sketch that is
        # gone, is a document the user has to fix. Reporting it as a refusal
        # keeps this a question anyone may ask of any tag at any time.
        return DatumProposal(ok=False, reason=str(error))

    parameters, frames = _resolved(document)
    same_plane = _datums_on(plane, frames, parameters)
    payload = plane.payload(_identifier_for(tag, document, same_plane))
    existing = same_plane[0] if same_plane else None

    # Measured against whichever datum the caller will actually sketch on. A
    # reused datum describes the same plane but may be rotated within it — it
    # was written by a hand or by an older derivation — and u,v for the proposal
    # would then be coordinates in a frame nobody is using.
    frame = frames.get(existing) if existing else None
    return DatumProposal(
        ok=True,
        datum=payload,
        existing=existing,
        explanation=plane.explanation,
        at=(
            _in_frame(frame, point)
            if frame is not None
            else _on_plane(payload, frames, parameters, point)
        ),
        size=_resolved_size(plane, parameters),
    )


def _in_frame(
    frame: Frame, point: tuple[float, float, float] | None
) -> tuple[float, float] | None:
    if point is None:
        return None
    local = frame.to_local(Vec3(*point))
    return (round(local.x, 4), round(local.y, 4))


def _resolved_size(
    plane: _Plane, parameters: ResolvedParameters | None
) -> dict[str, object] | None:
    """The face's own width and height, as expressions and as numbers.

    Both, because they answer different questions. The expressions are what a
    document should hold — centre a hole with ``w / 2`` and it stays centred
    when the part changes. The numbers are what a person needs to see to know
    the expressions are the right ones.
    """
    if plane.size is None:
        return None
    width, height = plane.size
    entry: dict[str, object] = {"u": width, "v": height}
    if parameters is not None:
        try:
            entry["uValue"] = round(resolve_value(width, parameters, where="face.width"), 4)
            entry["vValue"] = round(resolve_value(height, parameters, where="face.height"), 4)
        except FacetCADError:
            pass
    return entry


def _on_plane(
    payload: Mapping[str, object],
    frames: Mapping[str, Frame],
    parameters: ResolvedParameters | None,
    point: tuple[float, float, float] | None,
) -> tuple[float, float] | None:
    """A world point in the proposed plane's own coordinates."""
    if point is None or parameters is None:
        return None
    try:
        plane = DatumPlane(
            id=str(payload["id"]),
            origin=tuple(payload["origin"]),  # type: ignore[arg-type]
            normal=tuple(payload["normal"]),  # type: ignore[arg-type]
            x_axis=tuple(payload["x_axis"]) if payload.get("x_axis") else None,  # type: ignore[arg-type]
            parent=str(payload["parent"]) if payload.get("parent") else None,
        )
        frame = plane.resolve(parameters, frames)
    except FacetCADError:
        return None
    local = frame.to_local(Vec3(*point))
    return (round(local.x, 4), round(local.y, 4))


# --------------------------------------------------------------------------
# Deriving the plane
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Plane:
    """A derived plane, stated in a parent datum's own frame.

    Most faces are parallel to the sketch that made them, so ``offset`` along
    the parent's normal says everything. A side face is not: it stands on edge
    to its sketch. Those set ``origin``, ``normal`` and ``x_axis`` instead,
    still in the parent's coordinates, so the parent composes exactly as it
    does for any hand-written datum.
    """

    parent: str
    offset: Value
    explanation: str
    origin: tuple[Value, Value, Value] | None = None
    normal: tuple[Value, Value, Value] | None = None
    x_axis: tuple[Value, Value, Value] | None = None
    #: Extent along the plane's own u and v, when the document states it.
    size: tuple[Value, Value] | None = None

    def payload(self, identifier: str) -> dict[str, object]:
        if self.normal is None:
            return {
                "id": identifier,
                "parent": self.parent,
                "origin": [0, 0, self.offset],
                # Parallel to the parent, never flipped to match the face's
                # outward normal: the datum is a plane to sketch on, and
                # inheriting the parent's orientation keeps u and v pointing the
                # same way as the sketch that produced the face. Flipping would
                # mirror everything drawn on it.
                "normal": [0, 0, 1],
            }
        payload: dict[str, object] = {
            "id": identifier,
            "parent": self.parent,
            "origin": list(self.origin or (0, 0, 0)),
            "normal": list(self.normal),
        }
        if self.x_axis is not None:
            payload["x_axis"] = list(self.x_axis)
        return payload


class _NotDerivable(Exception):
    """This face's plane cannot be read out of the document."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _plane_of(spec: FeatureSpec, tag: FaceTag, document: Document) -> _Plane:
    role = tag.role
    if role in (Roles.SIDE, Roles.WALL):
        return _swept_plane(spec, tag, document)
    if role == Roles.CHAMFER:
        return _chamfer_plane(spec, tag, document)
    if role in _NOT_PARALLEL:
        raise _NotDerivable(
            f"'{spec.id}/{role}' cannot be derived: {_NOT_PARALLEL[role]}. Declare the "
            "datum yourself, or pick a face that is parallel to its sketch."
        )
    if spec.type == "pad":
        return _pad_plane(spec, role, document)
    if spec.type in _CUTTERS:
        return _cut_plane(spec, role, document)
    raise _NotDerivable(
        f"feature '{spec.id}' is a {spec.type}, which builds its faces from an existing "
        "solid rather than from a sketch plane, so there is no plane to read off it"
    )


def _pad_plane(spec: FeatureSpec, role: str, document: Document) -> _Plane:
    """A pad's caps: the sketch's datum, and the same offset by its length."""
    if role not in (Roles.CAP_POS, Roles.CAP_NEG):
        raise _NotDerivable(
            f"a pad has no '{role}' face; pad '{spec.id}' names its faces 'cap+', "
            "'cap-' and 'side'"
        )
    sketch = _sketch_of(spec, document)
    length = spec.option("length")

    if spec.flag("midplane"):
        # Both caps exist, half a length either side of the sketch. Which is
        # which follows from the sign alone — `direction` only decides which way
        # a one-sided pad grows, and a midplane pad grows both ways.
        half = _halved(length)
        offset = half if role == Roles.CAP_POS else _negated(half)
        side = "positive" if role == Roles.CAP_POS else "negative"
        return _Plane(
            parent=sketch.plane,
            offset=offset,
            explanation=(
                f"the {side} cap of midplane pad '{spec.id}', which pads sketch "
                f"'{sketch.id}' on datum '{sketch.plane}' by '{half}' each way"
            ),
        )

    # The naming engine decides a cap's sign from its outward normal against the
    # sketch's (`caps_by_normal` in :mod:`.naming`), so the cap the extrusion
    # runs *to* is cap+ when the pad grows along the normal and cap- when it
    # grows against it. The other cap is left behind on the sketch plane.
    direction = _direction(spec, 1)
    far = Roles.CAP_POS if direction > 0 else Roles.CAP_NEG
    verb = _VERBS[spec.type]

    if role != far:
        return _Plane(
            parent=sketch.plane,
            offset=0,
            explanation=(
                f"the near cap of pad '{spec.id}', which {verb} sketch '{sketch.id}' on "
                f"datum '{sketch.plane}', so it lies on '{sketch.plane}' itself"
            ),
        )

    offset = length if direction > 0 else _negated(length)
    return _Plane(
        parent=sketch.plane,
        offset=offset,
        explanation=(
            f"the far cap of pad '{spec.id}', which {verb} sketch '{sketch.id}' on datum "
            f"'{sketch.plane}' by '{offset}'"
        ),
    )


def _swept_plane(spec: FeatureSpec, tag: FaceTag, document: Document) -> _Plane:
    """A side or a wall: the plane through its curve and the sweep direction.

    A pad sweeps a profile along its sketch's normal, so the face left behind by
    one straight curve is the plane containing that curve and that normal. Both
    are written down — the curve's endpoints are sketch points, which are
    parameters — so the plane is as symbolic as any other proposal here.
    """
    sketch, curve = _curve_of(spec, tag, document)

    if curve.type != CurveKinds.LINE:
        raise _NotDerivable(
            f"'{tag}' is swept from "
            f"{'an arc' if curve.type == CurveKinds.ARC else 'a circle'}, so the face "
            "is a cylinder and lies in no plane. A face swept from a straight curve "
            "can be derived"
        )

    start = _point_of(sketch, curve.start, spec.id)
    end = _point_of(sketch, curve.end, spec.id)
    run = (_difference(end[0], start[0]), _difference(end[1], start[1]))

    # The in-plane perpendicular. For a curve walked anticlockwise, (dy, -dx)
    # points out of the loop and so out of the material — which is the sense a
    # person expects when they pick a face from outside. A clockwise loop is the
    # mirror of that, and the loop's own winding is in the document, so the sense
    # is read rather than assumed.
    outward = _winding(sketch, curve, document)
    normal = (
        (run[1], _negated(run[0]), 0)
        if outward > 0
        else (_negated(run[1]), run[0], 0)
    )

    return _Plane(
        parent=sketch.plane,
        offset=0,
        origin=(start[0], start[1], 0),
        normal=normal,
        # U runs along the curve, so a sketch drawn on this face reads the way
        # the face looks rather than at some arbitrary rotation.
        x_axis=(run[0], run[1], 0),
        size=(_length((run[0], run[1], 0)), _swept_by(spec)),
        explanation=(
            f"the face swept from curve '{curve.id}' of sketch '{sketch.id}', which sits "
            f"on datum '{sketch.plane}' — the plane through that curve and the sweep "
            "direction"
        ),
    )


def _curve_of(
    spec: FeatureSpec, tag: FaceTag, document: Document
) -> tuple[Sketch, SketchCurve]:
    """The sketch curve a side or wall was swept from.

    The tag says which one: a swept face is named ``feature/side[sketch.curve]``
    precisely so this question has an answer that does not depend on geometry.
    """
    source = tag.source
    if not isinstance(source, CurveRef):
        raise _NotDerivable(
            f"'{tag}' does not name the curve it was swept from, so there is no "
            "plane to read"
        )
    sketch = document.sketches.get(source.sketch)
    if sketch is None:
        raise _NotDerivable(
            f"'{tag}' was swept from sketch '{source.sketch}', which the document no "
            "longer has"
        )
    try:
        curve = sketch.curve(source.curve)
    except FacetCADError as error:
        raise _NotDerivable(str(error)) from error
    del spec
    return sketch, curve


def _point_of(sketch: Sketch, name: str, feature: str) -> tuple[Value, Value]:
    """A sketch point's two coordinates, left exactly as the document wrote them."""
    for point in sketch.points:
        if point.id == name:
            return (point.at[0], point.at[1])
    raise _NotDerivable(
        f"sketch '{sketch.id}' has no point '{name}', which feature '{feature}' needs"
    )


def _difference(later: Value, earlier: Value) -> Value:
    """``later - earlier``, keeping parameter names wherever they were used.

    Both sides are parenthesised unconditionally rather than only when they look
    compound: ``a - b - c`` and ``a - (b - c)`` differ, and deciding which by
    inspecting the text is exactly the kind of cleverness that ships a subtly
    wrong plane.
    """
    if _is_number(later) and _is_number(earlier):
        return float(later) - float(earlier)  # type: ignore[arg-type]
    return f"({later}) - ({earlier})"


def _is_number(value: Value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _winding(sketch: Sketch, curve: SketchCurve, document: Document) -> float:
    """+1 when the loop containing ``curve`` is walked anticlockwise, -1 otherwise.

    Read from the document, not assumed: an author may write a loop either way,
    and the two give opposite outward normals. Getting it backwards would face
    the datum into the material and mirror every sketch drawn on it.

    A loop whose points cannot be resolved — a parameter the sheet is missing —
    falls back to anticlockwise, which is the common case, and the explanation
    says the sense was assumed.
    """
    loop = next((entry for entry in sketch.loops if curve.id in entry.curves), None)
    if loop is None:
        return 1.0
    try:
        points = sketch.resolve_points(document.parameters.resolve())
    except FacetCADError:
        return 1.0

    ordered: list[tuple[float, float]] = []
    for reference in loop.curves:
        try:
            entry = sketch.curve(reference)
        except FacetCADError:
            return 1.0
        start = points.get(entry.start)
        if start is None:
            return 1.0
        ordered.append((start.x, start.y))

    twice_area = 0.0
    for index, (x, y) in enumerate(ordered):
        nx, ny = ordered[(index + 1) % len(ordered)]
        twice_area += x * ny - nx * y
    return 1.0 if twice_area >= 0 else -1.0


def _chamfer_plane(spec: FeatureSpec, tag: FaceTag, document: Document) -> _Plane:
    """A chamfer between two faces that are themselves derivable.

    A chamfer is named after the edge it replaced, and an edge is named after
    the two faces it separated — so the question "where is this chamfer?" turns
    into "where are those two faces?", which this module already answers. The
    chamfer's plane bisects them, set back by the feature's own ``distance``.

    Only the case where both parents stand on edge to the same sketch is
    derived: two swept faces share a vertical edge, and the chamfer between them
    is vertical too, so the whole construction stays in the sketch's plane and
    needs nothing the document does not state. A chamfer against a cap runs at
    an angle out of that plane and is refused.
    """
    source = tag.source
    if not isinstance(source, EdgeTag):
        raise _NotDerivable(
            f"'{tag}' does not name the edge it replaced, so its parents are unknown"
        )

    parents: list[_Plane] = []
    specs: list[FeatureSpec] = []
    for face in source.faces:
        try:
            parent_spec = document.feature(face.feature)
            plane = _plane_of(parent_spec, face.without_ordinal(), document)
        except (_NotDerivable, FacetCADError) as error:
            reason = error.reason if isinstance(error, _NotDerivable) else str(error)
            raise _NotDerivable(
                f"'{tag}' bevels between '{face}' and the other face of its edge, and "
                f"'{face}' cannot itself be derived: {reason}"
            ) from error
        parents.append(plane)
        specs.append(parent_spec)

    first, second = parents
    if first.normal is None or second.normal is None or first.parent != second.parent:
        raise _NotDerivable(
            f"'{tag}' bevels between a face parallel to its sketch and one on edge to "
            "it, so the chamfer runs out of the sketch's plane and its angle is not "
            "stated in the sheet. Pick one of the two faces it bevels between"
        )

    # Only now: both parents are swept, so both name a curve.
    curves = [
        _curve_of(parent_spec, face.without_ordinal(), document)
        for parent_spec, face in zip(specs, source.faces, strict=True)
    ]
    distance = spec.option("distance")

    # Proportional to the bisector; `Frame.from_origin_normal` normalises, so
    # only the *direction* has to be right here.
    normal = tuple(
        _sum(a, b)
        for a, b in zip(_unit(first.normal), _unit(second.normal), strict=True)
    )

    corner, along_first, along_second = _corner_of(curves, document)
    # One step, not two. The bevel runs from `distance` along one face to
    # `distance` along the other, so either of those two points is on its plane
    # — stepping along both would land at twice the setback, off the chamfer
    # entirely.
    first_edge = _stepped(corner, along_first, distance)
    second_edge = _stepped(corner, along_second, distance)
    origin = first_edge
    # U runs across the bevel, from one edge of it to the other, which leaves V
    # running up the part exactly as it does on a plain side face. Anything else
    # reads as a rotated sketch on a face that plainly is not rotated.
    across = tuple(_difference(second_edge[i], first_edge[i]) for i in range(3))

    return _Plane(
        parent=first.parent,
        offset=0,
        origin=origin,  # type: ignore[arg-type]
        normal=normal,  # type: ignore[arg-type]
        x_axis=across,  # type: ignore[arg-type]
        size=(_length(across), _swept_by(specs[0])),
        explanation=(
            f"the chamfer '{spec.id}' cuts between two faces that stand on edge to "
            f"sketch datum '{first.parent}', so it bisects them, set back by "
            f"'{distance}'"
        ),
    )


def _stepped(
    origin: tuple[Value, Value, Value],
    direction: tuple[Value, Value, Value],
    distance: Value,
) -> tuple[Value, Value, Value]:
    step = _scaled(_unit(direction), distance)
    return tuple(_sum(origin[i], step[i]) for i in range(3))  # type: ignore[return-value]


def _corner_of(
    curves: Sequence[tuple[Sketch, SketchCurve]], document: Document
) -> tuple[
    tuple[Value, Value, Value],
    tuple[Value, Value, Value],
    tuple[Value, Value, Value],
]:
    """The point the two curves share, and the direction away from it along each.

    Which endpoint they share is a structural fact about the document, so it is
    settled once by resolving the sheet — exactly as the loop's winding is — and
    the answer is then emitted as the *symbolic* coordinates of that point. The
    resolved values decide which point; they never appear in the datum.
    """
    (sketch_a, curve_a), (sketch_b, curve_b) = curves
    try:
        points = sketch_a.resolve_points(document.parameters.resolve())
    except FacetCADError as error:
        raise _NotDerivable(
            "the sketch these faces were swept from cannot be resolved, so the corner "
            f"they share cannot be identified: {error}"
        ) from error

    for shared, other in ((curve_a.start, curve_a.end), (curve_a.end, curve_a.start)):
        if sketch_b.id != sketch_a.id or shared not in (curve_b.start, curve_b.end):
            continue
        beyond = curve_b.end if curve_b.start == shared else curve_b.start
        here = _point_of(sketch_a, shared, curve_a.id)
        there = _point_of(sketch_a, other, curve_a.id)
        far = _point_of(sketch_b, beyond, curve_b.id)
        return (
            (here[0], here[1], 0),
            (_difference(there[0], here[0]), _difference(there[1], here[1]), 0),
            (_difference(far[0], here[0]), _difference(far[1], here[1]), 0),
        )

    del points
    raise _NotDerivable(
        f"curves '{curve_a.id}' and '{curve_b.id}' do not meet at a shared point, so "
        "the corner the chamfer cuts across is not stated in the sketch"
    )


def _length(vector: tuple[Value, Value, Value]) -> Value:
    """A vector's magnitude, kept symbolic wherever its parts are."""
    if all(_is_number(part) for part in vector):
        return math.hypot(*(float(part) for part in vector))  # type: ignore[arg-type]
    return f"hypot({vector[0]}, {vector[1]}, {vector[2]})"


def _swept_by(spec: FeatureSpec) -> Value:
    """How far the feature swept its profile — the height of any face it left.

    Stated as the feature's own option, so a face's height tracks the pad that
    made it. A through-all cut has no stated depth and so no stated height; the
    caller gets nothing rather than a guess.
    """
    if spec.type == "pad":
        return spec.option("length")
    if spec.flag("through_all"):
        raise _NotDerivable(
            f"'{spec.id}' cuts through all, so how deep the face runs is decided by "
            "the solid rather than stated in the sheet"
        )
    return spec.option("depth")


def _unit(vector: tuple[Value, Value, Value]) -> tuple[Value, Value, Value]:
    """The same direction, scaled to length one, symbolically.

    ``hypot`` is in the expression grammar, so the magnitude is one call rather
    than a hand-rolled sum of squares under a square root — shorter to read and
    with nothing to get wrong in the transcription.
    """
    if all(_is_number(part) for part in vector):
        length = math.hypot(*(float(part) for part in vector))  # type: ignore[arg-type]
        if length < 1e-12:
            return vector
        return tuple(float(part) / length for part in vector)  # type: ignore[return-value]
    length = f"hypot({vector[0]}, {vector[1]}, {vector[2]})"
    return tuple(f"({part}) / {length}" for part in vector)  # type: ignore[return-value]


def _sum(left: Value, right: Value) -> Value:
    if _is_number(left) and _is_number(right):
        return float(left) + float(right)  # type: ignore[arg-type]
    return f"({left}) + ({right})"


def _scaled(vector: tuple[Value, Value, Value], factor: Value) -> tuple[Value, Value, Value]:
    if _is_number(factor) and all(_is_number(part) for part in vector):
        return tuple(float(part) * float(factor) for part in vector)  # type: ignore[return-value,arg-type]
    return tuple(f"({part}) * ({factor})" for part in vector)  # type: ignore[return-value]


def _cut_plane(spec: FeatureSpec, role: str, document: Document) -> _Plane:
    """A pocket, hole or thread: the ceiling is the sketch, the floor is deeper."""
    if role not in (Roles.CEILING, Roles.FLOOR, Roles.COUNTERBORE_FLOOR):
        raise _NotDerivable(
            f"a {spec.type} has no '{role}' face; {spec.type} '{spec.id}' names its "
            "faces 'ceiling', 'floor' and 'wall'"
        )
    if spec.type == "thread" and not spec.flag("internal", True):
        raise _NotDerivable(
            f"thread '{spec.id}' is external, so it drills nothing and has no "
            f"'{role}' face"
        )

    sketch = _sketch_of(spec, document)
    verb = _VERBS[spec.type]
    if role == Roles.CEILING:
        return _Plane(
            parent=sketch.plane,
            offset=0,
            explanation=(
                f"the ceiling of {spec.type} '{spec.id}', which {verb} sketch "
                f"'{sketch.id}' on datum '{sketch.plane}', so it lies on "
                f"'{sketch.plane}' itself"
            ),
        )

    if role == Roles.FLOOR and spec.flag("through_all"):
        raise _NotDerivable(
            f"{spec.type} '{spec.id}' cuts through_all, so how deep it goes is decided "
            "by the solid it meets rather than by the document, and its floor — if it "
            "has one at all — is not derivable"
        )

    counterbore = role == Roles.COUNTERBORE_FLOOR
    depth = spec.option("counterbore_depth" if counterbore else "depth")
    offset = depth if _direction(spec, -1) > 0 else _negated(depth)
    what = "the counterbore shoulder" if counterbore else "the floor"
    return _Plane(
        parent=sketch.plane,
        offset=offset,
        explanation=(
            f"{what} of {spec.type} '{spec.id}', which {verb} sketch '{sketch.id}' on "
            f"datum '{sketch.plane}' by '{offset}'"
        ),
    )


def _sketch_of(spec: FeatureSpec, document: Document) -> Sketch:
    """The sketch a feature was built from, however it names it."""
    if spec.profile is not None:
        return document.sketch(spec.profile.sketch)
    # A hole or a thread is placed at a point rather than swept from a loop, so
    # it names its sketch through `at`, written 'sketch.point'.
    placement = str(spec.options.get("at", ""))
    if placement.count(".") == 1:
        return document.sketch(placement.split(".")[0])
    raise _NotDerivable(
        f"feature '{spec.id}' names no sketch, so there is no datum to derive from"
    )


# --------------------------------------------------------------------------
# Symbolic arithmetic on the offset
# --------------------------------------------------------------------------


def _negated(value: Value) -> Value:
    """Flip an offset without changing what it means.

    A leading minus binds tighter than ``+``, ``-`` and ``*`` in the expression
    grammar (see :mod:`facet.domain.expressions`), so ``-a + b`` parses as
    ``(-a) + b``. The parentheses are therefore not cosmetic: they are the
    difference between negating the offset and negating only its first term. A
    bare name has nothing to bind wrongly, so it is left to read plainly.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return -float(value)
    text = str(value).strip()
    return f"-{text}" if text.isidentifier() else f"-({text})"


def _halved(value: Value) -> Value:
    """Half an offset, with the same care over precedence as :func:`_negated`."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) / 2
    text = str(value).strip()
    return f"{text} / 2" if text.isidentifier() else f"({text}) / 2"


# --------------------------------------------------------------------------
# Naming, and finding a datum that is already there
# --------------------------------------------------------------------------


def _identifier_for(tag: FaceTag, document: Document, same_plane: Sequence[str]) -> str:
    """A readable, deterministic id for the proposed datum.

    Determinism is what makes the proposal safe to ask for twice. The caller
    asks about a face, is offered ``pad_1_cap_pos``, and asking again — after an
    edit, from another station, from a rerun of the same script — is offered the
    same id rather than a second datum beside the first. An id that varied with
    a counter or with what else happened to be in the document would turn
    "propose, then PUT" into a way of accumulating near-duplicate planes, which
    is the very thing ``existing`` is here to prevent.
    """
    base = f"{tag.feature}_{_role_word(tag)}"
    candidate, suffix = base, 2
    # A datum already on this plane is not a clash: PUTting over it is a no-op,
    # and the caller has been told to reuse it anyway.
    while candidate in document.datums.planes and candidate not in same_plane:
        candidate, suffix = f"{base}_{suffix}", suffix + 1
    return candidate


def _role_word(tag: FaceTag) -> str:
    """The id fragment for a role.

    A swept face needs the curve in the name as well: every side of a pad shares
    the role, and collapsing them onto one id would offer the same datum for
    four different planes.
    """
    known = _ROLE_WORDS.get(tag.role)
    if known is not None:
        return known
    source = tag.source
    if isinstance(source, CurveRef):
        return f"{tag.role}_{source.sketch}_{source.curve}"
    return tag.role


def _number(value: Value, parameters: ResolvedParameters) -> float:
    return resolve_value(value, parameters, where="datums.origin")


def _vector(
    value: tuple[Value, Value, Value], parameters: ResolvedParameters
) -> Vec3:
    return Vec3(*(_number(part, parameters) for part in value))


def _datums_on(
    plane: _Plane,
    frames: Mapping[str, Frame],
    parameters: ResolvedParameters | None,
) -> list[str]:
    """Datums that already describe the derived plane, in a stable order.

    Compared in world space rather than by matching the parent and the offset
    text: a document usually already holds the plane written another way — the
    top of a plate declared as ``[0, 0, plate_t]`` on the world frame is the
    same plane as this proposal's ``plate_t`` above ``base`` — and the point of
    the answer is to stop a second one being created beside it.
    """
    parent = frames.get(plane.parent)
    if parent is None or parameters is None:
        return []
    try:
        if plane.normal is None:
            origin = parent.to_world(Vec3(0, 0, _number(plane.offset, parameters)))
            normal = parent.z_axis
        else:
            origin = parent.to_world(_vector(plane.origin or (0, 0, 0), parameters))
            normal = parent.direction_to_world(_vector(plane.normal, parameters))
            if normal.length() < 1e-9:
                return []
            normal = normal.normalized()
    except FacetCADError:
        # The plane reads a parameter the sheet cannot resolve. The proposal
        # still stands — it is symbolic — but nothing can be compared against it.
        return []
    # The in-plane rotation matters as much as the plane. Two datums on one
    # plane but turned relative to each other are different sketching frames:
    # reusing one while reporting coordinates for the other puts a point at
    # numbers that are right in a frame nobody is using. Same plane, same sense,
    # same u direction, or it is a different datum.
    heading = _proposed_heading(plane, parent, parameters, normal)
    found = [
        identifier
        for identifier, frame in sorted(frames.items())
        # Same sense, not merely parallel: a datum facing the other way describes
        # the same plane but would mirror every sketch drawn on it.
        if frame.z_axis.dot(normal) >= 1.0 - ANGULAR_TOL
        and abs((frame.origin - origin).dot(normal)) <= COINCIDENT_TOLERANCE
        and (heading is None or frame.x_axis.dot(heading) >= 1.0 - ANGULAR_TOL)
    ]
    # Alphabetical, so a plane described by two datums always offers the same
    # one. Which of them is the better name is not something this can know.
    return found


def _proposed_heading(
    plane: _Plane,
    parent: Frame,
    parameters: ResolvedParameters,
    normal: Vec3,
) -> Vec3 | None:
    """Which way u would run on the proposed datum, in world terms."""
    if plane.x_axis is None:
        return None
    try:
        hint = parent.direction_to_world(_vector(plane.x_axis, parameters))
    except FacetCADError:
        return None
    # The same projection `Frame.from_origin_normal` performs, so the comparison
    # is against the axis the datum would really end up with.
    projected = hint - normal * normal.dot(hint)
    if projected.length() < 1e-9:
        return None
    return projected.normalized()


def _resolved(document: Document) -> tuple[ResolvedParameters | None, Mapping[str, Frame]]:
    """The parameter table and datum frames, or nothing if they do not resolve.

    A sheet that does not currently resolve must not stop a face's plane being
    read — the derivation never needed a number — so this fails quietly and the
    caller loses only the offer to reuse an existing datum.
    """
    try:
        parameters = document.parameters.resolve()
        return parameters, document.datums.resolve_all(parameters)
    except FacetCADError:
        return None, {}
