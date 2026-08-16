"""Laying a solid's faces out flat — the part as a cutting list.

Distinct from the enclosure generator, which builds a container *for* a part.
This takes the part apart: a notched block gives ten panels, a wedge gives
five, and a fillet gives none because a cylinder has no flat development.
"""

from __future__ import annotations

import copy
from itertools import pairwise

import pytest

pytest.importorskip("OCP", reason="requires the optional OCCT extra")

from facet.adapters.geometry.occt import OcctKernel
from facet.adapters.persistence.filesystem import FilesystemDocumentRepository
from facet.application.flatten import lay_out
from facet.application.ports.geometry import Line2D, Loop2D, Profile2D
from facet.application.services import ProjectService
from facet.domain.document import Document

pytestmark = pytest.mark.occt

#: A wedge (5 faces) and a notched block (10 faces) in one body, with a fillet
#: and two chamfers on the wedge — the shape this was reported against.
PART: dict[str, object] = {
    "schema": "cadsheet/1",
    "project": "flatten me",
    "parameters": [
        {"name": "span", "value": 40.0},
        {"name": "rise", "value": 15.0},
        {"name": "t", "value": 10.0},
        {"name": "block", "value": 10.0},
    ],
    "datums": {},
    "sketches": {
        "wedge": {
            "plane": "xy",
            "points": {"a": [0, 0], "b": ["span", 0], "c": ["span / 2", "rise"]},
            "curves": [
                {"id": "c0", "type": "line", "start": "a", "end": "b"},
                {"id": "c1", "type": "line", "start": "b", "end": "c"},
                {"id": "c2", "type": "line", "start": "c", "end": "a"},
            ],
            "loops": [{"id": "outer", "curves": ["c0", "c1", "c2"]}],
        },
        "notched": {
            "plane": "xy",
            "points": {
                "p0": ["span + 20", 0],
                "p1": ["span + 20 + block / 3", 0],
                "p2": ["span + 20 + block / 3", "block / 3"],
                "p3": ["span + 20 + 2 * block / 3", "block / 3"],
                "p4": ["span + 20 + 2 * block / 3", 0],
                "p5": ["span + 20 + block", 0],
                "p6": ["span + 20 + block", "block"],
                "p7": ["span + 20", "block"],
            },
            "curves": [
                {"id": f"e{i}", "type": "line", "start": f"p{i}", "end": f"p{(i + 1) % 8}"}
                for i in range(8)
            ],
            "loops": [{"id": "outer", "curves": [f"e{i}" for i in range(8)]}],
        },
    },
    "features": [
        {"id": "block", "type": "pad", "profile": "notched.outer", "length": "block"},
        {"id": "wedge", "type": "pad", "profile": "wedge.outer", "length": "t"},
        {
            "id": "bevel",
            "type": "chamfer",
            "distance": 1.0,
            "edges": "wedge/cap+ ^ wedge/side[wedge.c1], wedge/cap+ ^ wedge/side[wedge.c2]",
        },
        {"id": "soft", "type": "fillet", "radius": 2.0,
         "edges": "wedge/cap+ ^ wedge/side[wedge.c0]"},
    ],
}


@pytest.fixture(scope="module")
def service(tmp_path_factory) -> ProjectService:
    folder = tmp_path_factory.mktemp("flatten")
    repository = FilesystemDocumentRepository(folder)
    api = ProjectService(repository, OcctKernel())
    repository.create("part", Document.from_dict(copy.deepcopy(PART)))
    return api


def test_a_part_flattens_to_one_panel_per_planar_face(service: ProjectService) -> None:
    """Ten for the notched block, five for the wedge."""
    result = service.flat_faces("part")
    assert len(result.panels) == 15


def test_blend_faces_are_left_out_by_default(service: ProjectService) -> None:
    """A 1mm chamfer sliver is not something anybody wants to cut."""
    plain = service.flat_faces("part")
    with_blends = service.flat_faces("part", include_blends=True)
    assert len(with_blends.panels) == len(plain.panels) + 2


def test_a_curved_face_is_reported_rather_than_dropped(service: ProjectService) -> None:
    """A cutting list that quietly does not add up is worse than an error."""
    result = service.flat_faces("part", include_blends=True)
    assert len(result.skipped) == 1
    assert "soft/fillet" in result.skipped[0]


def test_nothing_is_skipped_when_blends_are_excluded(service: ProjectService) -> None:
    assert service.flat_faces("part").skipped == ()


def test_every_panel_is_named_after_its_face(service: ProjectService) -> None:
    """So a panel coming off the bed can be matched back to the model."""
    labels = [p.label for p in service.flat_faces("part").panels]
    assert all(labels)
    assert len(set(labels)) == len(labels)
    assert any(label.startswith("wedge/") for label in labels)
    assert any(label.startswith("block/") for label in labels)


def test_the_panels_do_not_overlap_on_the_sheet(service: ProjectService) -> None:
    """Faces are modelled where they sit in space; flattened they would pile up."""
    spans = []
    for panel in service.flat_faces("part").panels:
        xs = [c.start[0] for c in panel.loops[0].curves]
        spans.append((min(xs), max(xs)))
    spans.sort()
    for (_, end), (start, _) in pairwise(spans):
        assert start >= end - 1e-6


def test_flattening_survives_a_parameter_change(service: ProjectService) -> None:
    """The cutting list tracks the model, which is the point of doing it here."""
    before = {p.label for p in service.flat_faces("part").panels}

    service.update_parameters("part", {"span": 55.0})
    after = service.flat_faces("part")
    assert {p.label for p in after.panels} == before


# -- layout ----------------------------------------------------------------


def square(x: float, size: float = 10.0) -> Profile2D:
    return Profile2D(
        loops=(
            Loop2D(
                curves=(
                    Line2D((x, 0.0), (x + size, 0.0)),
                    Line2D((x + size, 0.0), (x + size, size)),
                    Line2D((x + size, size), (x, size)),
                    Line2D((x, size), (x, 0.0)),
                )
            ),
        ),
        label=f"at {x}",
    )


def test_layout_packs_from_the_origin() -> None:
    placed = lay_out([square(100.0), square(-40.0)], gap=5.0)
    first = [c.start[0] for c in placed[0].loops[0].curves]
    assert min(first) == pytest.approx(0.0)


def test_layout_keeps_a_gap_between_panels() -> None:
    placed = lay_out([square(0.0), square(0.0)], gap=5.0)
    first_end = max(c.start[0] for c in placed[0].loops[0].curves)
    second_start = min(c.start[0] for c in placed[1].loops[0].curves)
    assert second_start - first_end == pytest.approx(5.0)


def test_an_empty_profile_is_skipped_rather_than_shifting_the_row() -> None:
    placed = lay_out([Profile2D(loops=(Loop2D(),), label="empty"), square(0.0)])
    assert len(placed) == 1
