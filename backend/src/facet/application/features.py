"""Feature handlers and their registry.

Open/closed in practice: adding a feature type means writing a handler and
decorating it with :func:`register`. No existing handler changes, and there is
no ``if spec.type == ...`` chain anywhere in the recompute engine.

A handler receives a :class:`BuildContext` carrying only what a feature may
legitimately see — resolved parameters, the datum frames, the sketches, the
kernel port and the upstream solid. It cannot reach the repository or the HTTP
layer, so a feature stays a pure function of (document, upstream shape).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Protocol

from facet.domain import standards
from facet.domain.errors import CapabilityError, DocumentError, FeatureBuildError
from facet.domain.features import FeatureSpec
from facet.domain.math3d import Frame, Vec2
from facet.domain.parameters import ResolvedParameters
from facet.domain.selectors import EdgeSelector
from facet.domain.sketch import CurveKinds, ResolvedCurve, Sketch, sketch_frame
from facet.domain.standards import Fit
from facet.domain.tags import Roles
from facet.domain.values import resolve as resolve_value

from .naming import (
    CHAMFER_ROLES,
    FILLET_ROLES,
    PAD_ROLES,
    POCKET_ROLES,
    NamedSolid,
    RoleVocabulary,
)
from .ports.geometry import (
    BlendKernel,
    BlendRequest,
    Capability,
    CurveType,
    GeometryKernel,
    PadRequest,
    PocketRequest,
    Profile,
    ProfileCurve,
    SolidResult,
    ThreadRequest,
)


@dataclass(frozen=True)
class BuildContext:
    """Everything a feature handler is allowed to see."""

    parameters: ResolvedParameters
    frames: Mapping[str, Frame]
    sketches: Mapping[str, Sketch]
    kernel: GeometryKernel
    previous: NamedSolid | None
    #: How much geometry is worth computing — see :class:`Detail`. A handler
    #: may skip work that only a manufactured part needs, never work that
    #: changes what the part *is*.
    detail: str = "draft"

    @property
    def full_detail(self) -> bool:
        return self.detail == "full"

    def frame(self, identifier: str) -> Frame:
        frame = self.frames.get(identifier)
        if frame is None:
            raise DocumentError(reason=f"unknown datum '{identifier}'")
        return frame


@dataclass(frozen=True)
class FeatureBuild:
    """A handler's output: the kernel result plus how to name it."""

    result: SolidResult
    vocabulary: RoleVocabulary
    sketch: str
    frame: Frame
    consumes_previous: bool = True


@dataclass(frozen=True)
class Option:
    """One key a feature type understands.

    Declared rather than merely read, for two reasons. It lets an unknown key be
    refused instead of silently ignored — a `counterbore` on a pad used to be
    accepted, stored, and quietly do nothing, which is the exact failure this
    project exists to prevent. And it lets the API say what a feature takes
    without anyone having to provoke a build error to find out.
    """

    name: str
    describe: str
    required: bool = False


class FeatureHandler(Protocol):
    """Builds one kind of feature."""

    @property
    def type(self) -> str:
        ...

    @property
    def options(self) -> tuple[Option, ...]:
        """Every key this type reads, including the optional ones."""
        ...

    @property
    def required_capability(self) -> str:
        ...

    def build(
        self, spec: FeatureSpec, context: BuildContext
    ) -> FeatureBuild | Sequence[FeatureBuild]:
        """Build the feature.

        Returning several builds lets one feature perform several kernel
        operations — a counterbored hole is a bore then a wider, shallower cut —
        while still presenting as a single entry in the history. They are named
        in order under the same feature id, each with its own role vocabulary.
        """
        ...


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

_REGISTRY: dict[str, FeatureHandler] = {}


def register(handler_class: Callable[[], FeatureHandler]) -> Callable[[], FeatureHandler]:
    """Class decorator registering a handler under its declared type."""
    handler = handler_class()
    if handler.type in _REGISTRY:
        raise RuntimeError(f"feature type '{handler.type}' is already registered")
    _REGISTRY[handler.type] = handler
    return handler_class


def handler_for(spec: FeatureSpec) -> FeatureHandler:
    handler = _REGISTRY.get(spec.type)
    if handler is None:
        raise FeatureBuildError(
            feature=spec.id,
            reason=(
                f"unknown feature type '{spec.type}'; available types are "
                f"{', '.join(sorted(_REGISTRY)) or 'none'}"
            ),
        )
    validate_options(spec)
    return handler


def registered_types() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def describe_types() -> tuple[dict[str, object], ...]:
    """Every feature type with the options it takes.

    What `feature_types` returns. Names alone meant learning the required keys
    from build errors — good errors, but a round trip each, and only after
    writing something wrong.
    """
    return tuple(
        {
            "type": name,
            "options": [
                {
                    "name": option.name,
                    "describe": option.describe,
                    "required": option.required,
                }
                for option in handler.options
            ],
        }
        for name, handler in sorted(_REGISTRY.items())
    )


def validate_options(spec: FeatureSpec) -> None:
    """Refuse a key the feature type does not read.

    Without this a misspelled or misplaced option is accepted, written to the
    document and ignored — the part builds, looks wrong, and nothing anywhere
    says why. Everything else in this system fails loudly; this is the one place
    that quietly did not.
    """
    handler = _REGISTRY.get(spec.type)
    if handler is None:
        return  # handler_for reports the unknown type, with the list of known ones.
    known = {option.name for option in handler.options}
    unknown = sorted(set(spec.options) - known)
    if not unknown:
        return
    detail = []
    for key in unknown:
        near = get_close_matches(key, sorted(known), n=1, cutoff=0.7)
        detail.append(f"'{key}'" + (f" (did you mean '{near[0]}'?)" if near else ""))
    raise FeatureBuildError(
        feature=spec.id,
        reason=(
            f"{spec.type} does not take {', '.join(detail)}. "
            f"It takes: {', '.join(sorted(known)) or 'no options'}."
        ),
    )


# --------------------------------------------------------------------------
# Shared profile construction
# --------------------------------------------------------------------------


def build_profile(spec: FeatureSpec, context: BuildContext) -> tuple[Profile, Sketch]:
    """Resolve a feature's profile reference into kernel-ready geometry."""
    if spec.profile is None:
        raise FeatureBuildError(
            feature=spec.id, reason=f"a {spec.type} needs a 'profile' reference"
        )
    sketch = context.sketches.get(spec.profile.sketch)
    if sketch is None:
        raise FeatureBuildError(
            feature=spec.id, reason=f"unknown sketch '{spec.profile.sketch}'"
        )
    frame = sketch_frame(sketch, context.frames)
    resolved = sketch.resolve_loop(spec.profile.loop, context.parameters)
    curves = tuple(_to_profile_curve(curve) for curve in resolved)
    profile = Profile(
        sketch=sketch.id, loop=spec.profile.loop, frame=frame, curves=curves
    )
    return profile, sketch


#: Document curve kinds map one-to-one onto the port's curve types.
_CURVE_TYPES = {
    CurveKinds.LINE: CurveType.LINE,
    CurveKinds.ARC: CurveType.ARC,
    CurveKinds.CIRCLE: CurveType.CIRCLE,
}


def _to_profile_curve(resolved: ResolvedCurve) -> ProfileCurve:
    """Translate an evaluated sketch curve into the kernel's request type."""
    return ProfileCurve(
        id=resolved.id,
        type=_CURVE_TYPES[resolved.type],
        start=resolved.start,
        end=resolved.end,
        center=resolved.center,
        radius=resolved.radius,
        # The document states sweep direction as `clockwise`; the port takes the
        # opposite sense, so the flip happens once, here.
        counter_clockwise=not resolved.clockwise,
    )


def require_capability(kernel: GeometryKernel, capability: str, feature: str) -> None:
    if capability not in kernel.capabilities:
        raise CapabilityError(
            capability=capability,
            kernel=kernel.name,
            available=tuple(sorted(kernel.capabilities)),
        )
    del feature


def _direction(spec: FeatureSpec, default: int) -> int:
    """Read an explicit +1 / -1 direction. Never inferred from geometry."""
    raw = spec.options.get("direction", default)
    if isinstance(raw, str):
        cleaned = raw.strip().lower()
        if cleaned in {"+normal", "+", "up", "1"}:
            return 1
        if cleaned in {"-normal", "-", "down", "-1"}:
            return -1
        raise DocumentError(
            reason=(
                f"direction {raw!r} is not understood; use '+normal' or '-normal' "
                "(a direction is always explicit, never inferred)"
            ),
            path=f"features.{spec.id}.direction",
        )
    value = int(raw)  # type: ignore[arg-type]
    if value not in (1, -1):
        raise DocumentError(
            reason=f"direction must be +1 or -1, got {value}",
            path=f"features.{spec.id}.direction",
        )
    return value


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------


@register
class PadHandler:
    """Adds material by extruding a closed profile."""

    @property
    def type(self) -> str:
        return "pad"

    @property
    def options(self) -> tuple[Option, ...]:
        return (
            Option("length", "How far to extrude", required=True),
            Option("midplane", "Extrude half each way from the sketch plane"),
        Option("direction", "+1 or -1 along the profile normal"),
        )

    @property
    def required_capability(self) -> str:
        return Capability.PAD

    def build(self, spec: FeatureSpec, context: BuildContext) -> FeatureBuild:
        require_capability(context.kernel, Capability.PAD, spec.id)
        profile, sketch = build_profile(spec, context)
        length = resolve_value(
            spec.option("length"), context.parameters, where=f"features.{spec.id}.length"
        )
        result = context.kernel.pad(
            PadRequest(
                feature=spec.id,
                profile=profile,
                length=length,
                direction=_direction(spec, 1),
                midplane=spec.flag("midplane"),
            )
        )

        if context.previous is None:
            return FeatureBuild(
                result=result,
                vocabulary=PAD_ROLES,
                sketch=sketch.id,
                frame=profile.frame,
                consumes_previous=False,
            )

        # A body is one solid, so a later pad adds to what is there rather than
        # replacing it. Without this the earlier solid was silently discarded.
        # Disjoint pads are fine: the union is a compound holding both, which is
        # what a body with two lugs actually is.
        fused = context.kernel.fuse(context.previous.handle, result.solid)
        return FeatureBuild(
            result=fused,
            vocabulary=PAD_ROLES,
            sketch=sketch.id,
            frame=profile.frame,
        )


@register
class PocketHandler:
    """Removes material by extruding a closed profile and subtracting it."""

    @property
    def type(self) -> str:
        return "pocket"

    @property
    def options(self) -> tuple[Option, ...]:
        return (
            Option("depth", "How deep to cut; ignored when through_all is set"),
            Option("through_all", "Cut the whole way through the material"),
        Option("direction", "+1 or -1 along the profile normal"),
        )

    @property
    def required_capability(self) -> str:
        return Capability.POCKET

    def build(self, spec: FeatureSpec, context: BuildContext) -> FeatureBuild:
        require_capability(context.kernel, Capability.POCKET, spec.id)
        if context.previous is None:
            raise FeatureBuildError(
                feature=spec.id,
                reason="a pocket needs material to cut; add a pad before it",
            )
        profile, sketch = build_profile(spec, context)
        through_all = spec.flag("through_all")
        depth = (
            0.0
            if through_all
            else resolve_value(
                spec.option("depth"), context.parameters, where=f"features.{spec.id}.depth"
            )
        )
        result = context.kernel.pocket(
            context.previous.handle,
            PocketRequest(
                feature=spec.id,
                profile=profile,
                depth=depth,
                direction=_direction(spec, -1),
                through_all=through_all,
            ),
        )
        return FeatureBuild(
            result=result,
            vocabulary=POCKET_ROLES,
            sketch=sketch.id,
            frame=profile.frame,
        )


# --------------------------------------------------------------------------
# Holes
# --------------------------------------------------------------------------

#: A counterbore names its widened wall and the shoulder it leaves.
COUNTERBORE_ROLES = RoleVocabulary(
    swept=Roles.COUNTERBORE,
    cap_start=Roles.CEILING,
    cap_end=Roles.COUNTERBORE_FLOOR,
)


@register
class HoleHandler:
    """A drilled hole, placed at a sketch point rather than swept from a loop.

    This is what separates a hole from a circular pocket. You give it a named
    point and a size — often a fastener designation rather than a diameter,
    because nobody remembers that a normal-fit clearance hole for an M6 is
    6.6mm — and it generates its own circular profile.

    The tag source is the placement point, so a bore reads
    ``bolt/wall[plate.h1]``: the same shape of name as every other face, rooted
    in an id the user chose.

    A counterbore is cut as a second, wider, shallower step. It is one entry in
    the history but two kernel operations, which is why handlers may return a
    sequence.
    """

    @property
    def type(self) -> str:
        return "hole"

    @property
    def options(self) -> tuple[Option, ...]:
        return (
            Option("at", "Where to drill, as 'sketch.point'", required=True),
            Option("depth", "How deep; ignored when through_all is set"),
            Option("through_all", "Drill the whole way through"),
            Option("standard", "An ISO designation such as M6, instead of diameter"),
            Option("diameter", "Explicit hole diameter, instead of standard"),
            Option("fit", "close, normal or free — how much clearance a standard gets"),
            Option("counterbore_diameter", "Width of the counterbore, if any"),
            Option("counterbore_depth", "Depth of the counterbore, if any"),
        Option("direction", "+1 or -1 along the profile normal"),
        )

    @property
    def required_capability(self) -> str:
        return Capability.POCKET

    def build(self, spec: FeatureSpec, context: BuildContext) -> Sequence[FeatureBuild]:
        require_capability(context.kernel, Capability.POCKET, spec.id)
        if context.previous is None:
            raise FeatureBuildError(
                feature=spec.id, reason="a hole needs material to drill; add a pad before it"
            )

        sketch, point_id, centre = _placement(spec, context)
        frame = sketch_frame(sketch, context.frames)
        diameter = _hole_diameter(spec, context)
        direction = _direction(spec, -1)
        through_all = spec.flag("through_all")
        depth = (
            0.0
            if through_all
            else resolve_value(
                spec.option("depth"), context.parameters, where=f"features.{spec.id}.depth"
            )
        )
        if not through_all and depth <= 0:
            raise FeatureBuildError(
                feature=spec.id,
                reason=f"hole depth must be positive, got {depth:.6g}. Use through_all to "
                "drill straight through.",
            )

        builds = [
            FeatureBuild(
                result=context.kernel.pocket(
                    context.previous.handle,
                    PocketRequest(
                        feature=spec.id,
                        profile=_circular_profile(sketch, point_id, frame, centre, diameter / 2),
                        depth=depth,
                        direction=direction,
                        through_all=through_all,
                    ),
                ),
                vocabulary=POCKET_ROLES,
                sketch=sketch.id,
                frame=frame,
            )
        ]

        counterbore = _counterbore(spec, context, diameter, depth, through_all)
        if counterbore is not None:
            cbore_diameter, cbore_depth = counterbore
            builds.append(
                FeatureBuild(
                    result=context.kernel.pocket(
                        builds[-1].result.solid,
                        PocketRequest(
                            feature=spec.id,
                            profile=_circular_profile(
                                sketch, point_id, frame, centre, cbore_diameter / 2
                            ),
                            depth=cbore_depth,
                            direction=direction,
                        ),
                    ),
                    vocabulary=COUNTERBORE_ROLES,
                    sketch=sketch.id,
                    frame=frame,
                )
            )
        return builds


def _placement(spec: FeatureSpec, context: BuildContext) -> tuple[Sketch, str, Vec2]:
    """Resolve the ``sketch.point`` a hole is drilled at."""
    raw = str(spec.option("at"))
    parts = raw.split(".")
    if len(parts) != 2 or not all(part.isidentifier() for part in parts):
        raise FeatureBuildError(
            feature=spec.id,
            reason=f"'at' must be written 'sketch.point', got {raw!r}",
        )
    sketch_id, point_id = parts

    sketch = context.sketches.get(sketch_id)
    if sketch is None:
        raise FeatureBuildError(feature=spec.id, reason=f"unknown sketch '{sketch_id}'")
    points = sketch.resolve_points(context.parameters)
    if point_id not in points:
        raise FeatureBuildError(
            feature=spec.id,
            reason=(
                f"sketch '{sketch_id}' has no point '{point_id}'; it has "
                f"{', '.join(sorted(points)) or 'none'}"
            ),
        )
    return sketch, point_id, points[point_id]


def _hole_diameter(spec: FeatureSpec, context: BuildContext) -> float:
    """Either an explicit diameter, or one looked up from a fastener size."""
    designation = spec.options.get("standard")
    explicit = spec.options.get("diameter")

    if designation and explicit:
        raise FeatureBuildError(
            feature=spec.id,
            reason=(
                f"hole '{spec.id}' gives both a standard ({designation}) and an explicit "
                "diameter; use one or the other"
            ),
        )
    if designation:
        return standards.hole_diameter(str(designation), str(spec.options.get("fit", Fit.NORMAL)))
    if explicit is None:
        raise FeatureBuildError(
            feature=spec.id,
            reason=(
                f"hole '{spec.id}' needs a 'diameter', or a 'standard' such as M6 with an "
                "optional 'fit' of close, normal, loose or tapped"
            ),
        )

    diameter = resolve_value(
        explicit, context.parameters, where=f"features.{spec.id}.diameter"
    )
    if diameter <= 0:
        raise FeatureBuildError(
            feature=spec.id, reason=f"hole diameter must be positive, got {diameter:.6g}"
        )
    return diameter


def _counterbore(
    spec: FeatureSpec,
    context: BuildContext,
    diameter: float,
    depth: float,
    through_all: bool,
) -> tuple[float, float] | None:
    """Validate the counterbore, if one was asked for."""
    raw_diameter = spec.options.get("counterbore_diameter")
    raw_depth = spec.options.get("counterbore_depth")
    if raw_diameter is None and raw_depth is None:
        return None
    if raw_diameter is None or raw_depth is None:
        raise FeatureBuildError(
            feature=spec.id,
            reason="a counterbore needs both counterbore_diameter and counterbore_depth",
        )

    where = f"features.{spec.id}"
    cbore_diameter = resolve_value(
        raw_diameter, context.parameters, where=f"{where}.counterbore_diameter"
    )
    cbore_depth = resolve_value(
        raw_depth, context.parameters, where=f"{where}.counterbore_depth"
    )

    if cbore_diameter <= diameter:
        raise FeatureBuildError(
            feature=spec.id,
            reason=(
                f"the counterbore ({cbore_diameter:.6g}mm) must be wider than the hole "
                f"it steps down to ({diameter:.6g}mm)"
            ),
        )
    if cbore_depth <= 0:
        raise FeatureBuildError(
            feature=spec.id,
            reason=f"counterbore depth must be positive, got {cbore_depth:.6g}",
        )
    if not through_all and cbore_depth >= depth:
        raise FeatureBuildError(
            feature=spec.id,
            reason=(
                f"the counterbore is {cbore_depth:.6g}mm deep but the hole is only "
                f"{depth:.6g}mm; it would swallow the bore entirely"
            ),
        )
    return cbore_diameter, cbore_depth


def _circular_profile(
    sketch: Sketch, point_id: str, frame: Frame, centre: Vec2, radius: float
) -> Profile:
    """A generated circle, tagged against the point that placed it."""
    return Profile(
        sketch=sketch.id,
        loop=point_id,
        frame=frame,
        curves=(
            ProfileCurve(id=point_id, type=CurveType.CIRCLE, center=centre, radius=radius),
        ),
    )


# --------------------------------------------------------------------------
# Blends
# --------------------------------------------------------------------------


class BlendHandler:
    """Shared machinery for fillet and chamfer.

    The edges are stated as a selector, not picked, so a rebuild re-resolves
    what the user meant rather than replaying an index. That is the entire
    reason the naming system exists, and blends are where it earns its keep:
    this is exactly where FreeCAD reattaches a fillet to the wrong edge.

    ``on_failure: skip`` exists because blends are the one operation whose
    failure is genuinely kernel-bound. A radius that does not fit is not a bug
    in the document, and a model should be able to survive one without the whole
    history halting.
    """

    #: Subclasses set these.
    kind = ""
    role_vocabulary: RoleVocabulary = PAD_ROLES

    @property
    def type(self) -> str:
        return self.kind

    @property
    def options(self) -> tuple[Option, ...]:
        size = "radius" if self.kind == "fillet" else "distance"
        return (
            Option(
                "edges",
                "Edge selector string such as 'a ^ b', or a comma-separated union. "
                "A string, not a list",
                required=True,
            ),
            Option(size, f"The {self.kind} size", required=True),
            Option(
                "on_failure",
                "'skip' leaves the blend out rather than failing the rebuild",
            ),
        )

    @property
    def required_capability(self) -> str:
        return Capability.FILLET if self.kind == "fillet" else Capability.CHAMFER

    def build(self, spec: FeatureSpec, context: BuildContext) -> FeatureBuild:
        require_capability(context.kernel, self.required_capability, spec.id)
        if context.previous is None:
            raise FeatureBuildError(
                feature=spec.id,
                reason=f"a {self.kind} needs a solid to work on; add a pad before it",
            )

        size = resolve_value(
            spec.option("radius" if self.kind == "fillet" else "distance"),
            context.parameters,
            where=f"features.{spec.id}",
        )
        selector = _edge_selector(spec)
        matched = selector.resolve(context.previous.topology, feature=spec.id)

        refs = [context.previous.ref_of_edge(entry.tag) for entry in matched]
        missing = [str(entry.tag) for entry, ref in zip(matched, refs, strict=True) if ref is None]
        if missing:
            raise FeatureBuildError(
                feature=spec.id,
                reason=(
                    f"resolved {len(matched)} edge(s) but the kernel does not know "
                    f"{', '.join(missing)}"
                ),
            )

        blender: BlendKernel = context.kernel  # type: ignore[assignment]
        operation = blender.fillet if self.kind == "fillet" else blender.chamfer
        request = BlendRequest(
            feature=spec.id, edges=tuple(r for r in refs if r), size=size
        )

        try:
            result = operation(context.previous.handle, request)
        except FeatureBuildError:
            if spec.options.get("on_failure") == "skip":
                # Pass the upstream solid through untouched. Reported as
                # 'bypassed' so it is visible rather than silently dropped.
                raise BlendSkipped(feature=spec.id, reason=f"the {self.kind} did not fit") from None
            raise

        return FeatureBuild(
            result=result,
            vocabulary=self.role_vocabulary,
            sketch=spec.id,
            frame=context.frames.get("xy", Frame.world()),
        )


class BlendSkipped(Exception):
    """A blend that failed but was allowed to, by ``on_failure: skip``."""

    def __init__(self, feature: str, reason: str) -> None:
        super().__init__(reason)
        self.feature = feature
        self.reason = reason


def _edge_selector(spec: FeatureSpec) -> EdgeSelector:
    raw = spec.targets.get("edges")
    if raw is not None:
        return EdgeSelector(touching=raw.include, direction=raw.direction)
    text = spec.options.get("edges")
    if not text:
        raise FeatureBuildError(
            feature=spec.id,
            reason=(
                f"{spec.type} '{spec.id}' needs an 'edges' selector, such as "
                "'base/cap+ ^ base/side[*]' for a whole top perimeter"
            ),
        )
    return EdgeSelector.parse(str(text))


#: A thread's faces are named like a hole's wall, from the placement point.
THREAD_ROLES = RoleVocabulary(swept=Roles.THREAD)


@register
class ThreadHandler:
    """A tapped hole, or an external thread on a boss.

    One feature, two kernel operations: the hole is drilled at the tap-drill
    size and then the thread form is cut into it. Splitting them across two
    document entries would let someone change the drill without changing the
    thread, which is a real part that does not fit a real screw.

    The thread form is only modelled when asked for. It costs a few seconds to
    cut and most parts never need the geometry — a machinist taps the hole, and
    a drawing says M6. Printed parts do need it, which is why the option exists
    at all.
    """

    @property
    def type(self) -> str:
        return "thread"

    @property
    def options(self) -> tuple[Option, ...]:
        return (
            Option("at", "Where to place it, as 'sketch.point'", required=True),
            Option("standard", "ISO designation such as M6", required=True),
            Option("depth", "Threaded length", required=True),
            Option("internal", "True taps a hole, false threads a boss"),
            Option("through_all", "Drill the whole way through"),
            Option("hand", "right or left"),
            Option(
                "modelled",
                "true, false, or 'export' to cut the helix for files but not the viewport",
            ),
        Option("direction", "+1 or -1 along the profile normal"),
        )

    @property
    def required_capability(self) -> str:
        return Capability.POCKET

    def build(self, spec: FeatureSpec, context: BuildContext) -> Sequence[FeatureBuild]:
        require_capability(context.kernel, Capability.POCKET, spec.id)
        if context.previous is None:
            raise FeatureBuildError(
                feature=spec.id,
                reason="a thread needs material to cut; add a pad before it",
            )

        sketch, point_id, centre = _placement(spec, context)
        frame = sketch_frame(sketch, context.frames)
        thread = _thread_standard(spec)
        internal = spec.flag("internal", True)
        direction = _direction(spec, -1)
        depth = resolve_value(
            spec.option("depth"), context.parameters, where=f"features.{spec.id}.depth"
        )
        if depth <= 0:
            raise FeatureBuildError(
                feature=spec.id, reason=f"thread depth must be positive, got {depth:.6g}"
            )

        builds: list[FeatureBuild] = []
        if internal:
            # Drill at the tap-drill size, which is what the thread is cut into.
            builds.append(
                FeatureBuild(
                    result=context.kernel.pocket(
                        context.previous.handle,
                        PocketRequest(
                            feature=spec.id,
                            profile=_circular_profile(
                                sketch, point_id, frame, centre, thread.tap_drill / 2
                            ),
                            depth=depth,
                            direction=direction,
                            through_all=spec.flag("through_all"),
                        ),
                    ),
                    vocabulary=POCKET_ROLES,
                    sketch=sketch.id,
                    frame=frame,
                )
            )

        if not _modelled_now(spec, context):
            if not builds:
                raise FeatureBuildError(
                    feature=spec.id,
                    reason=(
                        "an external thread has nothing to do unless it is modelled; "
                        "set modelled: true or 'export', or use a pad for the "
                        "plain boss"
                    ),
                )
            return builds

        require_capability(context.kernel, Capability.THREAD, spec.id)
        previous = builds[-1].result.solid if builds else context.previous.handle
        origin = frame.point_at(centre, 0.0)
        axis = frame.z_axis * float(direction)
        # The thread starts at the mouth and runs inwards, so the axis points
        # the way the drill went and the origin sits on the face it entered.
        builds.append(
            FeatureBuild(
                result=context.kernel.thread(
                    previous,
                    ThreadRequest(
                        feature=spec.id,
                        origin=origin,
                        direction=axis,
                        major=thread.nominal,
                        pitch=thread.pitch,
                        length=depth,
                        internal=internal,
                        right_handed=str(spec.options.get("hand", "right")) != "left",
                        curve=point_id,
                    ),
                ),
                vocabulary=THREAD_ROLES,
                sketch=sketch.id,
                frame=frame,
            )
        )
        return builds


def _modelled_now(spec: FeatureSpec, context: BuildContext) -> bool:
    """Whether to cut the helix on this rebuild.

    ``modelled`` is deliberately tri-state. ``false`` is a cosmetic thread: the
    hole is drilled at the tap-drill size and the designation is a note, which
    is what a machinist wants. ``true`` always cuts the form. ``"export"`` cuts
    it only when the rebuild is for a file rather than for the screen — a
    printed part needs the geometry, but on screen it is a grey cylinder either
    way and the cut costs seconds every time anything upstream changes.
    """
    raw = spec.options.get("modelled", False)
    if isinstance(raw, str):
        wanted = raw.strip().lower()
        if wanted == "export":
            return context.full_detail
        if wanted in {"true", "always", "yes"}:
            return True
        if wanted in {"false", "never", "no"}:
            return False
        raise DocumentError(
            reason=(
                f"modelled must be true, false or 'export', got {raw!r}. "
                "'export' cuts the thread for a file but not for the viewport."
            ),
            path=f"features.{spec.id}.modelled",
        )
    return bool(raw)


def _thread_standard(spec: FeatureSpec) -> standards.Thread:
    designation = spec.options.get("standard")
    if not designation:
        raise FeatureBuildError(
            feature=spec.id,
            reason=(
                f"thread '{spec.id}' needs a 'standard', such as M6. "
                f"Known: {', '.join(standards.designations())}"
            ),
        )
    return standards.thread(str(designation))


@register
class FilletHandler(BlendHandler):
    """Rounds edges."""

    kind = "fillet"
    role_vocabulary = FILLET_ROLES


@register
class ChamferHandler(BlendHandler):
    """Bevels edges."""

    kind = "chamfer"
    role_vocabulary = CHAMFER_ROLES
