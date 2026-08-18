"""FastAPI driving adapter.

Thin by design. Every route translates HTTP into a :class:`ProjectService` call
and back; no geometry, no naming and no validation logic lives here. The web UI
uses only these endpoints, which is what keeps an MCP server a wrapper rather
than a parallel implementation.

Two endpoints exist specifically to make the system usable by an agent:
``/topology`` lists every current tag, and ``/resolve`` answers "what would this
selector match?" before anything is committed to the document.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from facet.adapters.export import drawing
from facet.adapters.export import mesh as exporters
from facet.adapters.export.sheet_import import parameters_from_csv
from facet.adapters.http.guide import guide_markdown
from facet.adapters.persistence.filesystem import document_from_yaml, yaml_text
from facet.application.ports.repository import ProjectExists, ProjectNotFound
from facet.application.recompute import Detail
from facet.application.services import ProjectService

# Aliased: FastAPI exports a `Body` of its own for request payloads.
from facet.domain.body import Body as BodySpec
from facet.domain.body import Placement
from facet.domain.datum import DatumPlane
from facet.domain.document import Document
from facet.domain.errors import FacetCADError
from facet.domain.features import FeatureSpec
from facet.domain.parameters import Parameter
from facet.domain.sketch import Sketch

router = APIRouter(prefix="/api")

#: Module-level singleton so the dependency is not constructed per call.
_BODY = Body(...)

#: Set by the composition root in ``facet.main``.
_service: ProjectService | None = None


def configure(service: ProjectService) -> None:
    global _service
    _service = service


def service() -> ProjectService:
    if _service is None:  # pragma: no cover - configuration bug
        raise RuntimeError("the API was not configured with a ProjectService")
    return _service


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------


class CreateProject(BaseModel):
    id: str = Field(description="URL-safe project identifier")
    name: str = ""
    document: dict[str, Any] | None = Field(
        default=None, description="Optional initial document; a blank one is used if omitted"
    )


class ParameterChanges(BaseModel):
    changes: dict[str, float | str] = Field(
        description="Parameter name to new literal value, or to a new expression string"
    )


class ParameterPayload(BaseModel):
    name: str
    value: float | None = None
    expr: str | None = None
    unit: str = "mm"
    group: str = ""
    doc: str = ""


class ParameterEdit(BaseModel):
    """Any subset of a parameter's row. Changing `name` renames it everywhere."""

    name: str | None = None
    value: float | None = None
    expr: str | None = None
    unit: str | None = None
    group: str | None = None
    doc: str | None = None


class SketchPayload(BaseModel):
    id: str
    plane: str = "xy"
    points: dict[str, list[Any]] = Field(default_factory=dict)
    curves: list[dict[str, Any]] = Field(default_factory=list)
    loops: list[dict[str, Any]] = Field(default_factory=list)


class DatumPayload(BaseModel):
    id: str
    origin: list[Any] = Field(default_factory=lambda: [0, 0, 0])
    normal: list[Any] = Field(default_factory=lambda: [0, 0, 1])
    x_axis: list[Any] | None = None
    parent: str | None = None


class FeaturePayload(BaseModel):
    spec: dict[str, Any]
    at: int | None = Field(default=None, description="Insertion index; appends when omitted")
    body: str | None = Field(default=None, description="Target body; the first when omitted")


class BodyPayload(BaseModel):
    id: str
    origin: list[Any] = Field(default_factory=lambda: [0, 0, 0])
    rotation: list[Any] = Field(default_factory=lambda: [0, 0, 0])


class CopyPayload(BaseModel):
    """Where a copy of a body goes, and optionally what to call it."""

    id: str | None = Field(
        default=None,
        description="Id for the copy; generated from the source's name when omitted",
    )
    origin: list[Any] | None = Field(
        default=None,
        description=(
            "[x, y, z]; expressions allowed. Defaults to the source's own "
            "placement, which puts the copy exactly on top of it — visible "
            "in the tree, and asking to be moved."
        ),
    )
    rotation: list[Any] | None = Field(default=None, description="[rx, ry, rz] in degrees")


class FaceTagPayload(BaseModel):
    tag: str = Field(description="A face tag, such as 'pad_1/cap+'")
    point: list[float] | None = Field(
        default=None,
        description=(
            "A world point on that face, [x, y, z]. Comes back as 'at', its "
            "coordinates on the derived plane — which are not the same as its "
            "coordinates on the parent unless the two happen to be parallel."
        ),
    )


class ReorderPayload(BaseModel):
    order: list[str]


class ImportPayload(BaseModel):
    format: str = Field(default="csv", description="Currently only 'csv'")
    body: str = Field(description="The file contents")


class ResolvePayload(BaseModel):
    selector: str | None = Field(default=None, description="Face selector shorthand")
    kind: str = Field(default="faces", description="'faces' or 'edges'")
    between: list[str] | None = Field(
        default=None, description="Two face patterns, for edges between them"
    )
    body: str | None = Field(
        default=None,
        description=(
            "Resolve within one body only — the question a feature asks, since "
            "it can never name a face another body made. Omit it to search the "
            "whole document."
        ),
    )


class DocumentPayload(BaseModel):
    document: dict[str, Any] | None = None
    yaml: str | None = None


# --------------------------------------------------------------------------
# Error translation — domain errors keep their structure across the wire
# --------------------------------------------------------------------------


def _fail(error: Exception) -> HTTPException:
    # Imported here: the guarded kernel is optional, and the API must still
    # start when geometry runs in-process.
    from facet.adapters.geometry.guarded import KernelBusy, KernelRestarted, KernelTimeout

    if isinstance(error, KernelBusy):
        # 503 with Retry-After: the request never ran, and will probably work in
        # a moment. Saying so is what stops a client retrying immediately and
        # making the queue it is waiting on longer.
        return HTTPException(
            status_code=503,
            detail={"message": str(error)},
            headers={"Retry-After": "5"},
        )
    if isinstance(error, KernelTimeout):
        # 503 rather than 500: the server is fine and the request may well
        # succeed on a simpler model, which is what Retry-After-less 503 means.
        return HTTPException(status_code=503, detail={"message": str(error)})
    if isinstance(error, KernelRestarted):
        # 409: the state the request was written against is gone. Repeating it
        # verbatim works, because a rebuild starts from the document.
        return HTTPException(status_code=409, detail={"message": str(error)})
    if isinstance(error, ProjectNotFound):
        return HTTPException(status_code=404, detail={"message": str(error)})
    if isinstance(error, ProjectExists):
        return HTTPException(status_code=409, detail={"message": str(error)})
    if isinstance(error, FacetCADError):
        return HTTPException(status_code=422, detail=error.as_dict())
    return HTTPException(status_code=500, detail={"message": str(error)})


# --------------------------------------------------------------------------
# Meta
# --------------------------------------------------------------------------


@router.get(
    "/mcp",
    summary="How to drive this system, written for an agent",
    response_class=PlainTextResponse,
)
def agent_guide() -> str:
    """A single page an agent can read to become useful immediately.

    Deliberately not the OpenAPI schema. A schema lists fields; this says which
    of them will bite you — that a selector is re-resolved on every rebuild,
    that a pocket cuts from its own sketch plane, that deriving a dimension is
    what makes a model survive an edit. Those are the facts that go wrong.
    """
    return guide_markdown()


@router.get("/health", summary="Liveness probe")
def health() -> dict[str, object]:
    return {"status": "ok"}


@router.get("/kernel", summary="Which geometry kernel is active and what it supports")
def kernel() -> dict[str, object]:
    return service().kernel_info().to_dict()


@router.get("/expressions", summary="What an expression may refer to")
def expression_vocabulary() -> dict[str, object]:
    """Function and constant names the expression language knows.

    Exposed so a client can tell an undefined *parameter* from a legitimate
    function call without keeping its own copy of the list, which would drift.
    """
    from facet.domain.expressions import CONSTANTS, FUNCTIONS

    return {"functions": sorted(FUNCTIONS), "constants": sorted(CONSTANTS)}


@router.get("/feature-types", summary="Registered feature types and their options")
def feature_types() -> dict[str, object]:
    from facet.application.features import describe_types, registered_types

    # 'types' stays a plain list of names for anything already reading it;
    # 'features' carries the options each one takes.
    return {"types": list(registered_types()), "features": list(describe_types())}


# --------------------------------------------------------------------------
# Projects
# --------------------------------------------------------------------------


@router.get("/projects", summary="List projects")
def list_projects() -> dict[str, object]:
    return {"projects": [s.to_dict() for s in service().list_projects()]}


@router.post("/projects", status_code=201, summary="Create a project")
def create_project(payload: CreateProject) -> dict[str, object]:
    try:
        # Inside the try: reading the document can refuse it — a body that is
        # both a copy and a history, a `features` that is not a list — and
        # outside, that refusal escaped as an unhandled 500 rather than as the
        # message it carries.
        document = (
            Document.from_dict(payload.document)
            if payload.document is not None
            else Document(name=payload.name or payload.id)
        )
        if payload.name:
            document.name = payload.name
        return service().create_project(payload.id, document).to_dict()
    except Exception as error:
        raise _fail(error) from error


@router.delete("/projects/{project_id}", status_code=204, summary="Delete a project")
def delete_project(project_id: str) -> Response:
    try:
        service().delete_project(project_id)
    except Exception as error:
        raise _fail(error) from error
    return Response(status_code=204)


@router.get("/projects/{project_id}/document", summary="Read the whole document")
def get_document(project_id: str, fmt: str = Query(default="json", pattern="^(json|yaml)$")):
    try:
        document = service().load(project_id)
    except Exception as error:
        raise _fail(error) from error
    if fmt == "yaml":
        return Response(content=yaml_text(document), media_type="application/x-yaml")
    return document.to_dict()


@router.put("/projects/{project_id}/document", summary="Replace the whole document")
def put_document(project_id: str, payload: DocumentPayload) -> dict[str, object]:
    try:
        if payload.yaml is not None:
            document = document_from_yaml(payload.yaml)
        elif payload.document is not None:
            document = Document.from_dict(payload.document)
        else:
            raise HTTPException(
                status_code=400, detail={"message": "provide either 'document' or 'yaml'"}
            )
        return service().replace_document(project_id, document).to_dict()
    except HTTPException:
        raise
    except Exception as error:
        raise _fail(error) from error


# --------------------------------------------------------------------------
# Editing
# --------------------------------------------------------------------------


@router.patch("/projects/{project_id}/parameters", summary="Change parameter values")
def patch_parameters(project_id: str, payload: ParameterChanges) -> dict[str, object]:
    try:
        return service().update_parameters(project_id, payload.changes).to_dict()
    except Exception as error:
        raise _fail(error) from error


@router.post("/projects/{project_id}/parameters", summary="Add a parameter")
def add_parameter(project_id: str, payload: ParameterPayload) -> dict[str, object]:
    try:
        parameter = Parameter(
            name=payload.name,
            value=payload.value,
            expr=payload.expr,
            unit=payload.unit,
            group=payload.group,
            doc=payload.doc,
        )
        return service().add_parameter(project_id, parameter).to_dict()
    except Exception as error:
        raise _fail(error) from error


@router.patch("/projects/{project_id}/parameters/{name}", summary="Edit a parameter row")
def edit_parameter(project_id: str, name: str, payload: ParameterEdit) -> dict[str, object]:
    """Change any part of a parameter, including its name.

    A rename is followed through every expression in the document, so nothing is
    left pointing at a name that no longer exists.
    """
    try:
        changes = payload.model_dump(exclude_none=True)
        return service().edit_parameter(project_id, name, changes).to_dict()
    except Exception as error:
        raise _fail(error) from error


@router.get("/projects/{project_id}/parameters/{name}/usage", summary="What reads a parameter")
def parameter_usage(project_id: str, name: str) -> dict[str, object]:
    try:
        return {"name": name, "usedBy": service().parameter_usage(project_id, name)}
    except Exception as error:
        raise _fail(error) from error


@router.delete("/projects/{project_id}/parameters/{name}", summary="Delete a parameter")
def delete_parameter(project_id: str, name: str) -> dict[str, object]:
    try:
        return service().delete_parameter(project_id, name).to_dict()
    except Exception as error:
        raise _fail(error) from error


# --------------------------------------------------------------------------
# Sketches and datums
# --------------------------------------------------------------------------


@router.put("/projects/{project_id}/sketches/{sketch_id}", summary="Create or replace a sketch")
def put_sketch(project_id: str, sketch_id: str, payload: SketchPayload) -> dict[str, object]:
    try:
        sketch = Sketch.from_dict(
            sketch_id,
            {
                "plane": payload.plane,
                "points": payload.points,
                "curves": payload.curves,
                "loops": payload.loops,
            },
        )
        return service().put_sketch(project_id, sketch).to_dict()
    except Exception as error:
        raise _fail(error) from error


@router.delete("/projects/{project_id}/sketches/{sketch_id}", summary="Delete a sketch")
def delete_sketch(project_id: str, sketch_id: str) -> dict[str, object]:
    try:
        return service().delete_sketch(project_id, sketch_id).to_dict()
    except Exception as error:
        raise _fail(error) from error


@router.put("/projects/{project_id}/datums/{datum_id}", summary="Create or replace a datum")
def put_datum(project_id: str, datum_id: str, payload: DatumPayload) -> dict[str, object]:
    try:
        plane = DatumPlane.from_dict(
            datum_id,
            {
                "type": "plane",
                "origin": payload.origin,
                "normal": payload.normal,
                "x_axis": payload.x_axis,
                "parent": payload.parent,
            },
        )
        return service().put_datum(project_id, plane).to_dict()
    except Exception as error:
        raise _fail(error) from error


@router.delete("/projects/{project_id}/datums/{datum_id}", summary="Delete a datum")
def delete_datum(project_id: str, datum_id: str) -> dict[str, object]:
    try:
        return service().delete_datum(project_id, datum_id).to_dict()
    except Exception as error:
        raise _fail(error) from error


@router.post(
    "/projects/{project_id}/datums/for-face",
    summary="Propose a datum on the plane of a named face",
)
def datum_for_face(project_id: str, payload: FaceTagPayload) -> dict[str, object]:
    """Answer 'where is that face?' with an expression rather than a number.

    The plane is read out of the history — the sketch's datum, offset by the
    feature's own length or depth as written — so the datum in the answer is
    ready to PUT and still follows the parameter sheet afterwards. ``existing``
    names a datum already on that plane, when there is one, so a document does
    not collect near-duplicates.

    A face whose plane cannot be read comes back with ``ok: false`` and the
    reason, still as 200: 'this one you must place yourself' is an answer, not a
    failure.
    """
    point = payload.point
    if point is not None and len(point) != 3:
        raise HTTPException(
            status_code=400, detail={"message": "point must be [x, y, z]"}
        )
    try:
        located = (
            (float(point[0]), float(point[1]), float(point[2]))
            if point is not None
            else None
        )
        return service().datum_for_face(project_id, payload.tag, located).to_dict()
    except Exception as error:
        raise _fail(error) from error


@router.post("/projects/{project_id}/features", summary="Add a feature")
def add_feature(project_id: str, payload: FeaturePayload) -> dict[str, object]:
    try:
        spec = FeatureSpec.from_dict(payload.spec)
        return service().add_feature(project_id, spec, payload.at, payload.body).to_dict()
    except Exception as error:
        raise _fail(error) from error


@router.patch("/projects/{project_id}/features/{feature_id}", summary="Update a feature")
def update_feature(
    project_id: str, feature_id: str, spec: dict[str, Any] = _BODY
) -> dict[str, object]:
    try:
        merged = FeatureSpec.from_dict({**spec, "id": feature_id})
        return service().update_feature(project_id, merged).to_dict()
    except Exception as error:
        raise _fail(error) from error


@router.delete("/projects/{project_id}/features/{feature_id}", summary="Delete a feature")
def delete_feature(project_id: str, feature_id: str) -> dict[str, object]:
    try:
        return service().delete_feature(project_id, feature_id).to_dict()
    except Exception as error:
        raise _fail(error) from error


@router.post("/projects/{project_id}/features/reorder", summary="Reorder the history")
def reorder_features(project_id: str, payload: ReorderPayload) -> dict[str, object]:
    try:
        return service().reorder_features(project_id, payload.order).to_dict()
    except Exception as error:
        raise _fail(error) from error


# --------------------------------------------------------------------------
# Rebuild, inspect, export
# --------------------------------------------------------------------------


@router.post("/projects/{project_id}/recompute", summary="Rebuild and report per feature")
def recompute(
    project_id: str,
    force: bool = Query(
        default=False,
        description=(
            "Discard every cached feature first, so the whole history is rebuilt "
            "from scratch. The cache is keyed on content and should never be "
            "wrong, so this is a way out of a state that should not happen "
            "rather than part of normal use."
        ),
    ),
) -> dict[str, object]:
    try:
        api = service()
        if force:
            api.invalidate_caches()
        return api.recompute(project_id).to_dict()
    except Exception as error:
        raise _fail(error) from error


@router.get("/projects/{project_id}/topology", summary="Every current face and edge tag")
def topology(
    project_id: str,
    body: str | None = Query(
        default=None,
        description=(
            "Narrow to one body. Without it every body is listed, each tag "
            "carrying the body that made it."
        ),
    ),
) -> dict[str, object]:
    """Every tag the model currently has, across every body.

    It answered for the first body until the document grew a second one, at
    which point half an assembly had no faces as far as any caller could see.
    Each entry now names its body; ``/topologies`` is the same information
    grouped, for a client drawing a tree.
    """
    try:
        return service().topology_payload(project_id, body)
    except Exception as error:
        raise _fail(error) from error


class PointPayload(BaseModel):
    point: list[float]


@router.post(
    "/projects/{project_id}/locate",
    summary="Express a world point in each datum's plane",
)
def locate(project_id: str, payload: PointPayload) -> dict[str, object]:
    """Turn a click in the viewport into coordinates a sketch can hold.

    Nothing about the clicked face is recorded: a datum is computed from
    parameters alone, so this only saves typing two numbers.

    Each row names the parameter that already resolves to its offset, so a
    datum declared from the answer can be written against that parameter and
    keeps following the model instead of freezing today's number.
    """
    if len(payload.point) != 3:
        raise HTTPException(
            status_code=400, detail={"message": "point must be [x, y, z]"}
        )
    try:
        x, y, z = (float(v) for v in payload.point)
        return {"datums": service().locate(project_id, (x, y, z))}
    except HTTPException:
        raise
    except Exception as error:
        raise _fail(error) from error


@router.get(
    "/projects/{project_id}/sketches/geometry",
    summary="Sketch curves and points as world-space polylines",
)
def sketch_geometry(project_id: str) -> dict[str, object]:
    """Drawable sketch geometry, available even when the model does not build."""
    try:
        return service().sketch_geometry(project_id)
    except Exception as error:
        raise _fail(error) from error


@router.get(
    "/projects/{project_id}/state",
    summary="Document, bodies, topologies and sketches in one response",
)
def view_state(project_id: str) -> dict[str, object]:
    """Everything needed to draw the project, from a single rebuild.

    The four endpoints this replaces are still here and still work. This one
    exists because a client that needs all of them was making four requests
    where three re-entered the recompute engine and all four re-read the
    document off disk — and after an edit, five, since the mutation had already
    rebuilt and had its answer discarded.
    """
    try:
        return service().view_state(project_id)
    except Exception as error:
        raise _fail(error) from error


@router.get("/projects/{project_id}/bodies", summary="Every body, tessellated")
def body_meshes(project_id: str) -> dict[str, object]:
    """Per-body geometry with placements, for drawing an assembly."""
    try:
        meshes, result = service().body_meshes(project_id)
    except Exception as error:
        raise _fail(error) from error
    return {"bodies": meshes, "build": result.to_dict()}


@router.post("/projects/{project_id}/bodies", summary="Add a body")
def add_body(project_id: str, payload: BodyPayload) -> dict[str, object]:
    try:
        body = BodySpec(
            id=payload.id,
            placement=Placement(
                origin=tuple(payload.origin),  # type: ignore[arg-type]
                rotation=tuple(payload.rotation),  # type: ignore[arg-type]
            ),
        )
        return service().add_body(project_id, body).to_dict()
    except Exception as error:
        raise _fail(error) from error


@router.post(
    "/projects/{project_id}/bodies/{body_id}/copies",
    summary="Show a body again at another placement",
)
def duplicate_body(
    project_id: str, body_id: str, payload: CopyPayload
) -> dict[str, object]:
    """Add a copy of a body: the same solid, elsewhere, built only once.

    The copy holds no features. Editing the source edits every copy, and the
    document records how many of the part it calls for — the piece count a
    copy-pasted history cannot give you.
    """
    try:
        placement = (
            Placement(
                origin=tuple(payload.origin or [0, 0, 0]),
                rotation=tuple(payload.rotation or [0, 0, 0]),
            )
            if payload.origin is not None or payload.rotation is not None
            else None
        )
        identifier, result = service().duplicate_body(
            project_id, body_id, payload.id, placement
        )
    except Exception as error:
        raise _fail(error) from error
    return {"id": identifier, **result.to_dict()}


@router.patch("/projects/{project_id}/bodies/{body_id}", summary="Move a body")
def update_body(project_id: str, body_id: str, payload: BodyPayload) -> dict[str, object]:
    """Set a body's placement. Parameter expressions are accepted."""
    try:
        placement = Placement(
            origin=tuple(payload.origin),  # type: ignore[arg-type]
            rotation=tuple(payload.rotation),  # type: ignore[arg-type]
        )
        return service().update_body(project_id, body_id, placement).to_dict()
    except Exception as error:
        raise _fail(error) from error


@router.delete("/projects/{project_id}/bodies/{body_id}", summary="Delete a body")
def delete_body(project_id: str, body_id: str) -> dict[str, object]:
    try:
        return service().delete_body(project_id, body_id).to_dict()
    except Exception as error:
        raise _fail(error) from error


@router.get("/projects/{project_id}/topologies", summary="Named geometry per body")
def body_topologies(project_id: str) -> dict[str, object]:
    try:
        return service().body_topologies(project_id)
    except Exception as error:
        raise _fail(error) from error


@router.get("/projects/{project_id}/mesh", summary="Tessellation for the viewer")
def get_mesh(project_id: str) -> dict[str, object]:
    try:
        tessellation, result = service().mesh(project_id)
    except Exception as error:
        raise _fail(error) from error

    solid = result.solid
    tags = (
        {ref: str(tag) for ref, tag in solid.refs.items()} if solid is not None else {}
    )
    return {
        "positions": list(tessellation.positions),
        "normals": list(tessellation.normals),
        "indices": list(tessellation.indices),
        "faceRanges": [
            {"ref": r.ref, "tag": tags.get(r.ref, r.ref), "start": r.start, "count": r.count}
            for r in tessellation.face_ranges
        ],
        "edges": [{"ref": e.ref, "points": list(e.points)} for e in tessellation.edges],
        "build": result.to_dict(),
    }


@router.post("/projects/{project_id}/resolve", summary="Preview what a selector matches")
def resolve(project_id: str, payload: ResolvePayload) -> dict[str, object]:
    """What a selector matches now, per body, without writing anything.

    Searches the whole document and says which body each match came from. A
    feature resolves only within its own body, so ``body`` narrows this to the
    question a feature would ask — and an answer of nothing always says why,
    rather than leaving a correct tag looking like a typo.
    """
    try:
        if payload.between is not None and len(payload.between) == 2:
            preview = service().resolve_edge_selector(
                project_id, payload.between[0], payload.between[1], payload.body
            )
        elif payload.selector:
            preview = service().resolve_selector(
                project_id, payload.selector, payload.kind, payload.body
            )
        else:
            raise HTTPException(
                status_code=400, detail={"message": "provide 'selector' or 'between'"}
            )
        return preview.to_dict()
    except HTTPException:
        raise
    except Exception as error:
        raise _fail(error) from error


@router.post("/projects/{project_id}/import", summary="Import the parameter sheet")
def import_sheet(project_id: str, payload: ImportPayload) -> dict[str, object]:
    """Replace the parameter table from a spreadsheet export.

    Only parameters are touched; datums, sketches and the feature history are
    preserved, so a round trip through Excel cannot damage what a spreadsheet
    has no way to represent.
    """
    api = service()
    try:
        if payload.format != "csv":
            raise HTTPException(
                status_code=400,
                detail={"message": f"unsupported import format {payload.format!r}"},
            )
        updated = parameters_from_csv(api.load(project_id), payload.body)
        api.replace_document(project_id, updated)
        return api.recompute(project_id).to_dict()
    except HTTPException:
        raise
    except Exception as error:
        raise _fail(error) from error


@router.get("/projects/{project_id}/export", summary="Export mesh, sheet or topology")
def export(
    project_id: str,
    fmt: str = Query(default="stl"),
    body: str | None = Query(
        default=None,
        description=(
            "Export one body on its own — what printing a multi-part model "
            "needs, since the parts go on the bed separately. Omit it and every "
            "body is included."
        ),
    ),
) -> Response:
    api = service()
    try:
        document = api.load(project_id)
        if fmt in exporters.MESH_FORMATS:
            # A mesh is what reaches a slicer, so it is built at full detail:
            # a thread declared 'export' is cut here even though the viewport
            # skipped it.
            tessellation, result = api.mesh(project_id, detail=Detail.FULL, body=body)
            if result.solid is None:
                raise HTTPException(
                    status_code=422,
                    detail={"message": "the model does not build, so it cannot be exported"},
                )
            content = _mesh_bytes(fmt, tessellation, document.name)
            stem = f"{project_id}-{body}" if body else project_id
            return Response(
                content=content,
                media_type=exporters.MESH_FORMATS[fmt],
                headers={
                    "Content-Disposition": f'attachment; filename="{stem}.{_suffix(fmt)}"'
                },
            )

        if fmt in exporters.BREP_FORMATS:
            content = api.export_brep(project_id, fmt, body)
            stem = f"{project_id}-{body}" if body else project_id
            return Response(
                content=content,
                media_type=exporters.BREP_FORMATS[fmt],
                headers={
                    "Content-Disposition": f'attachment; filename="{stem}.{fmt}"'
                },
            )

        if fmt in exporters.SHEET_FORMATS:
            result = api.recompute(project_id)
            content = _sheet_bytes(fmt, document, result, api.topology_payload(project_id))
            return Response(
                content=content,
                media_type=exporters.SHEET_FORMATS[fmt],
                headers={
                    "Content-Disposition": f'attachment; filename="{project_id}.{_suffix(fmt)}"'
                },
            )
    except HTTPException:
        raise
    except Exception as error:
        raise _fail(error) from error

    raise HTTPException(
        status_code=400,
        detail={
            "message": f"unsupported format {fmt!r}",
            "supported": list(exporters.supported_formats()),
        },
    )


#: Media types for the 2D formats, keyed the same way the mesh table is.
DRAWING_MEDIA = {"dxf": "image/vnd.dxf", "svg": "image/svg+xml"}


@router.get(
    "/projects/{project_id}/export/cut",
    summary="The 2D cut path of the faces a selector resolves to",
)
def export_cut(
    project_id: str,
    selector: str = Query(description="Face selector, e.g. 'lid/cap+' or 'panel/*'"),
    fmt: str = Query(default="dxf"),
    body: str | None = Query(default=None, description="Restrict to one body"),
) -> Response:
    """Flatten faces into the path a laser, router or waterjet would cut.

    The selector is re-resolved against the current model on every call, so the
    same request keeps producing the right file as parameters change — which is
    the entire reason for the naming engine.
    """
    try:
        profiles = service().cut_paths(project_id, selector, body)
        content = drawing.export_drawing(profiles, fmt, title=selector)
    except Exception as error:
        raise _fail(error) from error
    return Response(
        content=content,
        media_type=DRAWING_MEDIA.get(fmt.lower(), "application/octet-stream"),
        headers={
            "Content-Disposition": f'attachment; filename="{project_id}-cut.{fmt.lower()}"'
        },
    )


@router.get(
    "/projects/{project_id}/export/views",
    summary="Orthographic sections, for a setup drawing",
)
def export_views(
    project_id: str,
    fmt: str = Query(default="dxf"),
    views: str = Query(default="top", description="Comma separated: top, front, right..."),
    body: str | None = Query(default=None),
) -> Response:
    wanted = [name.strip() for name in views.split(",") if name.strip()]
    try:
        content = service().export_views(project_id, fmt, wanted or ["top"], body)
    except Exception as error:
        raise _fail(error) from error
    return Response(
        content=content,
        media_type=DRAWING_MEDIA.get(fmt.lower(), "application/octet-stream"),
        headers={
            "Content-Disposition": f'attachment; filename="{project_id}-views.{fmt.lower()}"'
        },
    )


@router.get(
    "/projects/{project_id}/export/flat",
    summary="Every planar face of the part, flattened and laid out",
)
def export_flat(
    project_id: str,
    fmt: str = Query(default="svg"),
    body: str | None = Query(default=None),
    blends: bool = Query(
        default=False, description="Include fillet and chamfer faces"
    ),
) -> Response:
    """The part itself as a cutting list, rather than a box to put it in.

    Curved faces have no flat development and are reported in a header rather
    than dropped, so the list can be seen not to add up when it does not.
    """
    try:
        flattened = service().flat_faces(project_id, body, blends)
        content = drawing.export_drawing(
            list(flattened.panels), fmt, title=f"{project_id} faces"
        )
    except Exception as error:
        raise _fail(error) from error
    headers = {
        "Content-Disposition": f'attachment; filename="{project_id}-faces.{fmt.lower()}"',
        "X-Faces-Flattened": str(len(flattened.panels)),
    }
    if flattened.skipped:
        headers["X-Faces-Skipped"] = ", ".join(flattened.skipped)
    return Response(
        content=content,
        media_type=DRAWING_MEDIA.get(fmt.lower(), "application/octet-stream"),
        headers=headers,
    )


@router.get(
    "/projects/{project_id}/export/jointed",
    summary="The part's own faces, with finger joints on shared edges",
)
def export_jointed(
    project_id: str,
    thickness: float = Query(default=3.0, gt=0, description="Sheet thickness in mm"),
    finger: float = Query(default=10.0, gt=0, description="Tooth width in mm"),
    kerf: float = Query(default=0.15, ge=0),
    fmt: str = Query(default="svg"),
    body: str | None = Query(default=None),
    teeth: int | None = Query(
        default=None,
        description=(
            "Fixed number of teeth per edge, whatever its length. Odd, at least 3. "
            "Use instead of 'finger' when face sizes vary widely."
        ),
    ),
    depth: float | None = Query(
        default=None, gt=0, description="Recess depth; defaults to the thickness"
    ),
    finger_for: str | None = Query(
        default=None,
        description=(
            "Per-face tooth widths, as 'tag:width' pairs separated by semicolons, "
            "e.g. 'lid/cap+:6;body/side[s.c1]:4'. Applies to both faces of any "
            "edge it touches, since a joint has to mate."
        ),
    ),
    fit: str = Query(
        default="outer",
        description=(
            "'outer' if the modelled solid is the outside of the assembly, "
            "'inner' if it is the cavity. They differ by one thickness at "
            "every joint."
        ),
    ),
) -> Response:
    """Cut the modelled shape out of sheet and fold it together.

    Between /export/flat, which gives plain panels, and /export/enclosure,
    which gives a rectangular box that ignores the part's shape.
    """
    try:
        result = service().jointed_faces(
            project_id,
            thickness,
            finger,
            kerf,
            body,
            teeth=teeth,
            depth=depth,
            overrides=_finger_overrides(finger_for),
            fit=fit,
        )
        content = drawing.export_drawing(
            list(result.panels), fmt, title=f"{project_id} jointed"
        )
    except Exception as error:
        raise _fail(error) from error
    headers = {
        "Content-Disposition": (
            f'attachment; filename="{project_id}-jointed.{fmt.lower()}"'
        ),
        "X-Joints-Cut": str(result.joints),
    }
    if result.plain:
        headers["X-Panels-Plain"] = ", ".join(result.plain)
    return Response(
        content=content,
        media_type=DRAWING_MEDIA.get(fmt.lower(), "application/octet-stream"),
        headers=headers,
    )


def _finger_overrides(text: str | None) -> dict[str, float]:
    """Parse ``tag:width;tag:width``.

    Semicolons rather than commas, because a face tag may contain a comma once
    a selector union is involved, and splitting on the wrong character would
    silently drop half an override.
    """
    if not text:
        return {}
    overrides: dict[str, float] = {}
    for part in text.split(";"):
        entry = part.strip()
        if not entry:
            continue
        tag, _, width = entry.rpartition(":")
        if not tag:
            raise HTTPException(
                status_code=400,
                detail={"message": f"expected 'tag:width', got {entry!r}"},
            )
        try:
            overrides[tag.strip()] = float(width)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail={"message": f"{width!r} is not a width, in {entry!r}"},
            ) from None
    return overrides


@router.get(
    "/projects/{project_id}/export/enclosure",
    summary="Flat panels for a laser-cut box around the part",
)
def export_enclosure(
    project_id: str,
    thickness: float = Query(default=3.0, gt=0, description="Sheet thickness in mm"),
    finger: float = Query(default=10.0, gt=0, description="Target finger width in mm"),
    kerf: float = Query(default=0.15, ge=0, description="Laser cut width in mm"),
    clearance: float = Query(default=2.0, ge=0, description="Space around the part"),
    fmt: str = Query(default="svg"),
    body: str | None = Query(default=None),
) -> Response:
    """Six interlocking panels on one sheet, sized from the model's bounds."""
    try:
        panels = service().enclosure(project_id, thickness, finger, kerf, clearance, body)
        content = drawing.export_drawing(panels, fmt, title=f"{project_id} enclosure")
    except Exception as error:
        raise _fail(error) from error
    return Response(
        content=content,
        media_type=DRAWING_MEDIA.get(fmt.lower(), "application/octet-stream"),
        headers={
            "Content-Disposition": (
                f'attachment; filename="{project_id}-enclosure.{fmt.lower()}"'
            )
        },
    )


def _mesh_bytes(fmt: str, tessellation, name: str) -> bytes:
    if fmt == "stl":
        return exporters.stl_binary(tessellation, name)
    if fmt == "stl-ascii":
        return exporters.stl_ascii(tessellation, name)
    return exporters.obj_text(tessellation, name)


def _sheet_bytes(fmt: str, document: Document, result, topology: dict[str, object]) -> bytes:
    if fmt == "csv":
        return exporters.parameters_csv(document, result.parameters)
    if fmt == "json":
        return exporters.parameters_json(document, result.parameters)
    if fmt == "topology":
        # The whole document's naming, not the first body's: this file is the
        # offline copy of what /topology answers, and the two disagreeing on an
        # assembly is how a cutting list ends up missing a part.
        return exporters.topology_json(topology)
    return yaml_text(document).encode()


def _suffix(fmt: str) -> str:
    return {"stl-ascii": "stl", "topology": "topology.json"}.get(fmt, fmt)
