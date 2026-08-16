"""The MCP endpoint as it is actually deployed: mounted, over HTTP.

The unit tests in `test_mcp.py` exercise the tools against a mocked API. These
exercise the *mount* — the part that makes the server something you reach at a
URL rather than something every client installs. Both failures found while
wiring it were invisible to the tool tests: a sub-application's lifespan is not
run by its parent, and the transport refuses a Host it has not been told about.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("mcp", reason="requires the optional MCP extra")

from facet.adapters.geometry.fake import FakeKernel
from facet.adapters.persistence.filesystem import FilesystemDocumentRepository
from facet.application.services import ProjectService
from facet.main import create_app

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}
HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def app(tmp_path):
    return create_app(ProjectService(FilesystemDocumentRepository(tmp_path), FakeKernel()))


def _result(response) -> dict:
    """Pull the JSON-RPC payload out of an SSE response."""
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    raise AssertionError(f"no SSE data frame in {response.text[:200]!r}")


def test_the_endpoint_is_mounted_beside_the_api(app) -> None:
    """The URL a client is handed is the short one: https://host/mcp."""
    assert any(getattr(route, "path", "") == "/mcp" for route in app.routes)


def test_a_client_can_initialise_over_http(app) -> None:
    """Proves the mounted lifespan ran.

    A sub-application's lifespan is not started by its parent, and the
    streamable transport allocates its task group there. Mounted without it the
    endpoint answers every call with 'task group is not initialized' — present,
    reachable and inert, which no route test would catch.
    """
    with TestClient(app, base_url="http://localhost") as client:
        response = client.post("/mcp/", json=INITIALIZE, headers=HEADERS)
        assert response.status_code == 200, response.text
        assert _result(response)["result"]["serverInfo"]["name"] == "facet"


def test_every_tool_is_reachable_through_the_mount(app) -> None:
    """The tools are the product; a mount that exposes none of them is scenery."""
    with TestClient(app, base_url="http://localhost") as client:
        client.post("/mcp/", json=INITIALIZE, headers=HEADERS)
        listed = client.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers=HEADERS,
        )
        names = {tool["name"] for tool in _result(listed)["result"]["tools"]}

    assert {"list_projects", "add_feature", "resolve_selector", "export"} <= names


def test_an_unknown_host_is_refused(app) -> None:
    """DNS rebinding protection stays on.

    Without it a page in someone's browser could resolve your hostname to a
    server they control and then talk to this one. `FACET_HOSTS` is how a
    deployment names itself; the default trusts loopback only.
    """
    with TestClient(app, base_url="http://evil.example") as client:
        response = client.post("/mcp/", json=INITIALIZE, headers=HEADERS)
    assert response.status_code == 421


def test_a_named_host_is_accepted(monkeypatch, tmp_path) -> None:
    """A deployment reached at a domain name has to be able to say so."""
    monkeypatch.setenv("FACET_HOSTS", "cad.example")
    app = create_app(ProjectService(FilesystemDocumentRepository(tmp_path), FakeKernel()))

    with TestClient(app, base_url="http://cad.example") as client:
        response = client.post("/mcp/", json=INITIALIZE, headers=HEADERS)
    assert response.status_code == 200, response.text


def test_the_api_still_serves_when_the_host_is_unknown(app) -> None:
    """The guard belongs to the MCP transport, not to the whole application."""
    with TestClient(app, base_url="http://evil.example") as client:
        assert client.get("/api/health").status_code == 200


# -- telling a client where to find you -------------------------------------


def test_the_server_hands_out_its_own_client_configuration(app) -> None:
    """A client has to be told the address before it can speak the protocol.

    That address usually arrives in a checked-out `.mcp.json`, which is fine
    until whoever is connecting has no reason to clone anything.
    """
    with TestClient(app, base_url="http://localhost") as client:
        body = client.get("/mcp.json").json()

    assert body["mcpServers"]["facet"]["type"] == "http"
    assert body["mcpServers"]["facet"]["url"].endswith("/mcp")


def test_the_configuration_names_the_host_it_was_reached_at(app) -> None:
    """A copy behind another hostname tells the truth about itself.

    Reading the request beats writing the address into a config that then has to
    be kept in step with wherever the thing is actually deployed.
    """
    with TestClient(app, base_url="http://cad.example") as client:
        body = client.get("/mcp.json", headers={"X-Forwarded-Proto": "https"}).json()

    assert body["mcpServers"]["facet"]["url"] == "https://cad.example/mcp"
    assert body["guide"] == "https://cad.example/api/mcp"


def test_the_configuration_offers_the_same_fact_as_a_command(app) -> None:
    """Not every client takes a file."""
    with TestClient(app, base_url="http://localhost") as client:
        body = client.get("/mcp.json").json()

    assert body["install"].startswith("claude mcp add --transport http facet ")
    assert body["install"].endswith(body["mcpServers"]["facet"]["url"])
