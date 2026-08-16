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
        timeout: float = 60.0,
    ) -> None:
        self.base_url = (base_url or os.environ.get("FACET_URL", DEFAULT_URL)).rstrip("/")
        self._http = httpx.Client(base_url=self.base_url, transport=transport, timeout=timeout)

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
    return _detail_text(detail) or (
        f"the API refused the request with status {response.status_code}"
    )


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
    """
    notes: list[str] = []

    features: list[dict[str, Any]] = []
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
        features.append(row)

    bodies = [
        {"id": body.get("id"), "ok": body.get("ok"), "faceCount": body.get("faceCount")}
        for body in _rows(result.get("bodies"))
        if isinstance(body, Mapping)
    ]

    parameters = result.get("parameters")
    if isinstance(parameters, Mapping):
        names = _cap(sorted(parameters), "parameters", notes)
        resolved = {name: parameters[name] for name in names}
    else:
        resolved = {}

    report: dict[str, Any] = {
        "ok": bool(result.get("ok")),
        "features": features,
        "failures": [f for f in features if "error" in f],
        "parameters": resolved,
        "bodies": _cap(bodies, "bodies", notes),
    }
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
    """The feature types `add_feature` will accept.

    Read from the live handler registry rather than from documentation, so it
    cannot drift from what the server actually builds.
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


# --------------------------------------------------------------------------
# Understanding the model
# --------------------------------------------------------------------------


@server.tool()
def recompute(project: Project) -> dict[str, Any]:
    """Rebuild the model and report what each feature did.

    Use it to check the state of a project you did not just edit, or after
    editing the document by some other route. The editing tools already return
    this same report, so calling it straight after one of them tells you
    nothing new.

    A feature that fails halts the chain: the ones after it come back *skipped*,
    `lastGoodFeature` says how far the build got, and the last good solid is
    kept — so you can see the part as far as it built, with the culprit named.
    """
    return _build_report(client().json("POST", _project(project) + "/recompute"))


@server.tool()
def topology(project: Project) -> dict[str, Any]:
    """Every face and edge tag the model currently has — the vocabulary of selectors.

    Read this before writing any selector. A tag is a provenance path, not an
    index: `base/side[outline.left]` is the face swept from curve `left` of
    sketch `outline` by feature `base`, and it keeps that name across parameter
    changes.

    `retired` lists tags that existed in an earlier state and no longer do, with
    the reason and the feature that consumed them. When a selector has just
    stopped resolving, that list usually contains the answer.

    Only the tags come back, never the geometric fingerprints behind them: the
    tags are what you select with, and the fingerprints are large, numeric and
    of no use to a caller.
    """
    notes: list[str] = []
    body = client().json("GET", _project(project) + "/topology")

    faces = [str(entry.get("tag")) for entry in _rows(body.get("faces"))]
    edges = [str(entry.get("tag")) for entry in _rows(body.get("edges"))]
    retired = [
        {
            "tag": str(entry.get("tag")),
            "reason": entry.get("reason"),
            "retiredBy": entry.get("retired_by"),
        }
        for entry in _rows(body.get("retired"))
    ]

    return _with_notes(
        {
            "faces": _cap(faces, "faces", notes),
            "edges": _cap(edges, "edges", notes),
            "retired": _cap(retired, "retired", notes),
            "counts": {"faces": len(faces), "edges": len(edges), "retired": len(retired)},
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
) -> dict[str, Any]:
    """Ask what a selector would match — without committing it to the document.

    The tool to reach for before `add_feature` or `update_feature`. A selector
    written into a document is re-resolved on every rebuild and a wrong one
    fails the build, so the cheap move is always to check it here first, look at
    `matched`, and only then write it in.

    `ok` is false when the selector matched nothing, with `error` explaining
    why. Nothing is changed either way.
    """
    payload: dict[str, Any] = {"kind": kind}
    if between is not None:
        payload["between"] = between
    if selector is not None:
        payload["selector"] = selector

    notes: list[str] = []
    body = client().json("POST", _project(project) + "/resolve", json=payload)
    matched = [str(tag) for tag in _rows(body.get("matched"))]
    return _with_notes(
        {
            "selector": body.get("selector"),
            "ok": body.get("ok"),
            "count": body.get("count", len(matched)),
            "matched": _cap(matched, "matched", notes),
            "error": body.get("error"),
        },
        notes,
    )


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
