"""Bodies: one solid each, positioned independently.

A body owns a linear history and produces exactly one solid, as PartDesign
does. Within a body, features chain and a second pad *fuses*. Across bodies,
solids stay separate — which is the whole point, because you cannot assemble
things that have been merged into one.

Placement is applied for display and export only. Keeping it out of the
modelled geometry means moving a body can never perturb a face fingerprint or
a split ordinal, so the naming guarantee survives assembly.
"""

from __future__ import annotations

import copy
import math
import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from facet.adapters.geometry.fake import FakeKernel
from facet.adapters.persistence.filesystem import FilesystemDocumentRepository
from facet.application.services import ProjectService
from facet.main import create_app

from ..application.test_recompute import BRACKET

#: Two separate bodies sharing one parameter sheet — the assembly shape.
TWO_BODIES: dict[str, object] = {
    "schema": "cadsheet/1",
    "project": "two bodies",
    "parameters": [
        {"name": "plate_w", "value": 80.0},
        {"name": "plate_t", "value": 6.0},
        {"name": "pin_w", "value": 10.0},
        {"name": "gap", "value": 100.0},
    ],
    "datums": {"base": {"type": "plane", "origin": [0, 0, 0], "normal": [0, 0, 1]}},
    "sketches": {
        "plate": {
            "plane": "base",
            "points": {
                "p0": [0, 0], "p1": ["plate_w", 0],
                "p2": ["plate_w", "plate_w"], "p3": [0, "plate_w"],
            },
            "curves": [
                {"id": "bottom", "start": "p0", "end": "p1"},
                {"id": "right", "start": "p1", "end": "p2"},
                {"id": "top", "start": "p2", "end": "p3"},
                {"id": "left", "start": "p3", "end": "p0"},
            ],
            "loops": [{"id": "outer", "curves": ["bottom", "right", "top", "left"]}],
        },
        "pin": {
            "plane": "base",
            "points": {
                "q0": [0, 0], "q1": ["pin_w", 0],
                "q2": ["pin_w", "pin_w"], "q3": [0, "pin_w"],
            },
            "curves": [
                {"id": "b", "start": "q0", "end": "q1"},
                {"id": "r", "start": "q1", "end": "q2"},
                {"id": "t", "start": "q2", "end": "q3"},
                {"id": "l", "start": "q3", "end": "q0"},
            ],
            "loops": [{"id": "outer", "curves": ["b", "r", "t", "l"]}],
        },
    },
    "bodies": [
        {
            "id": "plate",
            "features": [
                {"id": "slab", "type": "pad", "profile": "plate.outer", "length": "plate_t"}
            ],
        },
        {
            "id": "pin",
            "placement": {"origin": ["gap", 0, 0], "rotation": [0, 0, 0]},
            "features": [
                {"id": "shaft", "type": "pad", "profile": "pin.outer", "length": 30}
            ],
        },
    ],
}


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    service = ProjectService(FilesystemDocumentRepository(tmp_path), FakeKernel())
    api = TestClient(create_app(service))
    assert api.post(
        "/api/projects", json={"id": "asm", "document": TWO_BODIES}
    ).status_code == 201
    return api


def bodies(client: TestClient, project: str = "asm") -> list[dict]:
    return client.get(f"/api/projects/{project}/bodies").json()["bodies"]


# --------------------------------------------------------------------------
# Separate solids
# --------------------------------------------------------------------------


def test_both_bodies_build(client: TestClient) -> None:
    body = client.post("/api/projects/asm/recompute").json()
    assert body["ok"] is True, body
    assert [b["id"] for b in body["bodies"]] == ["plate", "pin"]
    assert all(b["ok"] for b in body["bodies"])


def test_each_body_keeps_its_own_solid(client: TestClient) -> None:
    """The defect that started this: a second solid used to replace the first."""
    meshes = bodies(client)
    assert len(meshes) == 2
    for mesh in meshes:
        assert len(mesh["positions"]) > 0, f"body {mesh['id']} rendered nothing"


def test_bodies_are_never_merged(client: TestClient) -> None:
    body = client.post("/api/projects/asm/recompute").json()
    plate = next(b for b in body["bodies"] if b["id"] == "plate")
    pin = next(b for b in body["bodies"] if b["id"] == "pin")
    assert plate["faceCount"] == 6
    assert pin["faceCount"] == 6


def test_each_body_has_its_own_named_geometry(client: TestClient) -> None:
    payload = client.get("/api/projects/asm/topologies").json()
    plate = next(b for b in payload["bodies"] if b["id"] == "plate")
    pin = next(b for b in payload["bodies"] if b["id"] == "pin")

    assert "slab/cap+" in {f["tag"] for f in plate["faces"]}
    assert "shaft/cap+" in {f["tag"] for f in pin["faces"]}
    # A body's tags never leak into another's.
    assert not any(f["tag"].startswith("shaft/") for f in plate["faces"])


def test_feature_ids_may_repeat_across_bodies(client: TestClient) -> None:
    """Bodies partition the namespace, so tags cannot collide between them."""
    data = copy.deepcopy(TWO_BODIES)
    data["bodies"][1]["features"][0]["id"] = "slab"  # type: ignore[index]
    client.post("/api/projects", json={"id": "twins", "document": data})

    payload = client.get("/api/projects/twins/topologies").json()
    for body in payload["bodies"]:
        assert "slab/cap+" in {f["tag"] for f in body["faces"]}


# --------------------------------------------------------------------------
# Placement
# --------------------------------------------------------------------------


def test_a_placement_travels_as_a_matrix(client: TestClient) -> None:
    pin = next(b for b in bodies(client) if b["id"] == "pin")
    matrix = pin["placement"]
    assert len(matrix) == 16
    # Column-major: the translation is the last column.
    assert matrix[12:15] == [100.0, 0.0, 0.0]


def test_placement_is_not_baked_into_the_geometry(client: TestClient) -> None:
    """A body is modelled in its own coordinates and placed for display.

    If the placement were applied to the points, moving a body would shift
    every fingerprint and could reorder a split — so it is kept separate.
    """
    pin = next(b for b in bodies(client) if b["id"] == "pin")
    xs = pin["positions"][0::3]
    assert min(xs) == pytest.approx(0.0)
    assert max(xs) == pytest.approx(10.0)  # pin_w, not offset by gap


def test_a_placement_can_be_an_expression(client: TestClient) -> None:
    client.patch("/api/projects/asm/parameters", json={"changes": {"gap": 250}})
    pin = next(b for b in bodies(client) if b["id"] == "pin")
    assert pin["placement"][12] == 250.0


def test_a_body_can_be_moved_over_the_api(client: TestClient) -> None:
    response = client.patch(
        "/api/projects/asm/bodies/pin",
        json={"id": "pin", "origin": [10, 20, 30], "rotation": [0, 0, 90]},
    )
    assert response.status_code == 200, response.text
    pin = next(b for b in bodies(client) if b["id"] == "pin")
    assert pin["placement"][12:15] == [10.0, 20.0, 30.0]


def test_rotation_produces_the_expected_axes(client: TestClient) -> None:
    client.patch(
        "/api/projects/asm/bodies/pin",
        json={"id": "pin", "origin": [0, 0, 0], "rotation": [0, 0, 90]},
    )
    matrix = next(b for b in bodies(client) if b["id"] == "pin")["placement"]
    # Rotating 90 degrees about Z sends X to +Y.
    assert matrix[0] == pytest.approx(0.0, abs=1e-9)
    assert matrix[1] == pytest.approx(1.0)


def test_moving_a_body_does_not_change_any_tag(client: TestClient) -> None:
    """Placement must not disturb naming — that is why it stays out of the solid."""
    before = client.get("/api/projects/asm/topologies").json()
    client.patch(
        "/api/projects/asm/bodies/pin",
        json={"id": "pin", "origin": [500, -300, 12], "rotation": [15, 30, 45]},
    )
    after = client.get("/api/projects/asm/topologies").json()
    assert after == before


# --------------------------------------------------------------------------
# Body lifecycle
# --------------------------------------------------------------------------


def test_a_body_can_be_added(client: TestClient) -> None:
    response = client.post(
        "/api/projects/asm/bodies", json={"id": "cover", "origin": [0, 0, 50]}
    )
    assert response.status_code == 200, response.text
    assert [b["id"] for b in bodies(client)] == ["plate", "pin", "cover"]


def test_a_feature_can_target_a_named_body(client: TestClient) -> None:
    client.post("/api/projects/asm/bodies", json={"id": "cover"})
    client.post(
        "/api/projects/asm/features",
        json={
            "spec": {"id": "lid", "type": "pad", "profile": "pin.outer", "length": 3},
            "body": "cover",
        },
    )
    payload = client.get("/api/projects/asm/topologies").json()
    cover = next(b for b in payload["bodies"] if b["id"] == "cover")
    assert "lid/cap+" in {f["tag"] for f in cover["faces"]}


def test_a_duplicate_body_id_is_refused(client: TestClient) -> None:
    assert client.post("/api/projects/asm/bodies", json={"id": "pin"}).status_code == 422


def test_a_body_can_be_deleted(client: TestClient) -> None:
    assert client.delete("/api/projects/asm/bodies/pin").status_code == 200
    assert [b["id"] for b in bodies(client)] == ["plate"]


def test_the_last_body_cannot_be_deleted(client: TestClient) -> None:
    client.delete("/api/projects/asm/bodies/pin")
    response = client.delete("/api/projects/asm/bodies/plate")
    assert response.status_code == 422
    assert "at least one body" in str(response.json()["detail"])


def test_one_body_failing_does_not_stop_the_others(client: TestClient) -> None:
    """Bodies are independent, so a broken one must not hide a working one."""
    data = copy.deepcopy(TWO_BODIES)
    data["bodies"][1]["features"][0]["length"] = -5  # type: ignore[index]
    client.post("/api/projects", json={"id": "half", "document": data})

    body = client.post("/api/projects/half/recompute").json()
    assert body["ok"] is False
    plate = next(b for b in body["bodies"] if b["id"] == "plate")
    pin = next(b for b in body["bodies"] if b["id"] == "pin")
    assert plate["ok"] is True
    assert pin["ok"] is False
    # The good body still renders.
    assert len(next(m for m in bodies(client, "half") if m["id"] == "plate")["positions"]) > 0


# --------------------------------------------------------------------------
# Documents written before bodies existed
# --------------------------------------------------------------------------


def test_a_flat_document_reads_as_one_body(client: TestClient) -> None:
    client.post("/api/projects", json={"id": "old", "document": BRACKET})
    payload = client.post("/api/projects/old/recompute").json()
    assert [b["id"] for b in payload["bodies"]] == ["main"]
    assert payload["ok"] is True


def test_a_single_body_document_is_still_written_flat(client: TestClient) -> None:
    """Nothing already on disk should churn for a feature it does not use."""
    client.post("/api/projects", json={"id": "old", "document": BRACKET})
    document = client.get("/api/projects/old/document").json()
    assert "features" in document
    assert "bodies" not in document


def test_a_multi_body_document_is_written_as_bodies(client: TestClient) -> None:
    document = client.get("/api/projects/asm/document").json()
    assert "bodies" in document
    assert [b["id"] for b in document["bodies"]] == ["plate", "pin"]


def test_a_multi_body_document_round_trips(client: TestClient) -> None:
    exported = client.get("/api/projects/asm/document?fmt=yaml").text
    client.post("/api/projects", json={"id": "clone"})
    assert client.put(
        "/api/projects/clone/document", json={"yaml": exported}
    ).status_code == 200

    assert client.get("/api/projects/clone/topologies").json() == (
        client.get("/api/projects/asm/topologies").json()
    )


def test_volumes_are_unaffected_by_placement(client: TestClient) -> None:
    """Sanity: a moved body is the same body."""
    meshes = bodies(client)
    pin = next(m for m in meshes if m["id"] == "pin")
    triangles_before = len(pin["indices"])

    client.patch(
        "/api/projects/asm/bodies/pin",
        json={"id": "pin", "origin": [1, 2, 3], "rotation": [10, 20, 30]},
    )
    pin_after = next(m for m in bodies(client) if m["id"] == "pin")
    assert len(pin_after["indices"]) == triangles_before
    assert not math.isnan(pin_after["placement"][0])


# -- exporting one body at a time -------------------------------------------


def two_part_document() -> dict[str, object]:
    """A base and a lid, the lid set aside by its placement."""

    def rect(name: str, x: str, w: str) -> dict[str, object]:
        return {
            "plane": "xy",
            "points": {
                f"{name}0": [x, 0],
                f"{name}1": [f"({x}) + ({w})", 0],
                f"{name}2": [f"({x}) + ({w})", 20],
                f"{name}3": [x, 20],
            },
            "curves": [
                {"id": "e0", "type": "line", "start": f"{name}0", "end": f"{name}1"},
                {"id": "e1", "type": "line", "start": f"{name}1", "end": f"{name}2"},
                {"id": "e2", "type": "line", "start": f"{name}2", "end": f"{name}3"},
                {"id": "e3", "type": "line", "start": f"{name}3", "end": f"{name}0"},
            ],
            "loops": [{"id": "outer", "curves": ["e0", "e1", "e2", "e3"]}],
        }

    return {
        "schema": "cadsheet/1",
        "parameters": [{"name": "gap", "value": 50.0}],
        "datums": {},
        "sketches": {"a": rect("a", "0", "30"), "b": rect("b", "0", "30")},
        "bodies": [
            {
                "id": "base",
                "features": [
                    {"id": "base_pad", "type": "pad", "profile": "a.outer", "length": 10.0}
                ],
            },
            {
                "id": "lid",
                "placement": {"origin": ["gap", 0, 0], "rotation": [0, 0, 0]},
                "features": [
                    {"id": "lid_pad", "type": "pad", "profile": "b.outer", "length": 4.0}
                ],
            },
        ],
    }


@pytest.fixture
def two_part(client: TestClient) -> TestClient:
    created = client.post(
        "/api/projects",
        json={"id": "pair", "name": "Pair", "document": two_part_document()},
    )
    assert created.status_code == 201, created.text
    return client


def _triangles(stl: bytes) -> int:
    return struct.unpack("<I", stl[80:84])[0]


def _x_range(stl: bytes) -> tuple[float, float]:
    """The x extent of a binary STL, for checking a body landed where it should."""
    xs: list[float] = []
    for index in range(_triangles(stl)):
        start = 84 + index * 50 + 12
        for vertex in range(3):
            offset = start + vertex * 12
            xs.append(struct.unpack("<fff", stl[offset : offset + 12])[0])
    return (min(xs), max(xs))


def test_a_mesh_export_includes_every_body_by_default(two_part: TestClient) -> None:
    """A document that builds two parts must not quietly export one of them."""
    whole = two_part.get("/api/projects/pair/export?fmt=stl")
    base = two_part.get("/api/projects/pair/export?fmt=stl&body=base")
    lid = two_part.get("/api/projects/pair/export?fmt=stl&body=lid")

    assert whole.status_code == base.status_code == lid.status_code == 200
    assert _triangles(whole.content) == _triangles(base.content) + _triangles(lid.content)


def test_one_body_can_be_exported_on_its_own(two_part: TestClient) -> None:
    """Printing a multi-part model needs the parts separately."""
    response = two_part.get("/api/projects/pair/export?fmt=stl&body=lid")
    assert response.status_code == 200
    assert "pair-lid.stl" in response.headers["content-disposition"]


def test_an_exported_body_carries_its_placement(two_part: TestClient) -> None:
    """A file has nowhere to hold a matrix, so the points have to be moved.

    Without this the two parts land on top of each other in the slicer, which is
    the sort of thing only noticed after a print.
    """
    low, high = _x_range(two_part.get("/api/projects/pair/export?fmt=stl&body=lid").content)
    assert low == pytest.approx(50.0, abs=1e-3)
    assert high == pytest.approx(80.0, abs=1e-3)


def test_an_unknown_body_says_which_ones_there_are(two_part: TestClient) -> None:
    response = two_part.get("/api/projects/pair/export?fmt=stl&body=ghost")
    assert response.status_code == 422
    message = response.json()["detail"]["message"]
    assert "base" in message and "lid" in message
