"""Documents with arcs and circles, built end to end.

Requires OCCT: the analytic kernel deliberately refuses curved profiles rather
than approximating them, so these are marked accordingly.

The parts here are the ones people actually draw — a capsule slot with rounded
ends, a plate with a circular bore — and the point is that their curved faces
get the same stable names as flat ones.
"""

from __future__ import annotations

import copy
import math

import pytest

pytest.importorskip("OCP", reason="requires the optional OCCT extra")

from facet.adapters.geometry.fake import FakeKernel
from facet.adapters.geometry.occt import OcctKernel
from facet.application.recompute import recompute
from facet.domain.document import Document
from facet.domain.fingerprint import SurfaceKind
from facet.domain.selectors import EdgeSelector, FaceSelector

pytestmark = pytest.mark.occt

#: A capsule pad with a circular bore through it — arcs and a circle together.
CAPSULE: dict[str, object] = {
    "schema": "cadsheet/1",
    "project": "capsule",
    "parameters": [
        {"name": "half", "value": 30.0, "group": "Slot", "doc": "half the flank length"},
        {"name": "r", "value": 8.0, "group": "Slot", "doc": "end radius"},
        {"name": "thk", "value": 5.0, "group": "Slot"},
        {"name": "bore_d", "value": 6.0, "group": "Bore"},
    ],
    "datums": {
        "base": {"type": "plane", "origin": [0, 0, 0], "normal": [0, 0, 1]},
        "top": {"type": "plane", "origin": [0, 0, "thk"], "normal": [0, 0, 1]},
    },
    "sketches": {
        "outline": {
            "plane": "base",
            "points": {
                "a": ["-half", "-r"],
                "b": ["half", "-r"],
                "c": ["half", "r"],
                "d": ["-half", "r"],
                "rc": ["half", 0],
                "lc": ["-half", 0],
            },
            "curves": [
                {"id": "bottom", "start": "a", "end": "b"},
                {"id": "right", "type": "arc", "start": "b", "end": "c", "center": "rc"},
                {"id": "top", "start": "c", "end": "d"},
                {"id": "left", "type": "arc", "start": "d", "end": "a", "center": "lc"},
            ],
            "loops": [{"id": "outer", "curves": ["bottom", "right", "top", "left"]}],
        },
        "bore": {
            "plane": "top",
            "points": {"m": [0, 0]},
            "curves": [
                {"id": "rim", "type": "circle", "center": "m", "radius": "bore_d / 2"}
            ],
            "loops": [{"id": "outer", "curves": ["rim"]}],
        },
    },
    "features": [
        {"id": "body", "type": "pad", "profile": "outline.outer", "length": "thk"},
        {
            "id": "hole",
            "type": "pocket",
            "profile": "bore.outer",
            "depth": "thk",
            "direction": "-normal",
            "through_all": True,
        },
    ],
}


def capsule(**overrides: float) -> Document:
    data = copy.deepcopy(CAPSULE)
    for name, value in overrides.items():
        for row in data["parameters"]:  # type: ignore[union-attr]
            if row["name"] == name:
                row["value"] = value
    return Document.from_dict(data)


@pytest.fixture
def kernel() -> OcctKernel:
    return OcctKernel()


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------


def test_a_capsule_with_a_bore_builds(kernel: OcctKernel) -> None:
    result = recompute(capsule(), kernel)
    assert result.ok, [o.error for o in result.failures()]


def test_curved_faces_are_named_from_their_sketch_curves(kernel: OcctKernel) -> None:
    """An arc-swept face gets the same kind of name as a line-swept one."""
    result = recompute(capsule(), kernel)
    assert sorted(str(t) for t in result.topology.face_tags()) == [
        "body/cap+",
        "body/cap-",
        "body/side[outline.bottom]",
        "body/side[outline.left]",
        "body/side[outline.right]",
        "body/side[outline.top]",
        "hole/wall[bore.rim]",
    ]


def test_the_arc_faces_really_are_cylindrical(kernel: OcctKernel) -> None:
    result = recompute(capsule(), kernel)
    rounded = FaceSelector.parse("body/side[outline.right]").resolve(result.topology)
    assert rounded[0].fingerprint.surface == SurfaceKind.CYLINDER

    flat = FaceSelector.parse("body/side[outline.bottom]").resolve(result.topology)
    assert flat[0].fingerprint.surface == SurfaceKind.PLANE


def test_the_bore_wall_is_a_cylinder(kernel: OcctKernel) -> None:
    result = recompute(capsule(), kernel)
    wall = FaceSelector.parse("hole/wall[bore.rim]").resolve(result.topology)
    assert wall[0].fingerprint.surface == SurfaceKind.CYLINDER
    # A through bore of diameter 6 over a 5mm plate.
    assert wall[0].fingerprint.area == pytest.approx(math.pi * 6.0 * 5.0, rel=1e-3)


def test_the_volume_matches_a_capsule_minus_its_bore(kernel: OcctKernel) -> None:
    result = recompute(capsule(), kernel)
    capsule_area = (2 * 30.0) * (2 * 8.0) + math.pi * 8.0**2
    expected = capsule_area * 5.0 - math.pi * 3.0**2 * 5.0
    assert kernel.volume(result.solid.handle) == pytest.approx(expected, rel=1e-3)


# --------------------------------------------------------------------------
# Curved geometry is driven by the sheet like everything else
# --------------------------------------------------------------------------


def test_the_end_radius_follows_its_parameter(kernel: OcctKernel) -> None:
    narrow = recompute(capsule(r=4.0), kernel)
    wide = recompute(capsule(r=16.0), OcctKernel())

    def flank_area(result) -> float:
        return FaceSelector.parse("body/side[outline.right]").resolve(
            result.topology
        )[0].fingerprint.area

    # A semicircular end of radius r over thickness t has area pi*r*t.
    assert flank_area(narrow) == pytest.approx(math.pi * 4.0 * 5.0, rel=1e-3)
    assert flank_area(wide) == pytest.approx(math.pi * 16.0 * 5.0, rel=1e-3)


def test_the_bore_diameter_follows_its_expression(kernel: OcctKernel) -> None:
    result = recompute(capsule(bore_d=11.0), kernel)
    assert result.parameters is not None
    wall = FaceSelector.parse("hole/wall[bore.rim]").resolve(result.topology)
    assert wall[0].fingerprint.area == pytest.approx(math.pi * 11.0 * 5.0, rel=1e-3)


def test_names_survive_a_sweep_of_curved_geometry() -> None:
    """The headline guarantee, on arcs and circles rather than flat faces."""
    reference: list[str] | None = None

    for step in range(10):
        document = capsule(
            half=20.0 + step * 4.0,
            r=5.0 + step * 1.5,
            thk=3.0 + step * 0.5,
            bore_d=4.0 + step * 0.4,
        )
        result = recompute(document, OcctKernel())
        assert result.ok, f"step {step}: {[o.error for o in result.failures()]}"

        tags = sorted(str(t) for t in result.topology.face_tags())
        if reference is None:
            reference = tags
        assert tags == reference, f"naming drifted at step {step}"

    assert reference is not None and len(reference) == 7


def test_selectors_on_curved_faces_survive_the_sweep() -> None:
    rounded = FaceSelector.parse("body/side[outline.right]")
    bore = FaceSelector.parse("hole/wall[bore.rim]")
    # The rim where the bore breaks through the top face.
    mouth = EdgeSelector.between_patterns("body/cap+", "hole/wall[*]")

    for step in range(10):
        document = capsule(half=20.0 + step * 4.0, r=5.0 + step * 1.5, bore_d=4.0 + step * 0.4)
        result = recompute(document, OcctKernel())
        topology = result.topology
        assert len(rounded.resolve(topology)) == 1
        assert len(bore.resolve(topology)) == 1
        assert len(mouth.resolve(topology)) == 1


# --------------------------------------------------------------------------
# Honest failure on a kernel that cannot do curves
# --------------------------------------------------------------------------


def test_the_analytic_kernel_refuses_curves_clearly() -> None:
    """Better a clear refusal than a polygonal approximation nobody asked for."""
    result = recompute(capsule(), FakeKernel())
    assert not result.ok
    message = str(result.failures()[0].error)
    assert "rectangular" in message


# --------------------------------------------------------------------------
# Document round-trip
# --------------------------------------------------------------------------


def test_a_curved_document_round_trips(kernel: OcctKernel) -> None:
    original = capsule()
    restored = Document.from_dict(original.to_dict())
    assert recompute(restored, kernel).topology.face_tags() == recompute(
        original, OcctKernel()
    ).topology.face_tags()
