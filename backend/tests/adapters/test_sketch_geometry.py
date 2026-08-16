"""Drawable sketch geometry.

Sketches have to be visible in the viewport, not only in the editor — otherwise
you cannot see what a profile actually looks like without opening a dialog.

The important design property is that this works **even when the model does not
build**: a sketch you cannot yet pad is exactly the one you most need to look
at, so resolution goes straight from parameters and datums rather than through
the feature history.
"""

from __future__ import annotations

import copy
import math
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from facet.adapters.geometry.fake import FakeKernel
from facet.adapters.persistence.filesystem import FilesystemDocumentRepository
from facet.application.services import ProjectService
from facet.domain.document import Document
from facet.main import create_app

from ..application.test_recompute import BRACKET


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    service = ProjectService(FilesystemDocumentRepository(tmp_path), FakeKernel())
    api = TestClient(create_app(service))
    assert api.post(
        "/api/projects", json={"id": "bracket", "document": BRACKET}
    ).status_code == 201
    return api


def geometry(client: TestClient, project: str = "bracket") -> dict:
    return client.get(f"/api/projects/{project}/sketches/geometry").json()


def sketch(body: dict, identifier: str) -> dict:
    return next(s for s in body["sketches"] if s["id"] == identifier)


# --------------------------------------------------------------------------
# Straight profiles
# --------------------------------------------------------------------------


def test_every_sketch_is_reported(client: TestClient) -> None:
    body = geometry(client)
    assert {s["id"] for s in body["sketches"]} == {"outline", "hole"}
    assert body["error"] is None


def test_a_line_becomes_a_two_point_polyline(client: TestClient) -> None:
    outline = sketch(geometry(client), "outline")
    bottom = next(c for c in outline["curves"] if c["id"] == "bottom")
    assert bottom["type"] == "line"
    assert len(bottom["points"]) == 6  # two xyz triples


def test_polylines_are_in_world_coordinates_on_their_datum(client: TestClient) -> None:
    """The 'hole' sketch sits on the 'top' datum at z = plate_t."""
    body = geometry(client)
    assert all(p == 0.0 for c in sketch(body, "outline")["curves"] for p in c["points"][2::3])
    assert all(p == 6.0 for c in sketch(body, "hole")["curves"] for p in c["points"][2::3])


def test_the_outline_matches_its_parameters(client: TestClient) -> None:
    bottom = next(
        c for c in sketch(geometry(client), "outline")["curves"] if c["id"] == "bottom"
    )
    assert bottom["points"][:3] == [0.0, 0.0, 0.0]
    assert bottom["points"][3:6] == [120.0, 0.0, 0.0]  # plate_w


def test_geometry_follows_a_parameter_change(client: TestClient) -> None:
    client.patch("/api/projects/bracket/parameters", json={"changes": {"plate_w": 250}})
    bottom = next(
        c for c in sketch(geometry(client), "outline")["curves"] if c["id"] == "bottom"
    )
    assert bottom["points"][3] == 250.0


def test_named_points_are_reported_too(client: TestClient) -> None:
    """Hole placement points have no curves at all, so points must be shown."""
    outline = sketch(geometry(client), "outline")
    assert {p["id"] for p in outline["points"]} == {"p0", "p1", "p2", "p3"}
    p1 = next(p for p in outline["points"] if p["id"] == "p1")
    assert p1["at"] == [120.0, 0.0, 0.0]


# --------------------------------------------------------------------------
# Curves are sampled finely enough to look round
# --------------------------------------------------------------------------


CIRCLE_DOC: dict[str, object] = {
    "schema": "cadsheet/1",
    "project": "round",
    "parameters": [{"name": "r", "value": 20.0}],
    "datums": {"base": {"type": "plane", "origin": [0, 0, 0], "normal": [0, 0, 1]}},
    "sketches": {
        "bore": {
            "plane": "base",
            "points": {"m": [0, 0]},
            "curves": [{"id": "rim", "type": "circle", "center": "m", "radius": "r"}],
            "loops": [{"id": "outer", "curves": ["rim"]}],
        }
    },
    "features": [],
}


def test_a_circle_is_tessellated_into_a_smooth_polyline(client: TestClient) -> None:
    client.post("/api/projects", json={"id": "round", "document": CIRCLE_DOC})
    rim = sketch(geometry(client, "round"), "bore")["curves"][0]
    assert rim["type"] == "circle"

    count = len(rim["points"]) // 3
    assert count > 20, "a 20mm circle drawn with fewer points would look faceted"

    # Every sampled point lies on the circle.
    for index in range(count):
        x, y, _ = rim["points"][index * 3 : index * 3 + 3]
        assert math.hypot(x, y) == pytest.approx(20.0, abs=1e-6)


def test_a_circle_polyline_closes(client: TestClient) -> None:
    client.post("/api/projects", json={"id": "round", "document": CIRCLE_DOC})
    points = sketch(geometry(client, "round"), "bore")["curves"][0]["points"]
    assert points[:3] == pytest.approx(points[-3:])


def test_a_smaller_circle_still_looks_round(client: TestClient) -> None:
    """Segment count adapts to radius rather than being fixed."""
    tiny = copy.deepcopy(CIRCLE_DOC)
    tiny["parameters"] = [{"name": "r", "value": 1.0}]
    client.post("/api/projects", json={"id": "tiny", "document": tiny})
    rim = sketch(geometry(client, "tiny"), "bore")["curves"][0]
    assert len(rim["points"]) // 3 >= 8


# --------------------------------------------------------------------------
# The point of it: visible even when the model is broken
# --------------------------------------------------------------------------


def test_sketches_are_drawable_when_the_build_fails(client: TestClient) -> None:
    """A sketch you cannot pad is the one you most need to see."""
    broken = copy.deepcopy(BRACKET)
    broken["features"][1]["direction"] = "+normal"  # type: ignore[index]
    client.post("/api/projects", json={"id": "broken", "document": broken})

    assert client.post("/api/projects/broken/recompute").json()["ok"] is False
    body = geometry(client, "broken")
    assert len(body["sketches"]) == 2
    assert sketch(body, "outline")["curves"]


def test_sketches_are_drawable_with_no_features_at_all(client: TestClient) -> None:
    data = copy.deepcopy(BRACKET)
    data["features"] = []
    client.post("/api/projects", json={"id": "empty", "document": data})
    assert sketch(geometry(client, "empty"), "outline")["curves"]


def test_a_sketch_on_a_missing_datum_reports_itself(tmp_path: Path) -> None:
    """Reachable by hand-editing the YAML, since the API refuses to save it.

    Tested against the service directly rather than through HTTP, because
    `PUT /document` validates and would (correctly) reject this outright.
    """
    data = copy.deepcopy(BRACKET)
    data["sketches"]["outline"]["plane"] = "nowhere"  # type: ignore[index]
    data["features"] = []

    repository = FilesystemDocumentRepository(tmp_path)
    repository.create("hand-edited", Document.from_dict(data))
    service = ProjectService(repository, FakeKernel())

    body = service.sketch_geometry("hand-edited")
    outline = next(s for s in body["sketches"] if s["id"] == "outline")  # type: ignore[union-attr]
    assert outline["curves"] == []
    assert "nowhere" in str(outline["error"])


def test_an_unresolvable_sheet_reports_once_rather_than_per_sketch(
    client: TestClient,
) -> None:
    data = copy.deepcopy(BRACKET)
    data["parameters"] = [{"name": "a", "expr": "b"}, {"name": "b", "expr": "a"}]
    data["features"] = []
    client.post("/api/projects", json={"id": "cyclic", "document": data})

    body = geometry(client, "cyclic")
    assert body["sketches"] == []
    assert "circular" in str(body["error"]).lower()


def test_a_project_without_sketches_is_empty_not_an_error(client: TestClient) -> None:
    client.post("/api/projects", json={"id": "bare", "name": "Bare"})
    body = geometry(client, "bare")
    assert body["sketches"] == []
    assert body["error"] is None
