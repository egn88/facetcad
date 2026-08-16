"""Corner tags — the edge construction one arity up.

An edge is named by the two faces it separates. Where blends meet, the kernel
emits a patch bounded by three or more named faces and attributable to none of
them, so it is named by the whole set. These tests pin the arity rule that keeps
``a ^ b`` (an edge) and ``a ^ b ^ c`` (a corner) unambiguous.
"""

from __future__ import annotations

import pytest

from facet.domain.errors import TagSyntaxError
from facet.domain.tags import CornerTag, EdgeTag, FaceTag, parse_tag

A = FaceTag.parse("base/cap+")
B = FaceTag.parse("base/side[outline.left]")
C = FaceTag.parse("round/fillet[base/cap+ ^ base/side[outline.top]]")
D = FaceTag.parse("base/cap-")


def test_a_corner_sorts_its_faces_canonically() -> None:
    """Identity must not depend on which face the kernel visited first."""
    assert CornerTag.of(C, A, B) == CornerTag.of(A, B, C)
    assert CornerTag.of(B, C, A).faces == CornerTag.of(A, B, C).faces


def test_two_faces_are_an_edge_not_a_corner() -> None:
    with pytest.raises(TagSyntaxError, match="at least three"):
        CornerTag.of(A, B)


def test_a_corner_needs_distinct_faces() -> None:
    with pytest.raises(TagSyntaxError, match="distinct"):
        CornerTag.of(A, B, A)


def test_a_corner_round_trips_through_text() -> None:
    corner = CornerTag.of(A, B, C)
    assert CornerTag.parse(str(corner)) == corner


def test_a_corner_round_trips_through_dict() -> None:
    corner = CornerTag.of(A, B, C, D)
    assert CornerTag.from_dict(corner.to_dict()) == corner


def test_arity_alone_separates_an_edge_from_a_corner() -> None:
    """The whole reason a corner may not have two faces."""
    assert isinstance(parse_tag(f"{A} ^ {B}"), EdgeTag)
    assert isinstance(parse_tag(f"{A} ^ {B} ^ {C}"), CornerTag)


def test_a_corner_nests_inside_a_face_tag() -> None:
    tag = FaceTag.parse(f"soften/corner[{A} ^ {B} ^ {C}]")
    assert isinstance(tag.source, CornerTag)
    assert FaceTag.parse(str(tag)) == tag
    assert FaceTag.from_dict(tag.to_dict()) == tag


def test_a_corner_reports_which_faces_bound_it() -> None:
    corner = CornerTag.of(A, B, C)
    assert corner.contains(B)
    assert not corner.contains(D)
