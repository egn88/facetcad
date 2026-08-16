"""The naming-level view of a solid.

A :class:`TopologyIndex` is what the rest of the system sees instead of a kernel
shape: named faces, derived edges, and — importantly — a record of tags that
*used to exist* along with what destroyed them.

That retirement record is what turns an unhelpful "selector matched nothing"
into "the face you referenced was consumed by feature `slot_1`". Carrying it is
cheap and it is most of the difference between a diagnostic a user can act on
and one they cannot.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from .fingerprint import EdgeFingerprint, FaceFingerprint
from .tags import EdgeTag, FaceTag


@dataclass(frozen=True, slots=True)
class FaceEntry:
    tag: FaceTag
    fingerprint: FaceFingerprint

    def to_dict(self) -> dict[str, object]:
        return {"tag": str(self.tag), "fingerprint": self.fingerprint.to_dict()}


@dataclass(frozen=True, slots=True)
class EdgeEntry:
    tag: EdgeTag
    fingerprint: EdgeFingerprint

    def to_dict(self) -> dict[str, object]:
        return {"tag": str(self.tag), "fingerprint": self.fingerprint.to_dict()}


@dataclass(frozen=True, slots=True)
class RetiredTag:
    """A face that existed in an earlier state and no longer does."""

    tag: FaceTag
    reason: str
    retired_by: str | None = None

    def describe(self) -> str:
        by = f" by feature '{self.retired_by}'" if self.retired_by else ""
        return f"face '{self.tag}' was {self.reason}{by}"

    def to_dict(self) -> dict[str, object]:
        return {"tag": str(self.tag), "reason": self.reason, "retired_by": self.retired_by}


@dataclass(frozen=True)
class TopologyIndex:
    """Named faces and derived edges of one solid state."""

    faces: tuple[FaceEntry, ...] = ()
    edges: tuple[EdgeEntry, ...] = ()
    retired: tuple[RetiredTag, ...] = ()
    _by_tag: dict[FaceTag, FaceEntry] = field(init=False, repr=False, compare=False)
    _retired_by_tag: dict[FaceTag, RetiredTag] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_tag", {entry.tag: entry for entry in self.faces})
        object.__setattr__(
            self, "_retired_by_tag", {entry.tag: entry for entry in self.retired}
        )

    # -- lookup ------------------------------------------------------------

    def face(self, tag: FaceTag) -> FaceEntry | None:
        return self._by_tag.get(tag)

    def has_face(self, tag: FaceTag) -> bool:
        return tag in self._by_tag

    def retirement_of(self, tag: FaceTag) -> RetiredTag | None:
        """Why a tag is gone — checked with and without its split ordinal.

        A face referenced as ``base/cap+`` may have been retired as
        ``base/cap+#1``; the user still deserves to be told what happened.
        """
        direct = self._retired_by_tag.get(tag)
        if direct is not None:
            return direct
        stripped = tag.without_ordinal()
        for retired in self.retired:
            if retired.tag.without_ordinal() == stripped:
                return retired
        return None

    def face_tags(self) -> tuple[FaceTag, ...]:
        return tuple(entry.tag for entry in self.faces)

    def edge_tags(self) -> tuple[EdgeTag, ...]:
        return tuple(entry.tag for entry in self.edges)

    def edges_touching(self, tag: FaceTag) -> tuple[EdgeEntry, ...]:
        return tuple(entry for entry in self.edges if entry.tag.contains(tag))

    def fragments_of(self, tag: FaceTag) -> tuple[FaceEntry, ...]:
        """Every face sharing ``tag``'s identity ignoring the split ordinal."""
        stripped = tag.without_ordinal()
        return tuple(e for e in self.faces if e.tag.without_ordinal() == stripped)

    def __len__(self) -> int:
        return len(self.faces)

    def __iter__(self) -> Iterator[FaceEntry]:
        return iter(self.faces)

    # -- construction ------------------------------------------------------

    @staticmethod
    def build(
        faces: Iterable[FaceEntry],
        edges: Iterable[EdgeEntry] = (),
        retired: Iterable[RetiredTag] = (),
    ) -> TopologyIndex:
        return TopologyIndex(
            faces=tuple(sorted(faces, key=lambda e: e.tag.sort_key)),
            edges=tuple(sorted(edges, key=lambda e: str(e.tag))),
            retired=tuple(retired),
        )

    def with_retired(self, extra: Iterable[RetiredTag]) -> TopologyIndex:
        return TopologyIndex.build(self.faces, self.edges, (*self.retired, *extra))

    def to_dict(self) -> dict[str, object]:
        return {
            "faces": [f.to_dict() for f in self.faces],
            "edges": [e.to_dict() for e in self.edges],
            "retired": [r.to_dict() for r in self.retired],
        }


EMPTY_TOPOLOGY = TopologyIndex()
