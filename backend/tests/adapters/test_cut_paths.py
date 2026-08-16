"""Flattening faces into cut paths — the CNC and laser side.

The claim being tested is the one the whole project rests on: a DXF exported
for ``lid/cap+`` today and again after the sheet changes describes the *same*
face, at coordinates that moved only as much as the part did. A CAM setup that
survives a dimension change is the payoff.
"""

from __future__ import annotations

import copy

import pytest

pytest.importorskip("OCP", reason="requires the optional OCCT extra")

from facet.adapters.export.drawing import export_drawing
from facet.adapters.geometry.occt import OcctKernel
from facet.application.ports.geometry import Arc2D, Capability, Line2D
from facet.application.recompute import recompute
from facet.domain.document import Document
from facet.domain.errors import FeatureBuildError

pytestmark = pytest.mark.occt

PANEL: dict[str, object] = {
    "schema": "cadsheet/1",
    "project": "laser panel",
    "parameters": [
        {"name": "w", "value": 80.0},
        {"name": "h", "value": 60.0},
        {"name": "t", "value": 3.0},
        {"name": "rad", "value": 6.0},
        {"name": "bore_d", "value": 9.0},
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
        },
        "bore": {
            "plane": "base",
            "points": {"c": ["w / 2", "h / 2"]},
            "curves": [{"id": "ring", "type": "circle", "center": "c", "radius": "bore_d"}],
            "loops": [{"id": "outer", "curves": ["ring"]}],
        },
    },
    "features": [
        {"id": "panel", "type": "pad", "profile": "outline.outer", "length": "t"},
        {"id": "round", "type": "fillet", "radius": "rad", "edges": "panel/side[*] dir=|z"},
        {"id": "bore", "type": "pocket", "profile": "bore.outer", "depth": "t",
         "direction": "+normal"},
    ],
}


@pytest.fixture(scope="module")
def kernel() -> OcctKernel:
    return OcctKernel()


def build(kernel: OcctKernel, **values: float):
    data = copy.deepcopy(PANEL)
    for row in data["parameters"]:  # type: ignore[index]
        if row["name"] in values:
            row["value"] = values[row["name"]]
    return recompute(Document.from_dict(data), kernel)


def profile_of(kernel: OcctKernel, result, tag: str):
    named = result.solid
    ref = next(r for r, t in named.refs.items() if str(t) == tag)
    return kernel.face_profile(named.handle, ref, 0.01)


# -- what comes out --------------------------------------------------------


def test_the_kernel_declares_it_can_flatten_a_face(kernel: OcctKernel) -> None:
    assert Capability.FACE_PROFILE in kernel.capabilities
    assert Capability.DRAWING_EXPORT in kernel.capabilities


def test_a_filleted_panel_flattens_to_lines_and_arcs(kernel: OcctKernel) -> None:
    """Not a polyline soup: a router cuts the arcs as arcs."""
    flat = profile_of(kernel, build(kernel), "panel/cap+")
    outer = next(loop for loop in flat.loops if loop.outer)
    assert sum(isinstance(c, Line2D) for c in outer.curves) == 4
    assert sum(isinstance(c, Arc2D) for c in outer.curves) == 4


def test_a_hole_comes_out_as_an_inner_loop(kernel: OcctKernel) -> None:
    flat = profile_of(kernel, build(kernel), "panel/cap+")
    inner = [loop for loop in flat.loops if not loop.outer]
    assert len(inner) == 1
    assert all(isinstance(c, Arc2D) for c in inner[0].curves)


def test_the_outer_loop_is_written_first(kernel: OcctKernel) -> None:
    """So a machine reading top to bottom cuts the part before its holes."""
    flat = profile_of(kernel, build(kernel), "panel/cap+")
    assert flat.loops[0].outer


def test_a_curved_face_is_refused_rather_than_projected(kernel: OcctKernel) -> None:
    """The projection of a cylinder is not a cut path."""
    result = build(kernel)
    named = result.solid
    ref = next(r for r, t in named.refs.items() if "fillet" in str(t))
    with pytest.raises(FeatureBuildError, match="planar"):
        kernel.face_profile(named.handle, ref, 0.01)


# -- the part that matters -------------------------------------------------


def test_the_cut_path_tracks_a_parameter_change(kernel: OcctKernel) -> None:
    """A wider panel gives a wider path — and still one outer plus one hole."""
    before = profile_of(kernel, build(kernel), "panel/cap+")
    after = profile_of(kernel, build(kernel, w=120.0), "panel/cap+")

    assert len(before.loops) == len(after.loops) == 2
    assert _width(after) == pytest.approx(_width(before) + 40.0, abs=1e-6)


def test_the_same_document_flattens_identically_twice(kernel: OcctKernel) -> None:
    """Byte-identical, or a CAM diff is unreadable."""
    first = export_drawing([profile_of(kernel, build(kernel), "panel/cap+")], "dxf")
    second = export_drawing([profile_of(kernel, build(kernel), "panel/cap+")], "dxf")
    assert first == second


@pytest.mark.parametrize(
    "values",
    [{"w": 95.0}, {"h": 71.0}, {"rad": 9.0}, {"bore_d": 12.0}, {"t": 5.0}],
    ids=["wider", "taller", "rounder", "bigger bore", "thicker"],
)
def test_the_hole_stays_centred_through_a_sweep(
    kernel: OcctKernel, values: dict[str, float]
) -> None:
    """The failure mode this project exists to prevent, in 2D form."""
    flat = profile_of(kernel, build(kernel, **values), "panel/cap+")
    inner = next(loop for loop in flat.loops if not loop.outer)
    hole = inner.curves[0]
    assert isinstance(hole, Arc2D)

    outer_x, outer_y = _centre(flat)
    assert hole.centre[0] == pytest.approx(outer_x, abs=1e-6)
    assert hole.centre[1] == pytest.approx(outer_y, abs=1e-6)


# -- drawings --------------------------------------------------------------


def test_three_views_land_in_one_file(kernel: OcctKernel) -> None:
    data = kernel.export_drawing(build(kernel).solid.handle, "dxf", ["top", "front", "right"])
    text = data.decode("ascii")
    for view in ("top", "front", "right"):
        assert view in text


def test_an_unknown_view_lists_the_ones_that_exist(kernel: OcctKernel) -> None:
    from facet.domain.errors import DocumentError

    with pytest.raises(DocumentError, match="front"):
        kernel.export_drawing(build(kernel).solid.handle, "dxf", ["isometric"])


# -- helpers ---------------------------------------------------------------


def _extent(profile) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for loop in profile.loops:
        if not loop.outer:
            continue
        for curve in loop.curves:
            if isinstance(curve, Line2D):
                xs.extend((curve.start[0], curve.end[0]))
                ys.extend((curve.start[1], curve.end[1]))
            else:
                xs.extend((curve.centre[0] - curve.radius, curve.centre[0] + curve.radius))
                ys.extend((curve.centre[1] - curve.radius, curve.centre[1] + curve.radius))
    return (min(xs), min(ys), max(xs), max(ys))


def _width(profile) -> float:
    min_x, _, max_x, _ = _extent(profile)
    return max_x - min_x


def _centre(profile) -> tuple[float, float]:
    min_x, min_y, max_x, max_y = _extent(profile)
    return ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0)


# -- turning a click into coordinates --------------------------------------


def test_a_world_point_maps_into_every_datum_plane(tmp_path) -> None:
    """The click-to-place path: a point becomes two numbers on a datum.

    Deliberately no face reference in the answer. Datums come from parameters
    alone, so this only saves typing.
    """
    from facet.adapters.persistence.filesystem import FilesystemDocumentRepository
    from facet.application.services import ProjectService

    repository = FilesystemDocumentRepository(tmp_path)
    service = ProjectService(repository, OcctKernel())
    repository.create("p", Document.from_dict(copy.deepcopy(PANEL)))

    found = service.locate("p", (40.0, 30.0, 3.0))
    assert found, "every datum should be offered, not just one"

    by_datum = {row["datum"]: row for row in found}
    assert by_datum["base"]["u"] == pytest.approx(40.0)
    assert by_datum["base"]["v"] == pytest.approx(30.0)
    assert by_datum["base"]["offset"] == pytest.approx(3.0)

    # Nearest plane first, so the obvious choice is the default one.
    offsets = [abs(float(row["offset"])) for row in found]
    assert offsets == sorted(offsets)

    # No topology in the answer: a click is numbers and, at most, the name of a
    # parameter that already resolves to the offset — never a reference.
    expected = {"datum", "u", "v", "offset", "offsetParameter"}
    assert all(set(row) == expected for row in found)
    assert by_datum["base"]["offsetParameter"] == "t"  # the 3mm sheet thickness


def test_a_selector_naming_one_body_does_not_fail_on_the_others(tmp_path) -> None:
    """Two bodies, a face on one of them: the export must still work."""
    from facet.adapters.persistence.filesystem import FilesystemDocumentRepository
    from facet.application.services import ProjectService

    data = copy.deepcopy(PANEL)
    data["bodies"] = [
        {"id": "panel", "features": data.pop("features")},
        {
            "id": "spacer",
            "placement": {"origin": [200.0, 0.0, 0.0], "rotation": [0.0, 0.0, 0.0]},
            "features": [
                {"id": "block", "type": "pad", "profile": "outline.outer", "length": 5.0}
            ],
        },
    ]

    repository = FilesystemDocumentRepository(tmp_path)
    service = ProjectService(repository, OcctKernel())
    repository.create("two", Document.from_dict(data))

    paths = service.cut_paths("two", "panel/cap+")
    assert len(paths) == 1
    assert paths[0].label == "panel/cap+"


def test_an_unmatched_selector_still_says_so(tmp_path) -> None:
    from facet.adapters.persistence.filesystem import FilesystemDocumentRepository
    from facet.application.services import ProjectService
    from facet.domain.errors import DocumentError

    repository = FilesystemDocumentRepository(tmp_path)
    service = ProjectService(repository, OcctKernel())
    repository.create("one", Document.from_dict(copy.deepcopy(PANEL)))

    with pytest.raises(DocumentError, match="nothing to cut"):
        service.cut_paths("one", "nosuch/cap+")
