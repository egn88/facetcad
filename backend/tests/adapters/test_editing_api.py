"""Editing parameters, sketches and datums over the API.

These are the operations that let a model be built without hand-editing YAML.
The one that has to be exactly right is **rename**: a parameter renamed in the
sheet must be renamed in every expression that reads it, everywhere in the
document, or the rename quietly breaks the model.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from facet.adapters.geometry.fake import FakeKernel
from facet.adapters.persistence.filesystem import FilesystemDocumentRepository
from facet.application.services import ProjectService
from facet.main import create_app

from ..application.test_recompute import BRACKET


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    service = ProjectService(FilesystemDocumentRepository(tmp_path), FakeKernel())
    api = TestClient(create_app(service))
    response = api.post(
        "/api/projects", json={"id": "bracket", "name": "Bracket", "document": BRACKET}
    )
    assert response.status_code == 201, response.text
    return api


def document(client: TestClient) -> dict:
    return client.get("/api/projects/bracket/document").json()


def parameter(client: TestClient, name: str) -> dict:
    return next(p for p in document(client)["parameters"] if p["name"] == name)


# --------------------------------------------------------------------------
# Adding parameters
# --------------------------------------------------------------------------


def test_a_parameter_can_be_added(client: TestClient) -> None:
    body = client.post(
        "/api/projects/bracket/parameters",
        json={"name": "fillet_r", "value": 3.0, "group": "Edges", "doc": "corner radius"},
    ).json()
    assert body["ok"] is True
    assert body["parameters"]["fillet_r"] == 3.0
    assert parameter(client, "fillet_r")["doc"] == "corner radius"


def test_an_added_parameter_may_be_an_expression(client: TestClient) -> None:
    body = client.post(
        "/api/projects/bracket/parameters",
        json={"name": "area", "expr": "plate_w * plate_h"},
    ).json()
    assert body["parameters"]["area"] == pytest.approx(120.0 * 72.0)


def test_a_duplicate_parameter_name_is_refused(client: TestClient) -> None:
    response = client.post(
        "/api/projects/bracket/parameters", json={"name": "plate_w", "value": 1.0}
    )
    assert response.status_code == 422


def test_a_parameter_needing_a_value_or_expression_is_refused(client: TestClient) -> None:
    response = client.post("/api/projects/bracket/parameters", json={"name": "empty"})
    assert response.status_code == 422


def test_an_added_parameter_may_carry_a_unit(client: TestClient) -> None:
    body = client.post(
        "/api/projects/bracket/parameters",
        json={"name": "bore", "value": 0.5, "unit": "in"},
    ).json()
    assert body["parameters"]["bore"] == pytest.approx(12.7)


# --------------------------------------------------------------------------
# Renaming — the operation that must follow through everywhere
# --------------------------------------------------------------------------


def test_renaming_rewrites_dependent_expressions(client: TestClient) -> None:
    """`plate_h` is `plate_w * 0.6`; renaming plate_w must update it."""
    body = client.patch(
        "/api/projects/bracket/parameters/plate_w", json={"name": "width"}
    ).json()
    assert body["ok"] is True, body
    assert parameter(client, "plate_h")["expr"] == "width * 0.6"
    assert body["parameters"]["plate_h"] == pytest.approx(72.0)


def test_renaming_rewrites_sketch_coordinates(client: TestClient) -> None:
    client.patch("/api/projects/bracket/parameters/plate_w", json={"name": "width"})
    points = document(client)["sketches"]["outline"]["points"]
    assert points["p1"] == ["width", 0]
    assert document(client)["sketches"]["hole"]["points"]["q0"][0] == "width / 2 - slot_w / 2"


def test_renaming_rewrites_datums_and_features(client: TestClient) -> None:
    client.patch("/api/projects/bracket/parameters/plate_t", json={"name": "thickness"})
    assert document(client)["datums"]["top"]["origin"] == [0, 0, "thickness"]
    assert document(client)["features"][0]["length"] == "thickness"


def test_the_model_still_builds_identically_after_a_rename(client: TestClient) -> None:
    before = client.get("/api/projects/bracket/topology").json()
    client.patch("/api/projects/bracket/parameters/plate_w", json={"name": "width"})
    after = client.get("/api/projects/bracket/topology").json()
    assert [f["tag"] for f in after["faces"]] == [f["tag"] for f in before["faces"]]


def test_a_rename_keeps_the_row_in_place(client: TestClient) -> None:
    """Sheet order is how a sheet reads; a rename must not move the row."""
    before = [p["name"] for p in document(client)["parameters"]]
    client.patch("/api/projects/bracket/parameters/plate_w", json={"name": "width"})
    after = [p["name"] for p in document(client)["parameters"]]
    assert after == ["width", *before[1:]]


def test_renaming_onto_an_existing_name_is_refused(client: TestClient) -> None:
    response = client.patch(
        "/api/projects/bracket/parameters/plate_w", json={"name": "plate_t"}
    )
    assert response.status_code == 422


def test_renaming_to_a_non_identifier_is_refused(client: TestClient) -> None:
    response = client.patch(
        "/api/projects/bracket/parameters/plate_w", json={"name": "plate width"}
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------
# Editing other fields
# --------------------------------------------------------------------------


def test_group_and_doc_can_be_edited(client: TestClient) -> None:
    client.patch(
        "/api/projects/bracket/parameters/plate_t",
        json={"group": "Stock", "doc": "sheet thickness"},
    )
    row = parameter(client, "plate_t")
    assert row["group"] == "Stock"
    assert row["doc"] == "sheet thickness"


def test_a_literal_can_become_an_expression(client: TestClient) -> None:
    body = client.patch(
        "/api/projects/bracket/parameters/slot_w", json={"expr": "plate_w / 8"}
    ).json()
    assert body["parameters"]["slot_w"] == pytest.approx(15.0)
    assert parameter(client, "slot_w").get("value") is None


def test_an_expression_can_become_a_literal(client: TestClient) -> None:
    body = client.patch(
        "/api/projects/bracket/parameters/plate_h", json={"value": 90.0}
    ).json()
    assert body["parameters"]["plate_h"] == 90.0
    assert parameter(client, "plate_h").get("expr") is None


def test_editing_an_unknown_parameter_is_404_shaped(client: TestClient) -> None:
    response = client.patch("/api/projects/bracket/parameters/ghost", json={"value": 1.0})
    assert response.status_code == 422


# --------------------------------------------------------------------------
# Deleting, with a guard
# --------------------------------------------------------------------------


def test_an_unused_parameter_can_be_deleted(client: TestClient) -> None:
    client.post("/api/projects/bracket/parameters", json={"name": "spare", "value": 1.0})
    body = client.delete("/api/projects/bracket/parameters/spare").json()
    assert body["ok"] is True
    assert "spare" not in body["parameters"]


def test_deleting_a_used_parameter_is_refused_and_says_who_uses_it(
    client: TestClient,
) -> None:
    """Better than deleting it and leaving the model broken."""
    response = client.delete("/api/projects/bracket/parameters/plate_w")
    assert response.status_code == 422
    detail = str(response.json()["detail"])
    assert "plate_h" in detail
    assert "outline" in detail


def test_usage_can_be_checked_before_deleting(client: TestClient) -> None:
    body = client.get("/api/projects/bracket/parameters/plate_w/usage").json()
    assert any("plate_h" in user for user in body["usedBy"])
    assert any("outline" in user for user in body["usedBy"])


def test_an_unused_parameter_reports_no_usage(client: TestClient) -> None:
    client.post("/api/projects/bracket/parameters", json={"name": "spare", "value": 1.0})
    assert client.get("/api/projects/bracket/parameters/spare/usage").json()["usedBy"] == []


# --------------------------------------------------------------------------
# Sketches
# --------------------------------------------------------------------------

#: Rectangular, because these tests run on the analytic kernel.
PANEL = {
    "id": "tri",
    "plane": "base",
    # Inset from the plate's corner: flush with it, two of the panel's sides
    # would be coplanar with the plate's and merge into them, correctly
    # inheriting the plate's tags rather than gaining their own.
    "points": {
        "a": [10, 10],
        "b": ["plate_w / 3", 10],
        "c": ["plate_w / 3", "plate_h / 3"],
        "d": [10, "plate_h / 3"],
    },
    "curves": [
        {"id": "s0", "start": "a", "end": "b"},
        {"id": "s1", "start": "b", "end": "c"},
        {"id": "s2", "start": "c", "end": "d"},
        {"id": "s3", "start": "d", "end": "a"},
    ],
    "loops": [{"id": "outer", "curves": ["s0", "s1", "s2", "s3"]}],
}


def test_a_sketch_can_be_created(client: TestClient) -> None:
    response = client.put("/api/projects/bracket/sketches/tri", json=PANEL)
    assert response.status_code == 200, response.text
    assert "tri" in document(client)["sketches"]


def test_a_new_sketch_can_immediately_drive_a_feature(client: TestClient) -> None:
    """The point of the editor: draw a profile, then pad it.

    The panel sits on the `top` datum so the pad stands proud of the plate. On
    the base datum it would be buried inside it, and fusing would correctly
    swallow it whole.
    """
    client.put("/api/projects/bracket/sketches/tri", json={**PANEL, "plane": "top"})
    body = client.post(
        "/api/projects/bracket/features",
        json={"spec": {"id": "wedge", "type": "pad", "profile": "tri.outer", "length": 4}},
    ).json()
    assert body["ok"] is True, body
    tags = [f["tag"] for f in client.get("/api/projects/bracket/topology").json()["faces"]]
    assert "wedge/side[tri.s0]" in tags


def test_a_second_pad_adds_to_the_body_rather_than_replacing_it(
    client: TestClient,
) -> None:
    """A body is one solid, so pads accumulate.

    Before this, the later pad silently discarded everything built before it.
    """
    before = client.get("/api/projects/bracket/topology").json()
    client.put("/api/projects/bracket/sketches/tri", json={**PANEL, "plane": "top"})
    client.post(
        "/api/projects/bracket/features",
        json={"spec": {"id": "wedge", "type": "pad", "profile": "tri.outer", "length": 4}},
    )
    after = client.get("/api/projects/bracket/topology").json()

    tags = {f["tag"] for f in after["faces"]}
    # The original plate's faces are all still there...
    for face in before["faces"]:
        if face["tag"] != "base/cap+":  # the top gained the wedge, so it changed
            assert face["tag"] in tags
    # ...alongside the new pad's.
    assert any(tag.startswith("wedge/") for tag in tags)


def test_an_existing_sketch_can_be_replaced(client: TestClient) -> None:
    updated = {**PANEL, "id": "outline"}
    response = client.put("/api/projects/bracket/sketches/outline", json=updated)
    assert response.status_code == 200, response.text
    assert len(document(client)["sketches"]["outline"]["points"]) == 4


def test_a_sketch_referencing_an_unknown_point_is_refused(client: TestClient) -> None:
    broken = {**PANEL, "curves": [{"id": "s0", "start": "a", "end": "ghost"}]}
    response = client.put("/api/projects/bracket/sketches/tri", json=broken)
    assert response.status_code == 422
    assert "ghost" in str(response.json()["detail"])


def test_a_sketch_on_an_unknown_datum_is_refused(client: TestClient) -> None:
    response = client.put(
        "/api/projects/bracket/sketches/tri", json={**PANEL, "plane": "nowhere"}
    )
    assert response.status_code == 422


def test_an_unused_sketch_can_be_deleted(client: TestClient) -> None:
    client.put("/api/projects/bracket/sketches/tri", json=PANEL)
    assert client.delete("/api/projects/bracket/sketches/tri").status_code == 200
    assert "tri" not in document(client)["sketches"]


def test_deleting_a_sketch_a_feature_uses_is_refused(client: TestClient) -> None:
    response = client.delete("/api/projects/bracket/sketches/outline")
    assert response.status_code == 422
    assert "base" in str(response.json()["detail"])


# --------------------------------------------------------------------------
# Datums
# --------------------------------------------------------------------------


def test_a_datum_can_be_created_from_parameters(client: TestClient) -> None:
    response = client.put(
        "/api/projects/bracket/datums/mid",
        json={"id": "mid", "origin": [0, 0, "plate_t / 2"], "normal": [0, 0, 1]},
    )
    assert response.status_code == 200, response.text
    assert document(client)["datums"]["mid"]["origin"] == [0, 0, "plate_t / 2"]


def test_a_new_datum_can_host_a_sketch(client: TestClient) -> None:
    client.put(
        "/api/projects/bracket/datums/mid",
        json={"id": "mid", "origin": [0, 0, "plate_t / 2"], "normal": [0, 0, 1]},
    )
    response = client.put(
        "/api/projects/bracket/sketches/tri", json={**PANEL, "plane": "mid"}
    )
    assert response.status_code == 200, response.text


def test_a_zero_length_datum_normal_is_refused(client: TestClient) -> None:
    response = client.put(
        "/api/projects/bracket/datums/bad",
        json={"id": "bad", "origin": [0, 0, 0], "normal": [0, 0, 0]},
    )
    assert response.status_code == 422


def test_deleting_a_datum_a_sketch_sits_on_is_refused(client: TestClient) -> None:
    response = client.delete("/api/projects/bracket/datums/base")
    assert response.status_code == 422
    assert "outline" in str(response.json()["detail"])


def test_an_unused_datum_can_be_deleted(client: TestClient) -> None:
    client.put(
        "/api/projects/bracket/datums/spare", json={"id": "spare", "origin": [0, 0, 50]}
    )
    assert client.delete("/api/projects/bracket/datums/spare").status_code == 200


def test_a_datum_proposed_for_a_face_can_be_put_back_unchanged(
    client: TestClient,
) -> None:
    """Propose, then PUT: the payload is the one the datum endpoint takes.

    Any translation between the two would be a place for the expression to be
    resolved to a number in passing, which is the one thing the proposal exists
    to avoid.
    """
    proposed = client.post(
        "/api/projects/bracket/datums/for-face", json={"tag": "base/cap+"}
    ).json()
    assert proposed["ok"]
    assert proposed["datum"]["origin"] == [0, 0, "plate_t"]
    assert proposed["existing"] == "top"

    stored = client.put(
        f"/api/projects/bracket/datums/{proposed['datum']['id']}", json=proposed["datum"]
    )
    assert stored.status_code == 200, stored.text
    assert document(client)["datums"]["base_cap_pos"]["origin"] == [0, 0, "plate_t"]


def test_a_face_whose_plane_cannot_be_derived_answers_rather_than_fails(
    client: TestClient,
) -> None:
    """'You must place this one yourself' is an answer, so it is a 200.

    A counterbore wall is the case that stays refused however far the derivation
    grows: it is a cylinder, and a cylinder lies in no plane at all.
    """
    response = client.post(
        "/api/projects/bracket/datums/for-face", json={"tag": "slot/cbore"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["datum"] is None
    assert body["reason"]


def test_a_face_tag_naming_no_feature_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/projects/bracket/datums/for-face", json={"tag": "ghost/cap+"}
    )
    assert response.status_code == 422
    assert "ghost" in str(response.json()["detail"])


def test_edits_persist_across_a_reload(client: TestClient) -> None:
    client.post("/api/projects/bracket/parameters", json={"name": "extra", "value": 7.0})
    client.put("/api/projects/bracket/sketches/tri", json=PANEL)
    reloaded = document(client)
    assert any(p["name"] == "extra" for p in reloaded["parameters"])
    assert "tri" in reloaded["sketches"]
