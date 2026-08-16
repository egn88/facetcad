"""End-to-end: a YAML-shaped document in, a named solid out.

This is the vertical slice. It drives the real parameter sheet, real datums,
real sketches and the real feature registry through the analytic kernel, and
asserts the properties the whole design exists to deliver.
"""

from __future__ import annotations

import copy

import pytest

from facet.adapters.geometry.fake import FakeKernel
from facet.application.recompute import (
    FeatureStatus,
    RecomputeEngine,
    dirty_features,
    recompute,
)
from facet.domain.document import Document
from facet.domain.errors import DocumentError
from facet.domain.parameters import Parameter
from facet.domain.selectors import EdgeSelector, FaceSelector

BRACKET: dict[str, object] = {
    "schema": "cadsheet/1",
    "project": "bracket",
    "parameters": [
        {"name": "plate_w", "value": 120.0, "group": "Plate"},
        {"name": "plate_h", "expr": "plate_w * 0.6", "group": "Plate"},
        {"name": "plate_t", "value": 6.0, "group": "Plate"},
        {"name": "slot_w", "value": 20.0, "group": "Slot"},
        {"name": "slot_d", "value": 2.0, "group": "Slot"},
    ],
    "datums": {
        "base": {"type": "plane", "origin": [0, 0, 0], "normal": [0, 0, 1]},
        "top": {"type": "plane", "origin": [0, 0, "plate_t"], "normal": [0, 0, 1]},
    },
    "sketches": {
        "outline": {
            "plane": "base",
            "points": {
                "p0": [0, 0],
                "p1": ["plate_w", 0],
                "p2": ["plate_w", "plate_h"],
                "p3": [0, "plate_h"],
            },
            "curves": [
                {"id": "bottom", "start": "p0", "end": "p1"},
                {"id": "right", "start": "p1", "end": "p2"},
                {"id": "top", "start": "p2", "end": "p3"},
                {"id": "left", "start": "p3", "end": "p0"},
            ],
            "loops": [{"id": "outer", "curves": ["bottom", "right", "top", "left"]}],
        },
        "hole": {
            "plane": "top",
            "points": {
                "q0": ["plate_w / 2 - slot_w / 2", "plate_h / 2 - slot_w / 2"],
                "q1": ["plate_w / 2 + slot_w / 2", "plate_h / 2 - slot_w / 2"],
                "q2": ["plate_w / 2 + slot_w / 2", "plate_h / 2 + slot_w / 2"],
                "q3": ["plate_w / 2 - slot_w / 2", "plate_h / 2 + slot_w / 2"],
            },
            "curves": [
                {"id": "c0", "start": "q0", "end": "q1"},
                {"id": "c1", "start": "q1", "end": "q2"},
                {"id": "c2", "start": "q2", "end": "q3"},
                {"id": "c3", "start": "q3", "end": "q0"},
            ],
            "loops": [{"id": "outer", "curves": ["c0", "c1", "c2", "c3"]}],
        },
    },
    "features": [
        {"id": "base", "type": "pad", "profile": "outline.outer", "length": "plate_t"},
        {
            "id": "slot",
            "type": "pocket",
            "profile": "hole.outer",
            "depth": "slot_d",
            "direction": "-normal",
        },
    ],
}


def bracket() -> Document:
    return Document.from_dict(BRACKET)


@pytest.fixture
def kernel() -> FakeKernel:
    return FakeKernel()


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_the_document_rebuilds(kernel: FakeKernel) -> None:
    result = recompute(bracket(), kernel)
    assert result.ok, [o.error for o in result.failures()]
    assert [o.status for o in result.outcomes] == [FeatureStatus.BUILT, FeatureStatus.BUILT]


def test_expressions_drive_the_geometry(kernel: FakeKernel) -> None:
    result = recompute(bracket(), kernel)
    assert result.parameters is not None
    assert result.parameters["plate_h"] == pytest.approx(72.0)
    assert kernel.volume(result.solid.handle) == pytest.approx(120 * 72 * 6 - 20 * 20 * 2)


def test_every_face_is_named(kernel: FakeKernel) -> None:
    result = recompute(bracket(), kernel)
    assert sorted(str(t) for t in result.topology.face_tags()) == [
        "base/cap+",
        "base/cap-",
        "base/side[outline.bottom]",
        "base/side[outline.left]",
        "base/side[outline.right]",
        "base/side[outline.top]",
        "slot/floor",
        "slot/wall[hole.c0]",
        "slot/wall[hole.c1]",
        "slot/wall[hole.c2]",
        "slot/wall[hole.c3]",
    ]


def test_a_datum_expressed_in_parameters_places_the_pocket(kernel: FakeKernel) -> None:
    """The 'top' datum sits at z=plate_t, so the pocket starts at the top face."""
    result = recompute(bracket(), kernel)
    floor = FaceSelector.parse("slot/floor").resolve(result.topology)
    assert floor[0].fingerprint.centroid.z == pytest.approx(4.0)


def test_selectors_resolve_against_the_rebuilt_model(kernel: FakeKernel) -> None:
    result = recompute(bracket(), kernel)
    assert len(FaceSelector.parse("slot/wall[*]").resolve(result.topology)) == 4
    mouth = EdgeSelector.between_patterns("base/cap+", "slot/wall[*]")
    assert len(mouth.resolve(result.topology)) == 4


# --------------------------------------------------------------------------
# Parameter changes drive recalculation
# --------------------------------------------------------------------------


def test_changing_a_parameter_changes_the_geometry(kernel: FakeKernel) -> None:
    document = bracket()
    document.set_parameter("plate_w", value=200.0)
    result = recompute(document, kernel)
    assert result.ok
    assert result.parameters is not None
    assert result.parameters["plate_h"] == pytest.approx(120.0)
    assert kernel.volume(result.solid.handle) == pytest.approx(200 * 120 * 6 - 20 * 20 * 2)


def test_names_are_unchanged_by_a_parameter_change(kernel: FakeKernel) -> None:
    before = recompute(bracket(), kernel)
    document = bracket()
    document.set_parameter("plate_w", value=250.0)
    document.set_parameter("plate_t", value=9.0)
    after = recompute(document, FakeKernel())

    assert sorted(str(t) for t in after.topology.face_tags()) == sorted(
        str(t) for t in before.topology.face_tags()
    )


def test_a_full_sweep_keeps_every_selector_resolving() -> None:
    """The headline guarantee, exercised through the real document pipeline."""
    wall = FaceSelector.parse("slot/wall[hole.c0]")
    floor = FaceSelector.parse("slot/floor")
    mouth = EdgeSelector.between_patterns("base/cap+", "slot/wall[*]")

    for step in range(20):
        document = bracket()
        document.set_parameter("plate_w", value=90.0 + step * 11.0)
        document.set_parameter("plate_t", value=4.0 + step * 0.4)
        document.set_parameter("slot_w", value=10.0 + step * 1.5)

        result = recompute(document, FakeKernel())
        assert result.ok, f"step {step}: {[o.error for o in result.failures()]}"
        assert len(wall.resolve(result.topology)) == 1
        assert len(floor.resolve(result.topology)) == 1
        assert len(mouth.resolve(result.topology)) == 4


def test_dirty_analysis_reports_the_affected_features() -> None:
    document = bracket()
    assert dirty_features(document, "plate_w") == ["base", "slot"]
    assert dirty_features(document, "slot_d") == ["slot"]


def test_an_unused_parameter_dirties_nothing() -> None:
    document = bracket()
    document.parameters.add(Parameter("spare", value=1.0))
    assert dirty_features(document, "spare") == []


# --------------------------------------------------------------------------
# Caching
# --------------------------------------------------------------------------


def test_an_unchanged_rebuild_is_served_from_cache(kernel: FakeKernel) -> None:
    engine = RecomputeEngine(kernel)
    document = bracket()
    engine.recompute(document)
    second = engine.recompute(document)
    assert [o.status for o in second.outcomes] == [FeatureStatus.CACHED, FeatureStatus.CACHED]


def test_editing_the_last_feature_rebuilds_only_that_feature(kernel: FakeKernel) -> None:
    engine = RecomputeEngine(kernel)
    document = bracket()
    engine.recompute(document)

    document.set_parameter("slot_d", value=3.0)
    result = engine.recompute(document)
    assert [o.status for o in result.outcomes] == [FeatureStatus.CACHED, FeatureStatus.BUILT]


def test_editing_an_early_feature_invalidates_everything_after_it(kernel: FakeKernel) -> None:
    """A linear history means downstream features must rebuild too."""
    engine = RecomputeEngine(kernel)
    document = bracket()
    engine.recompute(document)

    document.set_parameter("plate_w", value=180.0)
    result = engine.recompute(document)
    assert [o.status for o in result.outcomes] == [FeatureStatus.BUILT, FeatureStatus.BUILT]


def test_cached_results_stay_correct(kernel: FakeKernel) -> None:
    engine = RecomputeEngine(kernel)
    document = bracket()
    first = engine.recompute(document)
    second = engine.recompute(document)
    assert sorted(str(t) for t in second.topology.face_tags()) == sorted(
        str(t) for t in first.topology.face_tags()
    )


# --------------------------------------------------------------------------
# Failure handling — partial state is preserved
# --------------------------------------------------------------------------


def upward_pocket() -> Document:
    """The bracket with the pocket's direction sign wrong, so it cuts into thin air.

    A realistic mistake, and exactly the kind of thing the engine must report
    precisely rather than producing a silently wrong solid.
    """
    data = copy.deepcopy(BRACKET)
    data["features"][1]["direction"] = "+normal"
    return Document.from_dict(data)


def test_a_failing_feature_does_not_destroy_the_earlier_ones(kernel: FakeKernel) -> None:
    result = recompute(upward_pocket(), kernel)

    assert not result.ok
    assert result.outcomes[0].status == FeatureStatus.BUILT
    assert result.outcomes[1].status == FeatureStatus.FAILED
    # The plate is still there to look at, which is the point.
    assert result.solid is not None
    assert result.last_good_feature == "base"
    assert len(result.topology.faces) == 6


def test_a_failure_names_the_responsible_feature_and_explains_itself(
    kernel: FakeKernel,
) -> None:
    result = recompute(upward_pocket(), kernel)
    failures = result.failures()
    assert len(failures) == 1
    assert failures[0].id == "slot"
    message = str(failures[0].error)
    assert "slot" in message
    assert "direction" in message


def test_features_after_a_failure_are_skipped_not_failed(kernel: FakeKernel) -> None:
    """Only the real culprit is marked failed; the rest are simply not attempted."""
    data = copy.deepcopy(BRACKET)
    data["features"][1]["direction"] = "+normal"
    data["features"].append(
        {
            "id": "second",
            "type": "pocket",
            "profile": "hole.outer",
            "depth": "slot_d",
            "direction": "-normal",
        }
    )
    result = recompute(Document.from_dict(data), kernel)
    assert [o.status for o in result.outcomes] == [
        FeatureStatus.BUILT,
        FeatureStatus.FAILED,
        FeatureStatus.SKIPPED,
    ]


def test_a_suppressed_feature_is_reported_and_skipped(kernel: FakeKernel) -> None:
    data = copy.deepcopy(BRACKET)
    data["features"][1]["suppressed"] = True
    result = recompute(Document.from_dict(data), kernel)
    assert result.ok
    assert result.outcomes[1].status == FeatureStatus.SUPPRESSED
    assert len(result.topology.faces) == 6


def test_an_unresolvable_document_fails_whole(kernel: FakeKernel) -> None:
    data = copy.deepcopy(BRACKET)
    data["parameters"] = [{"name": "a", "expr": "b"}, {"name": "b", "expr": "a"}]
    result = recompute(Document.from_dict(data), kernel)
    assert not result.ok
    assert result.error is not None
    assert result.solid is None


def test_an_open_profile_is_rejected_before_any_geometry_is_built(
    kernel: FakeKernel,
) -> None:
    """A loop with a gap must be caught by name, not surface as a kernel error."""
    data = copy.deepcopy(BRACKET)
    outline = data["sketches"]["outline"]
    outline["points"]["p4"] = [0, "plate_h / 2"]
    outline["curves"][3] = {"id": "left", "start": "p4", "end": "p0"}

    result = recompute(Document.from_dict(data), kernel)
    assert not result.ok
    message = str(result.failures()[0].error)
    assert "not closed" in message
    assert "left" in message


def test_an_unknown_feature_type_is_reported(kernel: FakeKernel) -> None:
    data = copy.deepcopy(BRACKET)
    data["features"] = [{"id": "x", "type": "loft", "profile": "outline.outer"}]
    result = recompute(Document.from_dict(data), kernel)
    assert not result.ok
    assert "unknown feature type" in str(result.failures()[0].error)


def test_a_pocket_without_material_is_refused(kernel: FakeKernel) -> None:
    data = copy.deepcopy(BRACKET)
    data["features"] = [data["features"][1]]
    result = recompute(Document.from_dict(data), kernel)
    assert not result.ok
    assert "add a pad" in str(result.failures()[0].error)


# --------------------------------------------------------------------------
# Document round-trip
# --------------------------------------------------------------------------


def test_the_document_round_trips_through_its_dict_form(kernel: FakeKernel) -> None:
    original = bracket()
    restored = Document.from_dict(original.to_dict())
    assert recompute(restored, kernel).topology.face_tags() == recompute(
        original, FakeKernel()
    ).topology.face_tags()


def test_reordering_features_requires_a_complete_list() -> None:
    document = bracket()
    with pytest.raises(DocumentError):
        document.reorder_features(["slot"])


def test_features_can_be_reordered() -> None:
    document = bracket()
    document.reorder_features(["slot", "base"])
    assert [f.id for f in document.features] == ["slot", "base"]


def test_a_cut_that_only_grazes_a_face_is_refused() -> None:
    """A tool flush against a face removes nothing but imprints its outline.

    This is what a pocket does when its sketch sits on the plane the pad was
    grown *from* and it is told to cut -normal: the tool goes out into space,
    the boolean reports a volume a fraction of a cubic millimetre lighter, and
    the feature 'succeeds' having only split the face in two. Measured on a
    3833mm3 body the drift was 1e-4mm3, which sails past any absolute
    tolerance, so the check is against the tool's own volume instead.
    """
    pytest.importorskip("OCP", reason="requires the optional OCCT extra")
    from facet.adapters.geometry.occt import OcctKernel

    document = Document.from_dict(
        {
            "schema": "cadsheet/1",
            "parameters": [{"name": "t", "value": 10.0}],
            "datums": {},
            "sketches": {
                "outline": {
                    "plane": "xy",
                    "points": {"a": [0, 0], "b": [40, 0], "c": [40, 30], "d": [0, 30]},
                    "curves": [
                        {"id": "e0", "type": "line", "start": "a", "end": "b"},
                        {"id": "e1", "type": "line", "start": "b", "end": "c"},
                        {"id": "e2", "type": "line", "start": "c", "end": "d"},
                        {"id": "e3", "type": "line", "start": "d", "end": "a"},
                    ],
                    "loops": [{"id": "outer", "curves": ["e0", "e1", "e2", "e3"]}],
                },
                "bore": {
                    "plane": "xy",
                    "points": {"c": [20, 15]},
                    "curves": [{"id": "r", "type": "circle", "center": "c", "radius": 4}],
                    "loops": [{"id": "outer", "curves": ["r"]}],
                },
            },
            "features": [
                {"id": "slab", "type": "pad", "profile": "outline.outer", "length": "t"},
                # Grown +normal from xy, so the material is above; cutting
                # -normal from the same plane drills away from it.
                {
                    "id": "wrong_way",
                    "type": "pocket",
                    "profile": "bore.outer",
                    "depth": "t",
                    "direction": "-normal",
                },
            ],
        }
    )

    result = recompute(document, OcctKernel())
    assert not result.ok
    message = str(result.failures()[0].error)
    assert "removes no material" in message
    assert "direction" in message

    # And the face it grazed is left whole, not imprinted in two.
    assert [str(f.tag) for f in result.topology.faces].count("slab/cap-") == 1
