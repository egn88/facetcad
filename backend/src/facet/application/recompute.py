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
import pickle
import re
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from facet.domain.body import Body
from facet.domain.document import Document
from facet.domain.errors import (
    DocumentError,
    FacetCADError,
    FeatureBuildError,
    SelectorResolutionError,
    UnknownReferenceError,
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
from .ports.snapshots import SnapshotStore


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
            # Rounded: this is for spotting the one slow feature in a long
            # history, and microseconds are noise at that job.
            "durationMs": round(self.duration_ms, 1),
        }


@dataclass(frozen=True)
class BodyResult:
    """One body's rebuilt solid, in its own coordinates.

    ``placement`` is where the body sits, and is applied for display and export
    only. Keeping it out of the modelled geometry means moving a body can never
    perturb a face fingerprint or a split ordinal.

    A **copy** is this same record holding the source's ``solid`` and ``key``
    under its own id and its own placement. Nothing downstream has to know:
    the viewport draws it, the exporter places it, and the mesh cache hits on
    the shared key, all through the path they already took.
    """

    id: str
    solid: NamedSolid | None = None
    outcomes: tuple[FeatureOutcome, ...] = ()
    placement: Frame = field(default_factory=Frame.world)
    error: FacetCADError | None = None
    #: The body this one copies; None for a body built from its own history.
    of: str | None = None
    #: Ids of the bodies copying this one. Empty on a copy — a copy is counted
    #: by its source, so the quantities across a document sum to the piece
    #: count instead of double-counting it.
    copies: tuple[str, ...] = ()
    #: The content key of the state ``solid`` is actually in — the key of the
    #: deepest feature that built, which on a history that stopped early is the
    #: last good one rather than the last declared one.
    #:
    #: Exposed because everything derived from a solid is a pure function of it:
    #: this is what lets a mesh be cached against the geometry rather than
    #: against a kernel handle, which is reissued on every restore and so could
    #: never be hit twice.
    key: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and all(
            o.ok or o.status == FeatureStatus.SUPPRESSED for o in self.outcomes
        )

    @property
    def is_copy(self) -> bool:
        return self.of is not None

    @property
    def quantity(self) -> int:
        """How many pieces of this body the model calls for.

        The number to send to a printer. 0 on a copy, which its source counts.
        """
        return 0 if self.is_copy else 1 + len(self.copies)

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
            "of": self.of,
            "copies": list(self.copies),
            "quantity": self.quantity,
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

    def topologies(self) -> tuple[tuple[str, TopologyIndex], ...]:
        """Every body's named geometry, in document order.

        The honest counterpart to :attr:`topology`, which reads through to the
        first body and so answers for a two-part document as though the second
        part had no faces. Anything asking "what is named in this model" wants
        this one; ``topology`` remains for the callers that predate bodies and
        mean the first.

        Copies are left out. A copy introduces no name — it shows the source's
        faces at another placement, so listing it would report every tag once
        per copy and turn "this face is on four bodies" into the answer to a
        question nobody asked. Selectors are written against a history, and the
        history is the source's.
        """
        return tuple(
            (body.id, body.topology) for body in self.bodies if not body.is_copy
        )

    @property
    def parts(self) -> tuple[tuple[str, int], ...]:
        """Each distinct part and how many of it the model calls for.

        The piece count for a print run: bodies that build themselves, each with
        the number of times it appears. Copies do not get their own row — they
        are the count on the row of the body they copy.
        """
        return tuple(
            (body.id, body.quantity) for body in self.bodies if not body.is_copy
        )

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
            "parts": [{"body": name, "quantity": n} for name, n in self.parts],
            "lastGoodFeature": self.last_good_feature,
            "error": self.error.as_dict() if self.error else None,
        }


@dataclass
class _CacheEntry:
    key: str
    solid: NamedSolid


#: Bumped when the *meaning* of anything inside a snapshot changes — a tag's
#: spelling, a role name, how refs are ordered — so old entries are ignored
#: rather than trusted. It goes into the key, so a bump orphans every existing
#: file and eviction reclaims them.
#:
#: This is a manual step and therefore a place to be wrong. What stops that
#: being silent is `tests/stress/test_kernel_baseline.py`: a change that alters a
#: name fails there first, which is the prompt to come and bump this.
SNAPSHOT_FORMAT = 1

#: How much rebuild time has to pile up before the state after it is worth
#: storing, in milliseconds.
#:
#: Only the finished body used to be kept, which is right for the common edit —
#: appending a feature resumes from the state before it — and useless for the
#: one that hurts: change something in the middle of a long history in a fresh
#: process and the only stored state is the one the edit just invalidated, so it
#: rebuilds from the first feature.
#:
#: The obvious fix is a snapshot per feature, and the measurements say no. On the
#: document this was tuned against an average feature rebuilds in ~10ms and
#: restores in ~8ms, so storing every one would cost more to write than it could
#: ever save — half a second of serialisation and several megabytes, to avoid
#: work that was never expensive. What is worth storing is the state after
#: something slow: a modelled thread or a fillet over a big face costs seconds,
#: and a restore costs the same ~8ms it always does.
#:
#: So the threshold is in units of the thing being avoided. Measured on the
#: tapped-cavity document, whose modelled thread costs about a second: editing
#: the hole *after* the thread, in a fresh process, went from 530ms to 137ms,
#: for one extra 200kB entry and 1-2% on the build that wrote it.
#:
#: Measured on the fourteen-body document at the same setting: nothing at all.
#: No feature there reaches the threshold, so no checkpoint is written, the
#: store stays the same size and the build takes the same time. That is the
#: intended answer, not a disappointing one — dropping the threshold to zero on
#: that document made a cold edit *slower*, 452ms against 535ms, because
#: restoring a checkpoint two cheap features up costs more than rebuilding them.
CHECKPOINT_MS = 250.0


@dataclass(frozen=True)
class _Snapshot:
    """A built body, in a form that outlives the process that built it.

    ``geometry`` is the kernel's own bytes. ``solid`` is the naming state, which
    is ordinary domain data and pickles as itself — the handle inside it is
    stale by definition and is replaced on restore.
    """

    format: int
    kernel: str
    key: str
    geometry: bytes
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

    def __init__(
        self,
        kernel: GeometryKernel,
        snapshots: SnapshotStore | None = None,
        *,
        checkpoint_ms: float = CHECKPOINT_MS,
    ) -> None:
        self._kernel = kernel
        # See CHECKPOINT_MS. Injected so a test can ask for a checkpoint after
        # every feature without needing a feature that takes a quarter second.
        self._checkpoint_ms = checkpoint_ms
        # Where built geometry goes so the *next* process does not rebuild it.
        # Optional, and every failure path through it is a miss: the in-memory
        # cache below is the fast path within a session, and this one exists
        # because a session starts cold. Measured on a 35-feature document: a
        # warm rebuild is 2.5ms, a cold one 2.5s, and a restart threw the
        # difference away.
        self._snapshots = snapshots
        self._cache: dict[str, _CacheEntry] = {}
        # One rebuild of a project at a time.
        #
        # The frontend asks for /bodies and /topologies together, and both need
        # the same rebuild. Without this they both miss a cold cache and both do
        # the whole thing, interleaved through the kernel — on a 35-feature
        # document that turned an 11s rebuild into 26s and a 502. Serialised, the
        # second waits and then finds the answer already cached.
        #
        # It also protects the cache dict itself, which two rebuilds were
        # mutating at once.
        self._lock = threading.RLock()

    @property
    def kernel(self) -> GeometryKernel:
        return self._kernel

    def invalidate(self, feature: str | None = None) -> None:
        with self._lock:
            if feature is None:
                self._cache.clear()
            else:
                self._cache.pop(feature, None)

    # -- the rebuild -------------------------------------------------------

    def recompute(self, document: Document, detail: str = Detail.DRAFT) -> RecomputeResult:
        with self._lock:
            return self._recompute(document, detail)

    def _recompute(self, document: Document, detail: str = Detail.DRAFT) -> RecomputeResult:
        try:
            document.validate()
            parameters = document.parameters.resolve()
            frames = document.datums.resolve_all(parameters)
        except FacetCADError as error:
            # A document that cannot even resolve its parameters has no partial
            # state worth showing, so this is the one whole-document failure.
            return RecomputeResult(error=error)

        # Sources first, then copies, so every copy has a built solid to point
        # at regardless of the order the two were written in. Document order is
        # restored afterwards: it is what the tree and the exporters show.
        built: dict[str, BodyResult] = {}
        for body in document.bodies:
            if body.is_copy:
                continue
            placement = self._placement_of(body, parameters)
            built[body.id] = (
                BodyResult(id=body.id, error=placement)
                if isinstance(placement, FacetCADError)
                else self._recompute_body(
                    body, document, parameters, frames, placement, detail
                )
            )

        for body in document.bodies:
            if not body.is_copy:
                continue
            placement = self._placement_of(body, parameters)
            if isinstance(placement, FacetCADError):
                built[body.id] = BodyResult(id=body.id, of=body.of, error=placement)
                continue
            built[body.id] = _as_copy(body, built.get(body.of or ""), placement)

        # Each source learns who copies it, so one read of a body answers "how
        # many of these do I make" without the caller rescanning the document.
        copies: dict[str, list[str]] = {}
        for body in document.bodies:
            if body.is_copy and body.of in built:
                copies.setdefault(body.of or "", []).append(body.id)

        bodies = [
            replace(built[body.id], copies=tuple(copies.get(body.id, ())))
            if not body.is_copy
            else built[body.id]
            for body in document.bodies
            if body.id in built
        ]

        return RecomputeResult(
            bodies=tuple(bodies), parameters=parameters, frames=frames
        )

    def _placement_of(
        self, body: Body, parameters: ResolvedParameters
    ) -> Frame | FacetCADError:
        """Where a body sits, or why that could not be worked out."""
        try:
            return body.placement.resolve(parameters, body.id)
        except FacetCADError as error:
            return error

    def _recompute_body(
        self,
        body: Body,
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
        #: Rebuild time accumulated since the last state was written to the
        #: store, which is what decides the next checkpoint.
        unsaved = 0.0

        # The key chain costs no geometry, so it can be worked out for the whole
        # history up front — which is what lets a state of the *last* feature
        # be found without building the thirty-four before it. Walking the
        # features first and looking for the cached one last would never reach
        # it: the first miss rebuilds, and after that every key still matches
        # but the work is already done.
        keys = self._key_chain(body, document, parameters, frames)
        resumed = self._resume(body, detail, keys, naming, document, frames, outcomes)
        if resumed is not None:
            start, current = resumed
            upstream_key = keys[start] or ""
        else:
            start = -1

        for index, spec in enumerate(body.features):
            if index <= start:
                continue  # accounted for by the restored snapshot
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

            started = time.perf_counter()
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
                        duration_ms=(time.perf_counter() - started) * 1000,
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
                        error=_contextualise(error, spec.id, document, body.id),
                        warnings=notes,
                        duration_ms=(time.perf_counter() - started) * 1000,
                    )
                )
                halted = True
                continue

            elapsed = (time.perf_counter() - started) * 1000
            self._cache[slot] = _CacheEntry(key=key, solid=current)
            upstream_key = key

            # Enough work has piled up behind this state to be worth keeping, so
            # an edit above it in a later process resumes here instead of at the
            # first feature. Deliberately measured in time rather than in
            # features: see CHECKPOINT_MS.
            unsaved += elapsed
            if unsaved >= self._checkpoint_ms:
                self._store(body.id, detail, key, current)
                unsaved = 0.0

            outcomes.append(
                FeatureOutcome(
                    id=spec.id,
                    type=spec.type,
                    status=FeatureStatus.BUILT,
                    warnings=notes,
                    duration_ms=elapsed,
                    face_count=len(current.topology.faces),
                )
            )

        self._keep(body, detail, keys, current, outcomes)
        return BodyResult(
            id=body.id,
            solid=current,
            outcomes=tuple(outcomes),
            placement=placement,
            key=upstream_key or None,
        )

    # -- snapshots ---------------------------------------------------------

    def _key_chain(
        self,
        body: Body,
        document: Document,
        parameters: ResolvedParameters,
        frames: Mapping[str, Frame],
    ) -> list[str | None]:
        """Every feature's cache key, in order, without building anything.

        ``None`` where a feature is suppressed — a suppressed feature does not
        advance the chain, because the state after it is the state before it.
        """
        keys: list[str | None] = []
        upstream = ""
        for spec in body.features:
            if spec.suppressed:
                keys.append(None)
                continue
            upstream = self._cache_key(spec, document, parameters, frames, upstream)
            keys.append(upstream)
        return keys

    def stored(self, document: Document, detail: str = Detail.DRAFT) -> bool:
        """Whether every body's finished state is already in the store.

        Answered without building anything: resolving the parameters and hashing
        the history is all it takes to know what a rebuild would produce, which
        is what a content-addressed cache is *for*. The cost is one existence
        probe per body.

        The caller is whoever is about to spend real money — starting a second
        OpenCascade process to warm export geometry, most of all. Reading a
        project scheduled one of those every time, and on a two-core server that
        second process competes with the request the user is waiting on.

        False when the document cannot even resolve, because then nothing is
        known rather than nothing is needed.
        """
        if self._snapshots is None:
            return False
        try:
            parameters = document.parameters.resolve()
            frames = document.datums.resolve_all(parameters)
        except FacetCADError:
            return False

        for body in document.bodies:
            if body.is_copy:
                # Nothing of its own to store: it is the source's state, and
                # the source is checked on its own pass through this loop.
                continue
            keys = self._key_chain(body, document, parameters, frames)
            final = next((key for key in reversed(keys) if key is not None), None)
            if final is None:
                # Nothing to build, so nothing can be missing.
                continue
            if not self._already_stored(self._snapshot_key(body.id, detail, final)):
                return False
        return True

    def _already_stored(self, key: str) -> bool:
        """Whether the store holds this, for a caller deciding whether to work.

        A store that cannot say answers no, and the caller does the work — the
        same degradation every other path through the store takes, and the
        reason a broken cache is a slow server rather than a failed one.
        """
        if self._snapshots is None:
            return False
        try:
            return self._snapshots.has(key)
        except Exception:
            return False

    def _snapshot_key(self, body_id: str, detail: str, key: str) -> str:
        blob = f"{SNAPSHOT_FORMAT}/{self._kernel.name}/{detail}/{body_id}/{key}"
        return hashlib.sha256(blob.encode()).hexdigest()

    def _resume(
        self,
        body: Body,
        detail: str,
        keys: Sequence[str | None],
        naming: NamingEngine,
        document: Document,
        frames: Mapping[str, Frame],
        outcomes: list[FeatureOutcome],
    ) -> tuple[int, NamedSolid] | None:
        """Start from the deepest state already available, wherever it lives.

        Searched from the end backwards, so the longest usable prefix wins: on
        opening a document that is the whole history, and after appending a
        feature it is everything but the new one.

        At each depth the in-process cache is asked *before* the snapshot store,
        and that ordering is the whole point of this method. A restore is not
        free — it reads a file, sends the bytes to the kernel, and has the kernel
        re-derive and re-check every face's fingerprint before it will trust the
        names — while a solid this process already holds costs a dict lookup.
        Reaching for the store first meant a warm server paid the cold price on
        every request: fourteen bodies restored and re-tessellated to answer a
        question it had already answered, 300ms against 55ms on the document that
        prompted this.

        The search is still by depth rather than by source, because a *deeper*
        stored state beats a shallower remembered one — after an edit early in a
        long history, the snapshot of the state just before it saves far more
        than the memory of the state before that.

        Appends ``outcomes`` for the features the resumed state accounts for.
        They report ``CACHED`` because nothing was recomputed, and a face count
        only where this process happens to hold that feature's own state: a
        restored solid is the sum of its history, with nothing per-feature left
        to count.
        """
        for index in range(len(keys) - 1, -1, -1):
            key = keys[index]
            if key is None:
                continue

            slot = f"{detail}/{body.id}/{body.features[index].id}"
            remembered = self._cache.get(slot)
            if remembered is not None and remembered.key == key:
                resumed = remembered.solid
            elif self._snapshots is None:
                continue
            else:
                loaded = self._load(body.id, detail, key)
                if loaded is None:
                    continue
                # Seeded so the next rebuild in this process finds it here and
                # does not go back to the store.
                self._cache[slot] = _CacheEntry(key=key, solid=loaded)
                resumed = loaded

            # Frames must be registered for the features that were *not* run.
            # Split ordering resolves fragments in the owning feature's frame, so
            # a later feature splitting a face that belongs to a skipped one
            # would otherwise order its fragments in the world frame and pick
            # different ordinals than a full rebuild.
            for position, spec in enumerate(body.features[: index + 1]):
                _reregister_frames(naming, document, spec, frames)
                held = self._cache.get(f"{detail}/{body.id}/{spec.id}")
                known = (
                    held.solid
                    if held is not None and held.key == keys[position]
                    else None
                )
                outcomes.append(
                    FeatureOutcome(
                        id=spec.id,
                        type=spec.type,
                        status=(
                            FeatureStatus.SUPPRESSED
                            if spec.suppressed
                            else FeatureStatus.CACHED
                        ),
                        face_count=len(known.topology.faces) if known else 0,
                    )
                )
            return index, resumed
        return None

    def _load(self, body_id: str, detail: str, key: str) -> NamedSolid | None:
        """A stored solid for this exact state, or None for any reason at all.

        Every failure here is a miss: a missing file, a truncated one, a pickle
        from an older layout, a kernel that cannot restore, refs that came back
        different. Rebuilding is always correct, so nothing about a snapshot is
        worth raising over — and a snapshot that cannot prove itself is exactly
        the one not to trust.
        """
        assert self._snapshots is not None
        blob = self._snapshots.load(self._snapshot_key(body_id, detail, key))
        if blob is None:
            return None
        try:
            stored = pickle.loads(blob)
            if (
                not isinstance(stored, _Snapshot)
                or stored.format != SNAPSHOT_FORMAT
                or stored.kernel != self._kernel.name
                or stored.key != key
            ):
                return None
            restore = getattr(self._kernel, "restore", None)
            if restore is None:
                return None
            result = restore(stored.geometry)
        except Exception:
            return None

        # The names were stored against refs; if the kernel handed back a
        # different set, they no longer describe this solid and the whole entry
        # is worthless. Checked rather than assumed, because the failure would be
        # a selector quietly pointing at the wrong face.
        if {record.ref for record in result.faces} != set(stored.solid.refs):
            return None
        return replace(stored.solid, handle=result.solid)

    def _keep(
        self,
        body: Body,
        detail: str,
        keys: Sequence[str | None],
        current: NamedSolid | None,
        outcomes: Sequence[FeatureOutcome],
    ) -> None:
        """Store the finished body, so the next process starts warm.

        Only the final state, and only when every feature is accounted for. A
        history that stopped early would be stored under the key of a state it
        never reached. States *within* a history are kept too, but only where
        they were expensive enough to be worth it — see CHECKPOINT_MS.
        """
        if current is None:
            return
        if any(o.status in (FeatureStatus.FAILED, FeatureStatus.SKIPPED) for o in outcomes):
            return
        final = next((key for key in reversed(keys) if key is not None), None)
        if final is None:
            return
        self._store(body.id, detail, final, current)

    def _store(self, body_id: str, detail: str, key: str, solid: NamedSolid) -> None:
        """Write one state to the store, if it is not already there.

        Every failure is silent. The rebuild has already succeeded and the caller
        has its answer; the only cost of not storing is a colder start later.
        """
        if self._snapshots is None:
            return

        # Already there, and the key says the bytes would be identical. Asked
        # rather than assumed from the outcomes, so a store that was added since
        # the last run — or that quietly failed a write — still gets filled.
        # Worth the question: without it a warm rebuild re-serialised every body
        # through the kernel and rewrote half a megabyte on every request, which
        # was 33ms of the 73 a warm rebuild had left.
        stored_at = self._snapshot_key(body_id, detail, key)
        if self._already_stored(stored_at):
            return

        take = getattr(self._kernel, "snapshot", None)
        if take is None:
            return
        try:
            geometry = take(solid.handle)
            blob = pickle.dumps(
                _Snapshot(
                    format=SNAPSHOT_FORMAT,
                    kernel=self._kernel.name,
                    key=key,
                    geometry=geometry,
                    solid=solid,
                ),
                protocol=pickle.HIGHEST_PROTOCOL,
            )
        except Exception:
            # A kernel that cannot serialise this solid, or a solid holding
            # something that will not pickle.
            return
        self._snapshots.save(stored_at, blob)

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


def _as_copy(body: Body, source: BodyResult | None, placement: Frame) -> BodyResult:
    """One body's solid, appearing again under another id and placement.

    No kernel call and no cache slot: the solid, its names and its content key
    are the source's, and only the transform differs. The shared key is what
    makes the tessellation free too — the mesh cache is keyed on the geometry,
    so the copy asks for triangles that have already been cut.

    A source whose history did not finish is reported as such on the copy too,
    pointing at the source rather than restating its feature outcomes. The copy
    still shows the partial solid — the same fail-loud-but-locally behaviour the
    source itself gets — but it does not claim to be fine when the thing it
    copies is not.
    """
    if source is None:
        # Validation rejects a copy of a body that is not there, so reaching
        # this means the document was built past it. Say so rather than draw
        # an empty body with no explanation.
        return BodyResult(
            id=body.id,
            of=body.of,
            placement=placement,
            error=UnknownReferenceError(
                kind="body", identifier=body.of or "", referenced_by=body.id
            ),
        )
    return BodyResult(
        id=body.id,
        solid=source.solid,
        of=body.of,
        placement=placement,
        key=source.key,
        error=source.error or _source_unfinished(body, source),
    )


def _source_unfinished(body: Body, source: BodyResult) -> DocumentError | None:
    """Why a copy of a half-built body is not itself ok."""
    if source.ok:
        return None
    return DocumentError(
        reason=(
            f"body {body.id!r} copies {source.id!r}, whose history did not finish "
            "building. Fix it there and every copy is fixed with it."
        ),
        path=f"bodies.{body.id}.of",
    )


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


#: The feature part of a tag, which is everything before the first ``/``.
#: Matched rather than parsed because the text may be a face selector, an edge
#: selector or a union of them, and all that is wanted from it is which
#: features it names.
_TAG_FEATURE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*/")


def _contextualise(
    error: FacetCADError,
    feature: str,
    document: Document | None = None,
    body: str | None = None,
) -> FacetCADError:
    """Attach the failing feature to errors that did not already name one."""
    if isinstance(error, SelectorResolutionError):
        if error.feature is None:
            error = replace(error, feature=feature)
        if document is not None and error.actual == 0:
            elsewhere = _named_in_other_bodies(document, error.selector, body)
            if elsewhere:
                error = replace(error, reasons=(*error.reasons, _across_bodies(elsewhere)))
        return error
    if isinstance(error, FeatureBuildError):
        return error
    return FeatureBuildError(feature=feature, reason=str(error), cause=error)


def _named_in_other_bodies(
    document: Document, selector: str, body: str | None
) -> dict[str, list[str]]:
    """Features the selector names that belong to some other body.

    Read off the document rather than off geometry, because at the point this
    is asked the other body may not have been built — and because a feature id
    is unique across the whole document, so which body owns it is a fact the
    document already holds.
    """
    named = set(_TAG_FEATURE.findall(selector))
    if not named:
        return {}
    return {
        other.id: [spec.id for spec in other.features if spec.id in named]
        for other in document.bodies
        if other.id != body and any(spec.id in named for spec in other.features)
    }


def _across_bodies(elsewhere: Mapping[str, Sequence[str]]) -> str:
    """Why a selector naming a real face still resolved to nothing.

    This is the one failure whose message would otherwise send the reader to
    fix something that is not wrong: the tag is spelled correctly, the face
    exists, and it is simply in another part. Bodies never see each other's
    solids, so a feature can only name faces its own body made.
    """
    parts = [
        f"{', '.join(repr(name) for name in sorted(features))} in body {body!r}"
        for body, features in sorted(elsewhere.items())
    ]
    return (
        f"the selector names {'; '.join(parts)}, and a feature resolves only within "
        "its own body — bodies never see each other's solids. Move the feature into "
        "that body, or select a face this one made."
    )


def recompute(
    document: Document, kernel: GeometryKernel, detail: str = Detail.DRAFT
) -> RecomputeResult:
    """Convenience for a one-shot rebuild with no cache reuse."""
    return RecomputeEngine(kernel).recompute(document, detail)


def dirty_features(document: Document, parameter: str) -> Sequence[str]:
    """Which features a parameter edit would rebuild."""
    return document.features_depending_on(parameter)
