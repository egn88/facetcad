"""Use cases.

Everything the HTTP API can do is a method here, and nothing here knows HTTP
exists. That is what makes the MCP server a thin wrapper rather than a second
implementation: both transports drive the same object.

The engine cache is kept per project, so editing one dimension in a large model
rebuilds only the affected features rather than the whole history.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from facet.domain.body import Body, Placement
from facet.domain.datum import DatumPlane
from facet.domain.document import Document
from facet.domain.errors import (
    CapabilityError,
    DocumentError,
    FacetCADError,
    UnknownReferenceError,
)
from facet.domain.features import FeatureSpec
from facet.domain.math3d import Frame, Vec3
from facet.domain.parameters import Parameter, ResolvedParameters
from facet.domain.selectors import EdgeSelector, FaceSelector
from facet.domain.sketch import Sketch
from facet.domain.topology import TopologyIndex

from .datum_proposal import DatumProposal, propose_datum_for_face
from .enclosure import enclosure_for_bounds, enclosure_panels
from .features import validate_options
from .flatten import FlattenResult, is_blend, lay_out
from .jointed import OUTER, JointedResult, JointSpec, joint_faces
from .naming import NamedSolid
from .ports.geometry import (
    BrepExporter,
    Capability,
    DrawingExporter,
    GeometryKernel,
    KernelInfo,
    Profile2D,
    ProfileExtractor,
    Tessellation,
)
from .ports.repository import DocumentRepository, ProjectSummary
from .ports.snapshots import SnapshotStore
from .recompute import Detail, RecomputeEngine, RecomputeResult

#: How finely a free-form curve is approximated when flattened for cutting (mm).
DRAWING_TOLERANCE = 0.01

#: How near a parameter must resolve to a located offset to be named as its
#: source. The offsets themselves are rounded to 4dp before anyone compares
#: them, so the real floor is 5e-5; 1e-6 sits comfortably below that while
#: still absorbing the float noise a frame transform leaves behind. Anything
#: looser would start naming parameters that merely happen to be nearby.
OFFSET_PARAMETER_TOLERANCE = 1e-6


@dataclass(frozen=True)
class ResolvePreview:
    """What a selector would match right now — without committing to it.

    Exposed over the API because it is the difference between an agent that can
    use this system and one that has to guess: ask what a selector matches,
    then write it into the document.
    """

    selector: str
    matched: tuple[str, ...]
    count: int
    ok: bool
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "selector": self.selector,
            "matched": list(self.matched),
            "count": self.count,
            "ok": self.ok,
            "error": self.error,
        }


class ProjectService:
    """Project lifecycle, editing and rebuilding."""

    def __init__(
        self,
        repository: DocumentRepository,
        kernel: GeometryKernel,
        *,
        tessellation_tolerance: float = 0.1,
        snapshots: SnapshotStore | None = None,
    ) -> None:
        self._repository = repository
        self._kernel = kernel
        self._tolerance = tessellation_tolerance
        # Handed to every engine, so a rebuild of any project can start from
        # geometry an earlier run left behind. Optional: without it the engines
        # behave exactly as they did, cold on every start.
        self._snapshots = snapshots
        self._engines: dict[str, RecomputeEngine] = {}

    def invalidate_caches(self) -> None:
        """Forget every cached rebuild.

        Called when the geometry worker is replaced: the caches hold handles to
        solids that lived in its memory, and the replacement numbers its solids
        from the start again. Keeping them would mean handing back a handle that
        now names a different shape.

        The snapshot store is deliberately left alone. Its entries are bytes, not
        handles — they never referred to the dead worker's memory, and they are
        the one thing that makes the recovery cheap instead of another full
        rebuild.
        """
        self._engines.clear()

    # -- kernel introspection ---------------------------------------------

    def kernel_info(self) -> KernelInfo:
        return KernelInfo(name=self._kernel.name, capabilities=self._kernel.capabilities)

    # -- project lifecycle -------------------------------------------------

    def list_projects(self) -> Sequence[ProjectSummary]:
        return self._repository.list_projects()

    def create_project(self, project_id: str, document: Document) -> ProjectSummary:
        return self._repository.create(project_id, document)

    def delete_project(self, project_id: str) -> None:
        self._repository.delete(project_id)
        self._engines.pop(project_id, None)

    def load(self, project_id: str) -> Document:
        return self._repository.load(project_id)

    def replace_document(self, project_id: str, document: Document) -> ProjectSummary:
        """Import a whole document — the 'clone onto another station' path."""
        document.validate()
        summary = self._repository.save(project_id, document)
        self._engine(project_id).invalidate()
        return summary

    # -- editing -----------------------------------------------------------

    def update_parameters(
        self, project_id: str, changes: Mapping[str, object]
    ) -> RecomputeResult:
        document = self._repository.load(project_id)
        for name, value in changes.items():
            if name not in document.parameters:
                raise UnknownReferenceError(kind="parameter", identifier=name)
            if isinstance(value, str):
                document.set_parameter(name, expr=value, value=None)
            else:
                document.set_parameter(name, value=float(value), expr=None)  # type: ignore[arg-type]
        return self._persist_and_rebuild(project_id, document)

    def add_parameter(self, project_id: str, parameter: Parameter) -> RecomputeResult:
        document = self._repository.load(project_id)
        document.add_parameter(parameter)
        return self._persist_and_rebuild(project_id, document)

    def edit_parameter(
        self, project_id: str, name: str, changes: Mapping[str, object]
    ) -> RecomputeResult:
        """Change a parameter's whole row, including its name.

        A rename is applied through the entire document, so no expression is
        left pointing at a name that no longer exists.
        """
        document = self._repository.load(project_id)
        if name not in document.parameters:
            raise UnknownReferenceError(kind="parameter", identifier=name)

        renamed = str(changes.get("name", name))
        if renamed != name:
            document.rename_parameter(name, renamed)

        fields = {
            key: value
            for key, value in changes.items()
            if key in {"unit", "group", "doc"} and value is not None
        }
        if changes.get("expr"):
            fields.update({"expr": str(changes["expr"]), "value": None})
        elif "value" in changes and changes["value"] is not None:
            fields.update({"value": float(changes["value"]), "expr": None})  # type: ignore[arg-type]
        if fields:
            document.set_parameter(renamed, **fields)

        return self._persist_and_rebuild(project_id, document)

    def delete_parameter(self, project_id: str, name: str) -> RecomputeResult:
        document = self._repository.load(project_id)
        document.remove_parameter(name)
        return self._persist_and_rebuild(project_id, document)

    def parameter_usage(self, project_id: str, name: str) -> list[str]:
        """Everything that reads a parameter — shown before offering to delete."""
        return self._repository.load(project_id).references_to(name)

    # -- sketches and datums ----------------------------------------------

    def put_sketch(self, project_id: str, sketch: Sketch) -> RecomputeResult:
        document = self._repository.load(project_id)
        document.put_sketch(sketch)
        self._engine(project_id).invalidate()
        return self._persist_and_rebuild(project_id, document)

    def delete_sketch(self, project_id: str, identifier: str) -> RecomputeResult:
        document = self._repository.load(project_id)
        document.remove_sketch(identifier)
        self._engine(project_id).invalidate()
        return self._persist_and_rebuild(project_id, document)

    def put_datum(self, project_id: str, plane: DatumPlane) -> RecomputeResult:
        document = self._repository.load(project_id)
        document.put_datum(plane)
        self._engine(project_id).invalidate()
        return self._persist_and_rebuild(project_id, document)

    def delete_datum(self, project_id: str, identifier: str) -> RecomputeResult:
        document = self._repository.load(project_id)
        document.remove_datum(identifier)
        self._engine(project_id).invalidate()
        return self._persist_and_rebuild(project_id, document)

    def add_feature(
        self,
        project_id: str,
        spec: FeatureSpec,
        at: int | None = None,
        body: str | None = None,
    ) -> RecomputeResult:
        # Refused here rather than on the rebuild path. This is where the mistake
        # is being made and where the caller can still fix it. Refusing at rebuild
        # instead broke documents already saved with an ignored key — three
        # working parts on a live server stopped building, because the keys had
        # always been ignored and the parts had been correct anyway.
        validate_options(spec)
        document = self._repository.load(project_id)
        document.add_feature(spec, at, body)
        return self._persist_and_rebuild(project_id, document)

    def update_feature(self, project_id: str, spec: FeatureSpec) -> RecomputeResult:
        validate_options(spec)
        document = self._repository.load(project_id)
        document.replace_feature(spec)
        return self._persist_and_rebuild(project_id, document)

    def delete_feature(self, project_id: str, feature_id: str) -> RecomputeResult:
        document = self._repository.load(project_id)
        document.remove_feature(feature_id)
        return self._persist_and_rebuild(project_id, document)

    def reorder_features(self, project_id: str, order: Sequence[str]) -> RecomputeResult:
        document = self._repository.load(project_id)
        document.reorder_features(order)
        # Reordering changes what every feature sees, so nothing may be reused.
        self._engine(project_id).invalidate()
        return self._persist_and_rebuild(project_id, document)

    # -- rebuilding --------------------------------------------------------

    def recompute(self, project_id: str, detail: str = Detail.DRAFT) -> RecomputeResult:
        return self._engine(project_id).recompute(self._repository.load(project_id), detail)

    def recompute_for_export(self, project_id: str) -> RecomputeResult:
        """A rebuild including geometry only a manufactured part needs.

        A thread declared ``modelled: export`` is cut here and skipped for the
        viewport, so a session stays responsive while the file that reaches a
        printer still has the thread in it.
        """
        return self.recompute(project_id, Detail.FULL)

    def topology(self, project_id: str) -> TopologyIndex:
        return self.recompute(project_id).topology

    def mesh(
        self,
        project_id: str,
        detail: str = Detail.DRAFT,
        body: str | None = None,
    ) -> tuple[Tessellation, RecomputeResult]:
        """Triangles for export, with each body's placement baked in.

        ``body`` names one to export on its own — which is what printing a
        multi-part model needs, since the parts go on the bed separately.
        Without it every body is included, because a document that builds two
        parts should not quietly export one.

        Placement *is* applied here, unlike in :meth:`body_meshes`. There it
        travels as a matrix so the viewport can move a body without a rebuild;
        a file has nowhere to carry a matrix, so the points have to be where the
        model says they are.
        """
        result = self.recompute(project_id, detail)
        wanted = self._bodies_for_mesh(result, body)
        if not wanted:
            return Tessellation(), result

        merged = Tessellation()
        for placement, solid in wanted:
            piece = self._kernel.tessellate(solid.handle, self._tolerance)
            merged = _joined(merged, _placed_mesh(piece, placement))
        return merged, result

    def _bodies_for_mesh(
        self, result: RecomputeResult, body: str | None
    ) -> list[tuple[Frame, NamedSolid]]:
        found = [
            (entry.placement, entry.solid)
            for entry in result.bodies
            if entry.solid is not None and (body is None or entry.id == body)
        ]
        if body is not None and not found:
            built = [entry.id for entry in result.bodies if entry.solid is not None]
            raise DocumentError(
                reason=(
                    f"no body named {body!r} built"
                    + (f"; this document builds {', '.join(built)}" if built else "")
                ),
                path="bodies",
            )
        return found

    def body_meshes(self, project_id: str) -> tuple[list[dict[str, object]], RecomputeResult]:
        """Every body, tessellated in its own coordinates with its placement.

        The placement travels as a matrix rather than being baked into the
        points, so a body can be moved without the geometry being rebuilt — and
        so an assembly can later drive it per frame.
        """
        result = self.recompute(project_id)
        meshes: list[dict[str, object]] = []

        for body in result.bodies:
            if body.solid is None:
                meshes.append(
                    {
                        "id": body.id,
                        "placement": list(body.placement.to_matrix()),
                        "positions": [], "normals": [], "indices": [],
                        "faceRanges": [], "edges": [],
                    }
                )
                continue

            tessellation = self._kernel.tessellate(body.solid.handle, self._tolerance)
            tags = {ref: str(tag) for ref, tag in body.solid.refs.items()}
            meshes.append(
                {
                    "id": body.id,
                    "placement": list(body.placement.to_matrix()),
                    "positions": list(tessellation.positions),
                    "normals": list(tessellation.normals),
                    "indices": list(tessellation.indices),
                    "faceRanges": [
                        {
                            "ref": r.ref,
                            "tag": tags.get(r.ref, r.ref),
                            "start": r.start,
                            "count": r.count,
                        }
                        for r in tessellation.face_ranges
                    ],
                    "edges": [
                        {"ref": e.ref, "points": list(e.points)} for e in tessellation.edges
                    ],
                }
            )
        return meshes, result

    def body_topologies(self, project_id: str) -> dict[str, object]:
        """Every body's named faces and edges, keyed by body."""
        result = self.recompute(project_id)
        return {
            "bodies": [
                {"id": body.id, **body.topology.to_dict()} for body in result.bodies
            ]
        }

    # -- body lifecycle ----------------------------------------------------

    def add_body(self, project_id: str, body: Body) -> RecomputeResult:
        document = self._repository.load(project_id)
        document.add_body(body)
        return self._persist_and_rebuild(project_id, document)

    def update_body(
        self, project_id: str, identifier: str, placement: Placement
    ) -> RecomputeResult:
        document = self._repository.load(project_id)
        body = document.body(identifier)
        body.placement = placement
        return self._persist_and_rebuild(project_id, document)

    def delete_body(self, project_id: str, identifier: str) -> RecomputeResult:
        document = self._repository.load(project_id)
        document.remove_body(identifier)
        self._engine(project_id).invalidate()
        return self._persist_and_rebuild(project_id, document)

    def export_brep(self, project_id: str, fmt: str, body: str | None = None) -> bytes:
        """Write STEP, when the configured kernel can.

        Checked up front against the declared capability rather than by calling
        and catching, so a mesh-only kernel fails with a clear message instead
        of an AttributeError halfway through.

        One body at a time. A STEP file can hold an assembly, but writing one
        needs the kernel to compose the placements, and quietly writing only the
        first of several bodies would be worse than saying which to pick.
        """
        if Capability.BREP_EXPORT not in self._kernel.capabilities:
            raise CapabilityError(
                capability=Capability.BREP_EXPORT,
                kernel=self._kernel.name,
                available=tuple(sorted(self._kernel.capabilities)),
            )
        result = self.recompute_for_export(project_id)
        wanted = self._bodies_for_mesh(result, body)
        if not wanted:
            raise CapabilityError(
                capability=Capability.BREP_EXPORT,
                kernel=self._kernel.name,
                available=tuple(sorted(self._kernel.capabilities)),
            )
        if body is None and len(wanted) > 1:
            names = ", ".join(entry.id for entry in result.bodies if entry.solid)
            raise DocumentError(
                reason=(
                    f"this document builds {len(wanted)} bodies ({names}); name one "
                    "with ?body= — a STEP file here holds a single solid"
                ),
                path="export",
            )
        exporter: BrepExporter = self._kernel  # type: ignore[assignment]
        return exporter.export_brep(wanted[0][1].handle, fmt)

    def cut_paths(
        self, project_id: str, selector: str, body: str | None = None
    ) -> list[Profile2D]:
        """The 2D cut paths of the faces a selector resolves to.

        Selector-driven rather than ref-driven on purpose: the whole value of
        the naming engine is that ``lid/cap+`` still means the same face after a
        parameter change, so the DXF you re-export lands on the same part.

        Returns port types, leaving the choice of file format to the adapter
        that asked — the dependency arrow keeps pointing inwards.
        """
        self._require(Capability.FACE_PROFILE)
        result = self.recompute(project_id)
        extractor: ProfileExtractor = self._kernel  # type: ignore[assignment]

        profiles: list[Profile2D] = []
        parsed = FaceSelector.parse(selector)
        for named in self._bodies_for(result, body):
            # `candidates` rather than `resolve`: a selector naming a face of one
            # body must not fail merely because another body has no such face.
            # The empty case is caught once, below, across every body.
            entries = parsed.candidates(named.topology)
            by_tag = {tag: ref for ref, tag in named.refs.items()}
            for entry in entries:
                ref = by_tag.get(entry.tag)
                if ref is None:
                    continue
                flat = extractor.face_profile(named.handle, ref, DRAWING_TOLERANCE)
                profiles.append(replace(flat, label=str(entry.tag)))

        if not profiles:
            raise DocumentError(
                reason=f"no face matched {selector!r}, so there is nothing to cut",
                path="export",
            )
        return profiles

    def export_views(
        self,
        project_id: str,
        fmt: str = "dxf",
        views: Sequence[str] = ("top",),
        body: str | None = None,
    ) -> bytes:
        """Orthographic sections of a body, for a setup drawing."""
        self._require(Capability.DRAWING_EXPORT)
        result = self.recompute(project_id)
        exporter: DrawingExporter = self._kernel  # type: ignore[assignment]
        bodies = self._bodies_for(result, body)
        if not bodies:
            raise DocumentError(
                reason="the model does not build, so it cannot be drawn", path="export"
            )
        return exporter.export_drawing(bodies[0].handle, fmt, views)

    def flat_faces(
        self,
        project_id: str,
        body: str | None = None,
        include_blends: bool = False,
    ) -> FlattenResult:
        """Every planar face of a body, flattened and laid out on one sheet.

        The cutting list for making the part itself out of sheet, as opposed to
        :meth:`enclosure`, which builds a container to put it in.
        """
        self._require(Capability.FACE_PROFILE)
        result = self.recompute(project_id)
        extractor: ProfileExtractor = self._kernel  # type: ignore[assignment]

        profiles: list[Profile2D] = []
        skipped: list[str] = []
        for named in self._bodies_for(result, body):
            for ref, tag in sorted(named.refs.items(), key=lambda item: str(item[1])):
                if not include_blends and is_blend(tag):
                    continue
                try:
                    flat = extractor.face_profile(named.handle, ref, DRAWING_TOLERANCE)
                except FacetCADError:
                    # A curved face has no development into a plane. Saying so
                    # is the point; quietly dropping it would leave a cutting
                    # list that does not add up.
                    skipped.append(str(tag))
                    continue
                profiles.append(replace(flat, label=str(tag)))

        if not profiles:
            raise DocumentError(
                reason=(
                    "no planar face could be flattened"
                    + (f"; {len(skipped)} curved face(s) were skipped" if skipped else "")
                ),
                path="export",
            )
        return FlattenResult(panels=tuple(lay_out(profiles)), skipped=tuple(skipped))

    def jointed_faces(
        self,
        project_id: str,
        thickness: float,
        finger: float = 10.0,
        kerf: float = 0.15,
        body: str | None = None,
        teeth: int | None = None,
        depth: float | None = None,
        overrides: Mapping[str, float] | None = None,
        fit: str = OUTER,
    ) -> JointedResult:
        """The part's own faces, with finger joints on the edges they share.

        The middle ground between :meth:`flat_faces`, which gives plain panels,
        and :meth:`enclosure`, which gives a rectangular box that ignores the
        part's shape.
        """
        flattened = self.flat_faces(project_id, body, include_blends=False)
        panels = {panel.label: panel for panel in flattened.panels}
        result = joint_faces(
            panels,
            JointSpec(
                thickness=thickness,
                finger=finger,
                kerf=kerf,
                teeth=teeth,
                depth=depth,
                overrides=dict(overrides or {}),
                fit=fit,
            ),
            adjacency=self._edge_adjacency(project_id, body),
        )
        return replace(result, panels=tuple(lay_out(list(result.panels))))

    def _edge_adjacency(
        self, project_id: str, body: str | None
    ) -> dict[str, tuple[str, str]]:
        """Model edge ref -> the two face tags it separates, *including* blends.

        The flattened panels only know about the faces that made it into the
        cutting list, so an edge against an excluded fillet or chamfer looks
        like a free edge and gets no joint — which is how a wedge ended up with
        plain sides where a bevel had been. This is the full picture, so the
        joint generator can see through a blend to the faces it sits between.
        """
        result = self.recompute(project_id)
        adjacency: dict[str, tuple[str, str]] = {}
        for named in self._bodies_for(result, body):
            for edge_tag, ref in named.edge_refs.items():
                first, second = edge_tag.faces
                adjacency[ref] = (str(first), str(second))
        return adjacency

    def enclosure(
        self,
        project_id: str,
        thickness: float,
        finger: float = 10.0,
        kerf: float = 0.15,
        clearance: float = 2.0,
        body: str | None = None,
    ) -> list[Profile2D]:
        """Flat panels for a laser-cut box that the part fits inside.

        Sized from the model's own bounding box, so the box tracks the part: a
        wider bracket gets a wider box on the next export, with no second set of
        dimensions to keep in step by hand.
        """
        result = self.recompute(project_id)
        solids = self._bodies_for(result, body)
        if not solids:
            raise DocumentError(
                reason="the model does not build, so there is nothing to enclose",
                path="enclosure",
            )

        lows: list[tuple[float, float, float]] = []
        highs: list[tuple[float, float, float]] = []
        for named in solids:
            box = self._kernel.bounding_box(named.handle)
            lows.append(box.min)
            highs.append(box.max)

        minimum = tuple(min(v[i] for v in lows) for i in range(3))
        maximum = tuple(max(v[i] for v in highs) for i in range(3))
        return enclosure_panels(
            enclosure_for_bounds(
                minimum, maximum, thickness, finger=finger, kerf=kerf, clearance=clearance
            )
        )

    def locate(self, project_id: str, point: tuple[float, float, float]) -> list[dict[str, object]]:
        """Express a world point in each datum's plane, nearest plane first.

        This is what turns a click in the viewport into numbers a document can
        hold. Note what it deliberately does *not* do: it never records which
        face was clicked. Datums are computed from parameters only, and a sketch
        attaches to a datum and nothing else — see :mod:`facet.domain.datum`.
        A click is a convenient way to type two numbers, not a reference that
        can go stale.

        Each row also carries ``offsetParameter``: the parameter, if any, that
        already resolves to that offset. Without it the obvious next step —
        declare a datum at the offset you were shown — bakes today's number
        into the document, and the new datum stops following the model the
        moment the thickness changes. With it the caller writes
        ``origin: [0, 0, plate_t]``, which stays a parameter-derived datum and
        so keeps the guarantee in :mod:`facet.domain.datum` intact.
        """
        result = self.recompute(project_id)
        target = Vec3(*point)

        found: list[dict[str, object]] = []
        for identifier, frame in result.frames.items():
            local = frame.to_local(target)
            offset = round(local.z, 4)
            found.append(
                {
                    "datum": identifier,
                    "u": round(local.x, 4),
                    "v": round(local.y, 4),
                    "offset": offset,
                    "offsetParameter": _offset_parameter(offset, result.parameters),
                }
            )
        found.sort(key=lambda item: abs(float(item["offset"])))
        return found

    def datum_for_face(
        self,
        project_id: str,
        tag: str,
        point: tuple[float, float, float] | None = None,
    ) -> DatumProposal:
        """Propose a datum on the plane of a named face.

        The counterpart to :meth:`locate` for the case where the user is
        pointing at a face rather than at a point. Where locate can only offer
        the parameter that happens to match the number it measured, this reads
        the plane straight out of the history — ``pad_1/cap+`` is a sketch's
        datum offset by that pad's own length — so the offset comes back as the
        expression the feature was written with rather than as today's value.

        Nothing is recomputed and no solid is consulted: the document already
        knows the answer, which is exactly why the datum stays parameter-derived
        and the rule in :mod:`facet.domain.datum` survives the convenience.
        """
        return propose_datum_for_face(self._repository.load(project_id), tag, point)

    def _bodies_for(self, result: RecomputeResult, body: str | None) -> list[NamedSolid]:
        """Built bodies, optionally narrowed to one by id."""
        found = [
            b.solid
            for b in result.bodies
            if b.solid is not None and (body is None or b.id == body)
        ]
        if body is not None and not found:
            raise DocumentError(reason=f"no body named {body!r} built", path="bodies")
        return found

    def _require(self, capability: str) -> None:
        if capability not in self._kernel.capabilities:
            raise CapabilityError(
                capability=capability,
                kernel=self._kernel.name,
                available=tuple(sorted(self._kernel.capabilities)),
            )

    def sketch_geometry(self, project_id: str) -> dict[str, object]:
        """Every sketch as world-space polylines, for drawing in the viewport.

        Deliberately independent of the feature history: a sketch you cannot
        yet build is exactly the one you most need to look at, so this resolves
        parameters and datums directly and reports per-sketch errors rather
        than failing as a whole.
        """
        document = self._repository.load(project_id)
        try:
            parameters = document.parameters.resolve()
            frames = document.datums.resolve_all(parameters)
        except FacetCADError as error:
            return {"sketches": [], "error": str(error)}

        sketches: list[dict[str, object]] = []
        for identifier, sketch in document.sketches.items():
            frame = frames.get(sketch.plane)
            if frame is None:
                sketches.append(
                    {
                        "id": identifier,
                        "plane": sketch.plane,
                        "curves": [],
                        "points": [],
                        "error": f"unknown datum '{sketch.plane}'",
                    }
                )
                continue
            sketches.append(_sketch_payload(identifier, sketch, frame, parameters))
        return {"sketches": sketches, "error": None}

    # -- selector preview --------------------------------------------------

    def resolve_selector(self, project_id: str, text: str, kind: str = "faces") -> ResolvePreview:
        """Answer 'what would this match?' without changing anything."""
        topology = self.topology(project_id)
        try:
            face_selector = FaceSelector.parse(text)
            if kind == "edges":
                # "every edge touching a face that matches this pattern"
                selector = EdgeSelector(touching=face_selector.include)
                matched = [str(e.tag) for e in selector.candidates(topology)]
            else:
                matched = [str(e.tag) for e in face_selector.candidates(topology)]
        except FacetCADError as error:
            return ResolvePreview(
                selector=text, matched=(), count=0, ok=False, error=str(error)
            )
        return ResolvePreview(
            selector=text, matched=tuple(matched), count=len(matched), ok=bool(matched)
        )

    def resolve_edge_selector(self, project_id: str, first: str, second: str) -> ResolvePreview:
        topology = self.topology(project_id)
        selector = EdgeSelector.between_patterns(first, second)
        try:
            matched = [str(e.tag) for e in selector.candidates(topology)]
        except FacetCADError as error:
            return ResolvePreview(
                selector=selector.describe(), matched=(), count=0, ok=False, error=str(error)
            )
        return ResolvePreview(
            selector=selector.describe(),
            matched=tuple(matched),
            count=len(matched),
            ok=bool(matched),
        )

    # -- internals ---------------------------------------------------------

    def _engine(self, project_id: str) -> RecomputeEngine:
        engine = self._engines.get(project_id)
        if engine is None:
            engine = RecomputeEngine(self._kernel, self._snapshots)
            self._engines[project_id] = engine
        return engine

    def _persist_and_rebuild(self, project_id: str, document: Document) -> RecomputeResult:
        """Save first, then rebuild.

        The document is the source of truth even when it does not build. Saving
        a model that currently fails is normal — the user is mid-edit — and the
        rebuild result carries the diagnostics.
        """
        document.validate()
        self._repository.save(project_id, document)
        return self._engine(project_id).recompute(document)


def _placed_mesh(mesh: Tessellation, placement: Frame) -> Tessellation:
    """Move a body's triangles into world space.

    Normals are rotated but not translated, and the frame is orthonormal, so no
    inverse-transpose is needed — which is worth stating, because that is the
    step people add out of habit and it would be wrong here.
    """
    if placement.is_identity:
        return mesh

    points: list[float] = []
    for index in range(0, len(mesh.positions), 3):
        moved = placement.to_world(Vec3(*mesh.positions[index : index + 3]))
        points.extend(moved.as_tuple())

    normals: list[float] = []
    for index in range(0, len(mesh.normals), 3):
        turned = placement.direction_to_world(Vec3(*mesh.normals[index : index + 3]))
        normals.extend(turned.as_tuple())

    return Tessellation(
        positions=tuple(points),
        normals=tuple(normals),
        indices=mesh.indices,
        face_ranges=mesh.face_ranges,
        edges=mesh.edges,
    )


def _joined(first: Tessellation, second: Tessellation) -> Tessellation:
    """Concatenate two meshes, shifting the second's indices past the first.

    Bodies are never fused, so this is a bag of triangles rather than a boolean
    — which is exactly what STL is, and why several parts can share one file
    without any of them being merged into another.
    """
    if not first.positions:
        return second
    offset = first.vertex_count
    return Tessellation(
        positions=first.positions + second.positions,
        normals=first.normals + second.normals,
        indices=first.indices + tuple(i + offset for i in second.indices),
        face_ranges=first.face_ranges,
        edges=first.edges,
    )


def _offset_parameter(
    offset: float, parameters: ResolvedParameters | None
) -> str | None:
    """Name the parameter a located offset came from, if one fits.

    Only a name is returned, never an expression, because the caller is going
    to put it straight into a datum origin and anything cleverer would be this
    module guessing at intent it does not have.
    """
    if parameters is None or abs(offset) <= OFFSET_PARAMETER_TOLERANCE:
        # An offset of zero is already on the plane and needs no parameter at
        # all; naming one that happens to resolve to zero would be noise.
        return None

    # Sorted rather than in declaration order: several parameters can share a
    # value, and the same click must answer the same way every time. This is
    # determinism, not a claim that the first name alphabetically is the better
    # one. The direct match is exhausted before the negated one so that a
    # genuinely negative parameter wins over a positive twin.
    names = sorted(parameters)
    for wanted in (offset, -offset):
        for name in names:
            if abs(parameters[name] - wanted) <= OFFSET_PARAMETER_TOLERANCE:
                return name
    return None


def _sketch_payload(
    identifier: str, sketch: Sketch, frame: Frame, parameters: ResolvedParameters
) -> dict[str, object]:
    """One sketch, flattened into polylines and point markers."""
    try:
        resolved = sketch.resolve_all_curves(parameters)
    except FacetCADError as error:
        resolved, message = [], str(error)
    else:
        message = None

    curves = [
        {
            "id": curve.id,
            "type": curve.type,
            "points": [c for point in curve.polyline(frame) for c in point.as_tuple()],
        }
        for curve in resolved
    ]

    points: list[dict[str, object]] = []
    try:
        for point_id, uv in sketch.resolve_points(parameters).items():
            points.append({"id": point_id, "at": list(frame.point_at(uv).as_tuple())})
    except FacetCADError:
        pass

    return {
        "id": identifier,
        "plane": sketch.plane,
        "curves": curves,
        "points": points,
        "error": message,
    }
