"""Fillet and chamfer meeting on the same face.

This is the case FreeCAD handles by producing geometry with unstable references:
round one edge of a face, then chamfer the rest, and the transition patches at
the corners belong to no single edge. Here they are named by the faces that
bound them, and that name has to survive a parameter sweep like every other.
"""

from __future__ import annotations

import copy

import pytest

pytest.importorskip("OCP", reason="requires the optional OCCT extra")

from facet.adapters.geometry.occt import OcctKernel
from facet.application.recompute import recompute
from facet.domain.document import Document
from facet.domain.selectors import FaceSelector
from facet.domain.tags import CornerTag

pytestmark = pytest.mark.occt

#: A triangle rather than a rectangle: three unequal corners, so a wrong
#: canonical order shows up as drift instead of cancelling out by symmetry.
TRIANGLE: dict[str, object] = {
    "schema": "cadsheet/1",
    "project": "mixed blends",
    "parameters": [
        {"name": "w", "value": 60.0},
        {"name": "h", "value": 40.0},
        {"name": "t", "value": 10.0},
        {"name": "rad", "value": 2.0},
    ],
    "datums": {"base": {"type": "plane", "origin": [0, 0, 0], "normal": [0, 0, 1]}},
    "sketches": {
        "plan": {
            "plane": "base",
            "points": {"p0": [0, 0], "p1": ["w", 0], "p2": [0, "h"]},
            "curves": [
                {"id": "c0", "start": "p0", "end": "p1"},
                {"id": "c1", "start": "p1", "end": "p2"},
                {"id": "c2", "start": "p2", "end": "p0"},
            ],
            "loops": [{"id": "outer", "curves": ["c0", "c1", "c2"]}],
        }
    },
    "features": [{"id": "wedge", "type": "pad", "profile": "plan.outer", "length": "t"}],
}

MIXED = [
    {
        "id": "soft",
        "type": "fillet",
        "radius": "rad",
        "edges": "wedge/side[plan.c0] ^ wedge/cap+",
    },
    {"id": "bevel", "type": "chamfer", "distance": 1.0, "edges": "wedge/cap+ ^ */*"},
]


def build(**values: float) -> object:
    data = copy.deepcopy(TRIANGLE)
    for row in data["parameters"]:  # type: ignore[index]
        if row["name"] in values:
            row["value"] = values[row["name"]]
    data["features"].extend(copy.deepcopy(MIXED))  # type: ignore[attr-defined]
    return recompute(Document.from_dict(data), OcctKernel())


@pytest.fixture(scope="module")
def baseline() -> object:
    return build()


def test_a_fillet_and_a_chamfer_can_share_a_face(baseline) -> None:
    """The operation FreeCAD does by guessing, done by naming instead."""
    assert baseline.ok, [str(o.error) for o in baseline.failures()]


def test_the_transition_patches_are_named_by_what_bounds_them(baseline) -> None:
    corners = FaceSelector.parse("bevel/corner[*]").resolve(baseline.topology)
    assert corners, "the corner patches must be named, not silently dropped"
    for entry in corners:
        source = entry.tag.source
        assert isinstance(source, CornerTag)
        assert len(source.faces) >= 3


def test_a_corner_names_the_blend_faces_it_meets(baseline) -> None:
    """A corner between a chamfer and a fillet says so, in both their names."""
    text = [str(e.tag) for e in FaceSelector.parse("bevel/corner[*]").resolve(baseline.topology)]
    assert any("soft/fillet[" in t and "bevel/chamfer[" in t for t in text)


@pytest.mark.parametrize(
    "values",
    [
        {"w": 65.0},
        {"h": 55.0},
        {"t": 14.0},
        {"rad": 3.5},
        {"w": 63.0, "h": 47.0, "t": 11.0, "rad": 2.5},
    ],
    ids=["wider", "taller", "thicker", "bigger radius", "all four"],
)
def test_every_tag_survives_a_parameter_sweep(baseline, values: dict[str, float]) -> None:
    """The whole point: a dimension change must not move a single name."""
    before = sorted(str(f.tag) for f in baseline.topology.faces)
    result = build(**values)
    assert result.ok, [str(o.error) for o in result.failures()]
    assert sorted(str(f.tag) for f in result.topology.faces) == before


def test_rebuilding_the_same_document_is_bit_identical(baseline) -> None:
    again = build()
    assert [str(f.tag) for f in again.topology.faces] == [
        str(f.tag) for f in baseline.topology.faces
    ]


def test_a_corner_tag_can_be_selected_and_round_trips(baseline) -> None:
    """A corner is a first-class target, not an internal artefact."""
    for entry in FaceSelector.parse("*/corner[*]").resolve(baseline.topology):
        exact = FaceSelector.parse(str(entry.tag)).resolve(baseline.topology)
        assert [e.tag for e in exact] == [entry.tag]


# -- naming several edges at once ------------------------------------------


def test_a_union_of_edge_pairs_selects_both() -> None:
    """`^` binds tighter than the comma, so "these and those" is sayable.

    Without this an edge selector could only ever name one pair of face
    patterns, and picking two specific edges was impossible to write.
    """
    from facet.domain.selectors import EdgeSelector

    selector = EdgeSelector.parse("a/cap+ ^ a/side[s.c1], a/cap+ ^ a/side[s.c2]")
    assert len(selector.alternatives) == 2
    assert all(alt.between is not None for alt in selector.alternatives)


def test_a_union_round_trips_through_its_stored_form() -> None:
    from facet.domain.selectors import EdgeSelector

    selector = EdgeSelector.parse("a/cap+ ^ a/side[s.c1], a/cap+ ^ a/side[s.c2]")
    assert EdgeSelector.from_dict(selector.to_dict()) == selector


def test_a_single_pattern_is_unchanged_by_the_union_rule() -> None:
    from facet.domain.selectors import EdgeSelector

    selector = EdgeSelector.parse("a/side[*]")
    assert not selector.alternatives
    assert len(selector.touching) == 1


def test_a_union_of_two_edges_resolves_to_exactly_those_two(baseline) -> None:
    from facet.domain.selectors import EdgeSelector

    both = EdgeSelector.parse(
        "wedge/cap+ ^ wedge/side[plan.c1], wedge/cap+ ^ wedge/side[plan.c2]"
    )
    # The baseline is already blended, so resolve against a plain pad instead.
    plain = recompute(Document.from_dict(copy.deepcopy(TRIANGLE)), OcctKernel())
    assert len(both.candidates(plain.topology)) == 2


def test_blending_two_named_edges_leaves_the_third_alone() -> None:
    """The workaround for a chamfer that would otherwise meet a fillet."""
    data = copy.deepcopy(TRIANGLE)
    data["features"].extend(  # type: ignore[attr-defined]
        [
            {
                "id": "bevel",
                "type": "chamfer",
                "distance": 1.0,
                "edges": "wedge/cap+ ^ wedge/side[plan.c1], wedge/cap+ ^ wedge/side[plan.c2]",
            },
            {
                "id": "soft",
                "type": "fillet",
                "radius": "rad",
                "edges": "wedge/cap+ ^ wedge/side[plan.c0]",
            },
        ]
    )
    result = recompute(Document.from_dict(data), OcctKernel())
    assert result.ok, [str(o.error) for o in result.failures()]

    # Blending in this order needs no transition patches at all, and nothing
    # reaches down the upright edges — see the ordering note in DESIGN.md.
    tags = [str(f.tag) for f in result.topology.faces]
    assert not [t for t in tags if "/corner[" in t]
    assert len(tags) == 8
