"""MCP adapter tests, driving the tools against a stubbed API.

The MCP server is the only adapter that reaches the application layer over the
wire, so what it has to get right is the wire: the shape of what comes back,
and — above all — that a refusal survives the extra hop with its diagnostic
intact. A stubbed transport is therefore the honest test double here. It is
also the only one that can pretend to be a four-thousand-face model without
building one.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

pytest.importorskip("mcp", reason="requires the optional MCP extra")

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from facet.adapters.mcp.server import MAX_ITEMS, FacetCADClient, build_server

BASE_URL = "http://facet.test/api"


class FakeApi:
    """A FacetCAD API that answers from a table instead of from geometry."""

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], httpx.Response] = {}
        self.seen: list[httpx.Request] = []

    def on(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        content: bytes | None = None,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._routes[method, path] = httpx.Response(
            status, json=json, content=content, headers=headers
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        response = self._routes.get((request.method, request.url.path))
        if response is None:  # pragma: no cover - a test asked for a route it never set
            raise AssertionError(f"unstubbed {request.method} {request.url.path}")
        return httpx.Response(
            response.status_code, content=response.content, headers=response.headers
        )


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def tools(api: FakeApi) -> MCPServer[Any]:
    return build_server(FacetCADClient(BASE_URL, transport=httpx.MockTransport(api.handle)))


def call(tools: MCPServer[Any], name: str, **arguments: Any) -> dict[str, Any]:
    result = asyncio.run(tools.call_tool(name, arguments))
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


def listed(tools: MCPServer[Any]) -> list[Any]:
    return asyncio.run(tools.list_tools())


def recompute_result(**overrides: Any) -> dict[str, Any]:
    """What every editing endpoint answers with."""
    return {
        "ok": True,
        "bodies": [{"id": "main", "ok": True, "faceCount": 6, "error": None}],
        "features": [
            {"id": "base", "type": "pad", "status": "built", "faceCount": 6, "error": None}
        ],
        "parameters": {"plate_w": 120.0, "plate_h": 72.0},
        "lastGoodFeature": "base",
        "error": None,
        **overrides,
    }


# --------------------------------------------------------------------------
# The tools as documentation
# --------------------------------------------------------------------------


def test_every_tool_carries_a_description_and_a_usable_input_schema(
    tools: MCPServer[Any],
) -> None:
    """The description and the schema are all an agent ever sees of a tool.

    It cannot read this module, so a tool that arrives undocumented or with a
    malformed schema is a tool that will be called wrongly or not at all.
    """
    registered = listed(tools)
    assert registered

    for tool in registered:
        assert tool.description and tool.description.strip(), tool.name
        schema = tool.input_schema
        assert schema["type"] == "object", tool.name
        properties = schema.get("properties", {})
        assert isinstance(properties, dict), tool.name
        for required in schema.get("required", []):
            assert required in properties, f"{tool.name}: {required} is required but undeclared"


def test_the_tools_cover_discovery_building_understanding_and_export(
    tools: MCPServer[Any],
) -> None:
    """An agent finds a tool by guessing at its group, so every group must exist."""
    names = {tool.name for tool in listed(tools)}
    assert {"list_projects", "kernel_info", "feature_types", "expression_help"} <= names
    assert {"create_project", "set_parameters", "add_feature", "put_sketch"} <= names
    assert {"recompute", "topology", "resolve_selector", "datum_for_face"} <= names
    assert {"export", "export_cut_path", "export_enclosure"} <= names


def test_the_tools_cover_the_document_operations_the_api_offers(
    tools: MCPServer[Any],
) -> None:
    """An operation the API has and the tools do not is one an agent cannot reach.

    It has no other route to the server: no shell, no URL building, nothing but
    this list. A gap here is a feature that, from where the agent sits, does not
    exist.
    """
    names = {tool.name for tool in listed(tools)}
    assert {"delete_project", "replace_document", "import_parameters"} <= names
    assert {"edit_parameter", "delete_parameter", "parameter_usage"} <= names
    assert {"delete_sketch", "delete_datum", "delete_feature"} <= names
    assert {"add_body", "move_body", "delete_body"} <= names
    assert "guide" in names


def test_add_feature_warns_that_a_selector_is_re_resolved_on_every_rebuild(
    tools: MCPServer[Any],
) -> None:
    """The trap has to be named where it is fallen into, not in a README.

    A selector written from memory costs a build, and the tool that accepts one
    is the last place an agent looks before paying for that.
    """
    description = next(t for t in listed(tools) if t.name == "add_feature").description or ""
    assert "re-resolved" in description
    assert "resolve_selector" in description


# --------------------------------------------------------------------------
# Round trips
# --------------------------------------------------------------------------


def test_a_successful_call_returns_what_the_api_returned(
    tools: MCPServer[Any], api: FakeApi
) -> None:
    """The adapter is a wrapper; inventing or dropping fields would make it a fork."""
    api.on("GET", "/api/projects", json={"projects": [{"id": "bracket", "name": "Bracket"}]})

    assert call(tools, "list_projects") == {
        "projects": [{"id": "bracket", "name": "Bracket"}],
        "count": 1,
    }


def test_setting_a_parameter_reports_the_rebuild_feature_by_feature(
    tools: MCPServer[Any], api: FakeApi
) -> None:
    """Changing a number is the whole product, so its answer is the whole verdict.

    An agent that cannot see which feature its edit broke has to go looking,
    and the derived values are how it confirms the change propagated at all.
    """
    api.on(
        "PATCH",
        "/api/projects/bracket/parameters",
        json=recompute_result(
            ok=False,
            features=[
                {"id": "base", "type": "pad", "status": "built", "error": None},
                {
                    "id": "slot",
                    "type": "pocket",
                    "status": "failed",
                    "error": {
                        "kind": "FeatureBuildError",
                        "message": "feature 'slot' failed: the pocket removes no material",
                        "feature": "slot",
                        "reason": "the pocket removes no material",
                    },
                },
                {"id": "edge_break", "type": "fillet", "status": "skipped", "error": None},
            ],
            parameters={"plate_w": 160.0, "plate_h": 96.0, "plate_t": 6.0},
        ),
    )

    report = call(tools, "set_parameters", project="bracket", changes={"plate_w": 160})

    assert report["ok"] is False
    assert [row["status"] for row in report["features"]] == ["built", "failed", "skipped"]
    assert [row["id"] for row in report["failures"]] == ["slot"]
    assert "removes no material" in report["failures"][0]["error"]
    assert report["parameters"]["plate_h"] == 96.0
    assert report["lastGoodFeature"] == "base"

    sent = json.loads(api.seen[-1].content)
    assert sent == {"changes": {"plate_w": 160}}


def test_a_topology_answers_with_tags_and_never_with_fingerprints(
    tools: MCPServer[Any], api: FakeApi
) -> None:
    """Tags are what a selector is written from; fingerprints are float noise.

    The API returns both because the UI diffs the fingerprints. An agent has no
    use for a face's centroid and every reason not to spend context on it.
    """
    api.on(
        "GET",
        "/api/projects/bracket/topologies",
        json={
            "bodies": [
                {
                    "id": "main",
                    "faces": [
                        {"tag": "base/cap+", "fingerprint": {"centroid": [1.0, 2.0, 3.0]}},
                        {
                            "tag": "base/side[outline.left]",
                            "fingerprint": {"centroid": [0.0, 0.0, 0.0]},
                        },
                    ],
                    "edges": [
                        {"tag": "base/cap+ ^ base/side[outline.left]", "fingerprint": {}}
                    ],
                    "retired": [
                        {"tag": "slot/floor", "reason": "consumed", "retired_by": "merge_1"}
                    ],
                }
            ]
        },
    )

    result = call(tools, "topology", project="bracket")

    assert result["faces"] == ["base/cap+", "base/side[outline.left]"]
    assert result["counts"] == {"faces": 2, "edges": 1, "retired": 1}
    assert result["retired"] == [
        {"tag": "slot/floor", "reason": "consumed", "retiredBy": "merge_1"}
    ]
    assert "fingerprint" not in json.dumps(result)


def two_bodies() -> dict[str, Any]:
    """A plate and a pin, each with its own named geometry."""
    return {
        "bodies": [
            {
                "id": "plate",
                "faces": [{"tag": "slab/cap+", "fingerprint": {}}],
                "edges": [],
                "retired": [],
            },
            {
                "id": "pin",
                "faces": [
                    {"tag": "shank/cap+", "fingerprint": {}},
                    {"tag": "shank/side[shaft.rim]", "fingerprint": {}},
                ],
                "edges": [],
                "retired": [],
            },
        ]
    }


def test_a_second_body_is_not_left_out_of_the_topology(
    tools: MCPServer[Any], api: FakeApi
) -> None:
    """The single-body endpoint answers for the first body and says nothing of the rest.

    An agent reading it on a two-part model concludes the second part has no
    faces, which is the one wrong belief that cannot be recovered from: it will
    write selectors for faces it was never shown, or decide the ones it can see
    are all there is.
    """
    api.on("GET", "/api/projects/assembly/topologies", json=two_bodies())

    result = call(tools, "topology", project="assembly")

    assert [body["id"] for body in result["bodies"]] == ["plate", "pin"]
    assert result["bodies"][1]["faces"] == ["shank/cap+", "shank/side[shaft.rim]"]
    assert result["counts"]["faces"] == 3


def test_one_body_can_be_asked_for_by_name(tools: MCPServer[Any], api: FakeApi) -> None:
    """Which part a face is on is what `export(body=...)` needs to be told."""
    api.on("GET", "/api/projects/assembly/topologies", json=two_bodies())

    result = call(tools, "topology", project="assembly", body="pin")

    assert result["body"] == "pin"
    assert result["faces"] == ["shank/cap+", "shank/side[shaft.rim]"]

    with pytest.raises(ToolError, match="plate, pin"):
        call(tools, "topology", project="assembly", body="ghost")


def test_a_selector_that_names_another_body_is_told_so_rather_than_left_wrong(
    tools: MCPServer[Any], api: FakeApi
) -> None:
    """The API resolves against the first body, and a face on the second is simply absent.

    Zero matches on a tag that plainly exists reads as "you typed it wrong",
    and an agent that believes that rewrites a selector that was already right.
    """
    api.on(
        "POST",
        "/api/projects/assembly/resolve",
        json={"selector": "shank/cap+", "matched": [], "count": 0, "ok": False, "error": None},
    )
    api.on("GET", "/api/projects/assembly/topologies", json=two_bodies())

    result = call(tools, "resolve_selector", project="assembly", selector="shank/cap+")

    assert result["count"] == 0
    assert "'pin'" in result["note"]
    assert "first body" in result["note"]


def test_a_selector_that_matches_costs_no_second_request(
    tools: MCPServer[Any], api: FakeApi
) -> None:
    """The hint is worth a round trip only where it changes what happens next."""
    api.on(
        "POST",
        "/api/projects/assembly/resolve",
        json={
            "selector": "slab/cap+",
            "matched": ["slab/cap+"],
            "count": 1,
            "ok": True,
            "error": None,
        },
    )

    result = call(tools, "resolve_selector", project="assembly", selector="slab/cap+")

    assert "note" not in result
    assert [request.url.path for request in api.seen] == ["/api/projects/assembly/resolve"]


def test_an_ignored_option_is_reported_rather_than_left_silent(
    tools: MCPServer[Any], api: FakeApi
) -> None:
    """A key the feature type does not read is warned about, not refused, on a rebuild.

    That was the point of the warning: refusing would break documents that have
    always built. It only works if the warning arrives — a report that drops it
    restores the exact silence that made a counterbore on a pad expensive to
    diagnose.
    """
    api.on(
        "PATCH",
        "/api/projects/bracket/parameters",
        json=recompute_result(
            features=[
                {
                    "id": "base",
                    "type": "pad",
                    "status": "built",
                    "error": None,
                    "warnings": ["pad does not take 'counterbore_depth'"],
                }
            ]
        ),
    )

    report = call(tools, "set_parameters", project="bracket", changes={"plate_w": 160})

    assert report["ok"] is True
    assert report["warnings"] == ["base: pad does not take 'counterbore_depth'"]
    assert report["features"][0]["warnings"] == ["pad does not take 'counterbore_depth'"]


def test_a_blend_that_was_allowed_to_fail_is_not_counted_as_a_failure(
    tools: MCPServer[Any], api: FakeApi
) -> None:
    """`on_failure: skip` means the build carried on, not that nothing happened.

    It carries an error and a healthy model, so reading the error as a failure
    would make every such document look broken — and reading the model as fine
    would hide a fillet that never got cut. It is its own outcome.
    """
    api.on(
        "POST",
        "/api/projects/bracket/recompute",
        json=recompute_result(
            features=[
                {"id": "base", "type": "pad", "status": "built", "error": None},
                {
                    "id": "soften",
                    "type": "fillet",
                    "status": "bypassed",
                    "error": {"message": "radius 2 does not fit at that corner"},
                },
            ]
        ),
    )

    report = call(tools, "recompute", project="bracket")

    assert report["ok"] is True
    assert report["failures"] == []
    assert [row["id"] for row in report["bypassed"]] == ["soften"]


def test_a_forced_rebuild_says_so_on_the_wire(tools: MCPServer[Any], api: FakeApi) -> None:
    """The flag is the whole difference between reusing the cache and not."""
    api.on("POST", "/api/projects/bracket/recompute", json=recompute_result())

    call(tools, "recompute", project="bracket", force=True)
    assert api.seen[-1].url.params.get("force") == "true"

    call(tools, "recompute", project="bracket")
    assert "force" not in api.seen[-1].url.params


def test_deleting_a_project_survives_an_answer_with_no_body(
    tools: MCPServer[Any], api: FakeApi
) -> None:
    """A 204 has nothing to parse, and reporting it as a failure would be a lie."""
    api.on("DELETE", "/api/projects/bracket", status=204)

    assert call(tools, "delete_project", project="bracket") == {"deleted": "bracket"}


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def test_a_refused_selector_arrives_with_its_own_diagnostic(
    tools: MCPServer[Any], api: FakeApi
) -> None:
    """Fail-loud is the product; a generic failure would throw the product away.

    The API has already serialised what went wrong, what was missing and why.
    Reducing that to "the request failed" leaves an agent with nothing to act
    on but a retry, which is exactly the guessing this system refuses to do.
    """
    api.on(
        "POST",
        "/api/projects/bracket/features",
        status=422,
        json={
            "detail": {
                "kind": "SelectorResolutionError",
                "message": "selector base/side[*] expected 4 result(s), resolved 2",
                "selector": "base/side[*]",
                "expected": 4,
                "actual": 2,
                "feature": "edge_break",
                "missing": ["base/side[outline.left]", "base/side[outline.right]"],
                "unexpected": [],
                "reasons": ["the pad no longer reaches that sketch curve"],
            }
        },
    )

    with pytest.raises(ToolError) as raised:
        call(tools, "add_feature", project="bracket", spec={"id": "edge_break"})

    text = str(raised.value)
    assert "expected 4 result(s), resolved 2" in text
    assert "base/side[outline.left]" in text
    assert "the pad no longer reaches that sketch curve" in text
    assert "edge_break" in text
    assert "SelectorResolutionError" in text


def test_a_missing_project_says_so_rather_than_reporting_a_status_code(
    tools: MCPServer[Any], api: FakeApi
) -> None:
    """A 404 from this API still carries a sentence, and the sentence is the answer."""
    api.on(
        "POST",
        "/api/projects/ghost/recompute",
        status=404,
        json={"detail": {"message": "no project 'ghost'"}},
    )

    with pytest.raises(ToolError, match="no project 'ghost'"):
        call(tools, "recompute", project="ghost")


def test_a_busy_kernel_is_reported_as_worth_retrying(
    tools: MCPServer[Any], api: FakeApi
) -> None:
    """Geometry is one worker, so a request can be turned away without being wrong.

    The server distinguishes the two with Retry-After. Losing that distinction
    leaves an agent reading "refused" and editing a model that was fine.
    """
    api.on(
        "POST",
        "/api/projects/bracket/recompute",
        status=503,
        json={"detail": {"message": "the geometry worker was busy for more than 30s"}},
        headers={"Retry-After": "5"},
    )

    with pytest.raises(ToolError) as raised:
        call(tools, "recompute", project="bracket")

    text = str(raised.value)
    assert "busy" in text
    assert "retrying in about 5s" in text
    assert "nothing was changed" in text


def test_a_slow_rebuild_is_not_reported_as_a_bad_address(tools: MCPServer[Any]) -> None:
    """The two have opposite remedies, and only one of them is 'check FACET_URL'.

    A rebuild queued behind a slow one can legitimately take longer than a
    minute, and calling that unreachable sent an agent to verify a URL that was
    never wrong.
    """
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    slow = build_server(
        FacetCADClient(BASE_URL, transport=httpx.MockTransport(timeout), timeout=1.0)
    )

    with pytest.raises(ToolError) as raised:
        call(slow, "list_projects")

    text = str(raised.value)
    assert "did not answer" in text
    assert "reachable" in text
    assert "FACET_URL" not in text


def test_an_unreachable_api_names_the_address_it_tried(tools: MCPServer[Any]) -> None:
    """The usual cause is FACET_URL, so the message has to contain the URL."""
    unreachable = build_server(
        FacetCADClient(
            BASE_URL,
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused"))
            ),
        )
    )

    with pytest.raises(ToolError) as raised:
        call(unreachable, "list_projects")

    assert BASE_URL in str(raised.value)
    assert "FACET_URL" in str(raised.value)


# --------------------------------------------------------------------------
# Size
# --------------------------------------------------------------------------


def test_a_long_list_is_capped_and_the_answer_admits_it(
    tools: MCPServer[Any], api: FakeApi
) -> None:
    """A truncated list that does not say so reads as the whole truth.

    An agent that believes it has seen every face will write a selector against
    the ones it was shown and conclude the rest do not exist.
    """
    faces = [{"tag": f"base/side[outline.c{n}]", "fingerprint": {}} for n in range(500)]
    api.on(
        "GET",
        "/api/projects/big/topologies",
        json={"bodies": [{"id": "main", "faces": faces, "edges": [], "retired": []}]},
    )

    result = call(tools, "topology", project="big")

    assert len(result["faces"]) == MAX_ITEMS
    assert result["counts"]["faces"] == 500
    assert any("500" in note for note in result["truncated"])


def test_a_binary_export_reports_its_size_and_where_to_fetch_it(
    tools: MCPServer[Any], api: FakeApi
) -> None:
    """An STL is triangles. Nothing an agent asks is answered by having them.

    The byte count says the export worked and the URL is what a shell, a
    browser or the user needs; the megabytes in between are pure cost.
    """
    api.on(
        "GET",
        "/api/projects/bracket/export",
        content=b"\x00" * 4096,
        headers={"content-type": "model/stl"},
    )

    result = call(tools, "export", project="bracket", fmt="stl")

    assert result["bytes"] == 4096
    assert result["mediaType"] == "model/stl"
    assert result["url"] == f"{BASE_URL}/projects/bracket/export?fmt=stl"
    assert "4096" in result["summary"]
    assert "content" not in result


def test_the_parameter_sheet_comes_back_inline_because_it_is_worth_reading(
    tools: MCPServer[Any], api: FakeApi
) -> None:
    """The rule is 'no raw geometry', not 'no content': a CSV is the sheet itself."""
    api.on(
        "GET",
        "/api/projects/bracket/export",
        content=b"name,value,unit\nplate_w,120,mm\n",
        headers={"content-type": "text/csv"},
    )

    result = call(tools, "export", project="bracket", fmt="csv")

    assert "plate_w,120,mm" in result["content"]
    assert result["url"].endswith("fmt=csv")


def test_an_export_that_reports_on_itself_keeps_what_it_reported(
    tools: MCPServer[Any], api: FakeApi
) -> None:
    """A cutting list that silently dropped its curved faces does not add up.

    The flattener says which faces it could not develop in a header rather than
    in the file, so that is the part that has to survive into the answer.
    """
    api.on(
        "GET",
        "/api/projects/bracket/export/flat",
        content=b"<svg/>",
        headers={
            "content-type": "image/svg+xml",
            "X-Faces-Flattened": "5",
            "X-Faces-Skipped": "shell/round[base]",
        },
    )

    result = call(tools, "export_faces", project="bracket")

    assert result["flattened"] == "5"
    assert result["skipped"] == "shell/round[base]"


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_the_client_points_where_facet_url_says(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole reason this adapter speaks HTTP is that the API lives elsewhere."""
    monkeypatch.setenv("FACET_URL", "http://cad.internal:9000/api/")
    assert FacetCADClient().base_url == "http://cad.internal:9000/api"

    monkeypatch.delenv("FACET_URL")
    assert FacetCADClient().base_url == "http://localhost:8000/api"


def test_the_default_timeout_outlasts_what_the_server_may_spend() -> None:
    """A client that gives up first turns a slow part into an unreachable server.

    The kernel's own deadline is 60s and a request may wait 30s for the single
    worker, so anything at or under that would report a working server as
    missing.
    """
    assert FacetCADClient(BASE_URL)._http.timeout.read >= 90
