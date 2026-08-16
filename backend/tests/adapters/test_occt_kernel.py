"""OCCT-specific behaviour, beyond the shared conformance contract.

The conformance suite proves OCCT is substitutable for the analytic kernel. This
file covers what OCCT can do that the analytic kernel deliberately cannot:
arbitrary polygon profiles, arcs and circles, datum planes at any orientation,
and STEP export.

The naming guarantee is re-proven here against the *real* kernel, because a
guarantee that only holds for the test double is worth nothing.
"""

from __future__ import annotations

import math
import struct

import pytest

pytest.importorskip("OCP", reason="requires the optional OCCT extra")

from facet.adapters.geometry.occt import OcctKernel
from facet.application.naming import (
    PAD_ROLES,
    POCKET_ROLES,
    NamedSolid,
    NamingEngine,
)
from facet.application.ports.geometry import (
    Capability,
    CurveType,
    Origin,
    PadRequest,
    PocketRequest,
    Profile,
    ProfileCurve,
)
from facet.domain.errors import FeatureBuildError
from facet.domain.fingerprint import SurfaceKind
from facet.domain.math3d import Frame, Vec2, Vec3
from facet.domain.selectors import EdgeSelector, FaceSelector

pytestmark = pytest.mark.occt


@pytest.fixture
def kernel() -> OcctKernel:
    return OcctKernel()


def polygon(
    sketch: str, points: list[Vec2], names: list[str], frame: Frame | None = None
) -> Profile:
    """A closed profile from an arbitrary list of points."""
    curves = tuple(
        ProfileCurve(
            id=names[index],
            type=CurveType.LINE,
            start=points[index],
            end=points[(index + 1) % len(points)],
        )
        for index in range(len(points))
    )
    return Profile(
        sketch=sketch, loop="outer", frame=frame or Frame.world(), curves=curves
    )


# --------------------------------------------------------------------------
# Arbitrary polygons — the analytic kernel refuses these
# --------------------------------------------------------------------------


def test_a_triangle_pads_and_names_every_side(kernel: OcctKernel) -> None:
    profile = polygon(
        "tri",
        [Vec2(0, 0), Vec2(60, 0), Vec2(30, 50)],
        ["bottom", "hypotenuse", "left"],
    )
    result = kernel.pad(PadRequest(feature="wedge", profile=profile, length=8))

    assert len(result.faces) == 5  # 3 sides + 2 caps
    swept = {f.provenance.curve for f in result.faces if f.provenance.origin == Origin.SWEPT}
    assert swept == {"bottom", "hypotenuse", "left"}
    assert kernel.volume(result.solid) == pytest.approx(0.5 * 60 * 50 * 8)


def test_an_l_shaped_profile_names_all_six_sides(kernel: OcctKernel) -> None:
    """A non-convex profile — exactly what a real bracket looks like."""
    points = [
        Vec2(0, 0), Vec2(80, 0), Vec2(80, 30),
        Vec2(30, 30), Vec2(30, 70), Vec2(0, 70),
    ]
    names = ["s0", "s1", "s2", "s3", "s4", "s5"]
    result = kernel.pad(
        PadRequest(feature="bracket", profile=polygon("l", points, names), length=5)
    )

    assert len(result.faces) == 8  # 6 sides + 2 caps
    swept = {f.provenance.curve for f in result.faces if f.provenance.origin == Origin.SWEPT}
    assert swept == set(names)
    assert kernel.volume(result.solid) == pytest.approx((80 * 30 + 30 * 40) * 5)


def test_every_face_of_a_non_rectangular_pad_is_attributed(kernel: OcctKernel) -> None:
    profile = polygon(
        "tri", [Vec2(0, 0), Vec2(40, 0), Vec2(20, 35)], ["a", "b", "c"]
    )
    result = kernel.pad(PadRequest(feature="wedge", profile=profile, length=4))
    assert [f.ref for f in result.faces if f.provenance.origin == Origin.UNKNOWN] == []


# --------------------------------------------------------------------------
# Curved profiles
# --------------------------------------------------------------------------


def test_a_circular_profile_produces_a_cylindrical_face(kernel: OcctKernel) -> None:
    profile = Profile(
        sketch="round",
        loop="outer",
        frame=Frame.world(),
        curves=(
            ProfileCurve(id="rim", type=CurveType.CIRCLE, center=Vec2(0, 0), radius=10.0),
        ),
    )
    result = kernel.pad(PadRequest(feature="boss", profile=profile, length=12))

    cylinders = [f for f in result.faces if f.fingerprint.surface == SurfaceKind.CYLINDER]
    assert len(cylinders) == 1
    assert cylinders[0].provenance.curve == "rim"
    assert kernel.volume(result.solid) == pytest.approx(math.pi * 100 * 12, rel=1e-3)


def test_a_circular_pocket_is_a_hole_with_a_named_wall(kernel: OcctKernel) -> None:
    plate = kernel.pad(
        PadRequest(
            feature="base",
            profile=polygon(
                "out",
                [Vec2(0, 0), Vec2(60, 0), Vec2(60, 60), Vec2(0, 60)],
                ["bottom", "right", "top", "left"],
            ),
            length=10,
        )
    )
    bore = Profile(
        sketch="bore",
        loop="outer",
        frame=Frame.world().with_origin(Vec3(0, 0, 10)),
        curves=(
            ProfileCurve(id="rim", type=CurveType.CIRCLE, center=Vec2(30, 30), radius=8.0),
        ),
    )
    result = kernel.pocket(
        plate.solid,
        PocketRequest(feature="hole", profile=bore, depth=4, direction=-1),
    )

    walls = [
        f for f in result.faces
        if f.provenance.origin == Origin.SWEPT and f.provenance.curve == "rim"
    ]
    assert len(walls) == 1
    assert walls[0].fingerprint.surface == SurfaceKind.CYLINDER
    assert kernel.volume(result.solid) == pytest.approx(60 * 60 * 10 - math.pi * 64 * 4, rel=1e-3)


# --------------------------------------------------------------------------
# Datum planes at any orientation
# --------------------------------------------------------------------------


def test_a_pad_on_a_rotated_datum_still_names_its_faces(kernel: OcctKernel) -> None:
    """Directions come from the datum, so a tilted plane changes nothing."""
    tilted = Frame.from_origin_normal(
        origin=Vec3(0, 0, 0),
        normal=Vec3(0, math.sin(math.radians(30)), math.cos(math.radians(30))),
        x_hint=Vec3(1, 0, 0),
    )
    profile = polygon(
        "out",
        [Vec2(0, 0), Vec2(40, 0), Vec2(40, 25), Vec2(0, 25)],
        ["bottom", "right", "top", "left"],
        frame=tilted,
    )
    result = kernel.pad(PadRequest(feature="fin", profile=profile, length=6))

    assert len(result.faces) == 6
    swept = {f.provenance.curve for f in result.faces if f.provenance.origin == Origin.SWEPT}
    assert swept == {"bottom", "right", "top", "left"}
    assert kernel.volume(result.solid) == pytest.approx(40 * 25 * 6)


def test_a_pad_on_the_yz_plane_is_named_normally(kernel: OcctKernel) -> None:
    frame = Frame.from_origin_normal(Vec3.zero(), Vec3(1, 0, 0), Vec3(0, 1, 0))
    profile = polygon(
        "out",
        [Vec2(0, 0), Vec2(30, 0), Vec2(30, 20), Vec2(0, 20)],
        ["a", "b", "c", "d"],
        frame=frame,
    )
    result = kernel.pad(PadRequest(feature="wall", profile=profile, length=5))
    box = kernel.bounding_box(result.solid)
    assert box.max[0] == pytest.approx(5.0)  # extruded along +X
    assert [f.ref for f in result.faces if f.provenance.origin == Origin.UNKNOWN] == []


# --------------------------------------------------------------------------
# The naming guarantee, against the real kernel
# --------------------------------------------------------------------------


def build_plate(
    kernel: OcctKernel, engine: NamingEngine, width: float, height: float, thickness: float
) -> NamedSolid:
    profile = polygon(
        "out",
        [Vec2(0, 0), Vec2(width, 0), Vec2(width, height), Vec2(0, height)],
        ["bottom", "right", "top", "left"],
    )
    result = kernel.pad(PadRequest(feature="base", profile=profile, length=thickness))
    return engine.name(
        feature="base", sketch="out", result=result,
        vocabulary=PAD_ROLES, frame=profile.frame,
    )


def cut_pocket(
    kernel: OcctKernel,
    engine: NamingEngine,
    base: NamedSolid,
    *,
    width: float,
    height: float,
    x0: float,
    y0: float,
    depth: float,
    z: float,
) -> NamedSolid:
    profile = polygon(
        "hole",
        [Vec2(x0, y0), Vec2(x0 + width, y0), Vec2(x0 + width, y0 + height), Vec2(x0, y0 + height)],
        ["c0", "c1", "c2", "c3"],
        frame=Frame.world().with_origin(Vec3(0, 0, z)),
    )
    result = kernel.pocket(
        base.handle,
        PocketRequest(feature="slot", profile=profile, depth=depth, direction=-1),
    )
    return engine.name(
        feature="slot", sketch="hole", result=result,
        vocabulary=POCKET_ROLES, frame=profile.frame, previous=base,
    )


def test_occt_produces_the_same_names_as_the_analytic_kernel(kernel: OcctKernel) -> None:
    """The two kernels are interchangeable all the way up to the tag strings."""
    engine = NamingEngine()
    plate = build_plate(kernel, engine, 120, 72, 6)
    pocketed = cut_pocket(
        kernel, engine, plate, width=20, height=20, x0=40, y0=26, depth=2, z=6
    )
    assert sorted(str(t) for t in pocketed.topology.face_tags()) == [
        "base/cap+",
        "base/cap-",
        "base/side[out.bottom]",
        "base/side[out.left]",
        "base/side[out.right]",
        "base/side[out.top]",
        "slot/floor",
        "slot/wall[hole.c0]",
        "slot/wall[hole.c1]",
        "slot/wall[hole.c2]",
        "slot/wall[hole.c3]",
    ]


def test_names_survive_a_parameter_sweep_on_the_real_kernel() -> None:
    """The headline guarantee, proven against OpenCascade rather than a double."""
    reference: list[str] | None = None

    for step in range(12):
        width = 80.0 + step * 9.0
        height = 50.0 + step * 4.0
        thickness = 4.0 + step * 0.5

        kernel = OcctKernel()
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
        assert tags == reference, f"naming drifted at step {step} (w={width})"

    assert reference is not None and len(reference) == 11


def test_selectors_keep_resolving_across_the_sweep() -> None:
    wall = FaceSelector.parse("slot/wall[hole.c0]")
    floor = FaceSelector.parse("slot/floor")
    mouth = EdgeSelector.between_patterns("base/cap+", "slot/wall[*]")

    for step in range(12):
        width = 80.0 + step * 9.0
        kernel = OcctKernel()
        engine = NamingEngine()
        plate = build_plate(kernel, engine, width, 60.0, 6.0)
        pocketed = cut_pocket(
            kernel, engine, plate,
            width=width * 0.2, height=12.0, x0=width * 0.4, y0=24.0, depth=2.0, z=6.0,
        )
        topology = pocketed.topology
        assert len(wall.resolve(topology)) == 1
        assert len(floor.resolve(topology)) == 1
        assert len(mouth.resolve(topology)) == 4


def test_a_channel_splits_the_top_face_with_stable_ordinals() -> None:
    """Split ordering must be stable on the real kernel too."""
    for step in range(8):
        width = 100.0 + step * 12.0
        kernel = OcctKernel()
        engine = NamingEngine()
        plate = build_plate(kernel, engine, width, 60.0, 6.0)
        channelled = cut_pocket(
            kernel, engine, plate,
            width=18.0, height=80.0, x0=width * 0.45, y0=-10.0, depth=2.0, z=6.0,
        )
        from facet.domain.tags import FaceTag

        first = channelled.topology.face(FaceTag.parse("base/cap+#0"))
        second = channelled.topology.face(FaceTag.parse("base/cap+#1"))
        assert first is not None and second is not None
        assert first.fingerprint.centroid.x < second.fingerprint.centroid.x


# --------------------------------------------------------------------------
# Capability and export
# --------------------------------------------------------------------------


def test_occt_declares_brep_export(kernel: OcctKernel) -> None:
    assert Capability.BREP_EXPORT in kernel.capabilities


def test_step_export_produces_a_valid_file(kernel: OcctKernel) -> None:
    """STEP is the reason to run a B-rep kernel rather than a mesh one."""
    result = kernel.pad(
        PadRequest(
            feature="base",
            profile=polygon(
                "out", [Vec2(0, 0), Vec2(20, 0), Vec2(20, 10), Vec2(0, 10)],
                ["a", "b", "c", "d"],
            ),
            length=3,
        )
    )
    data = kernel.export_brep(result.solid, "step")
    assert data.startswith(b"ISO-10303-21")
    assert b"ADVANCED_FACE" in data  # a real B-rep, not a tessellation
    assert data.rstrip().endswith(b"END-ISO-10303-21;")


def test_an_unsupported_brep_format_is_refused(kernel: OcctKernel) -> None:
    result = kernel.pad(
        PadRequest(
            feature="base",
            profile=polygon(
                "out", [Vec2(0, 0), Vec2(5, 0), Vec2(5, 5), Vec2(0, 5)], ["a", "b", "c", "d"]
            ),
            length=1,
        )
    )
    with pytest.raises(FeatureBuildError):
        kernel.export_brep(result.solid, "iges")


def test_tessellation_of_a_curved_face_is_dense_enough(kernel: OcctKernel) -> None:
    profile = Profile(
        sketch="round", loop="outer", frame=Frame.world(),
        curves=(ProfileCurve(id="rim", type=CurveType.CIRCLE, center=Vec2(0, 0), radius=20.0),),
    )
    result = kernel.pad(PadRequest(feature="boss", profile=profile, length=5))
    mesh = kernel.tessellate(result.solid, tolerance=0.05)
    assert mesh.triangle_count > 40  # a cylinder needs real subdivision
    assert len(mesh.edges) >= 2


def test_stl_export_works_through_the_shared_mesh_writer(kernel: OcctKernel) -> None:
    """The mesh exporters are kernel-agnostic, so OCCT gets them for free."""
    from facet.adapters.export.mesh import stl_binary

    result = kernel.pad(
        PadRequest(
            feature="base",
            profile=polygon(
                "out", [Vec2(0, 0), Vec2(10, 0), Vec2(10, 10), Vec2(0, 10)],
                ["a", "b", "c", "d"],
            ),
            length=2,
        )
    )
    body = stl_binary(kernel.tessellate(result.solid))
    declared = struct.unpack("<I", body[80:84])[0]
    assert len(body) == 84 + declared * 50
    assert declared == 12  # a box tessellates to 12 triangles
