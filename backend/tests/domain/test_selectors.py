"""Selector resolution — and, above all, its refusal to guess.

The tests at the bottom of this file encode the central promise of the project:
when a selector no longer resolves to what the document expected, the rebuild
fails with a diagnostic naming the responsible feature. It never silently
rebinds to a different face.
"""

from __future__ import annotations

import pytest

from facet.domain.errors import SelectorResolutionError, TagSyntaxError
from facet.domain.fingerprint import (
    CurveKind,
    EdgeFingerprint,
    FaceFingerprint,
    SurfaceKind,
)
from facet.domain.math3d import Vec3
from facet.domain.selectors import (
    DirectionFilter,
    EdgeSelector,
    Expectation,
    FaceSelector,
    TagPattern,
)
from facet.domain.tags import EdgeTag, FaceTag
from facet.domain.topology import EdgeEntry, FaceEntry, RetiredTag, TopologyIndex

# --------------------------------------------------------------------------
# A fixture modelling the documented example: pad a rectangle, pocket a hole
# --------------------------------------------------------------------------


def face(tag: str, normal: Vec3, centroid: Vec3, area: float = 100.0,
         surface: str = SurfaceKind.PLANE) -> FaceEntry:
    return FaceEntry(
        tag=FaceTag.parse(tag),
        fingerprint=FaceFingerprint(surface, area, centroid, normal),
    )


def edge(a: str, b: str, direction: Vec3, midpoint: Vec3, length: float = 10.0) -> EdgeEntry:
    return EdgeEntry(
        tag=EdgeTag.of(FaceTag.parse(a), FaceTag.parse(b)),
        fingerprint=EdgeFingerprint(CurveKind.LINE, length, midpoint, direction),
    )


UP, DOWN = Vec3(0, 0, 1), Vec3(0, 0, -1)
LEFT, RIGHT = Vec3(-1, 0, 0), Vec3(1, 0, 0)
FRONT, BACK = Vec3(0, -1, 0), Vec3(0, 1, 0)


@pytest.fixture
def padded_plate() -> TopologyIndex:
    """A 120x72x6 pad named 'base' — six faces, twelve edges (four shown)."""
    return TopologyIndex.build(
        faces=[
            face("base/cap+", UP, Vec3(60, 36, 6), area=8640),
            face("base/cap-", DOWN, Vec3(60, 36, 0), area=8640),
            face("base/side[out.left]", LEFT, Vec3(0, 36, 3), area=432),
            face("base/side[out.right]", RIGHT, Vec3(120, 36, 3), area=432),
            face("base/side[out.front]", FRONT, Vec3(60, 0, 3), area=720),
            face("base/side[out.back]", BACK, Vec3(60, 72, 3), area=720),
        ],
        edges=[
            edge("base/cap+", "base/side[out.left]", Vec3(0, 1, 0), Vec3(0, 36, 6), 72),
            edge("base/cap+", "base/side[out.right]", Vec3(0, 1, 0), Vec3(120, 36, 6), 72),
            edge("base/cap+", "base/side[out.front]", Vec3(1, 0, 0), Vec3(60, 0, 6), 120),
            edge("base/cap+", "base/side[out.back]", Vec3(1, 0, 0), Vec3(60, 72, 6), 120),
            edge("base/side[out.left]", "base/side[out.front]", Vec3(0, 0, 1), Vec3(0, 0, 3), 6),
        ],
    )


@pytest.fixture
def plate_with_pocket(padded_plate: TopologyIndex) -> TopologyIndex:
    """The same plate after a circular pocket named 'slot' cuts the top."""
    return TopologyIndex.build(
        faces=[
            *padded_plate.faces,
            face(
                "slot/wall[hole.c1]", RIGHT, Vec3(60, 36, 4),
                area=94, surface=SurfaceKind.CYLINDER,
            ),
            face("slot/floor", UP, Vec3(60, 36, 2), area=78),
        ],
        edges=padded_plate.edges,
    )


# --------------------------------------------------------------------------
# Pattern matching
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "tag", "expected"),
    [
        ("base/cap+", "base/cap+", True),
        ("base/cap+", "base/cap-", False),
        ("base/side[*]", "base/side[out.left]", True),
        ("base/side[*]", "base/cap+", False),
        ("base/*", "base/cap+", True),
        ("base/*", "slot/floor", False),
        ("*/cap+", "base/cap+", True),
        ("*/cap+", "slot/cap+", True),
        ("base/side[out.left]", "base/side[out.right]", False),
        ("base/cap+#1", "base/cap+#1", True),
        ("base/cap+#1", "base/cap+#0", False),
        ("base/cap+#*", "base/cap+#3", True),
        ("base/cap+", "base/cap+#2", True),  # bare pattern spans all fragments
    ],
)
def test_pattern_matching(pattern: str, tag: str, expected: bool) -> None:
    assert TagPattern.parse(pattern).matches(FaceTag.parse(tag)) is expected


def test_malformed_patterns_are_rejected() -> None:
    for text in ["", "base", "/cap+", "base//cap"]:
        with pytest.raises(TagSyntaxError):
            TagPattern.parse(text)


# --------------------------------------------------------------------------
# Face resolution
# --------------------------------------------------------------------------


def test_resolves_an_exact_tag(padded_plate: TopologyIndex) -> None:
    resolved = FaceSelector.parse("base/cap+").resolve(padded_plate)
    assert [str(e.tag) for e in resolved] == ["base/cap+"]


def test_resolves_a_wildcard_to_every_side_face(padded_plate: TopologyIndex) -> None:
    resolved = FaceSelector.parse("base/side[*]").resolve(padded_plate)
    assert len(resolved) == 4


def test_resolves_a_union(padded_plate: TopologyIndex) -> None:
    resolved = FaceSelector.parse("base/cap+, base/cap-").resolve(padded_plate)
    assert {str(e.tag) for e in resolved} == {"base/cap+", "base/cap-"}


def test_direction_filter_narrows_the_match(padded_plate: TopologyIndex) -> None:
    selector = FaceSelector(
        include=(TagPattern.parse("base/*"),), direction=DirectionFilter.parse("+z")
    )
    assert [str(e.tag) for e in selector.resolve(padded_plate)] == ["base/cap+"]


def test_exclusion_removes_matches(padded_plate: TopologyIndex) -> None:
    selector = FaceSelector(
        include=(TagPattern.parse("base/side[*]"),),
        exclude=(TagPattern.parse("base/side[out.left]"),),
    )
    assert len(selector.resolve(padded_plate)) == 3


def test_pocket_faces_are_addressable_by_provenance(plate_with_pocket: TopologyIndex) -> None:
    """The user's stated requirement: act on faces generated by the pocket."""
    wall = FaceSelector.parse("slot/wall[hole.c1]").resolve(plate_with_pocket)
    floor = FaceSelector.parse("slot/floor").resolve(plate_with_pocket)
    assert wall[0].fingerprint.surface == SurfaceKind.CYLINDER
    assert floor[0].fingerprint.centroid.z == 2


def test_selecting_every_face_of_one_feature(plate_with_pocket: TopologyIndex) -> None:
    assert len(FaceSelector.parse("slot/*").resolve(plate_with_pocket)) == 2


# --------------------------------------------------------------------------
# Fail loudly — the contract
# --------------------------------------------------------------------------


def test_a_missing_face_raises_rather_than_matching_nothing_silently(
    padded_plate: TopologyIndex,
) -> None:
    with pytest.raises(SelectorResolutionError) as excinfo:
        FaceSelector.parse("base/side[out.ghost]").resolve(padded_plate)
    assert excinfo.value.actual == 0


def test_a_retired_face_reports_the_feature_that_destroyed_it(
    padded_plate: TopologyIndex,
) -> None:
    """The diagnostic that replaces FreeCAD's silent re-binding."""
    topology = padded_plate.with_retired(
        [
            RetiredTag(
                tag=FaceTag.parse("base/side[out.left]"),
                reason="consumed",
                retired_by="slot_1",
            )
        ]
    )
    topology = TopologyIndex.build(
        faces=[f for f in topology.faces if str(f.tag) != "base/side[out.left]"],
        edges=topology.edges,
        retired=topology.retired,
    )

    with pytest.raises(SelectorResolutionError) as excinfo:
        FaceSelector.parse("base/side[out.left]").resolve(topology, feature="f1")

    error = excinfo.value
    assert error.feature == "f1"
    assert error.actual == 0
    assert any("slot_1" in reason for reason in error.reasons)
    assert "consumed" in str(error)


def test_a_changed_cardinality_raises_instead_of_rebinding(
    padded_plate: TopologyIndex,
) -> None:
    """Two faces now match where one did before — that must not be silently accepted."""
    selector = FaceSelector(
        include=(TagPattern.parse("base/side[*]"),),
        expect=Expectation(count=4),
    )
    reduced = TopologyIndex.build(
        faces=[f for f in padded_plate.faces if "side" not in str(f.tag)],
        edges=(),
    )
    with pytest.raises(SelectorResolutionError) as excinfo:
        selector.resolve(reduced, feature="fillet_1")
    assert excinfo.value.expected == 4
    assert excinfo.value.actual == 0


def test_ambiguity_is_reported_with_advice(padded_plate: TopologyIndex) -> None:
    selector = FaceSelector(
        include=(TagPattern.parse("base/side[*]"),), expect=Expectation(count=2)
    )
    with pytest.raises(SelectorResolutionError) as excinfo:
        selector.resolve(padded_plate)
    assert excinfo.value.actual == 4
    assert any("ambiguous" in r for r in excinfo.value.reasons)


def test_direction_filter_failure_is_explained(padded_plate: TopologyIndex) -> None:
    selector = FaceSelector(
        include=(TagPattern.parse("base/side[*]"),), direction=DirectionFilter.parse("+z")
    )
    with pytest.raises(SelectorResolutionError) as excinfo:
        selector.resolve(padded_plate)
    assert any("points" in r for r in excinfo.value.reasons)


# --------------------------------------------------------------------------
# Fingerprints arbitrate, but can never invent geometry
# --------------------------------------------------------------------------


def test_fingerprints_narrow_an_over_broad_match(padded_plate: TopologyIndex) -> None:
    left = padded_plate.face(FaceTag.parse("base/side[out.left]"))
    assert left is not None
    selector = FaceSelector(
        include=(TagPattern.parse("base/side[*]"),),
        expect=Expectation(count=1, fingerprints=(left.fingerprint,)),
    )
    resolved = selector.resolve(padded_plate)
    assert [str(e.tag) for e in resolved] == ["base/side[out.left]"]


def test_fingerprints_cannot_conjure_a_missing_face() -> None:
    """Arbitration only narrows. A vanished face still fails the rebuild."""
    empty = TopologyIndex.build(faces=[])
    selector = FaceSelector(
        include=(TagPattern.parse("base/cap+"),),
        expect=Expectation(
            count=1,
            fingerprints=(
                FaceFingerprint(SurfaceKind.PLANE, 100.0, Vec3(0, 0, 0), UP),
            ),
        ),
    )
    with pytest.raises(SelectorResolutionError):
        selector.resolve(empty)


def test_expectations_are_recorded_from_a_good_build(padded_plate: TopologyIndex) -> None:
    selector = FaceSelector.parse("base/side[*]")
    resolved = selector.resolve(padded_plate)
    updated = selector.with_expectation(resolved)
    assert updated.expect is not None
    assert updated.expect.count == 4
    assert len(updated.expect.fingerprints) == 4


# --------------------------------------------------------------------------
# Edge selection through named faces
# --------------------------------------------------------------------------


def test_top_perimeter_is_one_stable_query(padded_plate: TopologyIndex) -> None:
    """'The whole top perimeter' stated once, stable as the profile changes."""
    selector = EdgeSelector.between_patterns("base/cap+", "base/side[*]")
    assert len(selector.resolve(padded_plate)) == 4


def test_edge_selection_is_order_independent(padded_plate: TopologyIndex) -> None:
    forwards = EdgeSelector.between_patterns("base/cap+", "base/side[*]")
    backwards = EdgeSelector.between_patterns("base/side[*]", "base/cap+")
    assert len(forwards.resolve(padded_plate)) == len(backwards.resolve(padded_plate))


def test_edge_direction_filter_is_unsigned(padded_plate: TopologyIndex) -> None:
    """An edge has no inherent sense, so |z matches regardless of orientation."""
    selector = EdgeSelector(
        touching=(TagPattern.parse("base/side[*]"),),
        direction=DirectionFilter.parse("|z"),
    )
    resolved = selector.resolve(padded_plate)
    assert len(resolved) == 1
    assert resolved[0].fingerprint.length == 6


def test_missing_adjacent_face_explains_the_edge_failure(padded_plate: TopologyIndex) -> None:
    selector = EdgeSelector.between_patterns("base/cap+", "ghost/side[*]")
    with pytest.raises(SelectorResolutionError) as excinfo:
        selector.resolve(padded_plate, feature="f1")
    assert any("ghost" in r for r in excinfo.value.reasons)


# --------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------


def test_face_selector_round_trips(padded_plate: TopologyIndex) -> None:
    original = FaceSelector.parse("base/side[*]").with_expectation(
        FaceSelector.parse("base/side[*]").resolve(padded_plate)
    )
    restored = FaceSelector.from_dict(original.to_dict())
    assert restored == original
    assert len(restored.resolve(padded_plate)) == 4


def test_edge_selector_round_trips(padded_plate: TopologyIndex) -> None:
    original = EdgeSelector.between_patterns("base/cap+", "base/side[*]")
    restored = EdgeSelector.from_dict(original.to_dict())
    assert restored == original


def test_direction_filter_round_trips() -> None:
    for text in ["+x", "-x", "+y", "-y", "+z", "-z", "|x", "|z"]:
        original = DirectionFilter.parse(text)
        assert DirectionFilter.from_dict(original.to_dict()) == original
        assert str(original) == text
