"""The document aggregate — one project, one git-diffable file.

Everything about a part lives here: the parameter sheet, the datums, the
sketches and the ordered feature history. Because it is plain data with a
round-tripping dict form, "clone this project onto another station" is a file
copy, and a change to a dimension shows up in a diff as one line.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from .body import DEFAULT_BODY, Body
from .datum import DatumPlane, DatumSet
from .errors import DocumentError, DuplicateIdError, UnknownReferenceError
from .expressions import rename_reference
from .features import FeatureSpec
from .parameters import Parameter, ParameterSet
from .sketch import Sketch
from .values import Value

SCHEMA = "cadsheet/1"


@dataclass
class Document:
    """A complete parametric part definition."""

    name: str = "untitled"
    schema: str = SCHEMA
    parameters: ParameterSet = field(default_factory=ParameterSet)
    datums: DatumSet = field(default_factory=DatumSet)
    sketches: dict[str, Sketch] = field(default_factory=dict)
    #: Each body owns a linear history and produces one solid. Bodies are never
    #: merged with each other, which is what makes an assembly possible.
    bodies: list[Body] = field(default_factory=lambda: [Body(id=DEFAULT_BODY)])

    # -- bodies ------------------------------------------------------------

    def body(self, identifier: str) -> Body:
        found = next((b for b in self.bodies if b.id == identifier), None)
        if found is None:
            raise UnknownReferenceError(kind="body", identifier=identifier)
        return found

    def add_body(self, body: Body) -> None:
        if any(b.id == body.id for b in self.bodies):
            raise DuplicateIdError(kind="body", identifier=body.id)
        self.bodies.append(body)

    def remove_body(self, identifier: str) -> Body:
        if len(self.bodies) == 1:
            raise DocumentError(reason="a document needs at least one body")
        copies = self.copies_of(identifier)
        if copies:
            # Deleting the source silently would take its copies with it, which
            # is a lot of model to lose to one click. Refused with the list, in
            # the same spirit as a parameter something still reads.
            names = ", ".join(repr(body.id) for body in copies)
            raise DocumentError(
                reason=(
                    f"body {identifier!r} is copied by {names}. Delete the copies "
                    "first, or promote one of them to a body of its own."
                ),
                path=f"bodies.{identifier}",
            )
        index = next(i for i, b in enumerate(self.bodies) if b.id == identifier)
        return self.bodies.pop(index)

    def rename_body(self, old: str, new: str) -> None:
        """Rename a body and follow the change into everything naming it.

        Only copies name a body, so this is a short list today — but it is the
        same rule a parameter rename follows, and leaving a copy pointing at an
        id that no longer exists would break the document silently, which is the
        one outcome this project exists to prevent.

        Feature ids, selectors and tags are untouched: a tag is a provenance
        path through the *features* that made a face, and the body it lives in
        is not part of its name. Renaming a body therefore cannot invalidate a
        single selector, which is why this is safe to offer at all.
        """
        if old == new:
            return
        body = self.body(old)  # raises if unknown
        if any(b.id == new for b in self.bodies):
            raise DuplicateIdError(kind="body", identifier=new)
        if not new.isidentifier():
            raise DocumentError(reason=f"body id {new!r} must be an identifier")
        for copy in self.copies_of(old):
            copy.of = new
        body.id = new

    def copies_of(self, identifier: str) -> list[Body]:
        """Bodies that show ``identifier``'s solid at their own placement."""
        return [body for body in self.bodies if body.of == identifier]

    def quantity_of(self, identifier: str) -> int:
        """How many times a body appears in the model.

        The number of pieces to produce, which is the question a printer asks
        and the one a model that duplicates by copy-paste cannot answer. A copy
        reports 0: it is counted by the body it copies, so the quantities over
        the document sum to the piece count rather than double-counting it.
        """
        body = self.body(identifier)
        return 0 if body.is_copy else 1 + len(self.copies_of(identifier))

    @property
    def sources(self) -> list[Body]:
        """Bodies that build themselves — everything with a history."""
        return [body for body in self.bodies if not body.is_copy]

    def body_of_feature(self, identifier: str) -> Body:
        for body in self.bodies:
            if any(f.id == identifier for f in body.features):
                return body
        raise UnknownReferenceError(kind="feature", identifier=identifier)

    @property
    def default_body(self) -> Body:
        """Where feature edits land when no body is named.

        Single-body documents are still the common case, so the flat API below
        keeps working against the first body rather than forcing every caller
        to name one.
        """
        if not self.bodies:
            self.bodies.append(Body(id=DEFAULT_BODY))
        # Never a copy: a copy has no history, so a feature landing there has
        # nowhere to go. The first body that builds itself is what "the default"
        # has always meant, and on a document with no copies it is `bodies[0]`
        # exactly as before.
        return next((b for b in self.bodies if not b.is_copy), self.bodies[0])

    # -- lookup ------------------------------------------------------------

    @property
    def features(self) -> list[FeatureSpec]:
        """The default body's history, for single-body callers."""
        return self.default_body.features

    @features.setter
    def features(self, specs: list[FeatureSpec]) -> None:
        self.default_body.features = specs

    def feature(self, identifier: str) -> FeatureSpec:
        for body in self.bodies:
            found = next((f for f in body.features if f.id == identifier), None)
            if found is not None:
                return found
        raise UnknownReferenceError(kind="feature", identifier=identifier)

    def feature_index(self, identifier: str) -> int:
        for index, spec in enumerate(self.default_body.features):
            if spec.id == identifier:
                return index
        raise UnknownReferenceError(kind="feature", identifier=identifier)

    def sketch(self, identifier: str) -> Sketch:
        found = self.sketches.get(identifier)
        if found is None:
            raise UnknownReferenceError(kind="sketch", identifier=identifier)
        return found

    # -- mutation ----------------------------------------------------------

    def add_feature(
        self, spec: FeatureSpec, at: int | None = None, body: str | None = None
    ) -> None:
        # Feature ids are unique within a body, but a duplicate across bodies
        # would still confuse every flat lookup, so they are kept unique.
        for existing in self.bodies:
            if any(f.id == spec.id for f in existing.features):
                raise DuplicateIdError(kind="feature", identifier=spec.id)
        target = self.body(body) if body else self.default_body
        if target.is_copy:
            raise DocumentError(
                reason=(
                    f"body {target.id!r} is a copy of {target.of!r} and has no history "
                    f"of its own. Add {spec.id!r} to {target.of!r} instead and every "
                    "copy of it gets the feature."
                ),
                path=f"bodies.{target.id}.features",
            )
        target.add_feature(spec, at)

    def replace_feature(self, spec: FeatureSpec) -> None:
        self.body_of_feature(spec.id).replace_feature(spec)

    def remove_feature(self, identifier: str) -> FeatureSpec:
        return self.body_of_feature(identifier).remove_feature(identifier)

    def reorder_features(self, order: Sequence[str], body: str | None = None) -> None:
        (self.body(body) if body else self.default_body).reorder_features(order)

    def set_parameter(self, name: str, **changes: object) -> None:
        self.parameters.replace(name, **changes)

    def add_parameter(self, parameter: Parameter) -> None:
        self.parameters.add(parameter)

    def remove_parameter(self, name: str) -> None:
        """Delete a parameter, refusing while anything still reads it."""
        users = self.references_to(name)
        if users:
            raise DocumentError(
                reason=(
                    f"parameter '{name}' is still used by {', '.join(users)}. "
                    "Change those first, or the document would stop building."
                ),
                path=f"parameters.{name}",
            )
        self.parameters.remove(name)

    def rename_parameter(self, old: str, new: str) -> None:
        """Rename a parameter and follow the change into every expression.

        A rename that left dangling references behind would break the document
        silently, so this rewrites the whole model rather than only the row the
        user edited.
        """
        if old == new:
            return
        if new in self.parameters:
            raise DuplicateIdError(kind="parameter", identifier=new)
        if not new.isidentifier():
            raise DocumentError(reason=f"parameter name {new!r} must be an identifier")
        self.parameters[old]  # raises if unknown

        self.parameters.rename(old, new)
        # Sibling expressions read the old name too — miss these and the sheet
        # itself stops resolving, which is the loudest possible way to be wrong.
        for parameter in list(self.parameters):
            if parameter.expr is None:
                continue
            rewritten = rename_reference(parameter.expr, old, new)
            if rewritten != parameter.expr:
                self.parameters.replace(parameter.name, expr=rewritten)

        self.datums = DatumSet(
            planes={
                identifier: _rename_in_datum(plane, old, new)
                for identifier, plane in self.datums.planes.items()
            }
        )
        self.sketches = {
            identifier: _rename_in_sketch(sketch, old, new)
            for identifier, sketch in self.sketches.items()
        }
        self.features = [_rename_in_feature(spec, old, new) for spec in self.features]

    def references_to(self, parameter: str) -> list[str]:
        """Everything that reads a parameter, described for a human."""
        users: list[str] = []
        for other in self.parameters:
            if other.name != parameter and parameter in self.parameters.dependencies_of(other.name):
                users.append(f"parameter '{other.name}'")
        for identifier, plane in self.datums.planes.items():
            if parameter in plane.parameter_dependencies():
                users.append(f"datum '{identifier}'")
        for identifier, sketch in self.sketches.items():
            if parameter in sketch.parameter_dependencies():
                users.append(f"sketch '{identifier}'")
        for spec in self.features:
            if parameter in spec.parameter_dependencies():
                users.append(f"feature '{spec.id}'")
        return users

    # -- sketches and datums ----------------------------------------------

    def put_sketch(self, sketch: Sketch) -> None:
        """Create or replace a sketch wholesale."""
        sketch.validate()
        self.sketches[sketch.id] = sketch

    def remove_sketch(self, identifier: str) -> None:
        if identifier not in self.sketches:
            raise UnknownReferenceError(kind="sketch", identifier=identifier)
        users = [
            f"feature '{spec.id}'"
            for spec in self.features
            if (spec.profile is not None and spec.profile.sketch == identifier)
            or str(spec.options.get("at", "")).startswith(f"{identifier}.")
        ]
        if users:
            raise DocumentError(
                reason=f"sketch '{identifier}' is still used by {', '.join(users)}",
                path=f"sketches.{identifier}",
            )
        del self.sketches[identifier]

    def put_datum(self, plane: DatumPlane) -> None:
        self.datums.planes[plane.id] = plane

    def remove_datum(self, identifier: str) -> None:
        if identifier not in self.datums.planes:
            raise UnknownReferenceError(kind="datum", identifier=identifier)
        users = [
            f"sketch '{sid}'" for sid, sketch in self.sketches.items()
            if sketch.plane == identifier
        ] + [
            f"datum '{did}'" for did, plane in self.datums.planes.items()
            if plane.parent == identifier
        ]
        if users:
            raise DocumentError(
                reason=f"datum '{identifier}' is still used by {', '.join(users)}",
                path=f"datums.{identifier}",
            )
        del self.datums.planes[identifier]

    # -- validation --------------------------------------------------------

    def validate(self) -> None:
        """Structural checks that do not need geometry.

        Run before any kernel work so a malformed document fails fast and cheap,
        with a message pointing at the offending path rather than at a solid.
        """
        if self.schema != SCHEMA:
            raise DocumentError(reason=f"unsupported schema {self.schema!r}, expected {SCHEMA!r}")

        self.parameters.evaluation_order()  # raises on cycles / unknown names

        known_frames = set(self.datums.planes) | set(DatumSet.WORLD_PLANES)
        for sketch in self.sketches.values():
            if sketch.plane not in known_frames:
                raise UnknownReferenceError(
                    kind="datum", identifier=sketch.plane, referenced_by=sketch.id
                )
            sketch.validate()

        if not self.bodies:
            raise DocumentError(reason="a document needs at least one body")

        seen_bodies: set[str] = set()
        seen_features: set[str] = set()
        known = {body.id for body in self.bodies}
        for body in self.bodies:
            if body.id in seen_bodies:
                raise DuplicateIdError(kind="body", identifier=body.id)
            seen_bodies.add(body.id)
            self._validate_copy(body, known)
            body.validate()

            for spec in body.features:
                if spec.id in seen_features:
                    raise DuplicateIdError(kind="feature", identifier=spec.id)
                seen_features.add(spec.id)
                if spec.profile is not None:
                    sketch = self.sketches.get(spec.profile.sketch)
                    if sketch is None:
                        raise UnknownReferenceError(
                            kind="sketch",
                            identifier=spec.profile.sketch,
                            referenced_by=spec.id,
                        )
                    sketch.loop(spec.profile.loop)

    def _validate_copy(self, body: Body, known: set[str]) -> None:
        """A copy must point at one real body, and only one hop away.

        Chains are refused rather than followed. Following them is a page of
        cycle detection to buy an expressiveness nobody asked for -- a copy of
        a copy is the same solid at the same placement, so it is a copy of the
        source, spelled the long way. Refusing keeps "where does this geometry
        come from" a question with a one-word answer.
        """
        if body.of is None:
            return
        if body.of == body.id:
            raise DocumentError(
                reason=f"body {body.id!r} cannot be a copy of itself",
                path=f"bodies.{body.id}.of",
            )
        if body.of not in known:
            raise UnknownReferenceError(
                kind="body", identifier=body.of, referenced_by=body.id
            )
        source = self.body(body.of)
        if source.is_copy:
            raise DocumentError(
                reason=(
                    f"body {body.id!r} copies {body.of!r}, which is itself a copy of "
                    f"{source.of!r}. Copy {source.of!r} directly — a copy of a copy is "
                    "the same solid, and one hop keeps the source unambiguous."
                ),
                path=f"bodies.{body.id}.of",
            )

    # -- dependency analysis ----------------------------------------------

    def features_depending_on(self, parameter: str) -> list[str]:
        """Which features would have to rebuild if ``parameter`` changed.

        Because the history is linear, everything downstream of the first
        affected feature must rebuild too — that closure is applied here so
        callers get the full dirty set in one call.
        """
        affected = {parameter} | self.parameters.dependents_of(parameter)
        first: int | None = None
        for index, spec in enumerate(self.features):
            uses = spec.parameter_dependencies() & affected
            if not uses and spec.profile is not None:
                sketch = self.sketches.get(spec.profile.sketch)
                if sketch is not None:
                    uses = sketch.parameter_dependencies() & affected
            if not uses:
                frame_id = self._frame_of(spec)
                plane = self.datums.planes.get(frame_id) if frame_id else None
                if plane is not None:
                    uses = plane.parameter_dependencies() & affected
            if uses:
                first = index
                break
        return [] if first is None else [f.id for f in self.features[first:]]

    def _frame_of(self, spec: FeatureSpec) -> str | None:
        if spec.profile is None:
            return None
        sketch = self.sketches.get(spec.profile.sketch)
        return sketch.plane if sketch else None

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "project": self.name,
            "parameters": self.parameters.to_list(),
            "datums": {
                identifier: plane.to_dict()
                for identifier, plane in self.datums.planes.items()
            },
            "sketches": {
                identifier: sketch.to_dict() for identifier, sketch in self.sketches.items()
            },
            **self._history_to_dict(),
        }

    def _history_to_dict(self) -> dict[str, object]:
        """Write a flat `features` list while there is only one plain body.

        Keeps single-body documents byte-identical to what they were before
        bodies existed, so nothing already on disk churns for a feature it does
        not use.
        """
        only = len(self.bodies) == 1
        if only and self.bodies[0].id == DEFAULT_BODY and self.bodies[0].placement.is_default:
            return {"features": [spec.to_dict() for spec in self.bodies[0].features]}
        return {"bodies": [body.to_dict() for body in self.bodies]}

    @staticmethod
    def from_dict(data: Mapping[str, object]) -> Document:
        if not isinstance(data, Mapping):
            raise DocumentError(reason="document must be a mapping")

        raw_parameters = data.get("parameters") or []
        parameters = ParameterSet(
            Parameter.from_dict(row) for row in _as_sequence(raw_parameters, "parameters")
        )

        raw_datums = data.get("datums") or {}
        datums = DatumSet(
            planes={
                str(identifier): DatumPlane.from_dict(
                    str(identifier), _as_mapping(value, f"datums.{identifier}")
                )
                for identifier, value in _as_mapping(raw_datums, "datums").items()
            }
        )

        raw_sketches = data.get("sketches") or {}
        sketches = {
            str(identifier): Sketch.from_dict(
                str(identifier), _as_mapping(value, f"sketches.{identifier}")
            )
            for identifier, value in _as_mapping(raw_sketches, "sketches").items()
        }

        # A document written before bodies existed has a flat feature list; it
        # reads as a single body so nothing already saved has to be migrated.
        if data.get("bodies") is not None:
            bodies = [
                Body.from_dict(_as_mapping(row, "bodies"))
                for row in _as_sequence(data.get("bodies") or [], "bodies")
            ]
        else:
            bodies = [
                Body(
                    id=DEFAULT_BODY,
                    features=[
                        FeatureSpec.from_dict(_as_mapping(row, "features"))
                        for row in _as_sequence(data.get("features") or [], "features")
                    ],
                )
            ]

        return Document(
            name=str(data.get("project", "untitled")),
            schema=str(data.get("schema", SCHEMA)),
            parameters=parameters,
            datums=datums,
            sketches=sketches,
            bodies=bodies or [Body(id=DEFAULT_BODY)],
        )


def _as_mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise DocumentError(reason=f"expected an object, got {type(value).__name__}", path=path)
    return value


def _as_sequence(value: object, path: str) -> Iterable[Mapping[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise DocumentError(reason=f"expected a list, got {type(value).__name__}", path=path)
    return [_as_mapping(item, path) for item in value]


# --------------------------------------------------------------------------
# Renaming a parameter throughout the document
# --------------------------------------------------------------------------


def _rename_value(value: Value, old: str, new: str) -> Value:
    return rename_reference(value, old, new) if isinstance(value, str) else value


def _rename_triple(
    values: tuple[Value, Value, Value] | None, old: str, new: str
) -> tuple[Value, Value, Value] | None:
    if values is None:
        return None
    a, b, c = (_rename_value(v, old, new) for v in values)
    return (a, b, c)


def _rename_in_datum(plane: DatumPlane, old: str, new: str) -> DatumPlane:
    return replace(
        plane,
        origin=_rename_triple(plane.origin, old, new),
        normal=_rename_triple(plane.normal, old, new),
        x_axis=_rename_triple(plane.x_axis, old, new),
    )


def _rename_in_sketch(sketch: Sketch, old: str, new: str) -> Sketch:
    points = tuple(
        replace(
            point,
            at=(_rename_value(point.at[0], old, new), _rename_value(point.at[1], old, new)),
        )
        for point in sketch.points
    )
    curves = tuple(
        replace(curve, radius=_rename_value(curve.radius, old, new))
        for curve in sketch.curves
    )
    return replace(sketch, points=points, curves=curves)


def _rename_in_feature(spec: FeatureSpec, old: str, new: str) -> FeatureSpec:
    return replace(
        spec,
        options={
            key: _rename_value(value, old, new) for key, value in spec.options.items()
        },
    )
