"""Turning kernel provenance into stable names.

This is the seam described in :mod:`facet.application.ports.geometry`: the
kernel reports *how* a face came to exist, and this module decides *what it is
called*. Keeping the decision here — rather than in an adapter — is what lets
the analytic kernel and OCCT feed one naming engine.

Naming rules
------------

============================  ==================================
kernel provenance             tag
============================  ==================================
``SWEPT(curve)`` on a pad     ``feature/side[sketch.curve]``
``SWEPT(curve)`` on a pocket  ``feature/wall[sketch.curve]``
``CAP_*`` on a pad            ``feature/cap+`` or ``feature/cap-``
``CAP_END`` on a pocket       ``feature/floor``
``CAP_START`` on a pocket     ``feature/ceiling``
``INHERITED(parent)``         the parent's tag, carried forward
============================  ==================================

The vocabulary is supplied per feature type rather than switched on inside the
engine, so a new feature can introduce new roles without this module changing.

Cap sign is decided from the face's actual normal against the sketch plane
normal, not from the extrusion direction. Both give the same answer for a simple
pad, but only the normal-based rule stays correct for a midplane pad, where both
caps exist on opposite sides.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from facet.domain.errors import FeatureBuildError
from facet.domain.math3d import Frame
from facet.domain.splitting import Fragment, assign_ordinals
from facet.domain.tags import CornerTag, CurveRef, EdgeTag, FaceTag, Roles
from facet.domain.topology import (
    EdgeEntry,
    FaceEntry,
    RetiredTag,
    TopologyIndex,
)

from .ports.geometry import (
    EdgeRecord,
    FaceRecord,
    Origin,
    Ref,
    SolidHandle,
    SolidResult,
)


@dataclass(frozen=True)
class RoleVocabulary:
    """How one feature type names the faces its operation produces.

    Supplied by the feature handler, so adding a feature type never edits the
    naming engine (open/closed).
    """

    swept: str
    cap_start: str = ""
    cap_end: str = ""
    caps_by_normal: bool = False
    #: Role for a blend transition patch, which belongs to no single edge.
    corner: str = Roles.CORNER


#: Additive features name lateral faces "side" and caps by which side of the
#: sketch plane they fall on.
PAD_ROLES = RoleVocabulary(swept=Roles.SIDE, caps_by_normal=True)

#: Subtractive features name lateral faces "wall"; the far cap is the floor.
POCKET_ROLES = RoleVocabulary(
    swept=Roles.WALL, cap_start=Roles.CEILING, cap_end=Roles.FLOOR
)

#: Blends reuse ``swept`` for the blend face itself, named after its edge.
FILLET_ROLES = RoleVocabulary(swept=Roles.FILLET)
CHAMFER_ROLES = RoleVocabulary(swept=Roles.CHAMFER)


@dataclass(frozen=True)
class NamedSolid:
    """A kernel solid with every face named, plus the map back to kernel refs.

    ``refs`` is what allows the *next* operation to carry inherited tags
    forward: the kernel reports "this face came from ref f3", and this map says
    f3 was ``base/cap+``.
    """

    handle: SolidHandle
    topology: TopologyIndex
    refs: Mapping[Ref, FaceTag] = field(default_factory=dict)
    #: The reverse direction for edges, so a blend can act on what a selector
    #: resolved rather than re-finding it geometrically.
    edge_refs: Mapping[EdgeTag, Ref] = field(default_factory=dict)

    def tag_of(self, ref: Ref) -> FaceTag | None:
        return self.refs.get(ref)

    def ref_of_edge(self, tag: EdgeTag) -> Ref | None:
        return self.edge_refs.get(tag)

    @property
    def faces(self) -> tuple[FaceEntry, ...]:
        return self.topology.faces


EMPTY_NAMED_SOLID = NamedSolid(handle=SolidHandle(id=""), topology=TopologyIndex())


class NamingEngine:
    """Assigns tags to the faces of a kernel result.

    ``frames`` maps a feature id to the frame it was built on. It is used only
    for split ordering, and deliberately keyed by the *owning* feature: the
    fragments of ``base/cap+`` are ordered in ``base``'s frame no matter which
    later feature happened to split them, so the ordering cannot change just
    because a different feature did the cutting.
    """

    def __init__(self, frames: Mapping[str, Frame] | None = None) -> None:
        self._frames: dict[str, Frame] = dict(frames or {})

    def register_frame(self, feature: str, frame: Frame) -> None:
        self._frames[feature] = frame

    def frame_for(self, feature: str) -> Frame:
        return self._frames.get(feature, Frame.world())

    # -- entry point -------------------------------------------------------

    def name(
        self,
        *,
        feature: str,
        sketch: str,
        result: SolidResult,
        vocabulary: RoleVocabulary,
        frame: Frame,
        previous: NamedSolid | None = None,
    ) -> NamedSolid:
        """Name every face of ``result`` and build its topology index."""
        self.register_frame(feature, frame)

        base_tags: dict[Ref, FaceTag] = {}
        # Corners are named from their neighbours' names, so they wait for a
        # second pass rather than forcing an order on the first.
        corners = [r for r in result.faces if r.provenance.origin == Origin.BLEND_CORNER]
        for record in result.faces:
            if record.provenance.origin == Origin.BLEND_CORNER:
                continue
            base_tags[record.ref] = self._base_tag(
                feature=feature,
                sketch=sketch,
                record=record,
                vocabulary=vocabulary,
                frame=frame,
                previous=previous,
            )
        for record in corners:
            base_tags[record.ref] = self._corner_tag(feature, record, vocabulary, base_tags)

        refs = self._resolve_splits(base_tags, result.faces)
        faces = tuple(
            FaceEntry(tag=refs[record.ref], fingerprint=record.fingerprint)
            for record in result.faces
        )
        edges, edge_refs = self._name_edges(result.edges, refs, feature)
        retired = self._retire(result, previous, feature)

        return NamedSolid(
            handle=result.solid,
            topology=TopologyIndex.build(faces=faces, edges=edges, retired=retired),
            refs=refs,
            edge_refs=edge_refs,
        )

    # -- rule application --------------------------------------------------

    def _base_tag(
        self,
        *,
        feature: str,
        sketch: str,
        record: FaceRecord,
        vocabulary: RoleVocabulary,
        frame: Frame,
        previous: NamedSolid | None,
    ) -> FaceTag:
        provenance = record.provenance

        if provenance.origin == Origin.SWEPT:
            if not provenance.curve:
                raise FeatureBuildError(
                    feature=feature,
                    reason=(
                        f"the kernel reported face '{record.ref}' as swept but did not "
                        "say from which profile curve, so it cannot be named"
                    ),
                )
            return FaceTag(
                feature=feature,
                role=vocabulary.swept,
                source=CurveRef(sketch=sketch, curve=provenance.curve),
            )

        if provenance.origin in (Origin.CAP_START, Origin.CAP_END):
            return FaceTag(feature=feature, role=self._cap_role(record, vocabulary, frame))

        if provenance.origin == Origin.BLEND:
            return self._blend_tag(feature, record, vocabulary, previous)

        if provenance.origin == Origin.INHERITED:
            return self._inherited_tag(feature, record, previous)

        raise FeatureBuildError(
            feature=feature,
            reason=(
                f"the kernel could not attribute face '{record.ref}'. Every face must "
                "have provenance; an unattributable face has no stable name and would "
                "silently drift on the next rebuild."
            ),
        )

    def _cap_role(
        self, record: FaceRecord, vocabulary: RoleVocabulary, frame: Frame
    ) -> str:
        if vocabulary.caps_by_normal:
            alignment = record.fingerprint.normal.dot(frame.z_axis)
            return Roles.CAP_POS if alignment >= 0 else Roles.CAP_NEG
        return (
            vocabulary.cap_end
            if record.provenance.origin == Origin.CAP_END
            else vocabulary.cap_start
        )

    def _blend_tag(
        self,
        feature: str,
        record: FaceRecord,
        vocabulary: RoleVocabulary,
        previous: NamedSolid | None,
    ) -> FaceTag:
        """Name a fillet or chamfer after the edge it replaced.

        This is the payoff of deriving edge identity from face identity: a blend
        needs no new naming concept, because the edge it consumed already had a
        stable name made of its two adjacent faces.
        """
        parents = record.provenance.parents
        if previous is None or len(parents) != 2:
            raise FeatureBuildError(
                feature=feature,
                reason=(
                    f"face '{record.ref}' is a blend but the kernel did not report the "
                    "two faces whose edge it replaced, so it cannot be named"
                ),
            )
        first, second = (previous.tag_of(ref) for ref in parents)
        if first is None or second is None:
            raise FeatureBuildError(
                feature=feature,
                reason=(
                    f"face '{record.ref}' is a blend of unknown parent faces "
                    f"{parents}; the upstream naming state and the kernel disagree"
                ),
            )
        return FaceTag(
            feature=feature,
            role=vocabulary.swept,
            source=EdgeTag.of(first.without_ordinal(), second.without_ordinal()),
        )

    def _corner_tag(
        self,
        feature: str,
        record: FaceRecord,
        vocabulary: RoleVocabulary,
        named: Mapping[Ref, FaceTag],
    ) -> FaceTag:
        """Name a blend transition patch after the faces that bound it.

        The same construction as an edge, one arity up: an edge is two adjacent
        faces, a corner is the whole set. Nothing geometric is inferred — the
        kernel says which faces surround the patch, and those already have
        stable names.
        """
        parents = record.provenance.parents
        # Ordinals are dropped as they are for edges, so two fragments of one
        # logical face count once — hence dedupe before checking the arity.
        bounding: list[FaceTag] = []
        for ref in parents:
            tag = named.get(ref)
            if tag is None:
                continue
            base = tag.without_ordinal()
            if base not in bounding:
                bounding.append(base)
        if len(bounding) < 3:
            raise FeatureBuildError(
                feature=feature,
                reason=(
                    f"face '{record.ref}' is a blend corner but only "
                    f"{len(bounding)} of its bounding faces could be named, so it has "
                    "no stable identity. Widen the selector so the whole run of edges "
                    "meeting at that corner is blended."
                ),
            )
        return FaceTag(
            feature=feature,
            role=vocabulary.corner,
            source=CornerTag(faces=tuple(bounding)),
        )

    def _inherited_tag(
        self, feature: str, record: FaceRecord, previous: NamedSolid | None
    ) -> FaceTag:
        if previous is None:
            raise FeatureBuildError(
                feature=feature,
                reason=(
                    f"face '{record.ref}' claims to be inherited but this feature has "
                    "no upstream solid"
                ),
            )
        parent = record.provenance.parent
        inherited = previous.tag_of(parent) if parent else None
        if inherited is None:
            raise FeatureBuildError(
                feature=feature,
                reason=(
                    f"face '{record.ref}' is inherited from unknown parent ref "
                    f"'{parent}'. The upstream naming state and the kernel result "
                    "disagree, which would break every selector below this feature."
                ),
            )
        # Ordinals are re-derived from the new geometry, never carried forward:
        # a fragment that is alone again must lose its suffix.
        return inherited.without_ordinal()

    # -- splits ------------------------------------------------------------

    def _resolve_splits(
        self, base_tags: Mapping[Ref, FaceTag], records: Sequence[FaceRecord]
    ) -> dict[Ref, FaceTag]:
        """Assign ``#n`` suffixes to any tag claimed by more than one face."""
        grouped: dict[FaceTag, list[FaceRecord]] = {}
        for record in records:
            grouped.setdefault(base_tags[record.ref], []).append(record)

        resolved: dict[Ref, FaceTag] = {}
        for tag, members in grouped.items():
            if len(members) == 1:
                resolved[members[0].ref] = tag
                continue
            frame = self.frame_for(tag.feature)
            fragments = [
                Fragment(
                    centroid=frame.to_local(record.fingerprint.centroid),
                    payload=record.ref,
                )
                for record in members
            ]
            for assigned, ref in assign_ordinals(tag, fragments):
                resolved[ref] = assigned
        return resolved

    # -- edges and retirement ---------------------------------------------

    def _name_edges(
        self, records: Sequence[EdgeRecord], refs: Mapping[Ref, FaceTag], feature: str
    ) -> tuple[list[EdgeEntry], dict[EdgeTag, Ref]]:
        entries: list[EdgeEntry] = []
        edge_refs: dict[EdgeTag, Ref] = {}
        for record in records:
            first, second = record.faces
            tag_a, tag_b = refs.get(first), refs.get(second)
            if tag_a is None or tag_b is None or tag_a == tag_b:
                # A self-adjacent edge (a seam) carries no two-face identity, so
                # it is not addressable and is skipped rather than mis-named.
                continue
            tag = EdgeTag.of(tag_a, tag_b)
            entries.append(EdgeEntry(tag=tag, fingerprint=record.fingerprint))
            edge_refs.setdefault(tag, record.ref)
        return entries, edge_refs

    def _retire(
        self, result: SolidResult, previous: NamedSolid | None, feature: str
    ) -> list[RetiredTag]:
        if previous is None:
            return []
        retired: list[RetiredTag] = []
        for deleted in result.deleted:
            tag = previous.tag_of(deleted.ref)
            if tag is not None:
                retired.append(
                    RetiredTag(tag=tag, reason=deleted.reason, retired_by=feature)
                )
        # Tags that simply stopped appearing are retired too, so a selector that
        # loses its target can always say which feature was responsible.
        carried = {tag.without_ordinal() for tag in previous.refs.values()}
        present = {entry.tag.without_ordinal() for entry in _entries_of(result, previous)}
        for tag in sorted(carried - present, key=lambda t: t.sort_key):
            if any(r.tag.without_ordinal() == tag for r in retired):
                continue
            retired.append(RetiredTag(tag=tag, reason="consumed", retired_by=feature))
        return retired


def _entries_of(result: SolidResult, previous: NamedSolid) -> list[FaceEntry]:
    """Which previously-named tags survived into ``result``."""
    survivors: list[FaceEntry] = []
    for record in result.faces:
        if record.provenance.origin != Origin.INHERITED:
            continue
        parent = record.provenance.parent
        tag = previous.tag_of(parent) if parent else None
        if tag is not None:
            survivors.append(FaceEntry(tag=tag, fingerprint=record.fingerprint))
    return survivors
