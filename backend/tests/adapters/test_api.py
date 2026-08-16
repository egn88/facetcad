"""HTTP adapter tests, driving the full stack through the real API.

The UI is only ever a client of these endpoints, so what passes here is exactly
what the browser and an MCP wrapper can do.
"""

from __future__ import annotations

import copy
import struct
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from facet.adapters.geometry.fake import FakeKernel
from facet.adapters.persistence.filesystem import FilesystemDocumentRepository
from facet.application.services import ProjectService
from facet.main import create_app

from ..application.test_recompute import BRACKET


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    service = ProjectService(FilesystemDocumentRepository(tmp_path), FakeKernel())
    return TestClient(create_app(service))


@pytest.fixture
def bracket_client(client: TestClient) -> TestClient:
    response = client.post(
        "/api/projects", json={"id": "bracket", "name": "Bracket", "document": BRACKET}
    )
    assert response.status_code == 201, response.text
    return client


# --------------------------------------------------------------------------
# Meta
# --------------------------------------------------------------------------


def test_health(client: TestClient) -> None:
    assert client.get("/api/health").json() == {"status": "ok"}


def test_kernel_reports_its_capabilities(client: TestClient) -> None:
    body = client.get("/api/kernel").json()
    assert body["name"] == "analytic"
    assert "pad" in body["capabilities"]


def test_feature_types_are_discoverable(client: TestClient) -> None:
    """An agent can ask what it is allowed to build."""
    assert set(client.get("/api/feature-types").json()["types"]) == {
        "pad", "pocket", "hole", "fillet", "chamfer", "thread",
    }


def test_openapi_schema_is_generated(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert "/api/projects/{project_id}/recompute" in schema["paths"]
    assert "/api/projects/{project_id}/resolve" in schema["paths"]


# --------------------------------------------------------------------------
# Project lifecycle
# --------------------------------------------------------------------------


def test_create_and_list_projects(client: TestClient) -> None:
    client.post("/api/projects", json={"id": "one", "name": "First"})
    client.post("/api/projects", json={"id": "two", "name": "Second"})
    projects = client.get("/api/projects").json()["projects"]
    assert [p["id"] for p in projects] == ["one", "two"]
    assert projects[0]["name"] == "First"


def test_duplicate_project_id_is_rejected(client: TestClient) -> None:
    client.post("/api/projects", json={"id": "one"})
    assert client.post("/api/projects", json={"id": "one"}).status_code == 409


def test_unsafe_project_id_is_rejected(client: TestClient) -> None:
    assert client.post("/api/projects", json={"id": "../escape"}).status_code == 422


def test_missing_project_is_404(client: TestClient) -> None:
    assert client.get("/api/projects/ghost/document").status_code == 404


def test_delete_project(bracket_client: TestClient) -> None:
    assert bracket_client.delete("/api/projects/bracket").status_code == 204
    assert bracket_client.get("/api/projects/bracket/document").status_code == 404


# --------------------------------------------------------------------------
# Document round-trip — the clone-to-another-station path
# --------------------------------------------------------------------------


def test_document_reads_back_as_json(bracket_client: TestClient) -> None:
    document = bracket_client.get("/api/projects/bracket/document").json()
    assert document["project"] == "Bracket"
    assert [f["id"] for f in document["features"]] == ["base", "slot"]


def test_document_reads_back_as_yaml(bracket_client: TestClient) -> None:
    response = bracket_client.get("/api/projects/bracket/document?fmt=yaml")
    parsed = yaml.safe_load(response.text)
    assert parsed["schema"] == "cadsheet/1"
    assert len(parsed["parameters"]) == 5


def test_a_document_can_be_exported_and_reimported(bracket_client: TestClient) -> None:
    """Export the YAML, create a fresh project from it, get the same model."""
    exported = bracket_client.get("/api/projects/bracket/document?fmt=yaml").text
    bracket_client.post("/api/projects", json={"id": "clone"})
    response = bracket_client.put("/api/projects/clone/document", json={"yaml": exported})
    assert response.status_code == 200, response.text

    original = bracket_client.get("/api/projects/bracket/topology").json()
    clone = bracket_client.get("/api/projects/clone/topology").json()
    assert [f["tag"] for f in clone["faces"]] == [f["tag"] for f in original["faces"]]


def test_replacing_a_document_with_an_invalid_one_is_refused(
    bracket_client: TestClient,
) -> None:
    response = bracket_client.put(
        "/api/projects/bracket/document",
        json={"document": {"schema": "cadsheet/1", "parameters": [{"name": "a", "expr": "a"}]}},
    )
    assert response.status_code == 422
    assert "circular" in str(response.json()["detail"]).lower()


# --------------------------------------------------------------------------
# Recompute and topology
# --------------------------------------------------------------------------


def test_recompute_reports_per_feature_status(bracket_client: TestClient) -> None:
    body = bracket_client.post("/api/projects/bracket/recompute").json()
    assert body["ok"] is True
    assert [f["id"] for f in body["features"]] == ["base", "slot"]
    assert body["features"][0]["faceCount"] == 6


def test_topology_lists_every_tag(bracket_client: TestClient) -> None:
    body = bracket_client.get("/api/projects/bracket/topology").json()
    tags = {f["tag"] for f in body["faces"]}
    assert "slot/floor" in tags
    assert "base/side[outline.left]" in tags
    assert len(body["edges"]) > 0


def test_changing_a_parameter_triggers_recalculation(bracket_client: TestClient) -> None:
    body = bracket_client.patch(
        "/api/projects/bracket/parameters", json={"changes": {"plate_w": 200}}
    ).json()
    assert body["ok"] is True
    assert body["parameters"]["plate_w"] == 200.0
    assert body["parameters"]["plate_h"] == 120.0  # the dependent expression followed


def test_a_parameter_can_be_replaced_by_an_expression(bracket_client: TestClient) -> None:
    body = bracket_client.patch(
        "/api/projects/bracket/parameters", json={"changes": {"slot_d": "plate_t / 3"}}
    ).json()
    assert body["ok"] is True
    assert body["parameters"]["slot_d"] == pytest.approx(2.0)


def test_changing_an_unknown_parameter_is_refused(bracket_client: TestClient) -> None:
    response = bracket_client.patch(
        "/api/projects/bracket/parameters", json={"changes": {"nope": 1}}
    )
    assert response.status_code == 422


def test_parameter_changes_persist(bracket_client: TestClient) -> None:
    bracket_client.patch("/api/projects/bracket/parameters", json={"changes": {"plate_w": 175}})
    document = bracket_client.get("/api/projects/bracket/document").json()
    row = next(p for p in document["parameters"] if p["name"] == "plate_w")
    assert row["value"] == 175.0


def test_names_survive_a_parameter_change_over_the_api(bracket_client: TestClient) -> None:
    before = bracket_client.get("/api/projects/bracket/topology").json()
    bracket_client.patch(
        "/api/projects/bracket/parameters",
        json={"changes": {"plate_w": 300, "plate_t": 12, "slot_w": 45}},
    )
    after = bracket_client.get("/api/projects/bracket/topology").json()
    assert [f["tag"] for f in after["faces"]] == [f["tag"] for f in before["faces"]]


# --------------------------------------------------------------------------
# Feature editing
# --------------------------------------------------------------------------


def test_add_and_delete_a_feature(bracket_client: TestClient) -> None:
    spec = {
        "id": "second",
        "type": "pocket",
        "profile": "hole.outer",
        "depth": 1.0,
        "direction": "-normal",
    }
    added = bracket_client.post(
        "/api/projects/bracket/features", json={"spec": spec}
    ).json()
    assert [f["id"] for f in added["features"]] == ["base", "slot", "second"]

    removed = bracket_client.delete("/api/projects/bracket/features/second").json()
    assert [f["id"] for f in removed["features"]] == ["base", "slot"]


def test_update_a_feature(bracket_client: TestClient) -> None:
    body = bracket_client.patch(
        "/api/projects/bracket/features/slot",
        json={"type": "pocket", "profile": "hole.outer", "depth": 3.5, "direction": "-normal"},
    ).json()
    assert body["ok"] is True
    document = bracket_client.get("/api/projects/bracket/document").json()
    assert document["features"][1]["depth"] == 3.5


def test_reorder_requires_every_feature(bracket_client: TestClient) -> None:
    assert (
        bracket_client.post(
            "/api/projects/bracket/features/reorder", json={"order": ["slot"]}
        ).status_code
        == 422
    )


# --------------------------------------------------------------------------
# Failure reporting
# --------------------------------------------------------------------------


def test_a_broken_model_still_returns_the_last_good_state(client: TestClient) -> None:
    broken = copy.deepcopy(BRACKET)
    broken["features"][1]["direction"] = "+normal"
    client.post("/api/projects", json={"id": "broken", "document": broken})

    body = client.post("/api/projects/broken/recompute").json()
    assert body["ok"] is False
    assert body["lastGoodFeature"] == "base"
    assert body["features"][1]["status"] == "failed"
    assert "direction" in body["features"][1]["error"]["message"]


def test_a_broken_model_still_renders_what_built(client: TestClient) -> None:
    broken = copy.deepcopy(BRACKET)
    broken["features"][1]["direction"] = "+normal"
    client.post("/api/projects", json={"id": "broken", "document": broken})

    mesh = client.get("/api/projects/broken/mesh").json()
    assert len(mesh["positions"]) > 0  # the plate is visible
    assert mesh["build"]["ok"] is False


# --------------------------------------------------------------------------
# The agent-facing endpoints
# --------------------------------------------------------------------------


def test_resolve_previews_a_face_selector(bracket_client: TestClient) -> None:
    body = bracket_client.post(
        "/api/projects/bracket/resolve", json={"selector": "slot/wall[*]"}
    ).json()
    assert body["ok"] is True
    assert body["count"] == 4
    assert "slot/wall[hole.c0]" in body["matched"]


def test_resolve_reports_a_selector_that_matches_nothing(bracket_client: TestClient) -> None:
    body = bracket_client.post(
        "/api/projects/bracket/resolve", json={"selector": "ghost/floor"}
    ).json()
    assert body["ok"] is False
    assert body["count"] == 0


def test_resolve_reports_a_malformed_selector(bracket_client: TestClient) -> None:
    body = bracket_client.post(
        "/api/projects/bracket/resolve", json={"selector": "!!!"}
    ).json()
    assert body["ok"] is False
    assert body["error"]


def test_resolve_previews_edges_between_two_patterns(bracket_client: TestClient) -> None:
    body = bracket_client.post(
        "/api/projects/bracket/resolve",
        json={"between": ["base/cap+", "slot/wall[*]"]},
    ).json()
    assert body["count"] == 4


# --------------------------------------------------------------------------
# Mesh and export
# --------------------------------------------------------------------------


def test_mesh_carries_tags_for_click_to_select(bracket_client: TestClient) -> None:
    """Each triangle range names the face's stable tag, not a kernel index."""
    mesh = bracket_client.get("/api/projects/bracket/mesh").json()
    tags = {r["tag"] for r in mesh["faceRanges"]}
    assert "slot/floor" in tags
    assert len(mesh["indices"]) % 3 == 0
    assert len(mesh["edges"]) == 24


def test_stl_export_is_a_valid_binary_stl(bracket_client: TestClient) -> None:
    response = bracket_client.get("/api/projects/bracket/export?fmt=stl")
    assert response.status_code == 200
    body = response.content
    declared = struct.unpack("<I", body[80:84])[0]
    assert len(body) == 84 + declared * 50
    assert declared > 0


def test_stl_export_is_offered_as_a_download(bracket_client: TestClient) -> None:
    response = bracket_client.get("/api/projects/bracket/export?fmt=stl")
    assert "attachment" in response.headers["content-disposition"]
    assert "bracket.stl" in response.headers["content-disposition"]


def test_obj_export_groups_by_face(bracket_client: TestClient) -> None:
    text = bracket_client.get("/api/projects/bracket/export?fmt=obj").text
    assert text.startswith("# facet")
    assert text.count("\ng ") == 11  # one group per face


def test_parameter_sheet_exports_as_csv(bracket_client: TestClient) -> None:
    text = bracket_client.get("/api/projects/bracket/export?fmt=csv").text
    lines = text.strip().splitlines()
    assert lines[0].startswith("name,group,value,expr,unit,resolved")
    assert len(lines) == 6  # header plus five parameters
    assert "plate_h,Plate,,plate_w * 0.6,mm,72.0," in text


def test_topology_exports_as_json(bracket_client: TestClient) -> None:
    body = bracket_client.get("/api/projects/bracket/export?fmt=topology").json()
    assert len(body["faces"]) == 11


def test_an_unknown_export_format_lists_the_supported_ones(
    bracket_client: TestClient,
) -> None:
    response = bracket_client.get("/api/projects/bracket/export?fmt=dwg")
    assert response.status_code == 400
    assert "stl" in response.json()["detail"]["supported"]
    assert "step" in response.json()["detail"]["supported"]


def test_step_against_a_mesh_only_kernel_reports_the_capability_gap(
    bracket_client: TestClient,
) -> None:
    """A known format the *configured kernel* cannot produce fails clearly.

    These tests run on the analytic kernel, which has no B-rep writer. Rather
    than an AttributeError deep in an adapter, the capability is checked up
    front and the response names what this kernel can actually do.
    """
    response = bracket_client.get("/api/projects/bracket/export?fmt=step")
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["capability"] == "brep_export"
    assert detail["kernel"] == "analytic"
    assert "pad" in detail["available"]


def test_a_broken_model_cannot_be_exported(client: TestClient) -> None:
    broken = copy.deepcopy(BRACKET)
    broken["features"] = [{"id": "x", "type": "loft", "profile": "outline.outer"}]
    client.post("/api/projects", json={"id": "broken", "document": broken})
    response = client.get("/api/projects/broken/export?fmt=stl")
    assert response.status_code == 422


# --------------------------------------------------------------------------
# Sheet round-trip through a spreadsheet — the escape hatch for large sheets
# --------------------------------------------------------------------------


def test_the_sheet_round_trips_through_csv(bracket_client: TestClient) -> None:
    """Export, edit as a spreadsheet would, import — the model follows."""
    exported = bracket_client.get("/api/projects/bracket/export?fmt=csv").text
    edited = exported.replace("plate_w,Plate,120.0", "plate_w,Plate,400.0")
    assert edited != exported

    body = bracket_client.post(
        "/api/projects/bracket/import", json={"format": "csv", "body": edited}
    ).json()
    assert body["ok"] is True
    assert body["parameters"]["plate_w"] == 400.0
    assert body["parameters"]["plate_h"] == 240.0  # the expression followed


def test_csv_import_preserves_expressions(bracket_client: TestClient) -> None:
    exported = bracket_client.get("/api/projects/bracket/export?fmt=csv").text
    bracket_client.post("/api/projects/bracket/import", json={"format": "csv", "body": exported})
    document = bracket_client.get("/api/projects/bracket/document").json()
    row = next(p for p in document["parameters"] if p["name"] == "plate_h")
    assert row["expr"] == "plate_w * 0.6"


def test_csv_import_leaves_features_and_sketches_untouched(bracket_client: TestClient) -> None:
    """A spreadsheet cannot represent the feature tree, so it must not erase it."""
    exported = bracket_client.get("/api/projects/bracket/export?fmt=csv").text
    bracket_client.post("/api/projects/bracket/import", json={"format": "csv", "body": exported})
    document = bracket_client.get("/api/projects/bracket/document").json()
    assert [f["id"] for f in document["features"]] == ["base", "slot"]
    assert set(document["sketches"]) == {"outline", "hole"}


def test_csv_import_names_survive_the_round_trip(bracket_client: TestClient) -> None:
    before = bracket_client.get("/api/projects/bracket/topology").json()
    exported = bracket_client.get("/api/projects/bracket/export?fmt=csv").text
    bracket_client.post(
        "/api/projects/bracket/import",
        json={
            "format": "csv",
            "body": exported.replace("plate_w,Plate,120.0", "plate_w,Plate,333.0"),
        },
    )
    after = bracket_client.get("/api/projects/bracket/topology").json()
    assert [f["tag"] for f in after["faces"]] == [f["tag"] for f in before["faces"]]


def test_a_csv_without_a_name_column_is_refused(bracket_client: TestClient) -> None:
    response = bracket_client.post(
        "/api/projects/bracket/import", json={"format": "csv", "body": "a,b\n1,2\n"}
    )
    assert response.status_code == 422
    assert "name" in str(response.json()["detail"]).lower()


def test_a_row_with_both_value_and_expression_is_refused(bracket_client: TestClient) -> None:
    csv_text = "name,group,value,expr,unit,resolved_mm_deg,doc\nw,Plate,10,w2 * 2,mm,,\n"
    response = bracket_client.post(
        "/api/projects/bracket/import", json={"format": "csv", "body": csv_text}
    )
    assert response.status_code == 422
    assert "both" in str(response.json()["detail"]).lower()


def test_a_non_numeric_value_is_refused_with_advice(bracket_client: TestClient) -> None:
    csv_text = "name,group,value,expr,unit,resolved_mm_deg,doc\nw,Plate,ten,,mm,,\n"
    response = bracket_client.post(
        "/api/projects/bracket/import", json={"format": "csv", "body": csv_text}
    )
    assert response.status_code == 422
    assert "expr" in str(response.json()["detail"])


def test_a_partially_valid_csv_is_rejected_whole(bracket_client: TestClient) -> None:
    """Half-applied sheets are harder to reason about than refused ones."""
    csv_text = (
        "name,group,value,expr,unit,resolved_mm_deg,doc\n"
        "good,Plate,10,,mm,,\n"
        "bad,Plate,,,mm,,\n"
    )
    response = bracket_client.post(
        "/api/projects/bracket/import", json={"format": "csv", "body": csv_text}
    )
    assert response.status_code == 422
    document = bracket_client.get("/api/projects/bracket/document").json()
    assert [p["name"] for p in document["parameters"]] == [
        "plate_w", "plate_h", "plate_t", "slot_w", "slot_d",
    ]


def test_trailing_blank_rows_are_tolerated(bracket_client: TestClient) -> None:
    """Spreadsheets routinely append empty lines on save."""
    exported = bracket_client.get("/api/projects/bracket/export?fmt=csv").text
    response = bracket_client.post(
        "/api/projects/bracket/import", json={"format": "csv", "body": exported + ",,,,,,\n\n"}
    )
    assert response.status_code == 200


def test_expression_vocabulary_is_published(client: TestClient) -> None:
    """A client must be able to tell an unknown parameter from a function."""
    body = client.get("/api/expressions").json()
    assert "sqrt" in body["functions"]
    assert "min" in body["functions"]
    assert "pi" in body["constants"]
    assert "plate_w" not in body["functions"]
