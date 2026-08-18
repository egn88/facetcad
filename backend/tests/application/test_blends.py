"""Fillets and chamfers — what the whole naming design was built for.

A blend attaches to *edges*, and edges are where FreeCAD's index-based
references break down: change a dimension and the fillet reappears somewhere
else. Here an edge is named by its two adjacent faces, so a fillet is stated
once as a query and re-resolved on every rebuild.

The blend face itself is then named after the edge it replaced —
``round/fillet[base/cap+ ^ base/side[outline.left]]`` — which needs no new
naming concept at all.
"""

from __future__ import annotations

import copy
import math

import pytest

pytest.importorskip("OCP", reason="requires the optional OCCT extra")

from facet.adapters.geometry.occt import OcctKernel
from facet.application.recompute import FeatureStatus, recompute
from facet.domain.document import Document
from facet.domain.fingerprint import SurfaceKind
from facet.domain.selectors import EdgeSelector, FaceSelector
from facet.domain.tags import CornerTag, FaceTag

pytestmark = pytest.mark.occt

PLATE: dict[str, object] = {
    "schema": "cadsheet/1",
    "project": "blended plate",
    "parameters": [
        {"name": "w", "value": 80.0, "group": "Plate"},
        {"name": "h", "value": 60.0, "group": "Plate"},
        {"name": "t", "value": 12.0, "group": "Plate"},
        {"name": "rad", "value": 4.0, "group": "Edges"},
    ],
    "datums": {"base": {"type": "plane", "origin": [0, 0, 0], "normal": [0, 0, 1]}},
    "sketches": {
        "outline": {
            "plane": "base",
            "points": {"p0": [0, 0], "p1": ["w", 0], "p2": ["w", "h"], "p3": [0, "h"]},
            "curves": [
                {"id": "bottom", "start": "p0", "end": "p1"},
                {"id": "right", "start": "p1", "end": "p2"},
                {"id": "top", "start": "p2", "end": "p3"},
                {"id": "left", "start": "p3", "end": "p0"},
            ],
            "loops": [{"id": "outer", "curves": ["bottom", "right", "top", "left"]}],
        }
    },
    "features": [{"id": "base", "type": "pad", "profile": "outline.outer", "length": "t"}],
}


def plate_with(feature: dict, **overrides: object) -> Document:
    data = copy.deepcopy(PLATE)
    for name, value in overrides.items():
        for row in data["parameters"]:  # type: ignore[union-attr]
            if row["name"] == name:
                row["value"] = value
    data["features"].append(feature)  # type: ignore[union-attr]
    return Document.from_dict(data)


def vertical_fillet(**overrides: object) -> Document:
    """Round the four upright corners — the commonest blend on a plate."""
    return plate_with(
        {
            "id": "round",
            "type": "fillet",
            "radius": "rad",
            "edges": "base/side[*] ^ base/side[*]",
        },
        **overrides,
    )


@pytest.fixture
def kernel() -> OcctKernel:
    return OcctKernel()


# --------------------------------------------------------------------------
# Fillets
# --------------------------------------------------------------------------


def test_a_fillet_builds(kernel: OcctKernel) -> None:
    result = recompute(vertical_fillet(), kernel)
    assert result.ok, [o.error for o in result.failures()]


def test_a_blend_face_is_named_after_the_edge_it_replaced(kernel: OcctKernel) -> None:
    """The payoff of deriving edge identity from face identity."""
    result = recompute(vertical_fillet(), kernel)
    blends = [t for t in result.topology.face_tags() if t.role == "fillet"]
    assert len(blends) == 4
    rendered = sorted(str(t) for t in blends)
    assert rendered[0].startswith("round/fillet[base/side[outline.")
    assert " ^ " in rendered[0]


def test_blend_faces_are_cylindrical_and_correctly_sized(kernel: OcctKernel) -> None:
    result = recompute(vertical_fillet(), kernel)
    blends = FaceSelector.parse("round/fillet[*]").resolve(result.topology)
    assert len(blends) == 4
    for face in blends:
        assert face.fingerprint.surface == SurfaceKind.CYLINDER
        # A quarter-round of radius 4 over a 12mm thickness.
        assert face.fingerprint.area == pytest.approx(2 * math.pi * 4.0 / 4 * 12.0, rel=1e-3)


def test_the_filleted_solid_loses_the_right_volume(kernel: OcctKernel) -> None:
    result = recompute(vertical_fillet(), kernel)
    corner_waste = (4.0**2 - math.pi * 4.0**2 / 4) * 12.0
    expected = 80.0 * 60.0 * 12.0 - 4 * corner_waste
    assert kernel.volume(result.solid.handle) == pytest.approx(expected, rel=1e-4)


def test_the_faces_a_fillet_touched_keep_their_names(kernel: OcctKernel) -> None:
    """A blend shortens its neighbours; it must not rename them."""
    result = recompute(vertical_fillet(), kernel)
    tags = {str(t) for t in result.topology.face_tags()}
    for curve in ("bottom", "right", "top", "left"):
        assert f"base/side[outline.{curve}]" in tags
    assert "base/cap+" in tags and "base/cap-" in tags


def test_the_radius_follows_its_parameter(kernel: OcctKernel) -> None:
    small = recompute(vertical_fillet(rad=2.0), kernel)
    large = recompute(vertical_fillet(rad=9.0), OcctKernel())

    def blend_area(result) -> float:
        return FaceSelector.parse("round/fillet[*]").resolve(result.topology)[0].fingerprint.area

    assert blend_area(small) == pytest.approx(2 * math.pi * 2.0 / 4 * 12.0, rel=1e-3)
    assert blend_area(large) == pytest.approx(2 * math.pi * 9.0 / 4 * 12.0, rel=1e-3)


def test_a_top_perimeter_fillet_is_one_query(kernel: OcctKernel) -> None:
    """'The whole top edge', stated once and re-resolved on every rebuild."""
    document = plate_with(
        {
            "id": "soften",
            "type": "fillet",
            "radius": 2.0,
            "edges": "base/cap+ ^ base/side[*]",
        }
    )
    result = recompute(document, kernel)
    assert result.ok, [o.error for o in result.failures()]
    assert len(FaceSelector.parse("soften/fillet[*]").resolve(result.topology)) == 4


# --------------------------------------------------------------------------
# Chamfers
# --------------------------------------------------------------------------


def test_a_chamfer_builds_and_is_planar(kernel: OcctKernel) -> None:
    document = plate_with(
        {
            "id": "bevel",
            "type": "chamfer",
            "distance": 3.0,
            "edges": "base/cap+ ^ base/side[*]",
        }
    )
    result = recompute(document, kernel)
    assert result.ok, [o.error for o in result.failures()]

    bevels = FaceSelector.parse("bevel/chamfer[*]").resolve(result.topology)
    assert len(bevels) == 4
    assert all(f.fingerprint.surface == SurfaceKind.PLANE for f in bevels)


def test_a_chamfer_removes_the_expected_volume(kernel: OcctKernel) -> None:
    document = plate_with(
        {"id": "bevel", "type": "chamfer", "distance": 3.0, "edges": "base/cap+ ^ base/side[*]"}
    )
    result = recompute(document, kernel)
    # Four triangular prisms along the perimeter, less the corner overlaps.
    assert kernel.volume(result.solid.handle) < 80.0 * 60.0 * 12.0


# --------------------------------------------------------------------------
# The guarantee: a blend survives a parameter sweep
# --------------------------------------------------------------------------


def test_blend_names_survive_a_parameter_sweep() -> None:
    """The headline claim, on the operation that breaks FreeCAD."""
    reference: list[str] | None = None
    for step in range(10):
        document = vertical_fillet(
            w=60.0 + step * 7.0, h=45.0 + step * 5.0, t=8.0 + step * 0.8, rad=2.0 + step * 0.4
        )
        result = recompute(document, OcctKernel())
        assert result.ok, f"step {step}: {[o.error for o in result.failures()]}"

        tags = sorted(str(t) for t in result.topology.face_tags())
        if reference is None:
            reference = tags
        assert tags == reference, f"naming drifted at step {step}"
    assert reference is not None


def test_a_blend_selector_keeps_resolving_across_the_sweep() -> None:
    blends = FaceSelector.parse("round/fillet[*]")
    sides = FaceSelector.parse("base/side[*]")
    for step in range(10):
        document = vertical_fillet(w=60.0 + step * 7.0, rad=2.0 + step * 0.4)
        topology = recompute(document, OcctKernel()).topology
        assert len(blends.resolve(topology)) == 4
        assert len(sides.resolve(topology)) == 4


def test_a_fillet_can_be_stacked_on_a_chamfer(kernel: OcctKernel) -> None:
    """Each blend names its own faces, including edges the previous one made."""
    document = plate_with(
        {"id": "bevel", "type": "chamfer", "distance": 2.0, "edges": "base/cap+ ^ base/side[*]"}
    )
    document.add_feature(
        Document.from_dict(
            {
                "schema": "cadsheet/1",
                "features": [
                    {
                        "id": "round",
                        "type": "fillet",
                        "radius": 1.5,
                        "edges": "base/cap- ^ base/side[*]",
                    }
                ],
            }
        ).features[0]
    )
    result = recompute(document, kernel)
    assert result.ok, [o.error for o in result.failures()]
    tags = {str(t) for t in result.topology.face_tags()}
    assert any(t.startswith("bevel/chamfer[") for t in tags)
    assert any(t.startswith("round/fillet[") for t in tags)


# --------------------------------------------------------------------------
# Failure, which for blends is normal rather than exceptional
# --------------------------------------------------------------------------


def test_an_impossible_radius_fails_with_advice(kernel: OcctKernel) -> None:
    """A radius larger than the material cannot work; say so usefully."""
    result = recompute(vertical_fillet(rad=200.0), kernel)
    assert not result.ok
    message = str(result.failures()[0].error)
    assert "fillet" in message
    assert "smaller" in message


def test_on_failure_skip_lets_the_model_survive(kernel: OcctKernel) -> None:
    """Blend failure is kernel-bound, so a model may choose to tolerate it."""
    document = plate_with(
        {
            "id": "round",
            "type": "fillet",
            "radius": 500.0,
            "edges": "base/side[*] ^ base/side[*]",
            "on_failure": "skip",
        }
    )
    result = recompute(document, kernel)

    assert result.ok
    assert result.outcomes[1].status == FeatureStatus.BYPASSED
    # The plate is intact and unrounded.
    assert kernel.volume(result.solid.handle) == pytest.approx(80.0 * 60.0 * 12.0)
    assert "did not fit" in str(result.outcomes[1].error)


def test_a_bypassed_blend_is_still_reported(kernel: OcctKernel) -> None:
    """Tolerated is not the same as invisible."""
    document = plate_with(
        {
            "id": "round",
            "type": "fillet",
            "radius": 500.0,
            "edges": "base/side[*] ^ base/side[*]",
            "on_failure": "skip",
        }
    )
    body = recompute(document, kernel).to_dict()
    outcome = body["features"][1]  # type: ignore[index]
    assert outcome["status"] == "bypassed"
    assert outcome["error"] is not None


def test_a_selector_matching_nothing_is_refused(kernel: OcctKernel) -> None:
    document = plate_with(
        {"id": "round", "type": "fillet", "radius": 2.0, "edges": "ghost/cap+ ^ base/side[*]"}
    )
    result = recompute(document, kernel)
    assert not result.ok
    assert "ghost" in str(result.failures()[0].error)


def test_a_blend_without_edges_explains_the_syntax(kernel: OcctKernel) -> None:
    document = plate_with({"id": "round", "type": "fillet", "radius": 2.0})
    result = recompute(document, kernel)
    assert not result.ok
    assert "base/cap+ ^ base/side[*]" in str(result.failures()[0].error)


def test_a_blend_before_any_solid_is_refused(kernel: OcctKernel) -> None:
    data = copy.deepcopy(PLATE)
    data["features"] = [  # type: ignore[index]
        {"id": "round", "type": "fillet", "radius": 2.0, "edges": "a/cap+ ^ b/cap-"}
    ]
    result = recompute(Document.from_dict(data), kernel)
    assert not result.ok
    assert "add a pad" in str(result.failures()[0].error)


def test_a_non_positive_radius_is_refused(kernel: OcctKernel) -> None:
    result = recompute(vertical_fillet(rad=0.0), kernel)
    assert not result.ok
    assert "positive" in str(result.failures()[0].error)


# --------------------------------------------------------------------------
# Edge selector syntax
# --------------------------------------------------------------------------


def test_edge_selector_parses_the_between_form() -> None:
    selector = EdgeSelector.parse("base/cap+ ^ base/side[*]")
    assert selector.between is not None
    assert selector.between[0].matches(FaceTag.parse("base/cap+"))
    assert selector.between[1].matches(FaceTag.parse("base/side[outline.left]"))


def test_edge_selector_parses_a_single_pattern_as_touching() -> None:
    selector = EdgeSelector.parse("base/side[*]")
    assert selector.between is None
    assert len(selector.touching) == 1


def test_edge_selector_parses_a_direction_filter() -> None:
    selector = EdgeSelector.parse("base/side[*] dir=|z")
    assert selector.direction is not None
    assert str(selector.direction) == "|z"


def test_a_direction_filter_narrows_a_blend(kernel: OcctKernel) -> None:
    """Only the upright corners, not the top and bottom perimeters."""
    document = plate_with(
        {
            "id": "round",
            "type": "fillet",
            "radius": 3.0,
            "edges": "base/side[*] dir=|z",
        }
    )
    result = recompute(document, kernel)
    assert result.ok, [o.error for o in result.failures()]
    assert len(FaceSelector.parse("round/fillet[*]").resolve(result.topology)) == 4


def test_a_partial_blend_at_a_corner_is_named_from_its_neighbours(
    kernel: OcctKernel,
) -> None:
    """Blending some but not all edges meeting at a corner still names cleanly.

    OCCT builds a transition patch there and attributes it to a vertex, so it
    has no two-face name. It is named instead by the faces that bound it — the
    edge construction one arity up.
    """
    document = plate_with(
        {"id": "round", "type": "fillet", "radius": 6.0, "edges": "base/side[*] dir=|z"}
    )
    document.add_feature(
        Document.from_dict(
            {
                "schema": "cadsheet/1",
                "features": [
                    {
                        "id": "soften",
                        "type": "chamfer",
                        "distance": 2.0,
                        # Only the four flat sides — misses the rounded corners.
                        "edges": "base/cap+ ^ base/side[*]",
                    }
                ],
            }
        ).features[0]
    )
    result = recompute(document, kernel)
    assert result.ok, [str(o.error) for o in result.failures()]

    corners = FaceSelector.parse("soften/corner[*]").resolve(result.topology)
    assert corners, "the transition patches should be named, not dropped"
    for entry in corners:
        source = entry.tag.source
        assert isinstance(source, CornerTag)
        assert len(source.faces) >= 3


def test_blending_the_whole_run_of_edges_succeeds(kernel: OcctKernel) -> None:
    """Covering the whole run needs no transition patches at all."""
    document = plate_with(
        {"id": "round", "type": "fillet", "radius": 6.0, "edges": "base/side[*] dir=|z"}
    )
    document.add_feature(
        Document.from_dict(
            {
                "schema": "cadsheet/1",
                "features": [
                    {
                        "id": "soften",
                        "type": "chamfer",
                        "distance": 2.0,
                        "edges": "base/cap+ ^ */*",
                    }
                ],
            }
        ).features[0]
    )
    result = recompute(document, kernel)
    assert result.ok, [o.error for o in result.failures()]
    assert len(FaceSelector.parse("soften/chamfer[*]").resolve(result.topology)) == 8


# --------------------------------------------------------------------------
# Blends across bodies
#
# Bodies never see each other's solids, so a blend can only name edges its own
# body made. That is the design; what made it expensive was the message, which
# said the selector resolved to nothing and left the reader to discover that the
# face exists, is spelled correctly, and is simply in another part.
# --------------------------------------------------------------------------


def two_bodies_with(edges: str) -> Document:
    """A plate and a post, with a fillet in the plate naming `edges`."""
    data = copy.deepcopy(PLATE)
    data["bodies"] = [
        {"id": "plate", "features": list(data.pop("features"))},  # type: ignore[arg-type]
        {
            "id": "post",
            "features": [
                {"id": "stud", "type": "pad", "profile": "outline.outer", "length": "rad"}
            ],
        },
    ]
    data["bodies"][0]["features"].append(  # type: ignore[index]
        {"id": "round", "type": "fillet", "radius": "rad", "edges": edges}
    )
    return Document.from_dict(data)


def test_a_blend_naming_another_body_says_which_body_has_those_faces(
    kernel: OcctKernel,
) -> None:
    """The tag is right and the face exists — in the other part.

    Every other reading of "resolved to nothing" points at the selector, so
    without this the next move is to rewrite something that was already correct.
    """
    result = recompute(two_bodies_with("stud/cap+ ^ stud/side[*]"), kernel)

    failure = next(o for o in result.outcomes if o.status == FeatureStatus.FAILED)
    reason = str(failure.error)
    assert "'stud'" in reason
    assert "body 'post'" in reason
    assert "resolves only within its own body" in reason


def test_a_blend_naming_its_own_body_is_not_second_guessed(kernel: OcctKernel) -> None:
    """The note belongs only where it explains something.

    A selector that fails inside its own body has an ordinary cause, and adding
    a body to that message would send the reader looking for a part that has
    nothing to do with it.
    """
    result = recompute(two_bodies_with("base/cap+ ^ ghost/side[*]"), kernel)

    failure = next(o for o in result.outcomes if o.status == FeatureStatus.FAILED)
    assert "own body" not in str(failure.error)
