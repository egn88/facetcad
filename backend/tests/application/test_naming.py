"""The naming engine: kernel provenance in, stable tags out.

These tests run against the analytic kernel, so they exercise the real
provenance path rather than hand-built fixtures. The parameter-sweep test at the
end is the one that decides whether the project achieved its goal.
"""

from __future__ import annotations

import pytest

from facet.adapters.geometry.fake import FakeKernel
from facet.application.naming import (
    PAD_ROLES,
    POCKET_ROLES,
    NamedSolid,
    NamingEngine,
)
from facet.application.ports.geometry import PadRequest, PocketRequest
from facet.domain.math3d import Frame, Vec3
from facet.domain.selectors import EdgeSelector, FaceSelector
from facet.domain.tags import FaceTag

from ..conformance.profiles import rectangle

PLATE_W, PLATE_H, PLATE_T = 120.0, 72.0, 6.0


@pytest.fixture
def kernel() -> FakeKernel:
    return FakeKernel()


@pytest.fixture
def engine() -> NamingEngine:
    return NamingEngine()


def build_plate(
    kernel: FakeKernel,
    engine: NamingEngine,
    width: float = PLATE_W,
    height: float = PLATE_H,
    thickness: float = PLATE_T,
) -> NamedSolid:
    profile = rectangle("out", width, height)
    result = kernel.pad(
        PadRequest(feature="base", profile=profile, length=thickness, direction=1)
    )
    return engine.name(
        feature="base",
        sketch="out",
        result=result,
        vocabulary=PAD_ROLES,
        frame=profile.frame,
        previous=None,
    )


def cut_pocket(
    kernel: FakeKernel,
    engine: NamingEngine,
    base: NamedSolid,
    *,
    feature: str = "slot",
    sketch: str = "hole",
    width: float = 20.0,
    height: float = 20.0,
    x0: float = 40.0,
    y0: float = 26.0,
    depth: float = 2.0,
    z: float = PLATE_T,
) -> NamedSolid:
    profile = rectangle(sketch, width, height, x0=x0, y0=y0, z=z)
    result = kernel.pocket(
        base.handle,
        PocketRequest(feature=feature, profile=profile, depth=depth, direction=-1),
    )
    return engine.name(
        feature=feature,
        sketch=sketch,
        result=result,
        vocabulary=POCKET_ROLES,
        frame=profile.frame,
        previous=base,
    )


# --------------------------------------------------------------------------
# Pad naming
# --------------------------------------------------------------------------


def test_a_pad_names_every_face(kernel: FakeKernel, engine: NamingEngine) -> None:
    plate = build_plate(kernel, engine)
    assert sorted(str(t) for t in plate.topology.face_tags()) == [
        "base/cap+",
        "base/cap-",
        "base/side[out.bottom]",
        "base/side[out.left]",
        "base/side[out.right]",
        "base/side[out.top]",
    ]


def test_cap_sign_follows_the_sketch_plane_normal(
    kernel: FakeKernel, engine: NamingEngine
) -> None:
    plate = build_plate(kernel, engine)
    positive = plate.topology.face(FaceTag.parse("base/cap+"))
    negative = plate.topology.face(FaceTag.parse("base/cap-"))
    assert positive is not None and negative is not None
    assert positive.fingerprint.normal.z == pytest.approx(1.0)
    assert negative.fingerprint.normal.z == pytest.approx(-1.0)


def test_a_downward_pad_still_names_caps_by_normal(
    kernel: FakeKernel, engine: NamingEngine
) -> None:
    """Direction is explicit, so a reversed pad must not invert the vocabulary."""
    profile = rectangle("out", 10, 10)
    result = kernel.pad(
        PadRequest(feature="base", profile=profile, length=4, direction=-1)
    )
    named = engine.name(
        feature="base", sketch="out", result=result,
        vocabulary=PAD_ROLES, frame=profile.frame,
    )
    positive = named.topology.face(FaceTag.parse("base/cap+"))
    assert positive is not None
    assert positive.fingerprint.centroid.z == pytest.approx(0.0)


def test_no_ordinals_appear_when_nothing_is_split(
    kernel: FakeKernel, engine: NamingEngine
) -> None:
    plate = build_plate(kernel, engine)
    assert all(tag.ordinal is None for tag in plate.topology.face_tags())


def test_pad_edges_are_named_from_their_adjacent_faces(
    kernel: FakeKernel, engine: NamingEngine
) -> None:
    plate = build_plate(kernel, engine)
    assert len(plate.topology.edges) == 12
    top_perimeter = EdgeSelector.between_patterns("base/cap+", "base/side[*]")
    assert len(top_perimeter.resolve(plate.topology)) == 4


# --------------------------------------------------------------------------
# Pocket naming — the derived-face requirement
# --------------------------------------------------------------------------


def test_pocket_faces_are_named_from_their_sketch_curves(
    kernel: FakeKernel, engine: NamingEngine
) -> None:
    """The stated goal: after a pocket, act deterministically on what it generated."""
    plate = build_plate(kernel, engine)
    pocketed = cut_pocket(kernel, engine, plate)

    tags = sorted(str(t) for t in pocketed.topology.face_tags())
    assert tags == [
        "base/cap+",
        "base/cap-",
        "base/side[out.bottom]",
        "base/side[out.left]",
        "base/side[out.right]",
        "base/side[out.top]",
        "slot/floor",
        "slot/wall[hole.bottom]",
        "slot/wall[hole.left]",
        "slot/wall[hole.right]",
        "slot/wall[hole.top]",
    ]


def test_the_pocket_floor_is_selectable(kernel: FakeKernel, engine: NamingEngine) -> None:
    plate = build_plate(kernel, engine)
    pocketed = cut_pocket(kernel, engine, plate)
    floor = FaceSelector.parse("slot/floor").resolve(pocketed.topology)
    assert floor[0].fingerprint.area == pytest.approx(400.0)
    assert floor[0].fingerprint.centroid.z == pytest.approx(PLATE_T - 2.0)


def test_a_single_pocket_wall_is_addressable(
    kernel: FakeKernel, engine: NamingEngine
) -> None:
    plate = build_plate(kernel, engine)
    pocketed = cut_pocket(kernel, engine, plate)
    wall = FaceSelector.parse("slot/wall[hole.left]").resolve(pocketed.topology)
    assert len(wall) == 1
    assert wall[0].fingerprint.area == pytest.approx(20.0 * 2.0)


def test_all_pocket_walls_select_as_a_group(
    kernel: FakeKernel, engine: NamingEngine
) -> None:
    plate = build_plate(kernel, engine)
    pocketed = cut_pocket(kernel, engine, plate)
    assert len(FaceSelector.parse("slot/wall[*]").resolve(pocketed.topology)) == 4


def test_surviving_faces_keep_their_original_names(
    kernel: FakeKernel, engine: NamingEngine
) -> None:
    """The top face shrinks but is still 'base/cap+' — identity survives the cut."""
    plate = build_plate(kernel, engine)
    pocketed = cut_pocket(kernel, engine, plate)
    top = FaceSelector.parse("base/cap+").resolve(pocketed.topology)
    assert len(top) == 1
    assert top[0].fingerprint.area == pytest.approx(PLATE_W * PLATE_H - 400.0)


def test_the_pocket_perimeter_is_one_stable_edge_query(
    kernel: FakeKernel, engine: NamingEngine
) -> None:
    """A fillet around the pocket mouth, expressed once and stable thereafter."""
    plate = build_plate(kernel, engine)
    pocketed = cut_pocket(kernel, engine, plate)
    mouth = EdgeSelector.between_patterns("base/cap+", "slot/wall[*]")
    assert len(mouth.resolve(pocketed.topology)) == 4


def test_the_pocket_floor_perimeter_is_selectable(
    kernel: FakeKernel, engine: NamingEngine
) -> None:
    plate = build_plate(kernel, engine)
    pocketed = cut_pocket(kernel, engine, plate)
    floor_edges = EdgeSelector.between_patterns("slot/floor", "slot/wall[*]")
    assert len(floor_edges.resolve(pocketed.topology)) == 4


# --------------------------------------------------------------------------
# Splits
# --------------------------------------------------------------------------


def test_a_channel_splits_the_top_face_into_ordered_fragments(
    kernel: FakeKernel, engine: NamingEngine
) -> None:
    plate = build_plate(kernel, engine)
    channelled = cut_pocket(
        kernel, engine, plate,
        feature="channel", sketch="cut",
        width=20, height=PLATE_H + 20, x0=50, y0=-10, depth=2,
    )
    fragments = sorted(
        str(t) for t in channelled.topology.face_tags() if t.feature == "base" and t.role == "cap+"
    )
    assert fragments == ["base/cap+#0", "base/cap+#1"]


def test_split_fragments_are_ordered_left_to_right(
    kernel: FakeKernel, engine: NamingEngine
) -> None:
    plate = build_plate(kernel, engine)
    channelled = cut_pocket(
        kernel, engine, plate,
        feature="channel", sketch="cut",
        width=20, height=PLATE_H + 20, x0=50, y0=-10, depth=2,
    )
    first = channelled.topology.face(FaceTag.parse("base/cap+#0"))
    second = channelled.topology.face(FaceTag.parse("base/cap+#1"))
    assert first is not None and second is not None
    assert first.fingerprint.centroid.x < second.fingerprint.centroid.x


def test_a_bare_pattern_still_selects_every_fragment(
    kernel: FakeKernel, engine: NamingEngine
) -> None:
    """'base/cap+' means the whole logical face, split or not."""
    plate = build_plate(kernel, engine)
    channelled = cut_pocket(
        kernel, engine, plate,
        feature="channel", sketch="cut",
        width=20, height=PLATE_H + 20, x0=50, y0=-10, depth=2,
    )
    assert len(FaceSelector.parse("base/cap+").resolve(channelled.topology)) == 2


# --------------------------------------------------------------------------
# The guarantee: stability across a parameter sweep
# --------------------------------------------------------------------------


def test_names_are_identical_across_a_full_parameter_sweep() -> None:
    """Sweep the plate and pocket dimensions; every tag must stay the same.

    This is the property FreeCAD fails to provide and the reason this project
    exists. The pocket is positioned proportionally, so every face moves and
    resizes on each iteration while its name must not change.
    """
    reference: list[str] | None = None

    for step in range(30):
        width = 80.0 + step * 7.0
        height = 50.0 + step * 3.0
        thickness = 4.0 + step * 0.5

        kernel = FakeKernel()
        engine = NamingEngine()
        plate = build_plate(kernel, engine, width, height, thickness)
        pocketed = cut_pocket(
            kernel, engine, plate,
            width=width * 0.2, height=height * 0.2,
            x0=width * 0.4, y0=height * 0.4,
            depth=thickness * 0.4, z=thickness,
        )

        tags = sorted(str(t) for t in pocketed.topology.face_tags())
        if reference is None:
            reference = tags
        assert tags == reference, f"naming drifted at step {step} (w={width}, h={height})"

    assert reference is not None and len(reference) == 11


def test_selectors_survive_the_same_sweep() -> None:
    """Not just the names — the queries the document stores must keep resolving."""
    wall = FaceSelector.parse("slot/wall[hole.left]")
    floor = FaceSelector.parse("slot/floor")
    top = FaceSelector.parse("base/cap+")
    mouth = EdgeSelector.between_patterns("base/cap+", "slot/wall[*]")

    for step in range(30):
        width = 80.0 + step * 7.0
        height = 50.0 + step * 3.0
        thickness = 4.0 + step * 0.5

        kernel = FakeKernel()
        engine = NamingEngine()
        plate = build_plate(kernel, engine, width, height, thickness)
        pocketed = cut_pocket(
            kernel, engine, plate,
            width=width * 0.2, height=height * 0.2,
            x0=width * 0.4, y0=height * 0.4,
            depth=thickness * 0.4, z=thickness,
        )
        topology = pocketed.topology

        assert len(wall.resolve(topology)) == 1
        assert len(floor.resolve(topology)) == 1
        assert len(top.resolve(topology)) == 1
        assert len(mouth.resolve(topology)) == 4


def test_split_ordinals_are_stable_across_a_sweep() -> None:
    """The left fragment must stay #0 as the plate is resized."""
    for step in range(25):
        width = 100.0 + step * 9.0
        kernel = FakeKernel()
        engine = NamingEngine()
        plate = build_plate(kernel, engine, width, PLATE_H, PLATE_T)
        channelled = cut_pocket(
            kernel, engine, plate,
            feature="channel", sketch="cut",
            width=width * 0.15, height=PLATE_H + 20,
            x0=width * 0.45, y0=-10, depth=2,
        )
        first = channelled.topology.face(FaceTag.parse("base/cap+#0"))
        second = channelled.topology.face(FaceTag.parse("base/cap+#1"))
        assert first is not None and second is not None
        assert first.fingerprint.centroid.x < second.fingerprint.centroid.x


# --------------------------------------------------------------------------
# Frames and ordering ownership
# --------------------------------------------------------------------------


def test_fragments_are_ordered_in_the_owning_features_frame(
    kernel: FakeKernel, engine: NamingEngine
) -> None:
    """Ordering of base/cap+ fragments belongs to 'base', not to the cutter."""
    plate = build_plate(kernel, engine)
    engine.register_frame("base", Frame.world().with_origin(Vec3(1000, 0, 0)))
    channelled = cut_pocket(
        kernel, engine, plate,
        feature="channel", sketch="cut",
        width=20, height=PLATE_H + 20, x0=50, y0=-10, depth=2,
    )
    first = channelled.topology.face(FaceTag.parse("base/cap+#0"))
    second = channelled.topology.face(FaceTag.parse("base/cap+#1"))
    assert first is not None and second is not None
    # A translated frame shifts both centroids equally, so the order is unchanged.
    assert first.fingerprint.centroid.x < second.fingerprint.centroid.x
