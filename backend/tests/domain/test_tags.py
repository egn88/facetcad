"""The tag algebra must round-trip losslessly and order canonically.

If any of these fail, every downstream stability guarantee is void.
"""

from __future__ import annotations

import pytest

from facet.domain.errors import TagSyntaxError
from facet.domain.tags import CurveRef, EdgeTag, FaceTag, Roles, parse_tag

ROUND_TRIP_CASES = [
    "base/cap+",
    "base/cap-",
    "base/side[outline.left]",
    "slot/floor",
    "slot/wall[hole.c1]",
    "base/cap+#0",
    "base/cap+#12",
    "base/side[outline.left]#3",
    "f1/fillet[base/cap+ ^ base/side[outline.left]]",
    "f2/chamfer[slot/floor ^ slot/wall[hole.c1]]",
    "deep/fillet[a/fillet[x/cap+ ^ x/side[s.c]] ^ b/cap-]",
]


@pytest.mark.parametrize("text", ROUND_TRIP_CASES)
def test_string_form_round_trips(text: str) -> None:
    assert str(FaceTag.parse(text)) == text


@pytest.mark.parametrize("text", ROUND_TRIP_CASES)
def test_structured_form_round_trips(text: str) -> None:
    tag = FaceTag.parse(text)
    assert FaceTag.from_dict(tag.to_dict()) == tag


@pytest.mark.parametrize("text", ROUND_TRIP_CASES)
def test_structured_and_string_forms_agree(text: str) -> None:
    """The two representations are exactly equivalent, as designed."""
    tag = FaceTag.parse(text)
    assert str(FaceTag.from_dict(tag.to_dict())) == text


def test_parses_the_documented_pad_then_pocket_example() -> None:
    wall = FaceTag.parse("slot/wall[hole.c1]")
    assert wall.feature == "slot"
    assert wall.role == Roles.WALL
    assert wall.source == CurveRef(sketch="hole", curve="c1")
    assert wall.ordinal is None


def test_cap_roles_keep_their_sign() -> None:
    assert FaceTag.parse("base/cap+").role == Roles.CAP_POS
    assert FaceTag.parse("base/cap-").role == Roles.CAP_NEG
    assert FaceTag.parse("base/cap+") != FaceTag.parse("base/cap-")


def test_ordinal_is_parsed_and_strippable() -> None:
    tag = FaceTag.parse("base/cap+#2")
    assert tag.ordinal == 2
    assert str(tag.without_ordinal()) == "base/cap+"
    assert tag.with_ordinal(5).ordinal == 5


# --------------------------------------------------------------------------
# Edges are derived from faces, and their identity is order-independent
# --------------------------------------------------------------------------


def test_edge_identity_is_independent_of_face_order() -> None:
    a = FaceTag.parse("base/cap+")
    b = FaceTag.parse("base/side[outline.left]")
    assert EdgeTag.of(a, b) == EdgeTag.of(b, a)
    assert hash(EdgeTag.of(a, b)) == hash(EdgeTag.of(b, a))


def test_edge_string_form_is_canonically_ordered() -> None:
    a = FaceTag.parse("base/cap+")
    b = FaceTag.parse("base/side[outline.left]")
    assert str(EdgeTag.of(a, b)) == str(EdgeTag.of(b, a))


def test_non_canonical_input_is_normalised_on_parse() -> None:
    """Writing the pair 'backwards' in a document yields the same tag.

    This matters because a nested edge source appears verbatim in the string
    form; without normalisation the same edge would have two spellings and
    selector matching would become order-sensitive.
    """
    canonical = "f1/fillet[base/cap+ ^ base/side[outline.left]]"
    reversed_pair = "f1/fillet[base/side[outline.left] ^ base/cap+]"
    assert FaceTag.parse(reversed_pair) == FaceTag.parse(canonical)
    assert str(FaceTag.parse(reversed_pair)) == canonical


def test_edge_round_trips_through_both_forms() -> None:
    edge = EdgeTag.parse("base/cap+ ^ base/side[outline.left]")
    assert EdgeTag.parse(str(edge)) == edge
    assert EdgeTag.from_dict(edge.to_dict()) == edge


def test_edge_rejects_a_degenerate_pair() -> None:
    face = FaceTag.parse("base/cap+")
    with pytest.raises(TagSyntaxError):
        EdgeTag.of(face, face)


def test_edge_membership() -> None:
    a = FaceTag.parse("base/cap+")
    b = FaceTag.parse("base/side[outline.left]")
    c = FaceTag.parse("slot/floor")
    edge = EdgeTag.of(a, b)
    assert edge.contains(a) and edge.contains(b)
    assert not edge.contains(c)


def test_parse_tag_discriminates_faces_from_edges() -> None:
    assert isinstance(parse_tag("base/cap+"), FaceTag)
    assert isinstance(parse_tag("base/cap+ ^ slot/floor"), EdgeTag)


# --------------------------------------------------------------------------
# Malformed input is rejected, never silently coerced
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "base",
        "base/",
        "/cap+",
        "base/cap+[",
        "base/cap+]",
        "base/side[outline]",
        "base/side[outline.]",
        "base/cap+#",
        "base/cap+#x",
        "base/cap+ ^ ",
        "base cap+",
        "1base/cap+",
        "base/cap+ trailing",
        "base/side[a.b] ^ ",
    ],
)
def test_malformed_tags_raise(text: str) -> None:
    with pytest.raises(TagSyntaxError):
        FaceTag.parse(text)


def test_whitespace_around_the_edge_operator_is_insignificant() -> None:
    assert EdgeTag.parse("base/cap+^slot/floor") == EdgeTag.parse("base/cap+   ^   slot/floor")


def test_tags_are_hashable_and_usable_as_dict_keys() -> None:
    tags = {FaceTag.parse(t): i for i, t in enumerate(ROUND_TRIP_CASES)}
    assert len(tags) == len(ROUND_TRIP_CASES)
    assert tags[FaceTag.parse("slot/floor")] == ROUND_TRIP_CASES.index("slot/floor")
