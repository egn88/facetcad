"""The hole feature, built end to end.

Requires OCCT, since a hole is circular and the analytic kernel refuses curves.

What distinguishes a hole from a circular pocket is placement: you name a point
and a size, and it generates its own profile. The resulting faces are tagged
against that point, so a bore reads ``bolt/wall[plate.h1]`` — the same shape of
name as every other face in the system.
"""

from __future__ import annotations

import copy
import math

import pytest

pytest.importorskip("OCP", reason="requires the optional OCCT extra")

from facet.adapters.geometry.occt import OcctKernel
from facet.application.recompute import recompute
from facet.domain.document import Document
from facet.domain.fingerprint import SurfaceKind
from facet.domain.selectors import EdgeSelector, FaceSelector

pytestmark = pytest.mark.occt

PLATE: dict[str, object] = {
    "schema": "cadsheet/1",
    "project": "drilled plate",
    "parameters": [
        {"name": "w", "value": 80.0, "group": "Plate"},
        {"name": "h", "value": 60.0, "group": "Plate"},
        {"name": "t", "value": 10.0, "group": "Plate"},
        {"name": "bore", "value": 8.0, "group": "Hole"},
        {"name": "drill_depth", "value": 6.0, "group": "Hole"},
    ],
    "datums": {
        "base": {"type": "plane", "origin": [0, 0, 0], "normal": [0, 0, 1]},
        "top": {"type": "plane", "origin": [0, 0, "t"], "normal": [0, 0, 1]},
    },
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
        },
        "holes": {
            "plane": "top",
            "points": {"h1": ["w / 2", "h / 2"], "h2": ["w / 4", "h / 4"]},
            "curves": [],
            "loops": [],
        },
    },
    "features": [
        {"id": "plate", "type": "pad", "profile": "outline.outer", "length": "t"},
        {
            "id": "bolt",
            "type": "hole",
            "at": "holes.h1",
            "diameter": "bore",
            "depth": "drill_depth",
        },
    ],
}


def plate(**overrides: object) -> Document:
    """The plate document, with feature options or parameters overridden."""
    data = copy.deepcopy(PLATE)
    for name, value in overrides.items():
        if any(row["name"] == name for row in data["parameters"]):  # type: ignore[union-attr]
            for row in data["parameters"]:  # type: ignore[union-attr]
                if row["name"] == name:
                    row["value"] = value
        else:
            data["features"][1][name] = value  # type: ignore[index]
    return Document.from_dict(data)


@pytest.fixture
def kernel() -> OcctKernel:
    return OcctKernel()


# --------------------------------------------------------------------------
# Placement and naming
# --------------------------------------------------------------------------


def test_a_hole_builds_from_a_point_and_a_diameter(kernel: OcctKernel) -> None:
    result = recompute(plate(), kernel)
    assert result.ok, [o.error for o in result.failures()]


def test_hole_faces_are_tagged_against_their_placement_point(kernel: OcctKernel) -> None:
    """No sketch loop involved — the point is the root of the name."""
    result = recompute(plate(), kernel)
    tags = sorted(str(t) for t in result.topology.face_tags())
    assert "bolt/wall[holes.h1]" in tags
    assert "bolt/floor" in tags


def test_the_bore_is_cylindrical_and_correctly_sized(kernel: OcctKernel) -> None:
    result = recompute(plate(), kernel)
    wall = FaceSelector.parse("bolt/wall[holes.h1]").resolve(result.topology)[0]
    assert wall.fingerprint.surface == SurfaceKind.CYLINDER
    # A blind bore of diameter 8 drilled 6 deep.
    assert wall.fingerprint.area == pytest.approx(math.pi * 8.0 * 6.0, rel=1e-3)


def test_a_blind_hole_leaves_a_flat_floor(kernel: OcctKernel) -> None:
    result = recompute(plate(), kernel)
    floor = FaceSelector.parse("bolt/floor").resolve(result.topology)[0]
    assert floor.fingerprint.surface == SurfaceKind.PLANE
    assert floor.fingerprint.area == pytest.approx(math.pi * 4.0**2, rel=1e-3)
    assert floor.fingerprint.centroid.z == pytest.approx(4.0)  # 10mm plate, 6mm deep


def test_the_volume_matches_the_drilled_material(kernel: OcctKernel) -> None:
    result = recompute(plate(), kernel)
    expected = 80.0 * 60.0 * 10.0 - math.pi * 4.0**2 * 6.0
    assert kernel.volume(result.solid.handle) == pytest.approx(expected, rel=1e-4)


def test_the_hole_mouth_is_one_stable_edge_query(kernel: OcctKernel) -> None:
    """What a fillet or chamfer would attach to."""
    result = recompute(plate(), kernel)
    mouth = EdgeSelector.between_patterns("plate/cap+", "bolt/wall[*]")
    assert len(mouth.resolve(result.topology)) == 1


def test_a_through_hole_has_no_floor(kernel: OcctKernel) -> None:
    result = recompute(plate(through_all=True), kernel)
    assert result.ok
    tags = [str(t) for t in result.topology.face_tags()]
    assert "bolt/wall[holes.h1]" in tags
    assert "bolt/floor" not in tags


def test_two_holes_at_different_points_get_different_tags(kernel: OcctKernel) -> None:
    data = copy.deepcopy(PLATE)
    data["features"].append(  # type: ignore[union-attr]
        {"id": "dowel", "type": "hole", "at": "holes.h2", "diameter": 5.0, "depth": 4.0}
    )
    result = recompute(Document.from_dict(data), kernel)
    assert result.ok, [o.error for o in result.failures()]
    tags = sorted(str(t) for t in result.topology.face_tags())
    assert "bolt/wall[holes.h1]" in tags
    assert "dowel/wall[holes.h2]" in tags


# --------------------------------------------------------------------------
# Standard fastener sizes
# --------------------------------------------------------------------------


def test_a_standard_size_drills_the_right_clearance_hole(kernel: OcctKernel) -> None:
    """The point of naming M6 instead of typing 6.6."""
    data = copy.deepcopy(PLATE)
    data["features"][1] = {  # type: ignore[index]
        "id": "bolt", "type": "hole", "at": "holes.h1",
        "standard": "M6", "depth": "drill_depth",
    }
    result = recompute(Document.from_dict(data), kernel)
    assert result.ok, [o.error for o in result.failures()]
    wall = FaceSelector.parse("bolt/wall[holes.h1]").resolve(result.topology)[0]
    assert wall.fingerprint.area == pytest.approx(math.pi * 6.6 * 6.0, rel=1e-3)


def test_fit_changes_the_drilled_size(kernel: OcctKernel) -> None:
    def bore_area(fit: str) -> float:
        data = copy.deepcopy(PLATE)
        data["features"][1] = {  # type: ignore[index]
            "id": "bolt", "type": "hole", "at": "holes.h1",
            "standard": "M6", "fit": fit, "depth": "drill_depth",
        }
        result = recompute(Document.from_dict(data), OcctKernel())
        assert result.ok, [o.error for o in result.failures()]
        return FaceSelector.parse("bolt/wall[holes.h1]").resolve(
            result.topology
        )[0].fingerprint.area

    assert bore_area("close") == pytest.approx(math.pi * 6.4 * 6.0, rel=1e-3)
    assert bore_area("normal") == pytest.approx(math.pi * 6.6 * 6.0, rel=1e-3)
    assert bore_area("tapped") == pytest.approx(math.pi * 5.0 * 6.0, rel=1e-3)


def test_giving_both_a_standard_and_a_diameter_is_refused(kernel: OcctKernel) -> None:
    result = recompute(plate(standard="M6"), kernel)
    assert not result.ok
    assert "one or the other" in str(result.failures()[0].error)


def test_an_unknown_standard_is_refused_with_the_known_ones(kernel: OcctKernel) -> None:
    data = copy.deepcopy(PLATE)
    data["features"][1] = {  # type: ignore[index]
        "id": "bolt", "type": "hole", "at": "holes.h1", "standard": "M7", "depth": 5.0,
    }
    result = recompute(Document.from_dict(data), kernel)
    assert not result.ok
    assert "M6" in str(result.failures()[0].error)


# --------------------------------------------------------------------------
# Counterbores — one feature, two kernel steps
# --------------------------------------------------------------------------


def counterbored(**extra: object) -> Document:
    data = copy.deepcopy(PLATE)
    data["features"][1] = {  # type: ignore[index]
        "id": "bolt",
        "type": "hole",
        "at": "holes.h1",
        "diameter": 6.6,
        "through_all": True,
        "counterbore_diameter": 11.0,
        "counterbore_depth": 4.0,
        **extra,
    }
    return Document.from_dict(data)


def test_a_counterbore_adds_a_wall_and_a_shoulder(kernel: OcctKernel) -> None:
    result = recompute(counterbored(), kernel)
    assert result.ok, [o.error for o in result.failures()]
    tags = sorted(str(t) for t in result.topology.face_tags())
    assert "bolt/wall[holes.h1]" in tags
    assert "bolt/cbore[holes.h1]" in tags
    assert "bolt/cbore_floor" in tags


def test_the_counterbore_shoulder_is_an_annulus(kernel: OcctKernel) -> None:
    """The ring a fastener head seats against."""
    result = recompute(counterbored(), kernel)
    shoulder = FaceSelector.parse("bolt/cbore_floor").resolve(result.topology)[0]
    expected = math.pi * (5.5**2 - 3.3**2)
    assert shoulder.fingerprint.area == pytest.approx(expected, rel=1e-3)
    assert shoulder.fingerprint.centroid.z == pytest.approx(6.0)  # 10mm plate, 4mm cbore


def test_the_bore_below_a_counterbore_keeps_its_own_name(kernel: OcctKernel) -> None:
    """The second cut shortens the bore wall; it must not rename it."""
    result = recompute(counterbored(), kernel)
    wall = FaceSelector.parse("bolt/wall[holes.h1]").resolve(result.topology)[0]
    # 10mm plate less the 4mm counterbore leaves 6mm of bore.
    assert wall.fingerprint.area == pytest.approx(math.pi * 6.6 * 6.0, rel=1e-2)


def test_the_counterbore_volume_is_correct(kernel: OcctKernel) -> None:
    result = recompute(counterbored(), kernel)
    expected = (
        80.0 * 60.0 * 10.0
        - math.pi * 3.3**2 * 10.0        # the through bore
        - math.pi * (5.5**2 - 3.3**2) * 4.0  # the counterbore ring
    )
    assert kernel.volume(result.solid.handle) == pytest.approx(expected, rel=1e-3)


def test_a_counterbore_narrower_than_its_bore_is_refused(kernel: OcctKernel) -> None:
    result = recompute(counterbored(counterbore_diameter=4.0), kernel)
    assert not result.ok
    assert "wider than the hole" in str(result.failures()[0].error)


def test_a_counterbore_deeper_than_a_blind_hole_is_refused(kernel: OcctKernel) -> None:
    result = recompute(
        counterbored(through_all=False, depth=3.0, counterbore_depth=5.0), kernel
    )
    assert not result.ok
    assert "swallow the bore" in str(result.failures()[0].error)


def test_half_a_counterbore_specification_is_refused(kernel: OcctKernel) -> None:
    data = copy.deepcopy(PLATE)
    data["features"][1] = {  # type: ignore[index]
        "id": "bolt", "type": "hole", "at": "holes.h1",
        "diameter": 6.0, "depth": 5.0, "counterbore_diameter": 11.0,
    }
    result = recompute(Document.from_dict(data), kernel)
    assert not result.ok
    assert "both" in str(result.failures()[0].error)


# --------------------------------------------------------------------------
# Bad placement is reported clearly
# --------------------------------------------------------------------------


def test_a_hole_without_material_is_refused(kernel: OcctKernel) -> None:
    data = copy.deepcopy(PLATE)
    data["features"] = [data["features"][1]]  # type: ignore[index]
    result = recompute(Document.from_dict(data), kernel)
    assert not result.ok
    assert "add a pad" in str(result.failures()[0].error)


def test_an_unknown_point_lists_the_available_ones(kernel: OcctKernel) -> None:
    result = recompute(plate(at="holes.nope"), kernel)
    assert not result.ok
    message = str(result.failures()[0].error)
    assert "h1" in message and "h2" in message


def test_a_malformed_placement_is_refused(kernel: OcctKernel) -> None:
    result = recompute(plate(at="h1"), kernel)
    assert not result.ok
    assert "sketch.point" in str(result.failures()[0].error)


def test_a_hole_missing_its_size_explains_the_options(kernel: OcctKernel) -> None:
    data = copy.deepcopy(PLATE)
    data["features"][1] = {  # type: ignore[index]
        "id": "bolt", "type": "hole", "at": "holes.h1", "depth": 5.0,
    }
    result = recompute(Document.from_dict(data), kernel)
    assert not result.ok
    message = str(result.failures()[0].error)
    assert "diameter" in message and "M6" in message


def test_a_negative_depth_is_refused(kernel: OcctKernel) -> None:
    result = recompute(plate(drill_depth=-2.0), kernel)
    assert not result.ok
    assert "through_all" in str(result.failures()[0].error)


# --------------------------------------------------------------------------
# The naming guarantee
# --------------------------------------------------------------------------


def test_hole_names_survive_a_parameter_sweep() -> None:
    reference: list[str] | None = None
    for step in range(10):
        document = plate(
            w=60.0 + step * 6.0,
            h=40.0 + step * 4.0,
            t=8.0 + step * 0.6,
            bore=4.0 + step * 0.5,
            drill_depth=3.0 + step * 0.3,
        )
        result = recompute(document, OcctKernel())
        assert result.ok, f"step {step}: {[o.error for o in result.failures()]}"

        tags = sorted(str(t) for t in result.topology.face_tags())
        if reference is None:
            reference = tags
        assert tags == reference, f"naming drifted at step {step}"
    assert reference is not None


def test_counterbore_names_survive_a_sweep() -> None:
    """Two kernel steps in one feature must stay stable together."""
    wall = FaceSelector.parse("bolt/wall[holes.h1]")
    cbore = FaceSelector.parse("bolt/cbore[holes.h1]")
    shoulder = FaceSelector.parse("bolt/cbore_floor")

    for step in range(8):
        document = counterbored(
            counterbore_diameter=10.0 + step * 0.8,
            counterbore_depth=2.0 + step * 0.4,
        )
        result = recompute(document, OcctKernel())
        assert result.ok, f"step {step}: {[o.error for o in result.failures()]}"
        topology = result.topology
        assert len(wall.resolve(topology)) == 1
        assert len(cbore.resolve(topology)) == 1
        assert len(shoulder.resolve(topology)) == 1
