"""The agent guide, checked against the system it describes.

A guide that drifts from the API is worse than none: an agent trusts it, builds
on it, and fails in a way the error messages cannot explain. So the claims that
can be checked mechanically are checked here, and the worked example is built
for real rather than read for plausibility.
"""

from __future__ import annotations

import asyncio
import re

import pytest
from fastapi.testclient import TestClient

from facet.adapters.geometry.fake import FakeKernel
from facet.adapters.http.guide import guide_markdown
from facet.adapters.persistence.filesystem import FilesystemDocumentRepository
from facet.application.features import registered_types
from facet.application.services import ProjectService
from facet.main import create_app


@pytest.fixture
def client(tmp_path) -> TestClient:
    service = ProjectService(FilesystemDocumentRepository(tmp_path), FakeKernel())
    return TestClient(create_app(service))


def test_the_guide_is_served_as_plain_markdown(client: TestClient) -> None:
    """An agent fetches this with a plain GET and reads the body."""
    response = client.get("/api/mcp")
    assert response.status_code == 200
    assert response.text.startswith("# FacetCAD")
    assert "text/plain" in response.headers["content-type"]


def test_every_feature_type_is_documented() -> None:
    """A type the guide omits is one an agent will never reach for."""
    guide = guide_markdown()
    for kind in registered_types():
        assert f"`{kind}`" in guide, kind


def test_every_endpoint_the_guide_names_exists(client: TestClient) -> None:
    """The expensive kind of drift: a route that was renamed or removed.

    Checked against the OpenAPI paths rather than by calling them, so this stays
    a documentation test and not a second integration suite.
    """
    known = set(client.get("/openapi.json").json()["paths"])
    quoted = set(re.findall(r"(/api/[A-Za-z0-9_{}/.\-]+)", guide_markdown()))

    for path in quoted:
        # Strip a query string and normalise the {id} placeholders the guide
        # uses for readability onto the names the schema declares.
        route = path.split("?")[0].rstrip(".,")
        route = route.replace("{id}", "{project_id}")
        assert route in known, f"the guide names {path}, which is not a route"


def test_the_tool_count_the_guide_advertises_is_the_number_there_are() -> None:
    """A client decides whether to bother with MCP on this sentence.

    It is also the claim most likely to rot, because adding a tool is a change
    in one file and the number lives in another. Counting it here is what keeps
    "37 typed tools over everything described here" from becoming a number that
    used to be true.
    """
    pytest.importorskip("mcp", reason="requires the optional MCP extra")
    from facet.adapters.mcp.server import build_server

    tools = asyncio.run(build_server().list_tools())
    claimed = re.search(r"(\d+) typed tools", guide_markdown())

    assert claimed, "the guide no longer says how many tools there are"
    assert int(claimed.group(1)) == len(tools)


def test_the_guide_states_the_rule_the_whole_system_rests_on() -> None:
    """Everything else follows from parameters, so it has to lead."""
    guide = guide_markdown()
    assert "Never write a number where a parameter would do" in guide
    # And the reason, not just the instruction.
    assert "frozen at the value it" in guide


def test_the_guide_warns_about_the_traps_that_cost_a_round_trip() -> None:
    """Each of these was a real mistake made while building the system."""
    guide = guide_markdown()
    for trap in (
        "unique across the whole document",  # feature ids
        "cuts from its own sketch plane",  # pockets have no start face
        "Chamfer before filleting",  # blends meeting at a corner
        "re-resolved on every rebuild",  # what a selector is
    ):
        assert trap in guide, trap


def test_the_worked_example_actually_builds(client: TestClient) -> None:
    """The example is the part of a guide most likely to be copied verbatim.

    Reconstructed here from the same parameters and features the guide states,
    with the rectangles spelled out — the guide elides those for readability and
    this is where that elision gets checked.
    """
    document = {
        "schema": "cadsheet/1",
        "parameters": [
            {"name": "board_w", "value": 24.8},
            {"name": "board_l", "value": 14.5},
            {"name": "board_h", "value": 13.0},
            {"name": "wall", "value": 1.6},
            {"name": "gap", "value": 0.6},
            {"name": "fit", "value": 0.2},
            {"name": "lip_h", "value": 3.0},
            {"name": "cav_w", "expr": "board_w + 2 * gap"},
            {"name": "cav_l", "expr": "board_l + 2 * gap"},
            {"name": "cav_h", "expr": "board_h + gap + lip_h"},
            {"name": "outer_w", "expr": "cav_w + 2 * wall"},
            {"name": "outer_l", "expr": "cav_l + 2 * wall"},
            {"name": "base_h", "expr": "cav_h + wall"},
        ],
        "datums": {
            "cavity_top": {
                "type": "plane",
                "parent": "xy",
                "origin": [0, 0, "base_h"],
                "normal": [0, 0, 1],
            }
        },
        "sketches": {
            "base_outer": _rect("xy", "0", "0", "outer_w", "outer_l"),
            "base_cavity": _rect("cavity_top", "wall", "wall", "cav_w", "cav_l"),
            "lid_plate": _rect("xy", "0", "0", "outer_w", "outer_l"),
            "lid_lip": _rect(
                "xy", "wall + fit", "wall + fit", "cav_w - 2 * fit", "cav_l - 2 * fit"
            ),
        },
        "bodies": [
            {
                "id": "base",
                "features": [
                    {
                        "id": "shell",
                        "type": "pad",
                        "profile": "base_outer.outer",
                        "length": "base_h",
                        "direction": "+normal",
                    },
                    {
                        "id": "cavity",
                        "type": "pocket",
                        "profile": "base_cavity.outer",
                        "depth": "cav_h",
                        "direction": "-normal",
                    },
                ],
            },
            {
                "id": "lid",
                "placement": {"origin": ["outer_w + 10", 0, 0], "rotation": [0, 0, 0]},
                "features": [
                    {
                        "id": "plate",
                        "type": "pad",
                        "profile": "lid_plate.outer",
                        "length": "wall",
                        "direction": "+normal",
                    },
                    {
                        "id": "lip",
                        "type": "pad",
                        "profile": "lid_lip.outer",
                        "length": "lip_h",
                        "direction": "-normal",
                    },
                ],
            },
        ],
    }

    created = client.post(
        "/api/projects", json={"id": "case", "name": "Case", "document": document}
    )
    assert created.status_code == 201, created.text

    built = client.post("/api/projects/case/recompute").json()
    assert built["ok"] is True, built
    assert {body["id"] for body in built["bodies"]} == {"base", "lid"}


def test_the_lip_does_not_steal_the_board_s_headroom(client: TestClient) -> None:
    """The guide claims `cav_h` must include `lip_h`. Check the arithmetic.

    This is the one design point in the example that is easy to get wrong and
    impossible to see: the model builds either way, and the board simply does
    not fit.
    """
    guide = guide_markdown()
    assert '"cav_h",   "expr": "board_h + gap + lip_h"' in guide
    assert "steals the board's headroom" in guide


def _rect(plane: str, x: str, y: str, w: str, h: str) -> dict[str, object]:
    return {
        "plane": plane,
        "points": {
            "p0": [x, y],
            "p1": [f"({x}) + ({w})", y],
            "p2": [f"({x}) + ({w})", f"({y}) + ({h})"],
            "p3": [x, f"({y}) + ({h})"],
        },
        "curves": [
            {"id": "e0", "type": "line", "start": "p0", "end": "p1"},
            {"id": "e1", "type": "line", "start": "p1", "end": "p2"},
            {"id": "e2", "type": "line", "start": "p2", "end": "p3"},
            {"id": "e3", "type": "line", "start": "p3", "end": "p0"},
        ],
        "loops": [{"id": "outer", "curves": ["e0", "e1", "e2", "e3"]}],
    }
