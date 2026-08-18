"""Copies: one body, appearing several times.

A part that repeats is not several histories that happen to agree — it is one
history, shown more than once. `of` names the body a copy shows; the copy holds
no features and contributes no names, only a placement.

Three properties are what the feature is for, and each has tests below:

**Edited once.** There is one history, so the copies cannot drift apart.

**Built once.** The solid and its triangles are computed for the source and
transformed for the copies. A model with four of a part costs one of them.

**Counted.** The document knows the part is called for four times. That number
is what a print run needs, and it is the one thing a copy-pasted history cannot
provide, because nothing in it records that the four were meant to be the same.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from facet.adapters.geometry.fake import FakeKernel
from facet.adapters.persistence.filesystem import FilesystemDocumentRepository
from facet.application.services import ProjectService
from facet.main import create_app

#: One leg, plus the span it is placed across. Deliberately parametric: the
#: point of a copy is that moving `span_w` moves the copies with it.
ONE_LEG: dict[str, object] = {
    "schema": "cadsheet/1",
    "project": "table",
    "parameters": [
        {"name": "leg_w", "value": 20.0},
        {"name": "leg_h", "value": 60.0},
        {"name": "span_w", "value": 400.0},
    ],
    "datums": {"base": {"type": "plane", "origin": [0, 0, 0], "normal": [0, 0, 1]}},
    "sketches": {
        "leg": {
            "plane": "base",
            "points": {
                "p0": [0, 0], "p1": ["leg_w", 0],
                "p2": ["leg_w", "leg_w"], "p3": [0, "leg_w"],
            },
            "curves": [
                {"id": "bottom", "start": "p0", "end": "p1"},
                {"id": "right", "start": "p1", "end": "p2"},
                {"id": "top", "start": "p2", "end": "p3"},
                {"id": "left", "start": "p3", "end": "p0"},
            ],
            "loops": [{"id": "outer", "curves": ["bottom", "right", "top", "left"]}],
        },
    },
    "bodies": [
        {
            "id": "leg",
            "features": [
                {"id": "shaft", "type": "pad", "profile": "leg.outer", "length": "leg_h"}
            ],
        }
    ],
}


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    service = ProjectService(FilesystemDocumentRepository(tmp_path), FakeKernel())
    api = TestClient(create_app(service))
    assert api.post(
        "/api/projects", json={"id": "table", "document": ONE_LEG}
    ).status_code == 201
    return api


def copy_of(client: TestClient, body: str = "leg", **payload: object) -> dict:
    answer = client.post(f"/api/projects/table/bodies/{body}/copies", json=payload)
    assert answer.status_code == 200, answer.text
    return answer.json()


def refusal(client: TestClient, bodies: list[dict]) -> str:
    """The message a document with a bad copy graph rebuilds into.

    Creating a project does not validate it — that is the existing contract, and
    it is what lets a broken document be opened and read at all. The rebuild is
    where a structural problem is reported.
    """
    made = client.post(
        "/api/projects", json={"id": "bad", "document": dict(ONE_LEG, bodies=bodies)}
    )
    assert made.status_code == 201, made.text
    build = client.post("/api/projects/bad/recompute").json()
    assert build["ok"] is False, build
    return str(build["error"])


def document(client: TestClient) -> dict:
    return client.get("/api/projects/table/document").json()


def bodies(client: TestClient) -> list[dict]:
    return client.get("/api/projects/table/bodies").json()["bodies"]


def result_body(build: dict, identifier: str) -> dict:
    found = next((b for b in build["bodies"] if b["id"] == identifier), None)
    assert found is not None, build["bodies"]
    return found


# --------------------------------------------------------------------------
# The copy itself
# --------------------------------------------------------------------------


def test_a_copy_is_added_with_a_generated_id(client: TestClient) -> None:
    assert copy_of(client)["id"] == "leg_2"


def test_ids_count_the_pieces_not_the_copies(client: TestClient) -> None:
    """`leg`, `leg_2`, `leg_3` — three legs, read straight off the ids."""
    copy_of(client)
    assert copy_of(client)["id"] == "leg_3"


def test_a_copy_can_be_named(client: TestClient) -> None:
    assert copy_of(client, id="front_right")["id"] == "front_right"


def test_a_copy_carries_the_flag_saying_what_it_copies(client: TestClient) -> None:
    copy_of(client)
    written = next(b for b in document(client)["bodies"] if b["id"] == "leg_2")
    assert written["of"] == "leg"


def test_a_copy_writes_no_feature_list_at_all(client: TestClient) -> None:
    """It can never have one, and an empty list invites the reader to fill it."""
    copy_of(client)
    written = next(b for b in document(client)["bodies"] if b["id"] == "leg_2")
    assert "features" not in written


def test_a_copy_lands_where_it_is_put(client: TestClient) -> None:
    copy_of(client, origin=[400, 0, 0])
    placement = result_body(client.post("/api/projects/table/recompute").json(), "leg_2")
    # Column-major 4x4; the translation is the last column.
    assert placement["placement"][12:15] == [400.0, 0.0, 0.0]


def test_a_copy_placement_can_be_an_expression(client: TestClient) -> None:
    copy_of(client, origin=["span_w", 0, 0])
    build = client.post("/api/projects/table/recompute").json()
    assert result_body(build, "leg_2")["placement"][12] == 400.0


def test_moving_a_parameter_moves_the_copy(client: TestClient) -> None:
    """The copy is placed by the sheet like everything else."""
    copy_of(client, origin=["span_w", 0, 0])
    client.patch("/api/projects/table/parameters/span_w", json={"value": 500.0})
    build = client.post("/api/projects/table/recompute").json()
    assert result_body(build, "leg_2")["placement"][12] == 500.0


def test_a_copy_can_be_moved_like_any_other_body(client: TestClient) -> None:
    copy_of(client)
    moved = client.patch(
        "/api/projects/table/bodies/leg_2",
        json={"id": "leg_2", "origin": [10, 20, 0], "rotation": [0, 0, 0]},
    )
    assert moved.status_code == 200, moved.text
    assert result_body(moved.json(), "leg_2")["placement"][12:15] == [10.0, 20.0, 0.0]


def test_a_copy_defaults_to_the_source_placement(client: TestClient) -> None:
    """Visible in the tree and asking to be moved, rather than silently nowhere."""
    copy_of(client)
    build = client.post("/api/projects/table/recompute").json()
    assert result_body(build, "leg_2")["placement"] == result_body(build, "leg")["placement"]


# --------------------------------------------------------------------------
# Built once
# --------------------------------------------------------------------------


def test_a_copy_shows_the_source_geometry(client: TestClient) -> None:
    copy_of(client, origin=[400, 0, 0])
    drawn = bodies(client)
    source = next(b for b in drawn if b["id"] == "leg")
    copy = next(b for b in drawn if b["id"] == "leg_2")
    assert copy["positions"] == source["positions"]
    assert copy["indices"] == source["indices"]


def test_a_copy_is_not_built_again(client: TestClient) -> None:
    """No feature outcomes: there was no history to run."""
    copy_of(client)
    build = client.post("/api/projects/table/recompute").json()
    assert result_body(build, "leg_2")["features"] == []
    assert result_body(build, "leg")["features"] != []


def test_the_copy_reuses_the_source_triangles(client: TestClient) -> None:
    """Same content key, so the mesh cache answers for both.

    The saving this feature exists for. Asserted through the kernel rather than
    through a timer: a second tessellation would be a second call.
    """
    counting = _CountingKernel()
    service = ProjectService(FilesystemDocumentRepository(Path("/tmp")), counting)
    api = TestClient(create_app(service))
    api.post("/api/projects", json={"id": "table", "document": ONE_LEG})
    api.post("/api/projects/table/bodies/leg/copies", json={"origin": [400, 0, 0]})
    counting.tessellations = 0
    api.get("/api/projects/table/bodies")
    assert counting.tessellations == 1


def test_editing_the_source_moves_every_copy_with_it(client: TestClient) -> None:
    """The whole point: one history, so the copies cannot drift."""
    copy_of(client, origin=[400, 0, 0])
    before = len(next(b for b in bodies(client) if b["id"] == "leg_2")["positions"])
    client.patch("/api/projects/table/parameters/leg_h", json={"value": 90.0})
    after = next(b for b in bodies(client) if b["id"] == "leg_2")
    source = next(b for b in bodies(client) if b["id"] == "leg")
    assert after["positions"] == source["positions"]
    assert len(after["positions"]) == before  # same shape, new size
    assert max(after["positions"][2::3]) == pytest.approx(90.0)


# --------------------------------------------------------------------------
# Counted
# --------------------------------------------------------------------------


def test_the_source_reports_how_many_pieces_are_needed(client: TestClient) -> None:
    copy_of(client)
    copy_of(client)
    copy_of(client)
    build = client.post("/api/projects/table/recompute").json()
    assert result_body(build, "leg")["quantity"] == 4


def test_a_copy_is_counted_by_its_source_not_by_itself(client: TestClient) -> None:
    """So the quantities sum to the piece count instead of doubling it."""
    copy_of(client)
    build = client.post("/api/projects/table/recompute").json()
    assert result_body(build, "leg_2")["quantity"] == 0
    assert sum(b["quantity"] for b in build["bodies"]) == 2


def test_the_source_lists_its_copies(client: TestClient) -> None:
    copy_of(client)
    copy_of(client, id="front_right")
    build = client.post("/api/projects/table/recompute").json()
    assert result_body(build, "leg")["copies"] == ["leg_2", "front_right"]


def test_the_parts_list_is_the_print_run(client: TestClient) -> None:
    copy_of(client)
    copy_of(client)
    build = client.post("/api/projects/table/recompute").json()
    assert build["parts"] == [{"body": "leg", "quantity": 3}]


def test_a_model_without_copies_counts_one_of_each(client: TestClient) -> None:
    build = client.post("/api/projects/table/recompute").json()
    assert build["parts"] == [{"body": "leg", "quantity": 1}]


# --------------------------------------------------------------------------
# What a copy is not
# --------------------------------------------------------------------------


def test_a_copy_cannot_hold_features(client: TestClient) -> None:
    copy_of(client)
    refused = client.post(
        "/api/projects/table/features",
        json={
            "body": "leg_2",
            "spec": {"id": "extra", "type": "pad", "profile": "leg.outer", "length": 5},
        },
    )
    assert refused.status_code >= 400
    assert "leg" in refused.text and "copy" in refused.text


def test_a_copy_of_a_copy_is_refused_and_says_what_to_copy(client: TestClient) -> None:
    copy_of(client)
    refused = client.post("/api/projects/table/bodies/leg_2/copies", json={})
    assert refused.status_code >= 400
    assert "'leg'" in refused.text


def test_a_copy_of_a_body_that_does_not_exist_is_refused(client: TestClient) -> None:
    assert client.post("/api/projects/table/bodies/ghost/copies", json={}).status_code >= 400


def test_a_body_cannot_be_a_copy_of_itself(client: TestClient) -> None:
    assert "itself" in refusal(client, [{"id": "leg", "of": "leg", "features": []}])


def test_a_copy_naming_an_unknown_body_is_refused(client: TestClient) -> None:
    said = refusal(
        client,
        [*ONE_LEG["bodies"], {"id": "leg_2", "of": "ghost", "features": []}],  # type: ignore[misc]
    )
    assert "ghost" in said


def test_a_chain_written_by_hand_is_refused(client: TestClient) -> None:
    """One hop, so "where does this come from" has a one-word answer."""
    said = refusal(
        client,
        [
            *ONE_LEG["bodies"],  # type: ignore[misc]
            {"id": "leg_2", "of": "leg", "features": []},
            {"id": "leg_3", "of": "leg_2", "features": []},
        ],
    )
    assert "leg_2" in said and "leg_3" in said


def test_a_copy_written_with_features_is_refused(client: TestClient) -> None:
    """Caught as the document is read, before anything can act on it."""
    broken = dict(
        ONE_LEG,
        bodies=[
            *ONE_LEG["bodies"],  # type: ignore[misc]
            {
                "id": "leg_2",
                "of": "leg",
                "features": [
                    {"id": "extra", "type": "pad", "profile": "leg.outer", "length": 5}
                ],
            },
        ],
    )
    refused = client.post("/api/projects", json={"id": "bad", "document": broken})
    assert refused.status_code >= 400
    assert "copy" in refused.text


# --------------------------------------------------------------------------
# Deleting
# --------------------------------------------------------------------------


def test_deleting_a_copied_body_is_refused_and_names_the_copies(client: TestClient) -> None:
    copy_of(client)
    copy_of(client)
    refused = client.delete("/api/projects/table/bodies/leg")
    assert refused.status_code >= 400
    assert "leg_2" in refused.text and "leg_3" in refused.text


def test_a_copy_can_be_deleted_and_the_source_survives(client: TestClient) -> None:
    copy_of(client)
    build = client.delete("/api/projects/table/bodies/leg_2")
    assert build.status_code == 200, build.text
    assert result_body(build.json(), "leg")["quantity"] == 1


def test_deleting_every_copy_frees_the_source(client: TestClient) -> None:
    copy_of(client)
    client.delete("/api/projects/table/bodies/leg_2")
    # Still the last body, so still refused — but for the pre-existing reason.
    refused = client.delete("/api/projects/table/bodies/leg")
    assert "at least one body" in refused.text


# --------------------------------------------------------------------------
# Names, selectors and per-part exports
# --------------------------------------------------------------------------


def test_a_copy_names_no_faces_of_its_own(client: TestClient) -> None:
    """Otherwise every tag would be reported once per copy."""
    copy_of(client)
    listed = client.get("/api/projects/table/topology").json()
    assert set(listed["bodies"]) == {"leg"}


def test_asking_a_copy_for_its_faces_points_at_the_source(client: TestClient) -> None:
    copy_of(client)
    answer = client.get("/api/projects/table/topology", params={"body": "leg_2"})
    assert answer.status_code >= 400
    assert "'leg'" in answer.text and "copy" in answer.text


def test_a_selector_written_on_the_source_applies_to_every_copy(client: TestClient) -> None:
    """One name, four faces in the world — which is why copies stay out of the index."""
    copy_of(client)
    preview = client.post(
        "/api/projects/table/resolve", json={"selector": "shaft/cap+"}
    ).json()
    assert preview["ok"] is True
    assert preview["count"] == 1
    assert [entry["id"] for entry in preview["bodies"]] == ["leg"]


def test_the_mesh_export_includes_every_copy(client: TestClient) -> None:
    """The assembly as modelled — four legs, not one."""
    copy_of(client, origin=[400, 0, 0])
    one = _stl(client, body="leg")
    both = _stl(client)
    assert _triangles(both) == 2 * _triangles(one)


def test_one_part_can_still_be_exported_on_its_own(client: TestClient) -> None:
    """What goes to the printer; the parts list says how many times."""
    copy_of(client, origin=[400, 0, 0])
    assert _triangles(_stl(client, body="leg")) == _triangles(_stl(client, body="leg_2"))


def test_an_exported_copy_carries_its_own_placement(client: TestClient) -> None:
    copy_of(client, origin=[400, 0, 0])
    moved = _vertices(_stl(client, body="leg_2"))
    assert min(x for x, _, _ in moved) == pytest.approx(400.0)


# --------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------


def test_copies_round_trip_through_the_file(client: TestClient) -> None:
    copy_of(client, origin=[400, 0, 0])
    written = document(client)
    again = client.post("/api/projects", json={"id": "again", "document": written})
    assert again.status_code == 201, again.text
    assert client.get("/api/projects/again/document").json()["bodies"] == written["bodies"]


def test_a_body_that_copies_nothing_writes_no_of(client: TestClient) -> None:
    """So nothing already on disk churns for a feature it does not use."""
    copy_of(client)
    written = document(client)["bodies"]
    assert "of" not in next(b for b in written if b["id"] == "leg")


# --------------------------------------------------------------------------
# Failure travels from the source
# --------------------------------------------------------------------------


def test_a_copy_of_a_body_that_did_not_build_says_so(client: TestClient) -> None:
    copy_of(client)
    broken = client.patch(
        "/api/projects/table/features/shaft",
        json={"spec": {"id": "shaft", "type": "pad", "profile": "leg.nosuchloop"}},
    )
    build = broken.json() if broken.status_code == 200 else None
    if build is None:  # the edit itself was refused, which is also correct
        return
    copy = result_body(build, "leg_2")
    assert copy["ok"] is False
    assert "leg" in str(copy["error"])


class _CountingKernel(FakeKernel):
    """A kernel that says how often it was asked to tessellate."""

    def __init__(self) -> None:
        super().__init__()
        self.tessellations = 0

    def tessellate(self, solid, tolerance: float = 0.1):  # type: ignore[no-untyped-def]
        self.tessellations += 1
        return super().tessellate(solid, tolerance)


def _stl(client: TestClient, body: str | None = None) -> bytes:
    params = {"fmt": "stl", **({"body": body} if body else {})}
    answer = client.get("/api/projects/table/export", params=params)
    assert answer.status_code == 200, answer.text
    return answer.content


def _triangles(stl: bytes) -> int:
    return struct.unpack("<I", stl[80:84])[0]


def _vertices(stl: bytes) -> list[tuple[float, float, float]]:
    count = _triangles(stl)
    points: list[tuple[float, float, float]] = []
    for index in range(count):
        at = 84 + index * 50 + 12  # past the header, the count and the normal
        for corner in range(3):
            points.append(struct.unpack("<3f", stl[at + corner * 12 : at + corner * 12 + 12]))
    return points
