"""The recompute engine.

Rebuilds a document into a solid, feature by feature, and reports precisely what
happened at each step.

Three properties matter:

**Errors are per-feature.** A failure does not abandon the rebuild; it stops the
chain at that point and the caller still receives the last successfully built
state. The user sees the part as far as it got, with the failure marked — rather
than an empty viewport and a stack trace.

**Nothing is guessed.** A selector that no longer resolves raises, and the
diagnostic names the feature responsible. That is the whole point of the
project, enforced here at the point of use.

**Unchanged work is not redone.** Each feature caches its result under a content
hash of everything that could affect it, so editing one dimension rebuilds only
the features that actually depend on it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from facet.domain.document import Document
from facet.domain.errors import (
    FacetCADError,
    FeatureBuildError,
    SelectorResolutionError,
)
from facet.domain.math3d import Frame
from facet.domain.parameters import ResolvedParameters
from facet.domain.topology import TopologyIndex

from .features import (
    BlendSkipped,
    BuildContext,
    FeatureBuild,
    handler_for,
    unknown_options,
)
from .naming import NamedSolid, NamingEngine
from .ports.geometry import GeometryKernel


class FeatureStatus:
    BUILT = "built"
    CACHED = "cached"
    SUPPRESSED = "suppressed"
    FAILED = "failed"
    SKIPPED = "skipped"
    """Not attempted because an earlier feature failed."""
    BYPASSED = "bypassed"
    """Failed, but declared `on_failure: skip`, so the model carried on.

    Reported rather than swallowed: a fillet that silently did not happen is
    exactly the kind of quiet wrongness this project exists to avoid.
    """


@dataclass(frozen=True)
class FeatureOutcome:
    """What happened to one feature during a rebuild."""

    id: str
    type: str
    status: str
    error: FacetCADError | None = None
    #: Things worth saying about a feature that still built. An option the type
    #: does not read lands here: it is ignored, which is exactly the silence
    #: that made a counterbore on a pad so expensive to diagnose.
    warnings: tuple[str, ...] = ()
    face_count: int = 0
    duration_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status in (
            FeatureStatus.BUILT,
            FeatureStatus.CACHED,
            FeatureStatus.BYPASSED,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "type": self.type,
            "status": self.status,
            "faceCount": self.face_count,
            "error": self.error.as_dict() if self.error else None,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class BodyResult:
    """One body's rebuilt solid, in its own coordinates.

    ``placement`` is where the body sits, and is applied for display and export
    only. Keeping it out of the modelled geometry means moving a body can never
    perturb a face fingerprint or a split ordinal.
    """

    id: str
    solid: NamedSolid | None = None
    outcomes: tuple[FeatureOutcome, ...] = ()
    placement: Frame = field(default_factory=Frame.world)
    error: FacetCADError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and all(
            o.ok or o.status == FeatureStatus.SUPPRESSED for o in self.outcomes
        )

    @property
    def topology(self) -> TopologyIndex:
        return self.solid.topology if self.solid else TopologyIndex()

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "ok": self.ok,
            "features": [o.to_dict() for o in self.outcomes],
            "placement": list(self.placement.to_matrix()),
            "faceCount": len(self.topology.faces),
            "error": self.error.as_dict() if self.error else None,
        }


@dataclass(frozen=True)
class RecomputeResult:
    """The outcome of rebuilding a whole document."""

    bodies: tuple[BodyResult, ...] = ()
    parameters: ResolvedParameters | None = None
    frames: Mapping[str, Frame] = field(default_factory=dict)
    error: FacetCADError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and all(body.ok for body in self.bodies)

    # -- single-body conveniences -----------------------------------------
    #
    # Most documents have one body, and every caller predating bodies expects
    # these, so they read through to the first rather than forcing a lookup.

    @property
    def solid(self) -> NamedSolid | None:
        return self.bodies[0].solid if self.bodies else None

    @property
    def outcomes(self) -> tuple[FeatureOutcome, ...]:
        return tuple(o for body in self.bodies for o in body.outcomes)

    def body(self, identifier: str) -> BodyResult | None:
        return next((b for b in self.bodies if b.id == identifier), None)

    @property
    def topology(self) -> TopologyIndex:
        return self.solid.topology if self.solid else TopologyIndex()

    @property
    def last_good_feature(self) -> str | None:
        built = [o.id for o in self.outcomes if o.ok]
        return built[-1] if built else None

    def failures(self) -> tuple[FeatureOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status == FeatureStatus.FAILED)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "bodies": [body.to_dict() for body in self.bodies],
            # Flattened across bodies, for callers that predate them.
            "features": [o.to_dict() for o in self.outcomes],
            "parameters": self.parameters.as_dict() if self.parameters else {},
            "lastGoodFeature": self.last_good_feature,
            "error": self.error.as_dict() if self.error else None,
        }


@dataclass
class _CacheEntry:
    key: str
    solid: NamedSolid


class Detail:
    """How much geometry a rebuild is worth computing.

    Some geometry is expensive and adds nothing to a picture. A modelled thread
    costs seconds and ninety-odd faces, and on screen it is a grey cylinder
    either way — but it has to be in the STL or the printed part has no thread.
    So a feature may declare itself export-only and be skipped for the viewport.

    The two levels are cached separately rather than invalidating each other,
    because a session alternates between them constantly.
    """

    DRAFT = "draft"
    """Everything needed to see and select the part. The default."""
    FULL = "full"
    """Everything, including geometry only a manufactured part needs."""


class RecomputeEngine:
    """Rebuilds documents, caching per feature.

    The cache key for a feature is a hash of its own spec, the resolved
    parameters it reads, the frame it is built on, and the key of the feature
    before it. Chaining the upstream key means an edit early in the history
    invalidates everything after it — which a linear history requires — while an
    edit to the last feature rebuilds only that one.
    """

    def __init__(self, kernel: GeometryKernel) -> None:
        self._kernel = kernel
        self._cache: dict[str, _CacheEntry] = {}

    @property
    def kernel(self) -> GeometryKernel:
        return self._kernel

    def invalidate(self, feature: str | None = None) -> None:
        if feature is None:
            self._cache.clear()
        else:
            self._cache.pop(feature, None)

    # -- the rebuild -------------------------------------------------------

    def recompute(self, document: Document, detail: str = Detail.DRAFT) -> RecomputeResult:
        try:
            document.validate()
            parameters = document.parameters.resolve()
            frames = document.datums.resolve_all(parameters)
        except FacetCADError as error:
            # A document that cannot even resolve its parameters has no partial
            # state worth showing, so this is the one whole-document failure.
            return RecomputeResult(error=error)

        bodies: list[BodyResult] = []
        for body in document.bodies:
            try:
                placement = body.placement.resolve(parameters, body.id)
            except FacetCADError as error:
                bodies.append(BodyResult(id=body.id, error=error))
                continue
            bodies.append(
                self._recompute_body(body, document, parameters, frames, placement, detail)
            )

        return RecomputeResult(
            bodies=tuple(bodies), parameters=parameters, frames=frames
        )

    def _recompute_body(
        self,
        body,
        document: Document,
        parameters: ResolvedParameters,
        frames: Mapping[str, Frame],
        placement: Frame,
        detail: str = Detail.DRAFT,
    ) -> BodyResult:
        """Rebuild one body's history. Bodies never see each other's solids."""
        naming = NamingEngine()
        outcomes: list[FeatureOutcome] = []
        current: NamedSolid | None = None
        upstream_key = ""
        halted = False

        for spec in body.features:
            if halted:
                outcomes.append(
                    FeatureOutcome(id=spec.id, type=spec.type, status=FeatureStatus.SKIPPED)
                )
                continue

            if spec.suppressed:
                outcomes.append(
                    FeatureOutcome(id=spec.id, type=spec.type, status=FeatureStatus.SUPPRESSED)
                )
                continue

            ignored = unknown_options(spec)
            notes = (f"{ignored} — the extra key is ignored.",) if ignored else ()

            key = self._cache_key(spec, document, parameters, frames, upstream_key)
            # Namespaced by detail as well as by body: a session alternates
            # between drawing the viewport and writing an STL, and letting one
            # evict the other would recut a thread on every switch.
            slot = f"{detail}/{body.id}/{spec.id}"
            cached = self._cache.get(slot)
            if cached is not None and cached.key == key:
                current = cached.solid
                upstream_key = key
                _reregister_frames(naming, document, spec, frames)
                outcomes.append(
                    FeatureOutcome(
                        id=spec.id,
                        type=spec.type,
                        status=FeatureStatus.CACHED,
                        warnings=notes,
                        face_count=len(current.topology.faces),
                    )
                )
                continue

            try:
                current = self._build_one(
                    spec, document, parameters, frames, naming, current, detail
                )
            except BlendSkipped as skipped:
                outcomes.append(
                    FeatureOutcome(
                        id=spec.id,
                        type=spec.type,
                        status=FeatureStatus.BYPASSED,
                        error=FeatureBuildError(feature=spec.id, reason=skipped.reason),
                        warnings=notes,
                        face_count=len(current.topology.faces) if current else 0,
                    )
                )
                continue
            except FacetCADError as error:
                outcomes.append(
                    FeatureOutcome(
                        id=spec.id,
                        type=spec.type,
                        status=FeatureStatus.FAILED,
                        error=_contextualise(error, spec.id),
                        warnings=notes,
                    )
                )
                halted = True
                continue

            self._cache[slot] = _CacheEntry(key=key, solid=current)
            upstream_key = key
            outcomes.append(
                FeatureOutcome(
                    id=spec.id,
                    type=spec.type,
                    status=FeatureStatus.BUILT,
                    warnings=notes,
                    face_count=len(current.topology.faces),
                )
            )

        return BodyResult(
            id=body.id,
            solid=current,
            outcomes=tuple(outcomes),
            placement=placement,
        )

    # -- one feature -------------------------------------------------------

    def _build_one(
        self,
        spec,
        document: Document,
        parameters: ResolvedParameters,
        frames: Mapping[str, Frame],
        naming: NamingEngine,
        previous: NamedSolid | None,
        detail: str = Detail.DRAFT,
    ) -> NamedSolid:
        handler = handler_for(spec)
        context = BuildContext(
            parameters=parameters,
            frames=frames,
            sketches=document.sketches,
            kernel=self._kernel,
            previous=previous,
            detail=detail,
        )
        produced = handler.build(spec, context)
        builds = [produced] if isinstance(produced, FeatureBuild) else list(produced)
        if not builds:
            raise FeatureBuildError(feature=spec.id, reason="the handler produced no geometry")

        # A feature may take several kernel steps. Each is named in turn against
        # the previous *named* state, so inherited tags carry through the chain.
        current = previous
        for build in builds:
            current = naming.name(
                feature=spec.id,
                sketch=build.sketch,
                result=build.result,
                vocabulary=build.vocabulary,
                frame=build.frame,
                previous=current if build.consumes_previous else None,
            )
        assert current is not None
        return current

    # -- caching -----------------------------------------------------------

    def _cache_key(
        self,
        spec,
        document: Document,
        parameters: ResolvedParameters,
        frames: Mapping[str, Frame],
        upstream_key: str,
    ) -> str:
        payload: dict[str, object] = {
            "spec": spec.to_dict(),
            "upstream": upstream_key,
            "kernel": self._kernel.name,
        }

        needed = set(spec.parameter_dependencies())
        if spec.profile is not None:
            sketch = document.sketches.get(spec.profile.sketch)
            if sketch is not None:
                payload["sketch"] = sketch.to_dict()
                needed |= sketch.parameter_dependencies()
                frame = frames.get(sketch.plane)
                if frame is not None:
                    payload["frame"] = [
                        list(frame.origin.rounded().as_tuple()),
                        list(frame.x_axis.rounded().as_tuple()),
                        list(frame.z_axis.rounded().as_tuple()),
                    ]

        payload["parameters"] = {
            name: round(parameters[name], 9) for name in sorted(needed) if name in parameters
        }
        blob = json.dumps(payload, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()


def _reregister_frames(
    naming: NamingEngine, document: Document, spec, frames: Mapping[str, Frame]
) -> None:
    """Keep frame ownership correct when a feature comes from cache.

    Split ordering resolves fragments in the *owning* feature's frame, so that
    frame must be registered even on a cache hit — otherwise a cached pad
    followed by a rebuilt pocket would order fragments in the world frame and
    could produce different ordinals than a full rebuild.
    """
    if spec.profile is None:
        return
    sketch = document.sketches.get(spec.profile.sketch)
    if sketch is None:
        return
    frame = frames.get(sketch.plane)
    if frame is not None:
        naming.register_frame(spec.id, frame)


def _contextualise(error: FacetCADError, feature: str) -> FacetCADError:
    """Attach the failing feature to errors that did not already name one."""
    if isinstance(error, SelectorResolutionError) and error.feature is None:
        return replace(error, feature=feature)
    if isinstance(error, FeatureBuildError):
        return error
    return FeatureBuildError(feature=feature, reason=str(error), cause=error)


def recompute(
    document: Document, kernel: GeometryKernel, detail: str = Detail.DRAFT
) -> RecomputeResult:
    """Convenience for a one-shot rebuild with no cache reuse."""
    return RecomputeEngine(kernel).recompute(document, detail)


def dirty_features(document: Document, parameter: str) -> Sequence[str]:
    """Which features a parameter edit would rebuild."""
    return document.features_depending_on(parameter)
