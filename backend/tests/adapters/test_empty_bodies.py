"""An empty body: legitimate for a moment, a silent defect if it lasts.

Creating a body and then putting a pad in it is two calls, so between them the
document holds a body with no features. That state has to be allowed.

What it must not be is silent. An empty body used to pass recompute with `ok`
true and no mention anywhere in the report, and then behave exactly like a body
that had failed: no mesh, no faces, and an export refusing with a message about
a model that "does not build" — which sends the reader to debug a feature that
was never there.

Worse, the export guard read the *first* body's solid rather than the triangles
it had actually produced, so one empty body at the top of a document made every
export fail, including one naming a body that had built perfectly well.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from facet.adapters.geometry.fake import FakeKernel
from facet.adapters.persistence.filesystem import FilesystemDocumentRepository
from facet.application.services import ProjectService
from facet.main import create_app

from .test_body_copies import ONE_LEG


def document_with_an_empty_body_first() -> dict[str, object]:
    """The order you get from creating a body before the part it will hold."""
    doc = copy.deepcopy(ONE_LEG)
    doc["bodies"] = [{"id": "stay_r", "features": []}, *doc["bodies"]]  # type: ignore[misc]
    return doc


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    service = ProjectService(FilesystemDocumentRepository(tmp_path), FakeKernel())
    api = TestClient(create_app(service))
    assert api.post(
        "/api/projects",
        json={"id": "stay", "document": document_with_an_empty_body_first()},
    ).status_code == 201
    return api


def build(client: TestClient) -> dict:
    return client.post("/api/projects/stay/recompute").json()


def export(client: TestClient, **params: str):
    return client.get("/api/projects/stay/export", params={"fmt": "stl", **params})


# --------------------------------------------------------------------------
# It is reported
# --------------------------------------------------------------------------


def test_an_empty_body_is_not_an_error(client: TestClient) -> None:
    """It is the correct state between creating a body and filling it."""
    assert build(client)["ok"] is True


def test_an_empty_body_says_so(client: TestClient) -> None:
    """The whole point: it used to pass in complete silence."""
    warnings = build(client)["warnings"]
    assert any("stay_r" in note and "no features" in note for note in warnings), warnings


def test_the_warning_says_what_to_do_about_it(client: TestClient) -> None:
    note = next(w for w in build(client)["warnings"] if "stay_r" in w)
    assert "Add a feature" in note and "delete the body" in note


def test_the_body_row_is_marked_empty(client: TestClient) -> None:
    """So a caller reading the body list to pick an export target can see it."""
    bodies = {b["id"]: b for b in build(client)["bodies"]}
    assert bodies["stay_r"]["empty"] is True
    assert bodies["leg"]["empty"] is False


def test_a_body_that_built_carries_no_warning(client: TestClient) -> None:
    bodies = {b["id"]: b for b in build(client)["bodies"]}
    assert bodies["leg"]["warnings"] == []


def test_a_body_whose_features_are_all_suppressed_says_that_instead(
    client: TestClient,
) -> None:
    """Also builds nothing, but for a reason with a different remedy."""
    doc = copy.deepcopy(ONE_LEG)
    doc["bodies"][0]["features"][0]["suppressed"] = True  # type: ignore[index]
    client.post("/api/projects", json={"id": "off", "document": doc})
    warnings = client.post("/api/projects/off/recompute").json()["warnings"]
    assert any("suppressed" in note for note in warnings), warnings


def test_a_failing_body_reports_the_failure_and_not_emptiness(
    client: TestClient,
) -> None:
    """The failure is the story; 'it built nothing' would only be noise."""
    doc = copy.deepcopy(ONE_LEG)
    doc["bodies"][0]["features"][0]["profile"] = "leg.nosuchloop"  # type: ignore[index]
    made = client.post("/api/projects", json={"id": "bad", "document": doc})
    assert made.status_code >= 400 or not client.post(
        "/api/projects/bad/recompute"
    ).json()["warnings"]


# --------------------------------------------------------------------------
# It no longer breaks the exports
# --------------------------------------------------------------------------


def test_an_empty_body_does_not_stop_the_model_exporting(client: TestClient) -> None:
    """The reported failure: one empty body, and nothing could be exported."""
    answer = export(client)
    assert answer.status_code == 200, answer.text
    assert len(answer.content) > 84  # more than an empty STL header


def test_an_empty_body_does_not_stop_another_body_exporting(client: TestClient) -> None:
    """It named a built body and was still told the model does not build."""
    answer = export(client, body="leg")
    assert answer.status_code == 200, answer.text


def test_exporting_the_empty_body_itself_says_why(client: TestClient) -> None:
    answer = export(client, body="stay_r")
    assert answer.status_code == 422
    assert "no features" in answer.text
    assert "does not build" not in answer.text


def test_a_wrong_name_is_still_told_apart_from_an_empty_body(client: TestClient) -> None:
    answer = export(client, body="ghost")
    assert answer.status_code == 422
    assert "no body named 'ghost'" in answer.text
    assert "stay_r" in answer.text  # and the ones that do exist


def test_a_model_with_nothing_in_it_says_that_rather_than_blaming_the_build(
    client: TestClient,
) -> None:
    client.post("/api/projects", json={"id": "blank"})
    answer = client.get("/api/projects/blank/export", params={"fmt": "stl"})
    assert answer.status_code == 422
    assert "no features" in answer.text


def test_a_model_that_really_does_not_build_still_says_so(client: TestClient) -> None:
    doc = copy.deepcopy(ONE_LEG)
    doc["bodies"][0]["features"][0]["profile"] = "leg.nosuchloop"  # type: ignore[index]
    made = client.post("/api/projects", json={"id": "broken", "document": doc})
    if made.status_code >= 400:
        return  # refused at the door, which is also correct
    answer = client.get("/api/projects/broken/export", params={"fmt": "stl"})
    assert answer.status_code == 422
    assert "does not build" in answer.text


def test_obj_is_guarded_the_same_way(client: TestClient) -> None:
    assert export(client, fmt="obj").status_code == 200


def test_the_drawing_exports_explain_an_empty_body_too(client: TestClient) -> None:
    """They had their own copy of the same wrong one-liner."""
    answer = client.get(
        "/api/projects/stay/export/flat", params={"body": "stay_r"}
    )
    assert answer.status_code >= 400
    assert "no features" in answer.text or "does not support" in answer.text


def test_an_empty_body_is_not_a_piece_to_produce(client: TestClient) -> None:
    """The parts list says what to make, so an unmakeable row does not belong."""
    assert build(client)["parts"] == [{"body": "leg", "quantity": 1}]


# --------------------------------------------------------------------------
# It no longer blanks the viewport
# --------------------------------------------------------------------------


def test_an_empty_body_declares_the_encoding_like_any_other(client: TestClient) -> None:
    """The bug that blanked the whole viewport over one unfilled body.

    A client must check `encoding` — without it, a format change would have it
    reading the wrong bytes. The empty-body branch wrote hand-rolled empty
    arrays and omitted the key, so that check rejected the entire response and
    blamed a version mismatch between the API and the app.
    """
    state = client.get("/api/projects/stay/state").json()
    encodings = {b["id"]: b.get("encoding") for b in state["bodies"]}
    assert encodings["stay_r"] == encodings["leg"]
    assert encodings["stay_r"] is not None


def test_an_empty_body_is_flagged_in_the_geometry_payload(client: TestClient) -> None:
    """So the viewer can say why a body is not on screen."""
    bodies = {b["id"]: b for b in client.get("/api/projects/stay/bodies").json()["bodies"]}
    assert bodies["stay_r"]["empty"] is True
    assert bodies["leg"]["empty"] is False


def test_an_empty_body_carries_no_triangles_but_a_full_row(client: TestClient) -> None:
    bodies = {b["id"]: b for b in client.get("/api/projects/stay/bodies").json()["bodies"]}
    empty = bodies["stay_r"]
    assert empty["positions"] == [] and empty["faceRanges"] == []
    # Still placed, so a client can draw a marker where it would go.
    assert len(empty["placement"]) == 16


# --------------------------------------------------------------------------
# Copies of an empty body
# --------------------------------------------------------------------------


def test_a_copy_of_an_empty_body_points_at_the_source(client: TestClient) -> None:
    made = client.post("/api/projects/stay/bodies/stay_r/copies", json={})
    assert made.status_code == 200, made.text
    warnings = made.json()["warnings"]
    assert any("copies 'stay_r'" in note for note in warnings), warnings
