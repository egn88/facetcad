"""Selectors: queries the document stores instead of picked geometry.

A selector is re-evaluated on every rebuild. It never stores "face 6"; it stores
what the user meant, and if that meaning no longer resolves cleanly the rebuild
stops and says why.

Resolution order
----------------

1. **Exact tag** — the fast, normal path.
2. **Tag pattern** — wildcards, e.g. ``base/side[*]`` for every side face.
3. **Fingerprint** — when provenance leaves several candidates, the stored
   fingerprint from the last good build picks the intended one.
4. **Geometric filter** — direction constraints, applied last and only as a
   narrowing step, never as the sole identity.

If the outcome is ambiguous or the cardinality changed,
:class:`SelectorResolutionError` is raised. It is never resolved by picking the
first match — that behaviour is precisely the bug this project exists to remove.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from .errors import SelectorResolutionError, TagSyntaxError
from .fingerprint import DEFAULT_NORMAL_TOL, FaceFingerprint
from .math3d import Vec3
from .tags import EdgeTag, FaceTag
from .topology import EdgeEntry, FaceEntry, TopologyIndex

# --------------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------------

_WILDCARD = "*"
_PATTERN_RE = re.compile(
    r"^\s*(?P<feature>\*|[A-Za-z_][A-Za-z0-9_]*)"
    r"\s*/\s*(?P<role>\*|[A-Za-z_][A-Za-z0-9_]*[+-]?)"
    r"\s*(?:\[(?P<source>.*)\])?"
    r"\s*(?:\#(?P<ordinal>\*|\d+))?\s*$"
)


@dataclass(frozen=True, slots=True)
class TagPattern:
    """A face-tag matcher. ``None`` in any field means "any"."""

    feature: str | None = None
    role: str | None = None
    source: str | None = None
    ordinal: int | None = None
    any_ordinal: bool = True

    @staticmethod
    def parse(text: str) -> TagPattern:
        """Parse the shorthand, e.g. ``base/side[*]``, ``*/cap+``, ``slot/floor``."""
        match = _PATTERN_RE.match(text)
        if match is None:
            raise TagSyntaxError(text=text, reason="expected <feature>/<role>[<source>]#<n>")
        raw_ordinal = match.group("ordinal")
        raw_source = match.group("source")
        return TagPattern(
            feature=_none_if_wildcard(match.group("feature")),
            role=_none_if_wildcard(match.group("role")),
            source=None if raw_source is None or raw_source.strip() == _WILDCARD
            else raw_source.strip(),
            ordinal=int(raw_ordinal) if raw_ordinal and raw_ordinal != _WILDCARD else None,
            any_ordinal=raw_ordinal is None or raw_ordinal == _WILDCARD,
        )

    @staticmethod
    def exact(tag: FaceTag) -> TagPattern:
        return TagPattern(
            feature=tag.feature,
            role=tag.role,
            source=str(tag.source) if tag.source is not None else None,
            ordinal=tag.ordinal,
            any_ordinal=tag.ordinal is None,
        )

    def matches(self, tag: FaceTag) -> bool:
        if self.feature is not None and tag.feature != self.feature:
            return False
        if self.role is not None and tag.role != self.role:
            return False
        if self.source is not None and str(tag.source or "") != self.source:
            return False
        return self.any_ordinal or tag.ordinal == self.ordinal

    def __str__(self) -> str:
        text = f"{self.feature or _WILDCARD}/{self.role or _WILDCARD}"
        if self.source is not None:
            text += f"[{self.source}]"
        if not self.any_ordinal and self.ordinal is not None:
            text += f"#{self.ordinal}"
        return text

    def to_dict(self) -> dict[str, object]:
        return {
            "feature": self.feature,
            "role": self.role,
            "source": self.source,
            "ordinal": None if self.any_ordinal else self.ordinal,
        }

    @staticmethod
    def from_dict(data: dict[str, object]) -> TagPattern:
        ordinal = data.get("ordinal")
        return TagPattern(
            feature=_optional_str(data.get("feature")),
            role=_optional_str(data.get("role")),
            source=_optional_str(data.get("source")),
            ordinal=int(ordinal) if ordinal is not None else None,
            any_ordinal=ordinal is None,
        )


def _none_if_wildcard(value: str | None) -> str | None:
    return None if value is None or value == _WILDCARD else value


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


# --------------------------------------------------------------------------
# Direction filters
# --------------------------------------------------------------------------

_NAMED_AXES: dict[str, Vec3] = {
    "+x": Vec3(1, 0, 0), "-x": Vec3(-1, 0, 0),
    "+y": Vec3(0, 1, 0), "-y": Vec3(0, -1, 0),
    "+z": Vec3(0, 0, 1), "-z": Vec3(0, 0, -1),
}


@dataclass(frozen=True, slots=True)
class DirectionFilter:
    """A narrowing constraint on a face normal or an edge direction.

    ``signed`` distinguishes "faces pointing up" (``+z``) from "faces parallel to
    z" (``|z``). Edges use the unsigned form, since an edge has no inherent
    sense of direction.
    """

    axis: Vec3
    signed: bool = True
    tolerance: float = DEFAULT_NORMAL_TOL

    @staticmethod
    def parse(text: str) -> DirectionFilter:
        cleaned = text.strip().lower()
        if cleaned.startswith("|"):
            axis = _NAMED_AXES.get("+" + cleaned[1:].lstrip("+-"))
            if axis is None:
                raise TagSyntaxError(text=text, reason="expected |x, |y or |z")
            return DirectionFilter(axis=axis, signed=False)
        axis = _NAMED_AXES.get(cleaned if cleaned[:1] in "+-" else "+" + cleaned)
        if axis is None:
            raise TagSyntaxError(text=text, reason="expected +x, -x, +y, -y, +z, -z, |x, |y or |z")
        return DirectionFilter(axis=axis, signed=True)

    def accepts(self, direction: Vec3) -> bool:
        alignment = direction.dot(self.axis)
        if not self.signed:
            alignment = abs(alignment)
        return abs(alignment - 1.0) <= self.tolerance

    def __str__(self) -> str:
        name = next((k for k, v in _NAMED_AXES.items() if v.is_close(self.axis)), "?")
        return name if self.signed else "|" + name[1:]

    def to_dict(self) -> dict[str, object]:
        return {"axis": list(self.axis.as_tuple()), "signed": self.signed}

    @staticmethod
    def from_dict(data: dict[str, object]) -> DirectionFilter:
        axis = data["axis"]
        assert isinstance(axis, (list, tuple))
        return DirectionFilter(
            axis=Vec3(*(float(c) for c in axis)), signed=bool(data.get("signed", True))
        )


# --------------------------------------------------------------------------
# Expectations — the memory that makes failure detectable
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Expectation:
    """What the last good build resolved, stored so drift becomes visible."""

    count: int
    fingerprints: tuple[FaceFingerprint, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "count": self.count,
            "fingerprints": [f.to_dict() for f in self.fingerprints],
        }

    @staticmethod
    def from_dict(data: dict[str, object]) -> Expectation:
        raw = data.get("fingerprints") or []
        assert isinstance(raw, list)
        return Expectation(
            count=int(data["count"]),  # type: ignore[arg-type]
            fingerprints=tuple(FaceFingerprint.from_dict(f) for f in raw),
        )


# --------------------------------------------------------------------------
# Face selector
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FaceSelector:
    """Select faces by provenance, optionally narrowed by direction."""

    include: tuple[TagPattern, ...] = ()
    exclude: tuple[TagPattern, ...] = ()
    direction: DirectionFilter | None = None
    expect: Expectation | None = None
    label: str = field(default="", compare=False)

    @staticmethod
    def parse(text: str) -> FaceSelector:
        """Parse a comma-separated union of patterns, e.g. ``base/cap+, slot/floor``."""
        patterns = tuple(TagPattern.parse(part) for part in _split_union(text))
        return FaceSelector(include=patterns, label=text.strip())

    @staticmethod
    def for_tag(tag: FaceTag) -> FaceSelector:
        return FaceSelector(include=(TagPattern.exact(tag),), label=str(tag))

    def describe(self) -> str:
        base = self.label or (", ".join(str(p) for p in self.include) or "*")
        if self.direction is not None:
            base += f" dir={self.direction}"
        if self.exclude:
            base += " excluding " + ", ".join(str(p) for p in self.exclude)
        return f"faces({base})"

    # -- resolution --------------------------------------------------------

    def candidates(self, topology: TopologyIndex) -> list[FaceEntry]:
        """Everything matching provenance and direction, before arbitration."""
        matched = [
            entry
            for entry in topology.faces
            if (not self.include or any(p.matches(entry.tag) for p in self.include))
            and not any(p.matches(entry.tag) for p in self.exclude)
        ]
        if self.direction is not None:
            matched = [e for e in matched if self.direction.accepts(e.fingerprint.normal)]
        return matched

    def resolve(self, topology: TopologyIndex, *, feature: str | None = None) -> list[FaceEntry]:
        """Resolve to concrete faces, or raise with an actionable diagnostic."""
        matched = self.candidates(topology)

        if self.expect is not None and len(matched) != self.expect.count:
            matched = self._arbitrate(matched)

        expected = self.expect.count if self.expect is not None else None
        if not matched or (expected is not None and len(matched) != expected):
            raise SelectorResolutionError(
                selector=self.describe(),
                expected=expected,
                actual=len(matched),
                feature=feature,
                missing=self._missing(topology),
                reasons=self._reasons(topology, matched, expected),
            )
        return matched

    def _arbitrate(self, matched: list[FaceEntry]) -> list[FaceEntry]:
        """Use stored fingerprints to pick the intended subset.

        Only ever *narrows* an over-broad match. It cannot invent a face, so a
        selector that lost geometry still fails rather than silently rebinding.
        """
        assert self.expect is not None
        if len(matched) <= self.expect.count or not self.expect.fingerprints:
            return matched

        chosen: list[FaceEntry] = []
        remaining = list(matched)
        for reference in self.expect.fingerprints:
            if not remaining:
                break
            best = min(remaining, key=lambda e: reference.distance(e.fingerprint))
            if reference.distance(best.fingerprint) == float("inf"):
                continue
            remaining.remove(best)
            chosen.append(best)
        return chosen or matched

    def _missing(self, topology: TopologyIndex) -> tuple[str, ...]:
        """Exact tags the selector names that no longer exist."""
        missing: list[str] = []
        for pattern in self.include:
            if pattern.feature and pattern.role and not any(
                pattern.matches(t) for t in topology.face_tags()
            ):
                missing.append(str(pattern))
        return tuple(missing)

    def _reasons(
        self, topology: TopologyIndex, matched: Sequence[FaceEntry], expected: int | None
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        for pattern in self.include:
            if pattern.feature is None or pattern.role is None:
                continue
            if any(pattern.matches(t) for t in topology.face_tags()):
                continue
            for retired in topology.retired:
                if pattern.matches(retired.tag):
                    reasons.append(retired.describe())
                    break
            else:
                reasons.append(f"no face matches '{pattern}' in the current model")

        if expected is not None and len(matched) > expected and not reasons:
            reasons.append(
                f"the selector became ambiguous: {len(matched)} faces now match where "
                f"{expected} did before. Narrow it with a direction filter or an ordinal."
            )
        if self.direction is not None and not matched:
            reasons.append(f"no candidate face points {self.direction}")
        return tuple(reasons)

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {"include": [p.to_dict() for p in self.include]}
        if self.exclude:
            data["exclude"] = [p.to_dict() for p in self.exclude]
        if self.direction is not None:
            data["direction"] = self.direction.to_dict()
        if self.expect is not None:
            data["expect"] = self.expect.to_dict()
        if self.label:
            data["label"] = self.label
        return data

    @staticmethod
    def from_dict(data: dict[str, object]) -> FaceSelector:
        include = data.get("include") or []
        exclude = data.get("exclude") or []
        assert isinstance(include, list) and isinstance(exclude, list)
        direction = data.get("direction")
        expect = data.get("expect")
        return FaceSelector(
            include=tuple(TagPattern.from_dict(p) for p in include),
            exclude=tuple(TagPattern.from_dict(p) for p in exclude),
            direction=DirectionFilter.from_dict(direction) if direction else None,  # type: ignore[arg-type]
            expect=Expectation.from_dict(expect) if expect else None,  # type: ignore[arg-type]
            label=str(data.get("label", "")),
        )

    def with_expectation(self, entries: Sequence[FaceEntry]) -> FaceSelector:
        """Record what this build resolved, so the next one can detect drift."""
        return FaceSelector(
            include=self.include,
            exclude=self.exclude,
            direction=self.direction,
            expect=Expectation(
                count=len(entries), fingerprints=tuple(e.fingerprint for e in entries)
            ),
            label=self.label,
        )


# --------------------------------------------------------------------------
# Edge selector
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EdgeSelector:
    """Select edges via the faces they separate.

    ``between`` takes two face patterns and matches every edge whose adjacent
    pair satisfies them in either order — so ``between=[base/cap+, base/side[*]]``
    is "the whole top perimeter", stated once and stable as the profile changes.
    """

    between: tuple[TagPattern, TagPattern] | None = None
    touching: tuple[TagPattern, ...] = ()
    #: Several selectors unioned with commas. Needed because ``^`` binds tighter
    #: than the union does, so "these edges and those" cannot be said any other
    #: way once either side names a pair of faces.
    alternatives: tuple[EdgeSelector, ...] = ()
    direction: DirectionFilter | None = None
    expect: Expectation | None = None
    label: str = field(default="", compare=False)

    @staticmethod
    def parse(text: str) -> EdgeSelector:
        """Parse the shorthand a document uses to state which edges it means.

        ``"base/cap+ ^ base/side[*]"`` selects edges between two face patterns —
        the whole top perimeter, stated once. A single pattern selects every edge
        touching a matching face. An optional ``dir=|z`` narrows by direction.
        """
        remainder, direction = _split_direction(text)

        # Commas bind loosest, so they are split first: 'a ^ b, c ^ d' is two
        # edge selectors, not one selector joining four patterns.
        united = _split_union(remainder)
        if len(united) > 1:
            return EdgeSelector(
                alternatives=tuple(EdgeSelector.parse(part) for part in united),
                direction=direction,
                label=text.strip(),
            )

        parts = _split_top_level(remainder, "^")
        if len(parts) == 2:
            return EdgeSelector(
                between=(TagPattern.parse(parts[0]), TagPattern.parse(parts[1])),
                direction=direction,
                label=text.strip(),
            )
        if len(parts) == 1:
            return EdgeSelector(
                touching=(TagPattern.parse(parts[0]),),
                direction=direction,
                label=text.strip(),
            )
        raise TagSyntaxError(
            text=text, reason="an edge selector joins at most two face patterns with '^'"
        )

    @staticmethod
    def between_patterns(first: str, second: str) -> EdgeSelector:
        return EdgeSelector(
            between=(TagPattern.parse(first), TagPattern.parse(second)),
            label=f"between {first} and {second}",
        )

    def describe(self) -> str:
        if self.label:
            base = self.label
        elif self.alternatives:
            base = " or ".join(alt.describe() for alt in self.alternatives)
        elif self.between is not None:
            base = f"between {self.between[0]} and {self.between[1]}"
        else:
            base = ", ".join(str(p) for p in self.touching) or "*"
        if self.direction is not None:
            base += f" dir={self.direction}"
        return f"edges({base})"

    def candidates(self, topology: TopologyIndex) -> list[EdgeEntry]:
        if self.alternatives:
            # Kept in topology order rather than in the order written, so a
            # union and the equivalent single selector give the same answer.
            wanted = {
                entry.tag
                for alternative in self.alternatives
                for entry in alternative.candidates(topology)
            }
            matched = [entry for entry in topology.edges if entry.tag in wanted]
        else:
            matched = [entry for entry in topology.edges if self._matches(entry.tag)]
        if self.direction is not None:
            matched = [
                e
                for e in matched
                if e.fingerprint.is_parallel_to(self.direction.axis, self.direction.tolerance)
            ]
        return matched

    def _matches(self, tag: EdgeTag) -> bool:
        first, second = tag.faces
        if self.between is not None:
            a, b = self.between
            forwards = a.matches(first) and b.matches(second)
            backwards = a.matches(second) and b.matches(first)
            if not (forwards or backwards):
                return False
        return not self.touching or any(
            p.matches(first) or p.matches(second) for p in self.touching
        )

    def resolve(self, topology: TopologyIndex, *, feature: str | None = None) -> list[EdgeEntry]:
        matched = self.candidates(topology)
        expected = self.expect.count if self.expect is not None else None
        if not matched or (expected is not None and len(matched) != expected):
            raise SelectorResolutionError(
                selector=self.describe(),
                expected=expected,
                actual=len(matched),
                feature=feature,
                reasons=self._reasons(topology, matched, expected),
            )
        return matched

    def _reasons(
        self, topology: TopologyIndex, matched: Sequence[EdgeEntry], expected: int | None
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        patterns = list(self.between or ()) + list(self.touching)
        for pattern in patterns:
            if pattern.feature is None or pattern.role is None:
                continue
            if any(pattern.matches(t) for t in topology.face_tags()):
                continue
            retirement = next(
                (r for r in topology.retired if pattern.matches(r.tag)), None
            )
            reasons.append(
                retirement.describe()
                if retirement
                else f"no face matches '{pattern}', so no edge can be found through it"
            )
        if expected is not None and len(matched) != expected and not reasons:
            reasons.append(
                f"edge count changed from {expected} to {len(matched)}; the adjacent "
                "faces still exist, so the profile or a later feature changed the "
                "number of edges between them"
            )
        return tuple(reasons)

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {}
        if self.between is not None:
            data["between"] = [p.to_dict() for p in self.between]
        if self.touching:
            data["touching"] = [p.to_dict() for p in self.touching]
        if self.alternatives:
            data["alternatives"] = [a.to_dict() for a in self.alternatives]
        if self.direction is not None:
            data["direction"] = self.direction.to_dict()
        if self.expect is not None:
            data["expect"] = self.expect.to_dict()
        if self.label:
            data["label"] = self.label
        return data

    @staticmethod
    def from_dict(data: dict[str, object]) -> EdgeSelector:
        between = data.get("between")
        touching = data.get("touching") or []
        alternatives = data.get("alternatives") or []
        assert isinstance(alternatives, list)
        direction = data.get("direction")
        expect = data.get("expect")
        assert isinstance(touching, list)
        parsed_between: tuple[TagPattern, TagPattern] | None = None
        if isinstance(between, list) and len(between) == 2:
            parsed_between = (
                TagPattern.from_dict(between[0]),
                TagPattern.from_dict(between[1]),
            )
        return EdgeSelector(
            between=parsed_between,
            touching=tuple(TagPattern.from_dict(p) for p in touching),
            alternatives=tuple(EdgeSelector.from_dict(a) for a in alternatives),
            direction=DirectionFilter.from_dict(direction) if direction else None,  # type: ignore[arg-type]
            expect=Expectation.from_dict(expect) if expect else None,  # type: ignore[arg-type]
            label=str(data.get("label", "")),
        )


def _split_direction(text: str) -> tuple[str, DirectionFilter | None]:
    """Peel an optional trailing ``dir=...`` off a selector."""
    marker = text.rfind("dir=")
    if marker < 0:
        return text, None
    return text[:marker].strip(), DirectionFilter.parse(text[marker + 4 :].strip())


def _split_top_level(text: str, separator: str) -> list[str]:
    """Split on a separator that is not inside brackets."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        if char == separator and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _split_union(text: str) -> list[str]:
    """Split on commas that are not inside brackets."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    cleaned = [p.strip() for p in parts if p.strip()]
    if not cleaned:
        raise TagSyntaxError(text=text, reason="selector is empty")
    return cleaned
