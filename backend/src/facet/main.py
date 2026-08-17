"""Composition root.

The only module that knows both which kernel is installed and that FastAPI
exists. Everything else depends on ports, so swapping the kernel or the storage
backend is a change here and nowhere else.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.applications import Starlette

try:  # The MCP extra is optional; see `_mcp_app`.
    from mcp.server.transport_security import TransportSecuritySettings
except ImportError:  # pragma: no cover - depends on which extras are present
    TransportSecuritySettings = None  # type: ignore[assignment, misc]

from facet.adapters.http import api
from facet.adapters.persistence.filesystem import FilesystemDocumentRepository
from facet.application.ports.geometry import GeometryKernel
from facet.application.services import ProjectService

DESCRIPTION = """
Parameter-sheet driven parametric CAD with deterministic topological naming.

Faces are identified by **provenance**, not by kernel index. A face swept from
sketch curve `left` of feature `base` is `base/side[outline.left]` and stays
that after any parameter change. Documents store **selectors** — queries such as
`base/side[*]` — which are re-evaluated on every rebuild; when one stops
resolving cleanly the rebuild fails with a diagnostic naming the responsible
feature rather than silently rebinding to a different face.

Useful starting points for an agent:

* `GET  /api/topology`  — every tag that currently exists
* `POST /api/resolve`   — what would this selector match, before committing to it
* `POST /api/recompute` — rebuild, with per-feature status and structured errors
"""


def build_kernel() -> GeometryKernel:
    """Select a geometry kernel.

    Prefers OCCT when the optional extra is installed, and falls back to the
    analytic kernel otherwise so the stack is runnable without a heavy
    dependency. ``FACET_KERNEL`` forces a choice.

    OCCT is isolated in a child process by default. A call into OpenCascade
    holds the interpreter lock for its whole duration — measured here, a fine
    mesh over a boolean ran 12.87s and let another thread run three times out of
    a possible ~1,280, with a signal handler not firing until it returned. So
    nothing in this process can interrupt one, and a pathological input takes
    the server with it. In a child it can simply be killed.

    ``FACET_GEOMETRY_ISOLATION=off`` turns that off, which costs the protection
    and saves 5-8% on a rebuild. Worth it only for a benchmark.
    """
    requested = os.environ.get("FACET_KERNEL", "auto").lower()
    isolate = os.environ.get("FACET_GEOMETRY_ISOLATION", "on").lower() not in (
        "off",
        "0",
        "false",
        "no",
    )

    if requested in ("auto", "occt"):
        try:
            from facet.adapters.geometry.occt import OcctKernel
        except ImportError:
            if requested == "occt":
                raise
        else:
            if not isolate:
                return OcctKernel()
            from facet.adapters.geometry.guarded import GuardedKernel

            return GuardedKernel("occt")
    from facet.adapters.geometry.fake import FakeKernel

    return FakeKernel()


def build_service() -> ProjectService:
    root = Path(os.environ.get("FACET_DATA", "./data/projects"))
    kernel = build_kernel()
    service = ProjectService(FilesystemDocumentRepository(root), kernel)
    # When a wedged worker is killed, every cached solid handle refers to memory
    # that no longer exists — and the replacement reuses the same ids for
    # different shapes, so a stale cache would be worse than a slow one.
    set_hook = getattr(kernel, "set_on_restart", None)
    if set_hook is not None:
        set_hook(service.invalidate_caches)
    return service


def create_app(service: ProjectService | None = None) -> FastAPI:
    mcp_app = _mcp_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # A mounted sub-application's lifespan is *not* run by the parent, and
        # the streamable transport allocates its task group there. Without this
        # every tool call fails with "task group is not initialized" — mounted,
        # reachable, and inert.
        if mcp_app is None:
            yield
            return
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    app = FastAPI(
        title="FacetCAD",
        version="0.1.0",
        description=DESCRIPTION,
        openapi_tags=[{"name": "projects", "description": "Project lifecycle and editing"}],
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=os.environ.get("FACET_CORS", "*").split(","),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    api.configure(service or build_service())
    app.include_router(api.router)

    if mcp_app is not None:
        _serve_client_config(app)
        app.mount("/mcp", mcp_app)
    return app


def _serve_client_config(app: FastAPI) -> None:
    """Hand out the client configuration for *this* deployment, at ``/mcp.json``.

    MCP is not discovered by fetching a URL — it is a JSON-RPC session, and a
    client has to be told where the server is before it can speak to one. That
    address usually arrives in a checked-out `.mcp.json`, which is fine until
    the person connecting has no reason to clone anything.

    So the server states its own address. It reads the one it was actually
    reached at rather than one written into a config, which means a copy behind
    a different hostname tells the truth about itself with nothing to keep in
    step.
    """

    @app.get(
        "/mcp.json",
        summary="Ready-to-use MCP client configuration for this server",
        tags=["mcp"],
    )
    def client_config(request: Request) -> dict[str, object]:
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("host", request.url.netloc)
        url = f"{scheme}://{host}/mcp"
        return {
            "mcpServers": {"facet": {"type": "http", "url": url}},
            # A client that takes a command rather than a file needs the same
            # fact in the other shape.
            "install": f"claude mcp add --transport http facet {url}",
            "guide": f"{scheme}://{host}/api/mcp",
        }


def _mcp_app() -> Starlette | None:
    """The MCP protocol as an ASGI app, when the extra is installed.

    Served over HTTP rather than stdio, because that is what makes the server
    part of the *deployment* rather than something every client installs. Point
    a client at ``https://your-host/mcp`` and it works: nothing checked out,
    no subprocess spawned, and an agent on another machine is a first-class
    user. ``python -m facet.mcp`` still speaks stdio for a local client, and
    both drive the same tools.

    Conditional, so the API still boots without the extra. Refusing to serve
    geometry because an optional feature is missing would be a poor trade.
    """
    try:
        from mcp.server.transport_security import TransportSecuritySettings  # noqa: F401

        from facet.adapters.mcp.server import FacetCADClient, build_server
    except ImportError:  # pragma: no cover - depends on which extras are present
        return None

    # The tools reach this same process over loopback rather than calling the
    # service directly. That keeps one code path for both transports, and it
    # keeps the HTTP adapter's error shaping — which the MCP client already
    # knows how to unpack — instead of a second translation of domain errors.
    base = os.environ.get("FACET_SELF_URL", "http://localhost:8000/api")
    return build_server(FacetCADClient(base)).streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        transport_security=_transport_security(),
    )


def _transport_security() -> TransportSecuritySettings:
    """Which Host headers the MCP endpoint will answer to.

    The transport refuses unknown hosts by default, which guards against DNS
    rebinding: a page in someone's browser resolving your hostname to a server
    they control and then talking to this one. That matters, so it stays on —
    but the default allow-list is loopback only, and this is meant to be reached
    at a domain name.

    ``FACET_HOSTS`` is the list of hostnames this deployment answers to,
    comma separated. Setting it to ``*`` turns the protection off, which is
    reasonable behind a reverse proxy that has already vetted the Host and
    nowhere else.
    """
    configured = os.environ.get("FACET_HOSTS", "").strip()
    if configured == "*":
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    named = [entry.strip() for entry in configured.split(",") if entry.strip()]
    # Each name also on any port: a deployment on 8443 is the same deployment,
    # and the validator understands the ':*' form.
    hosts = named + [f"{name}:*" for name in named if ":" not in name]
    # Loopback is always allowed: it is how the stack is used before anyone
    # gives it a name, and a rebinding attack cannot target it.
    hosts += ["localhost", "127.0.0.1", "localhost:*", "127.0.0.1:*"]
    return TransportSecuritySettings(
        allowed_hosts=hosts,
        allowed_origins=[f"http://{h}" for h in hosts] + [f"https://{h}" for h in hosts],
    )


app = create_app()
