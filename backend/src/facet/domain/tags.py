"""The tag algebra: deterministic, human-readable identity for derived geometry.

This module is the reason the project exists. A face is never identified by a
kernel index. It is identified by *how it came to be*, expressed in names the
user chose:

    base/cap+                     the top cap of feature 'base'
    base/side[outline.left]       the side face swept from sketch curve 'left'
    slot/floor                    the floor of pocket 'slot'
    slot/wall[hole.c1]            the pocket wall swept from curve 'c1'
    base/cap+#1                   the second fragment, when a cut split the cap

Edges and vertices are *derived*, not tagged: an edge is the intersection of its
two adjacent named faces, written ``a ^ b`` with the pair in canonical order. So
the naming problem only has to be solved once, for faces, and edge stability —
which is what fillets depend on — follows for free.

The canonical representation is structured (see :meth:`FaceTag.to_dict`); the
string form is exact sugar over it and round-trips losslessly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import TagSyntaxError

# --------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ROLE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*[+-]?")


class Roles:
    """Well-known face roles.

    Intentionally *not* an enum. Feature handlers are pluggable, so a new
    feature type must be able to introduce a new role without editing a closed
    type here — the open/closed principle applied to the naming vocabulary.
    Only the syntax of a role is constrained, never its membership.
    """

    CAP_POS = "cap+"
    """Cap face on the positive side of the sketch plane normal."""
    CAP_NEG = "cap-"
    """Cap face on the negative side of the sketch plane normal."""
    SIDE = "side"
    """Lateral face swept from a profile curve by an additive feature."""
    FLOOR = "floor"
    """The bottom face created by a blind subtractive feature."""
    WALL = "wall"
    """Lateral face created by a subtractive feature, swept from a curve."""
    CEILING = "ceiling"
    """The near cap of a subtractive feature, exposed when it cuts upwards."""
    COUNTERBORE = "cbore"
    """The widened upper wall of a counterbored hole."""
    COUNTERBORE_FLOOR = "cbore_floor"
    """The annular shoulder a counterbore leaves for a fastener head."""
    FILLET = "fillet"
    CHAMFER = "chamfer"
    THREAD = "thread"
    """A helical thread flank, named from the point the thread was placed at."""
    CORNER = "corner"
    """A blend transition patch, named by the faces that bound it.

    Where blends meet at a corner the kernel emits a patch that came from no
    single edge, so it has no two-face name. It is named instead by the set of
    already-named faces around it — the same idea one arity up.
    """


def validate_role(role: str) -> str:
    if not role or not _ROLE_RE.fullmatch(role):
        raise TagSyntaxError(
            text=role, reason="role must be an identifier with an optional +/- suffix"
        )
    return role


def validate_ident(value: str, what: str) -> str:
    if not value or not _IDENT_RE.fullmatch(value):
        raise TagSyntaxError(text=value, reason=f"{what} must be an identifier")
    return value


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CurveRef:
    """A reference to a curve within a sketch — the root of most provenance."""

    sketch: str
    curve: str

    def __post_init__(self) -> None:
        validate_ident(self.sketch, "sketch name")
        validate_ident(self.curve, "curve name")

    def __str__(self) -> str:
        return f"{self.sketch}.{self.curve}"

    def to_dict(self) -> dict[str, object]:
        return {"kind": "curve", "sketch": self.sketch, "curve": self.curve}


@dataclass(frozen=True, slots=True)
class FaceTag:
    """The stable identity of a face.

    ``ordinal`` is populated only when a single logical face was split into
    several fragments; it is assigned by canonical geometric sort (see
    :mod:`facet.domain.splitting`), never by kernel enumeration order.
    """

    feature: str
    role: str
    source: TagSource | None = None
    ordinal: int | None = None

    def __post_init__(self) -> None:
        validate_ident(self.feature, "feature id")
        validate_role(self.role)
        if self.ordinal is not None and self.ordinal < 0:
            raise TagSyntaxError(text=str(self.ordinal), reason="ordinal must be non-negative")

    # -- string sugar ------------------------------------------------------

    def __str__(self) -> str:
        text = f"{self.feature}/{self.role}"
        if self.source is not None:
            text += f"[{self.source}]"
        if self.ordinal is not None:
            text += f"#{self.ordinal}"
        return text

    @staticmethod
    def parse(text: str) -> FaceTag:
        return _Parser(text).parse_complete_face()

    # -- canonical structured form ----------------------------------------

    def to_dict(self) -> dict[str, object]:
        return {
            "feature": self.feature,
            "role": self.role,
            "source": self.source.to_dict() if self.source is not None else None,
            "ordinal": self.ordinal,
        }

    @staticmethod
    def from_dict(data: dict[str, object]) -> FaceTag:
        try:
            feature = str(data["feature"])
            role = str(data["role"])
        except KeyError as exc:
            raise TagSyntaxError(text=repr(data), reason=f"missing key {exc}") from exc
        raw_source = data.get("source")
        source = _source_from_dict(raw_source) if raw_source else None
        raw_ordinal = data.get("ordinal")
        return FaceTag(
            feature=feature,
            role=role,
            source=source,
            ordinal=int(raw_ordinal) if raw_ordinal is not None else None,
        )

    # -- helpers -----------------------------------------------------------

    def with_ordinal(self, ordinal: int | None) -> FaceTag:
        return FaceTag(self.feature, self.role, self.source, ordinal)

    def without_ordinal(self) -> FaceTag:
        """The tag as it would be before any split — used to group fragments."""
        return FaceTag(self.feature, self.role, self.source, None)

    @property
    def sort_key(self) -> tuple[str, str, str, int]:
        return (self.feature, self.role, str(self.source or ""), self.ordinal or 0)


@dataclass(frozen=True, slots=True)
class EdgeTag:
    """An edge, identified as the intersection of its two adjacent faces.

    The pair is stored in canonical (sorted) order so that ``a ^ b`` and
    ``b ^ a`` are the same value — identity must not depend on which face the
    kernel happened to visit first.
    """

    faces: tuple[FaceTag, FaceTag]

    def __post_init__(self) -> None:
        a, b = self.faces
        if a == b:
            raise TagSyntaxError(text=str(a), reason="an edge needs two distinct faces")
        if a.sort_key > b.sort_key:
            object.__setattr__(self, "faces", (b, a))

    @staticmethod
    def of(a: FaceTag, b: FaceTag) -> EdgeTag:
        return EdgeTag(faces=(a, b))

    def __str__(self) -> str:
        return f"{self.faces[0]} ^ {self.faces[1]}"

    @staticmethod
    def parse(text: str) -> EdgeTag:
        return _Parser(text).parse_complete_edge()

    def to_dict(self) -> dict[str, object]:
        return {"kind": "edge", "faces": [f.to_dict() for f in self.faces]}

    @staticmethod
    def from_dict(data: dict[str, object]) -> EdgeTag:
        raw = data.get("faces")
        if not isinstance(raw, list) or len(raw) != 2:
            raise TagSyntaxError(text=repr(data), reason="edge requires exactly two faces")
        first, second = raw
        if not isinstance(first, dict) or not isinstance(second, dict):
            raise TagSyntaxError(text=repr(data), reason="edge faces must be objects")
        return EdgeTag.of(FaceTag.from_dict(first), FaceTag.from_dict(second))

    def contains(self, face: FaceTag) -> bool:
        return face in self.faces


@dataclass(frozen=True, slots=True)
class CornerTag:
    """A blend transition patch, identified by the faces that bound it.

    An :class:`EdgeTag` names an edge by the two faces it separates. Where two
    blends meet, the kernel emits a patch bounded by three or more already-named
    faces and attributable to none of them — so it is named by the whole set,
    the same construction one arity up.

    Three faces is the minimum, which is also what keeps ``a ^ b ^ c``
    unambiguous against an edge's ``a ^ b`` when the text is parsed back.
    """

    faces: tuple[FaceTag, ...]

    def __post_init__(self) -> None:
        if len(self.faces) < 3:
            raise TagSyntaxError(
                text=" ^ ".join(str(f) for f in self.faces),
                reason="a corner needs at least three faces; two faces are an edge",
            )
        if len(set(self.faces)) != len(self.faces):
            raise TagSyntaxError(
                text=" ^ ".join(str(f) for f in self.faces),
                reason="a corner needs distinct faces",
            )
        object.__setattr__(self, "faces", tuple(sorted(self.faces, key=lambda f: f.sort_key)))

    @staticmethod
    def of(*faces: FaceTag) -> CornerTag:
        return CornerTag(faces=tuple(faces))

    def __str__(self) -> str:
        return " ^ ".join(str(face) for face in self.faces)

    @staticmethod
    def parse(text: str) -> CornerTag:
        return _Parser(text).parse_complete_corner()

    def to_dict(self) -> dict[str, object]:
        return {"kind": "corner", "faces": [f.to_dict() for f in self.faces]}

    @staticmethod
    def from_dict(data: dict[str, object]) -> CornerTag:
        raw = data.get("faces")
        if not isinstance(raw, list) or len(raw) < 3:
            raise TagSyntaxError(
                text=repr(data), reason="corner requires at least three faces"
            )
        return CornerTag(
            faces=tuple(
                FaceTag.from_dict(item) if isinstance(item, dict) else _reject(item)
                for item in raw
            )
        )

    def contains(self, face: FaceTag) -> bool:
        return face in self.faces


def _reject(item: object) -> FaceTag:
    raise TagSyntaxError(text=repr(item), reason="corner faces must be objects")


TagSource = CurveRef | FaceTag | EdgeTag | CornerTag


def _source_from_dict(raw: object) -> TagSource:
    if not isinstance(raw, dict):
        raise TagSyntaxError(text=repr(raw), reason="source must be an object")
    kind = raw.get("kind")
    if kind == "curve":
        return CurveRef(sketch=str(raw["sketch"]), curve=str(raw["curve"]))
    if kind == "edge":
        return EdgeTag.from_dict(raw)
    if kind == "corner":
        return CornerTag.from_dict(raw)
    if kind == "face" or "feature" in raw:
        return FaceTag.from_dict(raw)
    raise TagSyntaxError(text=repr(raw), reason=f"unknown source kind {kind!r}")


def source_to_dict(source: TagSource) -> dict[str, object]:
    if isinstance(source, FaceTag):
        return {"kind": "face", **source.to_dict()}
    return source.to_dict()


# --------------------------------------------------------------------------
# Parser for the string shorthand
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"\s*(?:([A-Za-z_][A-Za-z0-9_]*)|(\d+)|([/\[\]#^.+-]))")


class _Parser:
    """Recursive-descent parser for the tag shorthand.

    Grammar::

        face   := IDENT '/' role ( '[' source ']' )? ( '#' INT )?
        role   := IDENT ( '+' | '-' )?
        source := corner | edge | face | curve
        edge   := face '^' face
        corner := face '^' face '^' face ( '^' face )*
        curve  := IDENT '.' IDENT
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.tokens = self._tokenize(text)
        self.pos = 0

    @staticmethod
    def _tokenize(text: str) -> list[tuple[str, str]]:
        tokens: list[tuple[str, str]] = []
        index = 0
        while index < len(text):
            match = _TOKEN_RE.match(text, index)
            if match is None:
                if text[index:].strip() == "":
                    break
                raise TagSyntaxError(text=text, reason=f"unexpected character {text[index]!r}")
            ident, number, punct = match.groups()
            if ident is not None:
                tokens.append(("IDENT", ident))
            elif number is not None:
                tokens.append(("INT", number))
            else:
                tokens.append(("PUNCT", punct))
            index = match.end()
        return tokens

    # -- token helpers -----------------------------------------------------

    def _peek(self, offset: int = 0) -> tuple[str, str] | None:
        index = self.pos + offset
        return self.tokens[index] if index < len(self.tokens) else None

    def _at_punct(self, char: str) -> bool:
        token = self._peek()
        return token is not None and token[0] == "PUNCT" and token[1] == char

    def _take(self, kind: str, what: str) -> str:
        token = self._peek()
        if token is None or token[0] != kind:
            found = token[1] if token else "end of input"
            raise TagSyntaxError(text=self.text, reason=f"expected {what}, found {found!r}")
        self.pos += 1
        return token[1]

    def _expect_punct(self, char: str) -> None:
        if not self._at_punct(char):
            token = self._peek()
            found = token[1] if token else "end of input"
            raise TagSyntaxError(text=self.text, reason=f"expected {char!r}, found {found!r}")
        self.pos += 1

    # -- productions -------------------------------------------------------

    def parse_complete_face(self) -> FaceTag:
        tag = self.parse_face()
        self._expect_end()
        return tag

    def parse_complete_corner(self) -> CornerTag:
        joined = [self.parse_face()]
        while self._at_punct("^"):
            self.pos += 1
            joined.append(self.parse_face())
        self._expect_end()
        return CornerTag(faces=tuple(joined))

    def parse_complete_edge(self) -> EdgeTag:
        first = self.parse_face()
        self._expect_punct("^")
        second = self.parse_face()
        self._expect_end()
        return EdgeTag.of(first, second)

    def _expect_end(self) -> None:
        if self.pos != len(self.tokens):
            raise TagSyntaxError(
                text=self.text, reason=f"trailing input at {self.tokens[self.pos][1]!r}"
            )

    def parse_face(self) -> FaceTag:
        feature = self._take("IDENT", "a feature id")
        self._expect_punct("/")
        role = self._take("IDENT", "a role")
        if self._at_punct("+") or self._at_punct("-"):
            role += self.tokens[self.pos][1]
            self.pos += 1

        source: TagSource | None = None
        if self._at_punct("["):
            self.pos += 1
            source = self.parse_source()
            self._expect_punct("]")

        ordinal: int | None = None
        if self._at_punct("#"):
            self.pos += 1
            ordinal = int(self._take("INT", "an ordinal"))

        return FaceTag(feature=feature, role=role, source=source, ordinal=ordinal)

    def parse_source(self) -> TagSource:
        first = self._parse_source_atom()
        if not self._at_punct("^"):
            return first
        if not isinstance(first, FaceTag):
            raise TagSyntaxError(text=self.text, reason="'^' joins face tags")

        joined = [first]
        while self._at_punct("^"):
            self.pos += 1
            joined.append(self.parse_face())
        # Two faces are an edge, three or more a blend corner. The arity is the
        # only thing that distinguishes them, which is why a corner may not have
        # fewer than three.
        return EdgeTag.of(*joined) if len(joined) == 2 else CornerTag(faces=tuple(joined))

    def _parse_source_atom(self) -> TagSource:
        head = self._peek()
        if head is None or head[0] != "IDENT":
            found = head[1] if head else "end of input"
            raise TagSyntaxError(text=self.text, reason=f"expected a source, found {found!r}")
        follower = self._peek(1)
        if follower is not None and follower[0] == "PUNCT" and follower[1] == "/":
            return self.parse_face()
        sketch = self._take("IDENT", "a sketch name")
        self._expect_punct(".")
        curve = self._take("IDENT", "a curve name")
        return CurveRef(sketch=sketch, curve=curve)


def parse_tag(text: str) -> FaceTag | EdgeTag | CornerTag:
    """Parse a face, edge or corner tag, whichever the text describes."""
    parser = _Parser(text)
    joined = [parser.parse_face()]
    while parser._at_punct("^"):
        parser.pos += 1
        joined.append(parser.parse_face())
    parser._expect_end()
    if len(joined) == 1:
        return joined[0]
    return EdgeTag.of(*joined) if len(joined) == 2 else CornerTag(faces=tuple(joined))
