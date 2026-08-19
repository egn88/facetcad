"""MCP driving adapter — the modelling system as tools an agent can call.

A driving adapter like ``adapters/http``, and it exposes the same use cases.
It differs from every other adapter in one deliberate way: it reaches
:class:`~facet.application.services.ProjectService` **over HTTP** rather
than by holding a reference to it.

That is a departure from "a driving adapter drives the application layer
directly", so it is worth saying why. The API is deployed as a container. The
MCP server is launched by whatever client the user happens to be sitting in
front of, on their machine, and has to reach a model that lives somewhere else
— often a model other people are editing at the same time. An in-process
adapter would have to carry the kernel, the repository and the project files
with it, which means either the agent edits a private copy of the document or
the container stops being the single source of truth. Pointing at
``FACET_URL`` keeps one service owning the documents and makes the MCP
server something a user can run anywhere.

The cost is one extra hop and the need to re-surface errors that have already
been serialised once. That is what :func:`_diagnostic` is for: this system's
whole selling point is that it refuses rather than guesses, so a 4xx carrying a
structured domain error must arrive at the agent as the same actionable
sentence the UI would show, not as "request failed".
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Annotated, Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

DEFAULT_URL = "http://localhost:8000/api"

#: How long to wait for one API call.
#:
#: Sized from what the server may legitimately spend, not from what feels
#: patient: geometry runs in a child process with a 60s deadline on the call and
#: a 30s wait for the single worker to come free, so a rebuild queued behind a
#: slow one can honestly take a little over 90 seconds. A 60s client timeout
#: turned that into "cannot reach the API", which sends an agent to check a URL
#: that was never wrong. ``FACET_TIMEOUT`` overrides it.
DEFAULT_TIMEOUT = float(os.environ.get("FACET_TIMEOUT", "120"))

#: How many entries any list-shaped answer may carry. A topology of a few
#: thousand faces is ordinary and would evict everything else from an agent's
#: context; the first couple of hundred tags plus an honest count is what
#: actually gets used.
MAX_ITEMS = 200

#: How much of a text export is worth inlining before the URL is the better
#: answer.
MAX_TEXT = 8000

#: Export formats whose content is small and directly useful to read — the
#: parameter sheet and the document itself. Everything else (meshes, STEP,
#: drawings) comes back as a summary and a URL.
#:
#: Spelled out here rather than imported from ``adapters/export`` on purpose:
#: this adapter knows the API only through its wire format, and borrowing a
#: table from the server it is talking to would quietly make it a second
#: in-process client.
TEXT_FORMATS = frozenset({"csv", "json", "yaml", "topology"})

INSTRUCTIONS = """\
Parameter-driven CAD. Faces are named by provenance — the face swept from
sketch curve `left` of feature `base` is `base/side[outline.left]` and is still
that after any parameter change. Documents store selectors, which are
re-resolved on every rebuild; when one stops resolving cleanly the rebuild
fails and names the feature responsible instead of silently binding to a
different face.

The working order is: ask what exists (`topology`), ask what a selector would
match (`resolve_selector`), then write it into the document (`add_feature`).
Guessing a tag is the one way to waste a turn here.

`set_parameters` is the point of the system: change a number and the whole
model rebuilds, with per-feature results telling you whether it still builds.
Read the report: `warnings` names an option a feature type ignored, and a
`bypassed` feature is one that failed and was allowed to.

A document may hold several bodies, each with its own history and each exported
separately. `topology` and `resolve_selector` answer for the whole document and
say which body each face is on; pass `body=` to ask what one part sees. A
feature resolves only within its own body and can never name a face another body
made — that boundary is the thing to keep in view when working on an assembly.

A part that appears more than once is a **copy**, not a second history:
`duplicate_body` shows an existing body again at another placement. It builds
once, edits once, and the build report's `parts` list says how many of each part
the model calls for — the number to produce. Never rebuild the same part twice
to place it twice; nothing then records that the two were meant to stay
identical, and no one can read the count off the model.

`guide` returns the full manual — selector syntax, a worked two-part enclosure,
and the mistakes that cost a rebuild — for when this is all you have been given.
"""


# --------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------


class FacetCADClient:
    """Talks to a running FacetCAD API.

    Synchronous, because tools are dispatched to a worker thread anyway and a
    modelling call is one request with no fan-out to overlap.
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("FACET_URL", DEFAULT_URL)).rstrip("/")
        self._http = httpx.Client(
            base_url=self.base_url,
            transport=transport,
            timeout=DEFAULT_TIMEOUT if timeout is None else timeout,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any | None = None,
    ) -> httpx.Response:
        try:
            response = self._http.request(
                method, path, params=_clean(params), json=json
            )
        except httpx.TimeoutException as error:
            # Distinct from unreachable, because the remedy is the opposite one.
            # The server is there and is working; something in the model is
            # taking longer than a client is willing to wait, and the document
            # is unchanged either way.
            raise ToolError(
                f"the FacetCAD API at {self.base_url} did not answer within "
                f"{self._http.timeout.read}s: {error}. The server is reachable, so "
                "this is a slow rebuild rather than a bad address — try again, "
                "or simplify what is being built. Nothing was changed."
            ) from error
        except httpx.RequestError as error:
            # Naming the URL matters more than naming the exception: the usual
            # cause is that FACET_URL points somewhere the container is not.
            raise ToolError(
                f"cannot reach the FacetCAD API at {self.base_url}: {error}. "
                "Check that it is running and that FACET_URL points at it."
            ) from error
        if response.is_error:
            raise ToolError(_diagnostic(response))
        return response

    def json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any | None = None,
    ) -> Any:
        return self.request(method, path, params=params, json=json).json()

    def url_for(self, path: str, params: Mapping[str, Any] | None = None) -> str:
        """The address a human or a shell can fetch the real bytes from."""
        return str(httpx.URL(self.base_url + path, params=_clean(params) or None))


def _clean(params: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Drop unset query parameters so the API applies its own defaults."""
    if params is None:
        return None
    return {k: v for k, v in params.items() if v is not None}


# --------------------------------------------------------------------------
# Error surfacing — a refusal has to survive the extra hop
# --------------------------------------------------------------------------


def _diagnostic(response: httpx.Response) -> str:
    """Rebuild the message a domain error carried, from its serialised form.

    The API answers a domain failure with ``detail`` holding the error's own
    ``as_dict()`` — a selector that resolved to nothing arrives with the tags
    it expected and the reasons it did not find them. Flattening that to a
    status code would throw away precisely the part that tells an agent what to
    do next.
    """
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    text = _detail_text(detail) or (
        f"the API refused the request with status {response.status_code}"
    )
    # Geometry runs in a child process with one worker, so a request can be
    # turned away because the worker was busy rather than because anything is
    # wrong with it. The server says so with Retry-After; without repeating that
    # here an agent reads "refused" and starts editing a model that is fine.
    retry_after = response.headers.get("retry-after")
    if retry_after:
        text += (
            f"\n  this one is worth retrying in about {retry_after}s — "
            "the request never ran, and nothing was changed"
        )
    return text


def _detail_text(detail: object) -> str:
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        # FastAPI's own request-validation errors, which are a list of rows.
        return "\n".join(
            str(row.get("msg", row)) if isinstance(row, Mapping) else str(row)
            for row in detail
        )
    if not isinstance(detail, Mapping):
        return ""

    message = str(detail.get("message") or detail.get("reason") or "")
    kind = detail.get("kind")
    lines = [f"{kind}: {message}" if kind and message else message or str(kind or "")]

    for field in ("missing", "unexpected", "reasons", "cycle", "available", "supported"):
        values = detail.get(field)
        if not values or not isinstance(values, Sequence) or isinstance(values, str):
            continue
        rendered = ", ".join(str(v) for v in values)
        # SelectorResolutionError already folds these into its own message.
        # Repeating them would make the diagnostic longer and no more useful.
        if rendered in message:
            continue
        lines.append(f"  {field}: {rendered}")

    feature = detail.get("feature")
    if feature and str(feature) not in message:
        lines.append(f"  feature: {feature}")
    return "\n".join(line for line in lines if line)


def _many(body: Mapping[str, Any]) -> bool:
    """Whether a body is called for more than once."""
    quantity = body.get("quantity")
    return isinstance(quantity, int) and quantity > 1


def _parts_list(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each distinct part with its quantity, when any part repeats.

    Nothing for a model of one-offs: a list of `x1`s says only that the reader
    has to check it, which is the kind of noise that gets a report skimmed.
    """
    parts = [row for row in _rows(result.get("parts")) if isinstance(row, Mapping)]
    if not any(isinstance(row.get("quantity"), int) and row["quantity"] > 1 for row in parts):
        return []
    return [{"body": row.get("body"), "quantity": row.get("quantity")} for row in parts]


def _error_summary(error: object) -> str | None:
    """One feature's build error, as text rather than as a nested object."""
    if not error:
        return None
    return _detail_text(error) or None


# --------------------------------------------------------------------------
# Shaping — what an agent can afford to read
# --------------------------------------------------------------------------


def _cap(values: Sequence[Any], what: str, notes: list[str], limit: int = MAX_ITEMS) -> list[Any]:
    """Trim a list, recording in ``notes`` that it was trimmed.

    Silent truncation reads as "that is all there is", which for a list of face
    tags is the difference between a selector an agent can trust and one it
    invented from a partial picture.
    """
    if len(values) <= limit:
        return list(values)
    notes.append(f"{what}: showing the first {limit} of {len(values)}")
    return list(values[:limit])


def _rows(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _with_notes(payload: dict[str, Any], notes: list[str]) -> dict[str, Any]:
    if notes:
        payload["truncated"] = notes
    return payload


def _build_report(result: Mapping[str, Any]) -> dict[str, Any]:
    """The verdict on a rebuild: does it still build, and if not, what broke.

    Every editing endpoint answers with a whole ``RecomputeResult`` — the full
    parameter table, every body's placement matrix, every feature's face count.
    An agent that just changed a number needs three things from that: did it
    build, which feature failed, and what the dependent parameters became. The
    rest is available from ``get_document`` when it is actually wanted.

    Warnings are the exception to "keep it short". A rebuild reports an option
    the feature type does not read as a warning rather than refusing — an
    existing document containing one has always built, and failing it now would
    break working parts — so the warning is the only trace that a key is being
    ignored. Dropping it here would restore exactly the silence it was added to
    end.
    """
    notes: list[str] = []

    features: list[dict[str, Any]] = []
    warnings: list[str] = []
    for outcome in _cap(_rows(result.get("features")), "features", notes):
        if not isinstance(outcome, Mapping):
            continue
        row: dict[str, Any] = {
            "id": outcome.get("id"),
            "type": outcome.get("type"),
            "status": outcome.get("status"),
        }
        error = _error_summary(outcome.get("error"))
        if error:
            row["error"] = error
        said = [str(note) for note in _rows(outcome.get("warnings"))]
        if said:
            row["warnings"] = said
            warnings.extend(f"{outcome.get('id')}: {note}" for note in said)
        features.append(row)

    bodies = [
        {
            "id": body.get("id"),
            "ok": body.get("ok"),
            "faceCount": body.get("faceCount"),
            # Present only where they say something, so a model with no copies
            # in it reads exactly as it did before they existed.
            **({"of": body.get("of")} if body.get("of") else {}),
            **({"quantity": body.get("quantity")} if _many(body) else {}),
            # Stated per body as well as in `warnings`: a caller reading the
            # body list to decide what to export needs it on the row.
            **({"empty": True} if body.get("empty") else {}),
        }
        for body in _rows(result.get("bodies"))
        if isinstance(body, Mapping)
    ]

    # Per-body notes — "this body is empty", chiefly. Folded in with the
    # feature warnings rather than given their own key, because a caller
    # scanning one list for "what should I know?" must not have to know there
    # are two.
    warnings.extend(str(note) for note in _rows(result.get("warnings")))

    parameters = result.get("parameters")
    if isinstance(parameters, Mapping):
        names = _cap(sorted(parameters), "parameters", notes)
        resolved = {name: parameters[name] for name in names}
    else:
        resolved = {}

    report: dict[str, Any] = {
        "ok": bool(result.get("ok")),
        # Distinct parts and how many of each the model calls for — the piece
        # count for a print run. Omitted where every part is a one-off, which is
        # every model that has not used `duplicate_body`.
        **({"parts": parts} if (parts := _parts_list(result)) else {}),
        "features": features,
        # Only the ones that actually stopped the build. A blend carrying
        # `on_failure: skip` also arrives with an error attached, and calling
        # that a failure would have every such model read as broken.
        "failures": [f for f in features if f.get("status") == "failed"],
        "parameters": resolved,
        "bodies": _cap(bodies, "bodies", notes),
    }
    # A feature that did not happen while `ok` stayed true is the quiet kind of
    # wrong this project exists to avoid, so it is stated rather than left to be
    # noticed in the feature list.
    bypassed = [f for f in features if f.get("status") == "bypassed"]
    if bypassed:
        report["bypassed"] = bypassed
    if warnings:
        report["warnings"] = _cap(warnings, "warnings", notes)
    if result.get("lastGoodFeature"):
        report["lastGoodFeature"] = result["lastGoodFeature"]
    error = _error_summary(result.get("error"))
    if error:
        report["error"] = error
    return _with_notes(report, notes)


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------

server: MCPServer[Any] = MCPServer(
    name="facet",
    version="0.1.0",
    instructions=INSTRUCTIONS,
)

#: Set by :func:`build_server`, the same way the HTTP adapter is handed its
#: service by the composition root.
_client: FacetCADClient | None = None


def configure(client: FacetCADClient) -> None:
    global _client
    _client = client


def client() -> FacetCADClient:
    if _client is None:  # pragma: no cover - configuration bug
        raise RuntimeError("the MCP server was not configured with a FacetCADClient")
    return _client


def build_server(api: FacetCADClient | None = None) -> MCPServer[Any]:
    configure(api or FacetCADClient())
    return server


def _project(project: str) -> str:
    return f"/projects/{project}"


# --------------------------------------------------------------------------
# Discovery — what exists and what this installation can do
# --------------------------------------------------------------------------

Project = Annotated[str, Field(description="Project id, as returned by list_projects")]


@server.tool()
def list_projects() -> dict[str, Any]:
    """Every project on this server, with its name and when it was last saved.

    The starting point when you do not already have a project id: every other
    tool needs one.
    """
    notes: list[str] = []
    body = client().json("GET", "/projects")
    projects = _rows(body.get("projects"))
    return _with_notes(
        {"projects": _cap(projects, "projects", notes), "count": len(projects)}, notes
    )


@server.tool()
def get_document(
    project: Project,
    fmt: Annotated[
        str, Field(description="'json' for structure, 'yaml' for the file as stored")
    ] = "json",
) -> dict[str, Any]:
    """Read a project's whole source: parameters, datums, sketches, features, bodies.

    This is the model as authored, not as built — it is readable even when the
    model does not currently build, which is exactly when you need to see it.
    Use `recompute` to find out what it does when built, and `topology` for the
    names the build produced.

    A body carrying `of` is a copy: it has no features, and its geometry is the
    body named there, placed at its own `placement`.
    """
    api = client()
    if fmt == "yaml":
        response = api.request(
            "GET", _project(project) + "/document", params={"fmt": "yaml"}
        )
        return {"yaml": response.text}

    document = api.json("GET", _project(project) + "/document")
    notes: list[str] = []
    for key in ("parameters", "features"):
        rows = document.get(key)
        if isinstance(rows, list):
            document[key] = _cap(rows, key, notes)
    document["url"] = api.url_for(_project(project) + "/document")
    return _with_notes(document, notes)


@server.tool()
def kernel_info() -> dict[str, Any]:
    """Which geometry kernel is running and what it is able to do.

    Worth asking before planning work: the analytic kernel handles axis-aligned
    prismatic solids and no more, so STEP export, arcs, fillets and flattening
    are only available when OCCT is installed. Capabilities are declared rather
    than discovered by failing, so this answer is authoritative.
    """
    return dict(client().json("GET", "/kernel"))


@server.tool()
def feature_types() -> dict[str, Any]:
    """The feature types `add_feature` will accept, and the options each takes.

    Read from the live handler registry rather than from documentation, so it
    cannot drift from what the server actually builds — the options listed here
    are exactly the ones a build will accept, and anything else is refused
    rather than ignored.

    Worth reading before the first `add_feature` of a session: it saves
    discovering that `at` wants 'sketch.point', or that a blend's `edges` is a
    selector string rather than a list, from a build error.
    """
    return dict(client().json("GET", "/feature-types"))


@server.tool()
def expression_help() -> dict[str, Any]:
    """The functions and constants an expression may use.

    Any dimension in this system can be an expression over parameter names —
    `plate_w * 0.6` is as valid as `72`. Check a name here before assuming a
    failed expression means a missing parameter: it may simply be a function
    that does not exist.
    """
    return dict(client().json("GET", "/expressions"))


@server.tool()
def guide() -> dict[str, Any]:
    """The whole manual for this system, as Markdown — read it once, up front.

    Written for a model that has been handed these tools and nothing else: the
    document's five parts in dependency order, selector syntax, a worked
    two-part enclosure that builds as written, a pattern for each thing you are
    likely to be asked for, and a table of the refusals you will meet with what
    each one means.

    It costs a few thousand tokens, which is less than one avoidable rebuild.
    Worth it at the start of any session that will do more than read.
    """
    response = client().request("GET", "/mcp")
    return {"guide": response.text, "url": client().url_for("/mcp")}


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------


@server.tool()
def create_project(
    project: Annotated[str, Field(description="URL-safe id; becomes the filename")],
    name: Annotated[str, Field(description="Display name; defaults to the id")] = "",
    document: Annotated[
        dict[str, Any] | None,
        Field(description="A whole document to start from; blank when omitted"),
    ] = None,
) -> dict[str, Any]:
    """Create a project, empty or from a complete document.

    Passing a `document` is the fast path: a whole model — parameters, datums,
    sketches and features — lands in one call and is validated as a unit. See
    `get_document` on an existing project for the shape.
    """
    return dict(
        client().json(
            "POST",
            "/projects",
            json={"id": project, "name": name, "document": document},
        )
    )


@server.tool()
def delete_project(
    project: Annotated[str, Field(description="Project id; the whole document goes")],
) -> dict[str, Any]:
    """Delete a project and everything in it.

    There is no undo and no trash: the document is the model, and removing it
    removes the parameters, the sketches and the history with it. Read
    `list_projects` first if the id came from anywhere but this session.
    """
    client().request("DELETE", _project(project))
    return {"deleted": project}


@server.tool()
def replace_document(
    project: Project,
    document: Annotated[
        dict[str, Any] | None,
        Field(description="A whole document, in the shape `get_document` returns"),
    ] = None,
    yaml: Annotated[
        str | None, Field(description="The same thing as YAML text, as stored on disk")
    ] = None,
) -> dict[str, Any]:
    """Replace a project's entire document in one call, then rebuild.

    Give either `document` or `yaml`. This is the tool for a change too broad to
    express as a series of edits — re-deriving a sheet of parameters at once,
    or copying a model onto another server — and for editing a document you have
    read as YAML.

    It is validated as a unit and applied whole or not at all, so a document
    that does not parse leaves the existing one untouched. What it is *not* is a
    substitute for the editing tools: rewriting a whole document to change one
    number loses the feature-by-feature verdict that says which edit broke what.

    Returns the rebuild, in the same shape `recompute` does — so a document you
    have just written can be checked in the call that wrote it.
    """
    return _build_report(
        client().json(
            "PUT",
            _project(project) + "/document",
            json={"document": document, "yaml": yaml},
        )
    )


@server.tool()
def add_parameter(
    project: Project,
    name: Annotated[str, Field(description="Identifier other expressions will use")],
    value: Annotated[float | None, Field(description="A literal number")] = None,
    expr: Annotated[
        str | None, Field(description="An expression over other parameters, e.g. 'plate_w * 0.6'")
    ] = None,
    unit: str = "mm",
    group: Annotated[str, Field(description="Sheet section this row belongs to")] = "",
    doc: Annotated[str, Field(description="What this dimension means")] = "",
) -> dict[str, Any]:
    """Add a row to the parameter sheet, then rebuild.

    Give either `value` or `expr`, not both. Prefer `expr` whenever the number
    is derived from another: a parameter written as an expression keeps
    following the model, whereas a copied number silently stops agreeing with
    it the first time its source changes.
    """
    return _build_report(
        client().json(
            "POST",
            _project(project) + "/parameters",
            json={
                "name": name,
                "value": value,
                "expr": expr,
                "unit": unit,
                "group": group,
                "doc": doc,
            },
        )
    )


@server.tool()
def set_parameters(
    project: Project,
    changes: Annotated[
        dict[str, float | str],
        Field(
            description=(
                "Parameter name to its new value. A number replaces the literal; "
                "a string replaces the expression, e.g. {'plate_w': 160, "
                "'plate_h': 'plate_w * 0.75'}"
            )
        ),
    ],
) -> dict[str, Any]:
    """Change parameter values and rebuild the whole model — the core operation.

    This is what the system exists for: change a number, and every dimension,
    every face and every selector that depends on it follows, while the face
    names stay put.

    The answer is the verdict on that rebuild, so read it rather than assuming
    the edit landed:

    * `ok` — whether the model still builds
    * `parameters` — every resolved value, including the derived ones, so you
      can see what your change propagated to
    * `features` — each feature's status: built, skipped, or failed
    * `failures` — the features that broke, each naming what went wrong

    A failure part-way through halts the chain: later features come back
    *skipped* rather than failed, and `lastGoodFeature` names how far the build
    got. The first entry in `failures` is the one to fix; the rest are usually
    consequences of it.

    Changing a dimension can legitimately destroy a face — shrink a plate past
    a pocket and the pocket's walls stop existing — at which point a selector
    written against them fails loudly. That is the design, not a bug: fix the
    selector or the dimension rather than looking for a way to suppress it.
    """
    return _build_report(
        client().json("PATCH", _project(project) + "/parameters", json={"changes": changes})
    )


@server.tool()
def edit_parameter(
    project: Project,
    parameter: Annotated[str, Field(description="The row to change, by its current name")],
    name: Annotated[
        str | None, Field(description="A new name; renames it everywhere it is read")
    ] = None,
    value: Annotated[float | None, Field(description="A new literal number")] = None,
    expr: Annotated[str | None, Field(description="A new expression")] = None,
    unit: str | None = None,
    group: Annotated[str | None, Field(description="Sheet section this row belongs to")] = None,
    doc: Annotated[str | None, Field(description="What this dimension means")] = None,
) -> dict[str, Any]:
    """Change any part of one parameter row — including its name — and rebuild.

    A rename is followed through every expression in the document, so nothing is
    left reading a name that no longer exists. That is what makes renaming
    `w` to `plate_w` a safe thing to do rather than a search and replace across
    a model you cannot see all of.

    Only what you pass is changed. To swap a literal for a derived value, give
    `expr`; `set_parameters` is the shorter route when the value is all that
    changes.
    """
    return _build_report(
        client().json(
            "PATCH",
            f"{_project(project)}/parameters/{parameter}",
            json={
                "name": name,
                "value": value,
                "expr": expr,
                "unit": unit,
                "group": group,
                "doc": doc,
            },
        )
    )


@server.tool()
def parameter_usage(
    project: Project,
    parameter: Annotated[str, Field(description="The parameter to trace")],
) -> dict[str, Any]:
    """Everything that reads a parameter — other expressions, datums, sketches, features.

    The question to ask before deleting or repurposing a row. A parameter that
    nothing reads is safe to remove; one that six dimensions depend on is a
    decision, and this is what turns that into a fact rather than a guess.
    """
    return dict(client().json("GET", f"{_project(project)}/parameters/{parameter}/usage"))


@server.tool()
def delete_parameter(
    project: Project,
    parameter: Annotated[str, Field(description="The row to remove")],
) -> dict[str, Any]:
    """Remove a parameter row and rebuild.

    Refused outright while anything still reads it, naming every reader — a
    document is not allowed to pass through a state where an expression points
    at a name that is gone. So this succeeds only on a row nothing depends on;
    `parameter_usage` tells you which case you are in before you spend the call.

    To retire a parameter that is still in use, change its readers first, or
    rename it into the one you are keeping with `edit_parameter`.
    """
    return _build_report(
        client().json("DELETE", f"{_project(project)}/parameters/{parameter}")
    )


@server.tool()
def import_parameters(
    project: Project,
    csv: Annotated[
        str,
        Field(description="The file contents, as exported by `export` with fmt='csv'"),
    ],
) -> dict[str, Any]:
    """Replace the parameter sheet from a CSV, then rebuild.

    The other half of `export(fmt='csv')`: the sheet goes out to a spreadsheet,
    comes back edited, and lands as a whole table in one rebuild.

    Only parameters are touched — datums, sketches and the feature history are
    left alone, so a round trip cannot damage what a spreadsheet has no way to
    represent. A bad file is rejected whole, naming the row; nothing is
    half-applied.
    """
    return _build_report(
        client().json(
            "POST", _project(project) + "/import", json={"format": "csv", "body": csv}
        )
    )


@server.tool()
def put_sketch(
    project: Project,
    sketch: Annotated[str, Field(description="Sketch id; replaces any sketch of that id")],
    plane: Annotated[str, Field(description="Id of the datum plane it lies on")] = "xy",
    points: Annotated[
        dict[str, list[float | str]] | None,
        Field(
            description=(
                "Point id to [u, v] in the plane. Each coordinate is a number or "
                "an expression, e.g. {'p1': ['plate_w', 0]}"
            )
        ),
    ] = None,
    curves: Annotated[
        list[dict[str, Any]] | None,
        Field(
            description=(
                "Each curve is {'id', 'start', 'end'} naming points, optionally "
                "with 'type' for arcs and circles"
            )
        ),
    ] = None,
    loops: Annotated[
        list[dict[str, Any]] | None,
        Field(description="Each loop is {'id', 'curves': [ids]}, forming a closed profile"),
    ] = None,
) -> dict[str, Any]:
    """Create or replace a sketch — the 2D profile a pad or pocket is built from.

    A sketch attaches to a **datum plane and nothing else**, never to a face.
    That is what makes it impossible for a sketch to move because a face it was
    drawn on got renamed or destroyed. Create the datum first with `put_datum`,
    or use `datum_for_face` to derive one from a face you can see.

    Curve ids are load-bearing: the face a pad sweeps from curve `left` is named
    `<feature>/side[<sketch>.left]`, so a curve id you choose now is the name
    you will select by later. Name them for what they are, not `c0`.

    Coordinates may be expressions, and should be wherever the position is
    derived — `'plate_w / 2'` keeps the hole centred when the plate is resized.
    """
    return _build_report(
        client().json(
            "PUT",
            f"{_project(project)}/sketches/{sketch}",
            json={
                "id": sketch,
                "plane": plane,
                "points": points or {},
                "curves": curves or [],
                "loops": loops or [],
            },
        )
    )


@server.tool()
def delete_sketch(
    project: Project,
    sketch: Annotated[str, Field(description="Id of the sketch to remove")],
) -> dict[str, Any]:
    """Remove a sketch and rebuild.

    Refused while any feature still draws its profile from one of the sketch's
    loops or places itself at one of its points, naming them. Delete those
    features first if that is what you meant — the refusal is the system saying
    the sketch is still load bearing, which is cheaper to hear now than as a
    build that stopped.
    """
    return _build_report(client().json("DELETE", f"{_project(project)}/sketches/{sketch}"))


@server.tool()
def put_datum(
    project: Project,
    datum: Annotated[str, Field(description="Datum id; replaces any datum of that id")],
    origin: Annotated[
        list[float | str] | None,
        Field(description="[x, y, z]; each may be an expression, e.g. [0, 0, 'plate_t']"),
    ] = None,
    normal: Annotated[
        list[float | str] | None, Field(description="[x, y, z] plane normal")
    ] = None,
    x_axis: Annotated[
        list[float | str] | None, Field(description="Fixes the in-plane u direction")
    ] = None,
    parent: Annotated[
        str | None, Field(description="Another datum this one is relative to")
    ] = None,
) -> dict[str, Any]:
    """Create or replace a datum plane — the only thing a sketch may attach to.

    A datum is computed from parameters and other datums, never from picked
    geometry. That is the rule that designs out the whole class of "the sketch
    flipped when I changed a dimension" failures, so keep it: write the origin
    as an expression (`[0, 0, 'plate_t']`) rather than as the number that
    expression happens to equal today, or the datum stops following the model.

    `locate_point` and `datum_for_face` both exist to give you that expression
    instead of the number.
    """
    return _build_report(
        client().json(
            "PUT",
            f"{_project(project)}/datums/{datum}",
            json={
                "id": datum,
                "origin": origin if origin is not None else [0, 0, 0],
                "normal": normal if normal is not None else [0, 0, 1],
                "x_axis": x_axis,
                "parent": parent,
            },
        )
    )


@server.tool()
def delete_datum(
    project: Project,
    datum: Annotated[str, Field(description="Id of the datum to remove")],
) -> dict[str, Any]:
    """Remove a datum plane and rebuild.

    Refused while a sketch still lies on it or another datum declares it as
    `parent`, naming them: deleting a plane out from under a sketch is not made
    safe by doing it quietly. Move the sketch to another datum first.
    """
    return _build_report(client().json("DELETE", f"{_project(project)}/datums/{datum}"))


@server.tool()
def add_feature(
    project: Project,
    spec: Annotated[
        dict[str, Any],
        Field(
            description=(
                "The feature, e.g. {'id': 'base', 'type': 'pad', 'profile': "
                "'outline.outer', 'length': 'plate_t'}. Call feature_types for "
                "the types this server builds."
            )
        ),
    ],
    at: Annotated[
        int | None, Field(description="Position in the history; appends when omitted")
    ] = None,
    body: Annotated[str | None, Field(description="Target body; the first when omitted")] = None,
) -> dict[str, Any]:
    """Add a feature to the history and rebuild.

    **The trap worth knowing:** any selector in the spec — the edges a fillet
    attaches to, the face a hole is placed on — is stored as a *query*, not as
    a pick, and is re-resolved from scratch on every single rebuild. A selector
    that matches today and matches nothing after the next parameter change will
    fail the rebuild rather than quietly binding to whichever face is nearest.
    That refusal is the feature; it is also why a selector typed from memory
    costs you a build.

    So: run `resolve_selector` on the selector first and confirm it matches what
    you meant, then write it in here. `topology` lists every tag that currently
    exists if you need to see the candidates.

    The feature's `id` becomes the prefix of every face it creates — a pad
    called `base` produces `base/cap+`, `base/cap-` and `base/side[...]` — so
    the id is a naming decision, not a label.
    """
    return _build_report(
        client().json(
            "POST", _project(project) + "/features", json={"spec": spec, "at": at, "body": body}
        )
    )


@server.tool()
def update_feature(
    project: Project,
    feature: Annotated[str, Field(description="Id of the feature to change")],
    spec: Annotated[
        dict[str, Any],
        Field(description="The feature's new fields; its id is taken from `feature`"),
    ],
) -> dict[str, Any]:
    """Replace a feature's definition in place and rebuild.

    The feature keeps its id and its position in the history, so the faces it
    owns keep their names and selectors elsewhere in the document keep
    resolving. Changing a selector inside the spec has the same trap as
    `add_feature`: check it with `resolve_selector` first.
    """
    return _build_report(
        client().json("PATCH", f"{_project(project)}/features/{feature}", json=spec)
    )


@server.tool()
def delete_feature(
    project: Project,
    feature: Annotated[str, Field(description="Id of the feature to remove")],
) -> dict[str, Any]:
    """Remove a feature from the history and rebuild.

    Everything the feature created stops existing, so any later feature whose
    selector named one of its faces will now fail. The rebuild report says
    which — that is the whole point of getting an error instead of a silently
    relocated fillet.
    """
    return _build_report(client().json("DELETE", f"{_project(project)}/features/{feature}"))


@server.tool()
def reorder_features(
    project: Project,
    order: Annotated[list[str], Field(description="Every feature id, in the order wanted")],
) -> dict[str, Any]:
    """Reorder the feature history and rebuild.

    Order is meaning: a fillet before the pocket that cuts through it produces a
    different part from the same two features the other way round. Reordering
    also changes what each feature sees, so nothing is reused from the cache and
    every selector is re-resolved against its new upstream shape.
    """
    return _build_report(
        client().json("POST", _project(project) + "/features/reorder", json={"order": order})
    )


@server.tool()
def add_body(
    project: Project,
    body: Annotated[str, Field(description="Body id")],
    origin: Annotated[
        list[float | str] | None, Field(description="[x, y, z] placement; expressions allowed")
    ] = None,
    rotation: Annotated[
        list[float | str] | None, Field(description="[rx, ry, rz] in degrees")
    ] = None,
) -> dict[str, Any]:
    """Add a body — an independently rebuilt part in the same document.

    Each body owns its own feature history and builds separately, so a body
    that fails does not take the others down with it. Placement is applied for
    display and export only and never reaches the modelled geometry, which is
    what lets you move a part around an assembly without perturbing a single
    face name.

    **Add a feature to it in the same breath.** A body with no features builds
    nothing: it will not appear in the viewport and cannot be exported. That is
    not an error — it is what a body is between being created and being filled —
    and every build report says so, as a `warnings` entry and `empty: true` on
    the body's row. A body still marked `empty` is one you have not finished.

    For a part that appears more than once — four legs, six identical brackets —
    use `duplicate_body` rather than adding a second body and rebuilding the
    same history in it. A copy is built once, edited once, and counted, and the
    count is what tells you how many to produce.
    """
    return _build_report(
        client().json(
            "POST",
            _project(project) + "/bodies",
            json={
                "id": body,
                "origin": origin if origin is not None else [0, 0, 0],
                "rotation": rotation if rotation is not None else [0, 0, 0],
            },
        )
    )


@server.tool()
def duplicate_body(
    project: Project,
    body: Annotated[str, Field(description="Id of the body to show again")],
    as_id: Annotated[
        str | None,
        Field(description="Id for the copy; generated from the source's name when omitted"),
    ] = None,
    origin: Annotated[
        list[float | str] | None,
        Field(description="[x, y, z] for the copy; expressions allowed, e.g. ['pitch * 2', 0, 0]"),
    ] = None,
    rotation: Annotated[
        list[float | str] | None, Field(description="[rx, ry, rz] in degrees")
    ] = None,
) -> dict[str, Any]:
    """Show an existing body again at another placement, without copying its history.

    Use this instead of rebuilding the same part a second time. The copy holds
    no features of its own — it *is* the source's solid, appearing again where
    you put it. Three consequences, each of which is the reason to prefer it:

    - **Edit once.** Change a dimension on the source and every copy changes
      with it. There is only one history, so the copies cannot drift apart.
    - **Build once.** The solid is computed and tessellated a single time and
      then transformed, so a model with twelve of a part costs one of them.
    - **Counted.** The document knows the part is called for four times, and
      every build report carries a `parts` list saying so — which is how many
      you need to print. A copy-pasted history cannot answer that question,
      because nothing in it records that the four were meant to be the same.

    `origin` defaults to the source's own placement, which puts the copy on top
    of it: visible in the tree, and asking to be moved. Move it later with
    `move_body`, which treats a copy like any other body.

    Copy the body that has the history, not another copy — the chain is refused
    rather than flattened, so that "where does this geometry come from" always
    has a one-word answer. Add features to the source; `add_feature` against a
    copy is refused and says where they belong.
    """
    payload: dict[str, Any] = {}
    if as_id is not None:
        payload["id"] = as_id
    if origin is not None:
        payload["origin"] = origin
    if rotation is not None:
        payload["rotation"] = rotation
    answer = client().json(
        "POST", f"{_project(project)}/bodies/{body}/copies", json=payload
    )
    report = _build_report(answer)
    if isinstance(answer, Mapping) and answer.get("id"):
        report["id"] = answer["id"]
    return report


@server.tool()
def move_body(
    project: Project,
    body: Annotated[str, Field(description="Id of the body to place")],
    origin: Annotated[
        list[float | str] | None,
        Field(description="[x, y, z]; expressions allowed, e.g. ['outer_w + 10', 0, 0]"),
    ] = None,
    rotation: Annotated[
        list[float | str] | None, Field(description="[rx, ry, rz] in degrees")
    ] = None,
) -> dict[str, Any]:
    """Set where a body sits, and rebuild.

    Placement is for display and export layout only; it never reaches the
    modelled geometry. Moving a part beside another to look at the assembly
    cannot perturb one face name, one fingerprint or one selector — which is
    what makes it a free operation rather than a rebuild of the part.

    Both arguments replace what is there, so pass both to change both.

    A copy made by `duplicate_body` is placed with this too — that is the whole
    of what distinguishes one copy from another.

    To rename a body or annotate it, use `update_body`.
    """
    return _build_report(
        client().json(
            "PATCH",
            f"{_project(project)}/bodies/{body}",
            json={
                "id": body,
                "origin": origin if origin is not None else [0, 0, 0],
                "rotation": rotation if rotation is not None else [0, 0, 0],
            },
        )
    )


@server.tool()
def update_body(
    project: Project,
    body: Annotated[str, Field(description="Id of the body to change")],
    rename_to: Annotated[
        str | None, Field(description="A new id for it; copies of it are followed")
    ] = None,
    doc: Annotated[
        str | None, Field(description="A note about what this part is")
    ] = None,
    origin: Annotated[
        list[float | str] | None,
        Field(description="[x, y, z]; expressions allowed, e.g. ['outer_w + 10', 0, 0]"),
    ] = None,
    rotation: Annotated[
        list[float | str] | None, Field(description="[rx, ry, rz] in degrees")
    ] = None,
) -> dict[str, Any]:
    """Change a body's id, its note, or where it sits — then rebuild.

    Only what you pass is applied, so annotating a body does not move it and
    moving one does not clear its note.

    **Renaming is safe here in a way renaming a parameter is not.** A tag names
    the *features* that made a face — `shaft/cap+` — and never the body they
    live in, so no selector anywhere can be invalidated by a body rename. What
    does name a body is a copy of it, and those are followed for you.

    Use `move_body` when placement is all you are changing; it is the same
    operation with a name that says so.
    """
    payload: dict[str, Any] = {}
    if rename_to is not None:
        payload["id"] = rename_to
    if doc is not None:
        payload["doc"] = doc
    if origin is not None:
        payload["origin"] = origin
    if rotation is not None:
        payload["rotation"] = rotation
    if not payload:
        raise ToolError(
            f"nothing to change on body '{body}'. Pass rename_to, doc, origin or "
            "rotation — an update that says nothing would rebuild for no reason."
        )
    return _build_report(
        client().json("PATCH", f"{_project(project)}/bodies/{body}", json=payload)
    )


@server.tool()
def delete_body(
    project: Project,
    body: Annotated[str, Field(description="Id of the body to remove")],
) -> dict[str, Any]:
    """Remove a body and its whole feature history, then rebuild.

    Bodies are independent, so the others are unaffected — this is the one
    delete in the document that cannot break something elsewhere. What goes
    with it is every feature declared in it and every face those features named.

    One exception: a body that other bodies copy is refused, naming them, since
    deleting it would take their geometry with it. Delete the copies first.
    Deleting a copy is always safe — it removes one placement and drops the
    part's quantity by one, and the source is untouched.
    """
    return _build_report(client().json("DELETE", f"{_project(project)}/bodies/{body}"))


# --------------------------------------------------------------------------
# Understanding the model
# --------------------------------------------------------------------------


@server.tool()
def recompute(
    project: Project,
    force: Annotated[
        bool,
        Field(
            description=(
                "Throw away every cached feature and rebuild the history from "
                "scratch. Slow, and normally unnecessary."
            )
        ),
    ] = False,
) -> dict[str, Any]:
    """Rebuild the model and report what each feature did.

    Use it to check the state of a project you did not just edit, or after
    editing the document by some other route. The editing tools already return
    this same report, so calling it straight after one of them tells you
    nothing new.

    A feature that fails halts the chain: the ones after it come back *skipped*,
    `lastGoodFeature` says how far the build got, and the last good solid is
    kept — so you can see the part as far as it built, with the culprit named.

    Each feature's `status` is one of:

    * `built` — rebuilt now
    * `cached` — unchanged, reused from the last build
    * `suppressed` — switched off in the document, so it did not run
    * `failed` — it broke, and everything after it was skipped
    * `skipped` — not attempted, because an earlier feature failed
    * `bypassed` — it failed, but declared `on_failure: skip`, so the build
      carried on without it. Reported rather than swallowed: a fillet that
      silently did not happen is the quiet wrongness this system exists to
      avoid.

    `force` exists for a state that should not happen. The cache is keyed on
    content, so a stale entry is a bug rather than a thing to work around: reach
    for it when geometry disagrees with the document, not as a habit.
    """
    return _build_report(
        client().json(
            "POST",
            _project(project) + "/recompute",
            params={"force": "true"} if force else None,
        )
    )


def _tags(index: Mapping[str, Any], notes: list[str], where: str = "") -> dict[str, Any]:
    """One solid's named geometry, as the tags and nothing else."""
    prefix = f"{where} " if where else ""
    faces = [str(entry.get("tag")) for entry in _rows(index.get("faces"))]
    edges = [str(entry.get("tag")) for entry in _rows(index.get("edges"))]
    retired = [
        {
            "tag": str(entry.get("tag")),
            "reason": entry.get("reason"),
            "retiredBy": entry.get("retired_by"),
        }
        for entry in _rows(index.get("retired"))
    ]
    return {
        "faces": _cap(faces, f"{prefix}faces", notes),
        "edges": _cap(edges, f"{prefix}edges", notes),
        "retired": _cap(retired, f"{prefix}retired", notes),
        "counts": {"faces": len(faces), "edges": len(edges), "retired": len(retired)},
    }


def _copied_by(project: str, body: str) -> str | None:
    """The body a named copy shows, or None when it is not a copy.

    Read from the document, and only on the path where something was not found —
    a second request to explain a failure is worth it; a second request on every
    successful call is not.
    """
    try:
        document = client().json("GET", _project(project) + "/document")
    except Exception:
        # This runs only to explain a failure. If it cannot, the caller still
        # gets the plain "no such body" message — an explanation that throws
        # would replace a clear error with an obscure one.
        return None
    for row in _rows(document.get("bodies")):
        if isinstance(row, Mapping) and str(row.get("id")) == body:
            source = row.get("of")
            return str(source) if source else None
    return None


def _bodies_of(project: str) -> list[Mapping[str, Any]]:
    """Every body's named geometry, keyed by body id."""
    payload = client().json("GET", _project(project) + "/topologies")
    return [body for body in _rows(payload.get("bodies")) if isinstance(body, Mapping)]


@server.tool()
def topology(
    project: Project,
    body: Annotated[
        str | None,
        Field(description="One body's tags only; every body when omitted"),
    ] = None,
) -> dict[str, Any]:
    """Every face and edge tag the model currently has — the vocabulary of selectors.

    Read this before writing any selector. A tag is a provenance path, not an
    index: `base/side[outline.left]` is the face swept from curve `left` of
    sketch `outline` by feature `base`, and it keeps that name across parameter
    changes.

    A document with more than one body answers per body, because a tag tells you
    what a face is but not which part it is on — and that is what you need both
    for `export(body=...)` and for knowing which body a feature using it has to
    live in. A single-body document answers flat.

    `retired` lists tags that existed in an earlier state and no longer do, with
    the reason and the feature that consumed them. When a selector has just
    stopped resolving, that list usually contains the answer.

    Only the tags come back, never the geometric fingerprints behind them: the
    tags are what you select with, and the fingerprints are large, numeric and
    of no use to a caller.
    """
    notes: list[str] = []
    bodies = _bodies_of(project)

    if body is not None:
        named = next((b for b in bodies if str(b.get("id")) == body), None)
        if named is None:
            copied = _copied_by(project, body)
            if copied is not None:
                # The faces exist; they are named by the history that made them.
                # Sending the reader there beats reporting a body that is not
                # missing as though it were a typo.
                raise ToolError(
                    f"body '{body}' is a copy of '{copied}' and names no faces of its "
                    f"own. Ask for '{copied}' — a selector written there applies to "
                    "every copy of it."
                )
            raise ToolError(
                f"no body '{body}' in project '{project}'. It has: "
                + (", ".join(str(b.get("id")) for b in bodies) or "no bodies")
            )
        return _with_notes({"body": body, **_tags(named, notes)}, notes)

    if len(bodies) <= 1:
        index = bodies[0] if bodies else {}
        return _with_notes(_tags(index, notes), notes)

    per_body = [
        {"id": str(entry.get("id")), **_tags(entry, notes, where=str(entry.get("id")))}
        for entry in bodies
    ]
    return _with_notes(
        {
            "bodies": per_body,
            "counts": {
                key: sum(int(b["counts"][key]) for b in per_body)
                for key in ("faces", "edges", "retired")
            },
        },
        notes,
    )


@server.tool()
def resolve_selector(
    project: Project,
    selector: Annotated[
        str | None,
        Field(description="Face selector shorthand, e.g. 'base/side[*]' or 'lid/cap+'"),
    ] = None,
    kind: Annotated[
        str, Field(description="'faces', or 'edges' for every edge touching a matching face")
    ] = "faces",
    between: Annotated[
        list[str] | None,
        Field(description="Two face patterns, to select the edges that separate them"),
    ] = None,
    body: Annotated[
        str | None,
        Field(
            description=(
                "Resolve within one body only — the question a feature asks. "
                "Omit it to search the whole document."
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """Ask what a selector would match — without committing it to the document.

    The tool to reach for before `add_feature` or `update_feature`. A selector
    written into a document is re-resolved on every rebuild and a wrong one
    fails the build, so the cheap move is always to check it here first, look at
    `matched`, and only then write it in.

    Two answers, and they are different questions:

    * without `body`, the whole document — "does this face exist, and where".
      `bodies` says which body each match came from.
    * with `body`, that body alone — "will the feature I am about to write see
      this". A feature resolves only within the body it is declared in and can
      never name a face another body made, so this is the one that predicts a
      build.

    `ok` is false when nothing matched, and `error` then says why rather than
    leaving you to guess: that the face was retired and by which feature, that
    it exists but on another body, or which existing tags come closest. Nothing
    is changed either way.
    """
    payload: dict[str, Any] = {"kind": kind}
    if between is not None:
        payload["between"] = between
    if selector is not None:
        payload["selector"] = selector
    if body is not None:
        payload["body"] = body

    notes: list[str] = []
    answer = client().json("POST", _project(project) + "/resolve", json=payload)
    matched = [str(tag) for tag in _rows(answer.get("matched"))]
    result: dict[str, Any] = {
        "selector": answer.get("selector"),
        "ok": answer.get("ok"),
        "count": answer.get("count", len(matched)),
        "matched": _cap(matched, "matched", notes),
        "error": answer.get("error"),
    }
    bodies = [
        {"id": entry.get("id"), "count": entry.get("count"), "matched": entry.get("matched")}
        for entry in _rows(answer.get("bodies"))
        if isinstance(entry, Mapping)
    ]
    # Only where it says something: on a single-body document the attribution is
    # the same word repeated, and the flat list is the whole answer.
    if len(bodies) > 1:
        result["bodies"] = bodies
    if answer.get("body"):
        result["body"] = answer["body"]
    if answer.get("note"):
        result["note"] = answer["note"]
    return _with_notes(result, notes)


@server.tool()
def datum_for_face(
    project: Project,
    tag: Annotated[str, Field(description="A face tag, e.g. 'base/cap+'")],
    point: Annotated[
        list[float] | None,
        Field(
            description=(
                "A world point on that face; comes back as 'at', its coordinates "
                "on the derived plane"
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """Derive a sketch plane from a face you can already name.

    The bridge between "I can see the face I want to build on" and "a sketch may
    only attach to a datum". The plane is read out of the feature history rather
    than measured off the solid, so the offset comes back as the *expression*
    the feature was written with — a datum placed from this answer keeps
    following the parameter sheet instead of freezing today's number.

    A face whose plane cannot be read that way answers `ok: false` with the
    reason. That is an answer, not a failure: place that one yourself with
    `put_datum`. `existing` names a datum already on the plane, when there is
    one, so a document does not accumulate near-duplicates.
    """
    return dict(
        client().json(
            "POST", _project(project) + "/datums/for-face", json={"tag": tag, "point": point}
        )
    )


@server.tool()
def locate_point(
    project: Project,
    point: Annotated[list[float], Field(description="A world point, [x, y, z]")],
) -> dict[str, Any]:
    """Express a world point in every datum's plane, nearest plane first.

    Turns a coordinate into the two in-plane numbers a sketch can hold, so you
    do not have to work out where a point lands on a rotated datum.

    Each row also carries `offsetParameter`: the parameter that already resolves
    to that offset, if one does. Use it. Declaring a datum at the offset you
    were shown bakes today's number into the document; declaring it at the
    parameter keeps it derived.
    """
    notes: list[str] = []
    body = client().json("POST", _project(project) + "/locate", json={"point": point})
    datums = _rows(body.get("datums"))
    return _with_notes({"datums": _cap(datums, "datums", notes)}, notes)


# --------------------------------------------------------------------------
# Getting geometry out
#
# Nothing here returns file content that is not meant to be read. A binary STL
# is megabytes of triangles that answer no question an agent can ask, and a DXF
# is a list of coordinates; both would evict the actual conversation from the
# context window to no purpose. What comes back is the byte count, whatever the
# export reported about itself, and the URL — which a shell, a browser or the
# user can fetch directly.
# --------------------------------------------------------------------------


def _download(
    path: str,
    params: Mapping[str, Any],
    *,
    what: str,
    headers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    api = client()
    response = api.request("GET", path, params=params)
    payload: dict[str, Any] = {
        "summary": f"{what}, {len(response.content)} bytes",
        "bytes": len(response.content),
        "mediaType": response.headers.get("content-type", ""),
        "url": api.url_for(path, params),
    }
    for key, label in (headers or {}).items():
        value = response.headers.get(key)
        if value is not None:
            payload[label] = value
    return payload


@server.tool()
def export(
    project: Project,
    fmt: Annotated[
        str,
        Field(
            description=(
                "'stl', 'stl-ascii' or 'obj' for a mesh; 'step' for B-rep CAD; "
                "'csv' or 'json' for the parameter sheet; 'yaml' for the document; "
                "'topology' for the named geometry"
            )
        ),
    ] = "stl",
    body: Annotated[
        str | None,
        Field(
            description=(
                "Export one body on its own, by id. Omit it and a mesh holds "
                "every body at its placement — right for viewing the assembly, "
                "wrong for a print bed. STEP holds a single solid and so "
                "requires this once a document has more than one body."
            )
        ),
    ] = None,
) -> dict[str, Any]:
    """Export the built model as a file.

    The sheet formats (`csv`, `json`, `yaml`, `topology`) come back inline,
    because they are text worth reading. Meshes and STEP come back as a byte
    count and a URL to fetch — an STL is triangles, and putting them in your
    context helps nobody.

    For a multi-part model, export each body separately with `body=`: the parts
    go on the bed one at a time, and a single file holding both at their
    placements is not what a slicer wants.

    A model that does not build cannot be exported, and says so rather than
    exporting the last good version under the current name. `step` additionally
    needs a kernel declaring BREP_EXPORT; ask `kernel_info` first.
    """
    api = client()
    path = _project(project) + "/export"
    params: dict[str, Any] = {"fmt": fmt}
    if body is not None:
        params["body"] = body

    if fmt not in TEXT_FORMATS:
        return _download(path, params, what=f"{fmt} export of {project}")

    response = api.request("GET", path, params=params)
    text = response.text
    payload: dict[str, Any] = {
        "format": fmt,
        "bytes": len(response.content),
        "url": api.url_for(path, params),
    }
    if len(text) > MAX_TEXT:
        payload["content"] = text[:MAX_TEXT]
        payload["truncated"] = [f"content: showing {MAX_TEXT} of {len(text)} characters"]
    else:
        payload["content"] = text
    return payload


@server.tool()
def export_cut_path(
    project: Project,
    selector: Annotated[str, Field(description="Face selector, e.g. 'lid/cap+' or 'panel/*'")],
    fmt: Annotated[str, Field(description="'dxf' or 'svg'")] = "dxf",
    body: Annotated[str | None, Field(description="Restrict to one body")] = None,
) -> dict[str, Any]:
    """Flatten the faces a selector matches into the path a laser or router would cut.

    Selector-driven rather than pick-driven on purpose: the same request keeps
    producing the right file as the model changes, which is the entire reason
    the naming engine exists. Check the selector with `resolve_selector` first —
    matching no face is an error here, not an empty file.
    """
    return _download(
        _project(project) + "/export/cut",
        {"selector": selector, "fmt": fmt, "body": body},
        what=f"cut path for {selector!r}",
    )


@server.tool()
def export_views(
    project: Project,
    fmt: Annotated[str, Field(description="'dxf' or 'svg'")] = "dxf",
    views: Annotated[
        str, Field(description="Comma separated, e.g. 'top,front,right'")
    ] = "top",
    body: Annotated[str | None, Field(description="Which body to draw")] = None,
) -> dict[str, Any]:
    """Orthographic projections of a body, for a setup or shop drawing.

    Needs a kernel declaring DRAWING_EXPORT — hidden-line removal is real
    geometry, not a projection of the mesh.
    """
    return _download(
        _project(project) + "/export/views",
        {"fmt": fmt, "views": views, "body": body},
        what=f"{views} views of {project}",
    )


@server.tool()
def export_faces(
    project: Project,
    fmt: Annotated[str, Field(description="'svg' or 'dxf'")] = "svg",
    body: Annotated[str | None, Field(description="Restrict to one body")] = None,
    blends: Annotated[bool, Field(description="Include fillet and chamfer faces")] = False,
) -> dict[str, Any]:
    """Every planar face of the part, flattened and laid out as a cutting list.

    The part itself made out of sheet, as opposed to `export_enclosure`, which
    is a box to put it in. Curved faces have no flat development and are
    reported in `skipped` rather than dropped, so a cutting list that does not
    add up can be seen not to add up.
    """
    return _download(
        _project(project) + "/export/flat",
        {"fmt": fmt, "body": body, "blends": blends},
        what=f"flattened faces of {project}",
        headers={"X-Faces-Flattened": "flattened", "X-Faces-Skipped": "skipped"},
    )


@server.tool()
def export_jointed(
    project: Project,
    thickness: Annotated[float, Field(description="Sheet thickness in mm")] = 3.0,
    finger: Annotated[float, Field(description="Tooth width in mm")] = 10.0,
    kerf: Annotated[float, Field(description="Cut width the machine removes, in mm")] = 0.15,
    fmt: Annotated[str, Field(description="'svg' or 'dxf'")] = "svg",
    body: Annotated[str | None, Field(description="Restrict to one body")] = None,
    teeth: Annotated[
        int | None,
        Field(
            description=(
                "Fixed tooth count per edge — odd, at least 3. Use when face "
                "sizes vary widely."
            )
        ),
    ] = None,
    depth: Annotated[
        float | None, Field(description="Recess depth; the thickness when omitted")
    ] = None,
    finger_for: Annotated[
        str | None,
        Field(description="Per-face tooth widths as 'tag:width' pairs separated by semicolons"),
    ] = None,
    fit: Annotated[
        str,
        Field(
            description=(
                "'outer' if the modelled solid is the outside of the assembly, "
                "'inner' if it is the cavity"
            )
        ),
    ] = "outer",
) -> dict[str, Any]:
    """The part's own faces with finger joints cut into the edges they share.

    Between `export_faces`, which gives plain panels you still have to join, and
    `export_enclosure`, which gives a rectangular box that ignores the part's
    shape.

    `fit` is the one to get right: outer and inner differ by a full thickness at
    every joint, so the wrong choice produces an assembly that is out by twice
    the sheet in each direction. Panels that ended up with no joint are listed
    in `plain`.
    """
    return _download(
        _project(project) + "/export/jointed",
        {
            "thickness": thickness,
            "finger": finger,
            "kerf": kerf,
            "fmt": fmt,
            "body": body,
            "teeth": teeth,
            "depth": depth,
            "finger_for": finger_for,
            "fit": fit,
        },
        what=f"jointed panels of {project}",
        headers={"X-Joints-Cut": "joints", "X-Panels-Plain": "plain"},
    )


@server.tool()
def export_enclosure(
    project: Project,
    thickness: Annotated[float, Field(description="Sheet thickness in mm")] = 3.0,
    finger: Annotated[float, Field(description="Target finger width in mm")] = 10.0,
    kerf: Annotated[float, Field(description="Cut width the machine removes, in mm")] = 0.15,
    clearance: Annotated[float, Field(description="Space left around the part, in mm")] = 2.0,
    fmt: Annotated[str, Field(description="'svg' or 'dxf'")] = "svg",
    body: Annotated[str | None, Field(description="Size the box around one body only")] = None,
) -> dict[str, Any]:
    """Six interlocking flat panels for a laser-cut box the part fits inside.

    Sized from the model's own bounding box, so the box tracks the part: widen
    the bracket and the next export is a wider box, with no second set of
    dimensions to keep in step by hand.
    """
    return _download(
        _project(project) + "/export/enclosure",
        {
            "thickness": thickness,
            "finger": finger,
            "kerf": kerf,
            "clearance": clearance,
            "fmt": fmt,
            "body": body,
        },
        what=f"enclosure for {project}",
    )
