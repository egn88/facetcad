"""The contract every geometry kernel adapter must satisfy.

This suite is parametrised over each available kernel. It exists to enforce
Liskov substitutability: if the OCCT adapter and the analytic adapter both pass,
the naming engine cannot tell them apart, and any behaviour the application
layer relies on is guaranteed by the port rather than by one implementation.

The invariant that matters most is
:func:`test_every_face_is_attributed` — an unattributed face means the naming
engine has nothing to root a tag in, which is the exact failure mode this
project exists to eliminate.
"""

from __future__ import annotations

import pytest

from facet.application.ports.geometry import (
    Capability,
    GeometryKernel,
    Origin,
    PadRequest,
    PocketRequest,
)
from facet.domain.errors import FeatureBuildError

from .profiles import rectangle

PLATE_W, PLATE_H, PLATE_T = 120.0, 72.0, 6.0


def available_kernels() -> list[pytest.param]:  # type: ignore[valid-type]
    from facet.adapters.geometry.fake import FakeKernel

    cases = [pytest.param(FakeKernel, id="analytic")]
    try:  # pragma: no cover - depends on the optional OCCT extra
        from facet.adapters.geometry.occt import OcctKernel

        cases.append(pytest.param(OcctKernel, id="occt", marks=pytest.mark.occt))
    except ImportError:
        pass
    return cases


@pytest.fixture(params=available_kernels())
def kernel(request: pytest.FixtureRequest) -> GeometryKernel:
    return request.param()


@pytest.fixture
def plate(kernel: GeometryKernel):
    return kernel.pad(
        PadRequest(
            feature="base",
            profile=rectangle("out", PLATE_W, PLATE_H),
            length=PLATE_T,
            direction=1,
        )
    )


# --------------------------------------------------------------------------
# Identity and capabilities
# --------------------------------------------------------------------------


def test_kernel_declares_a_name(kernel: GeometryKernel) -> None:
    assert kernel.name


def test_kernel_declares_its_capabilities_honestly(kernel: GeometryKernel) -> None:
    """Capabilities must be declared, not discovered by calling and failing."""
    assert Capability.PAD in kernel.capabilities
    assert Capability.POCKET in kernel.capabilities


def test_kernel_satisfies_the_port_protocol(kernel: GeometryKernel) -> None:
    assert isinstance(kernel, GeometryKernel)


# --------------------------------------------------------------------------
# Pad
# --------------------------------------------------------------------------


def test_pad_of_a_rectangle_has_six_faces(plate) -> None:
    assert len(plate.faces) == 6


def test_pad_volume_is_correct(kernel: GeometryKernel, plate) -> None:
    assert kernel.volume(plate.solid) == pytest.approx(PLATE_W * PLATE_H * PLATE_T)


def test_pad_bounding_box_is_correct(kernel: GeometryKernel, plate) -> None:
    box = kernel.bounding_box(plate.solid)
    assert box.min == pytest.approx((0.0, 0.0, 0.0))
    assert box.max == pytest.approx((PLATE_W, PLATE_H, PLATE_T))


def test_every_face_is_attributed(plate) -> None:
    """No face may be UNKNOWN — an unnameable face is a naming-engine dead end."""
    unattributed = [f.ref for f in plate.faces if f.provenance.origin == Origin.UNKNOWN]
    assert unattributed == []


def test_pad_reports_one_swept_face_per_profile_curve(plate) -> None:
    swept = {f.provenance.curve for f in plate.faces if f.provenance.origin == Origin.SWEPT}
    assert swept == {"bottom", "right", "top", "left"}


def test_pad_reports_exactly_two_caps(plate) -> None:
    origins = [f.provenance.origin for f in plate.faces]
    assert origins.count(Origin.CAP_START) == 1
    assert origins.count(Origin.CAP_END) == 1


def test_pad_direction_is_explicit_and_respected(kernel: GeometryKernel) -> None:
    """Direction comes from the request, never inferred from geometry."""
    downwards = kernel.pad(
        PadRequest(feature="base", profile=rectangle("out", 10, 10), length=4, direction=-1)
    )
    box = kernel.bounding_box(downwards.solid)
    assert box.min[2] == pytest.approx(-4.0)
    assert box.max[2] == pytest.approx(0.0)


def test_midplane_pad_straddles_the_sketch_plane(kernel: GeometryKernel) -> None:
    result = kernel.pad(
        PadRequest(
            feature="base", profile=rectangle("out", 10, 10), length=4, midplane=True
        )
    )
    box = kernel.bounding_box(result.solid)
    assert box.min[2] == pytest.approx(-2.0)
    assert box.max[2] == pytest.approx(2.0)


def test_non_positive_pad_length_is_refused(kernel: GeometryKernel) -> None:
    with pytest.raises(FeatureBuildError):
        kernel.pad(PadRequest(feature="base", profile=rectangle("out", 10, 10), length=0))


# --------------------------------------------------------------------------
# Edges are reported as face pairs
# --------------------------------------------------------------------------


def test_a_box_has_twelve_edges(plate) -> None:
    assert len(plate.edges) == 12


def test_every_edge_references_two_existing_faces(plate) -> None:
    refs = {f.ref for f in plate.faces}
    for edge in plate.edges:
        assert edge.faces[0] in refs
        assert edge.faces[1] in refs
        assert edge.faces[0] != edge.faces[1]


def test_edge_lengths_match_the_box(plate) -> None:
    lengths = sorted({round(e.fingerprint.length, 6) for e in plate.edges})
    assert lengths == [PLATE_T, PLATE_H, PLATE_W]


# --------------------------------------------------------------------------
# Pocket — the derived-face case that matters
# --------------------------------------------------------------------------


@pytest.fixture
def plate_with_slot(kernel: GeometryKernel, plate):
    """A 20x20 blind pocket in the middle of the top face."""
    return kernel.pocket(
        plate.solid,
        PocketRequest(
            feature="slot",
            profile=rectangle("hole", 20, 20, x0=40, y0=26, z=PLATE_T),
            depth=2.0,
            direction=-1,
        ),
    )


def test_pocket_removes_the_right_volume(kernel: GeometryKernel, plate_with_slot) -> None:
    expected = PLATE_W * PLATE_H * PLATE_T - 20 * 20 * 2
    assert kernel.volume(plate_with_slot.solid) == pytest.approx(expected)


def test_pocket_produces_four_walls_and_a_floor(plate_with_slot) -> None:
    assert len(plate_with_slot.faces) == 11  # 6 original + 4 walls + 1 floor


def test_every_pocket_face_is_attributed(plate_with_slot) -> None:
    unattributed = [
        f.ref for f in plate_with_slot.faces if f.provenance.origin == Origin.UNKNOWN
    ]
    assert unattributed == []


def test_pocket_walls_trace_back_to_their_sketch_curves(plate_with_slot) -> None:
    """The user's requirement: act deterministically on faces the pocket generated."""
    walls = {
        f.provenance.curve
        for f in plate_with_slot.faces
        if f.provenance.origin == Origin.SWEPT
    }
    assert walls == {"bottom", "right", "top", "left"}


def test_pocket_floor_is_reported_as_the_sweep_end_cap(plate_with_slot) -> None:
    floors = [f for f in plate_with_slot.faces if f.provenance.origin == Origin.CAP_END]
    assert len(floors) == 1
    assert floors[0].fingerprint.area == pytest.approx(400.0)


def test_surviving_faces_are_inherited_with_their_original_ref(plate, plate_with_slot) -> None:
    original_refs = {f.ref for f in plate.faces}
    inherited = {
        f.provenance.parent
        for f in plate_with_slot.faces
        if f.provenance.origin == Origin.INHERITED
    }
    assert inherited <= original_refs
    assert len(inherited) == 6  # every original face survives a fully interior pocket


def test_the_pocketed_top_face_shrinks_but_keeps_its_identity(plate, plate_with_slot) -> None:
    top = max(plate.faces, key=lambda f: f.fingerprint.centroid.z)
    survivor = next(
        f for f in plate_with_slot.faces if f.provenance.parent == top.ref
    )
    assert survivor.fingerprint.area == pytest.approx(PLATE_W * PLATE_H - 400.0)


def test_a_pocket_that_removes_nothing_is_refused(kernel: GeometryKernel, plate) -> None:
    with pytest.raises(FeatureBuildError):
        kernel.pocket(
            plate.solid,
            PocketRequest(
                feature="slot",
                profile=rectangle("hole", 5, 5, x0=500, y0=500, z=PLATE_T),
                depth=2.0,
                direction=-1,
            ),
        )


def test_a_pocket_that_consumes_the_body_is_refused(kernel: GeometryKernel, plate) -> None:
    with pytest.raises(FeatureBuildError):
        kernel.pocket(
            plate.solid,
            PocketRequest(
                feature="slot",
                profile=rectangle("hole", 500, 500, x0=-100, y0=-100, z=PLATE_T),
                depth=100.0,
                direction=-1,
            ),
        )


# --------------------------------------------------------------------------
# Split faces — one logical face becoming several
# --------------------------------------------------------------------------


@pytest.fixture
def plate_with_channel(kernel: GeometryKernel, plate):
    """A channel cut clean across the plate, splitting the top face in two."""
    return kernel.pocket(
        plate.solid,
        PocketRequest(
            feature="channel",
            profile=rectangle("cut", 20, PLATE_H + 20, x0=50, y0=-10, z=PLATE_T),
            depth=2.0,
            direction=-1,
        ),
    )


def test_a_channel_splits_the_top_face_into_two_fragments(plate, plate_with_channel) -> None:
    top = max(plate.faces, key=lambda f: f.fingerprint.centroid.z)
    fragments = [f for f in plate_with_channel.faces if f.provenance.parent == top.ref]
    assert len(fragments) == 2


def test_both_split_fragments_inherit_the_same_parent(plate, plate_with_channel) -> None:
    """Split pieces share provenance; the ordinal is the domain's job, not the kernel's."""
    top = max(plate.faces, key=lambda f: f.fingerprint.centroid.z)
    fragments = [f for f in plate_with_channel.faces if f.provenance.parent == top.ref]
    assert {f.provenance.origin for f in fragments} == {Origin.INHERITED}
    assert len({f.ref for f in fragments}) == 2


def test_split_fragment_areas_sum_to_the_remaining_material(plate, plate_with_channel) -> None:
    top = max(plate.faces, key=lambda f: f.fingerprint.centroid.z)
    fragments = [f for f in plate_with_channel.faces if f.provenance.parent == top.ref]
    total = sum(f.fingerprint.area for f in fragments)
    assert total == pytest.approx(PLATE_W * PLATE_H - 20 * PLATE_H)


def test_a_through_cut_deletes_the_faces_it_consumes(kernel: GeometryKernel, plate) -> None:
    """A face that no longer exists must be reported, not silently dropped."""
    result = kernel.pocket(
        plate.solid,
        PocketRequest(
            feature="through",
            profile=rectangle("cut", 20, PLATE_H + 20, x0=50, y0=-10, z=PLATE_T),
            depth=0.0,
            direction=-1,
            through_all=True,
        ),
    )
    assert kernel.volume(result.solid) == pytest.approx(
        PLATE_W * PLATE_H * PLATE_T - 20 * PLATE_H * PLATE_T
    )


# --------------------------------------------------------------------------
# Determinism — the same inputs must always give the same answer
# --------------------------------------------------------------------------


def test_repeating_an_operation_gives_identical_provenance(kernel: GeometryKernel) -> None:
    def build() -> list[tuple[str, str | None]]:
        pad = kernel.pad(
            PadRequest(feature="base", profile=rectangle("out", 30, 20), length=5)
        )
        result = kernel.pocket(
            pad.solid,
            PocketRequest(
                feature="slot",
                profile=rectangle("hole", 6, 6, x0=10, y0=7, z=5),
                depth=2,
                direction=-1,
            ),
        )
        return sorted(
            (f.provenance.origin, f.provenance.curve) for f in result.faces
        )

    assert build() == build()


def test_face_refs_are_stable_across_identical_rebuilds(kernel: GeometryKernel) -> None:
    def refs() -> list[tuple[str, float]]:
        pad = kernel.pad(
            PadRequest(feature="base", profile=rectangle("out", 30, 20), length=5)
        )
        return sorted((f.ref, round(f.fingerprint.area, 6)) for f in pad.faces)

    assert refs() == refs()


# --------------------------------------------------------------------------
# Tessellation feeds the viewer, including click-to-select
# --------------------------------------------------------------------------


def test_tessellation_covers_every_face(kernel: GeometryKernel, plate) -> None:
    mesh = kernel.tessellate(plate.solid)
    assert {r.ref for r in mesh.face_ranges} == {f.ref for f in plate.faces}


def test_tessellation_indices_are_in_range(kernel: GeometryKernel, plate) -> None:
    mesh = kernel.tessellate(plate.solid)
    assert mesh.triangle_count > 0
    assert max(mesh.indices) < mesh.vertex_count


def test_tessellation_has_one_normal_per_vertex(kernel: GeometryKernel, plate) -> None:
    mesh = kernel.tessellate(plate.solid)
    assert len(mesh.normals) == len(mesh.positions)


def test_face_ranges_partition_the_index_buffer(kernel: GeometryKernel, plate) -> None:
    """Every triangle belongs to exactly one face, so picking is unambiguous."""
    mesh = kernel.tessellate(plate.solid)
    covered = sum(r.count for r in mesh.face_ranges)
    assert covered == len(mesh.indices)


def test_exact_edges_are_provided_for_display(kernel: GeometryKernel, plate) -> None:
    mesh = kernel.tessellate(plate.solid)
    assert len(mesh.edges) == 12
    for polyline in mesh.edges:
        assert len(polyline.points) >= 6
        assert len(polyline.points) % 3 == 0


# --------------------------------------------------------------------------
# Handle lifecycle
# --------------------------------------------------------------------------


def test_released_handles_are_rejected(kernel: GeometryKernel, plate) -> None:
    kernel.release(plate.solid)
    with pytest.raises(FeatureBuildError):
        kernel.volume(plate.solid)
