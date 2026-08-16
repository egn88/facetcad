"""Bodies: one solid each, positioned in the document.

A body owns an ordered feature history and produces exactly one solid, as
PartDesign does. Within a body, features chain — a second pad fuses into what
is already there. Across bodies, solids stay separate, which is what makes an
assembly possible: you cannot join two things that have been merged into one.

Parameters, datums and sketches stay document-wide rather than per body,
because the whole point of the tool is that a hole and the pin passing through
it share a diameter.

Placement is deliberately **not** applied to the modelled geometry. A body is
built in its own coordinates and placed for display and export, so moving a
body cannot perturb a face fingerprint or a split ordinal. When joints arrive
they drive these values rather than introducing a new concept.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .errors import DocumentError, DuplicateIdError, UnknownReferenceError
from .features import FeatureSpec
from .math3d import Frame, Vec3
from .parameters import ResolvedParameters
from .values import Value, dependencies_of_many, resolve_vec3

#: The body a flat, pre-bodies document is read as.
DEFAULT_BODY = "main"


@dataclass(frozen=True)
class Placement:
    """Where a body sits, as parameters like everything else."""

    origin: tuple[Value, Value, Value] = (0.0, 0.0, 0.0)
    #: Intrinsic X, Y, Z rotation in degrees.
    rotation: tuple[Value, Value, Value] = (0.0, 0.0, 0.0)

    @property
    def is_default(self) -> bool:
        return all(_is_zero(v) for v in (*self.origin, *self.rotation))

    def parameter_dependencies(self) -> frozenset[str]:
        return dependencies_of_many([*self.origin, *self.rotation])

    def resolve(self, parameters: ResolvedParameters, body: str) -> Frame:
        where = f"bodies.{body}.placement"
        origin = resolve_vec3(self.origin, parameters, where=f"{where}.origin")
        rotation = resolve_vec3(self.rotation, parameters, where=f"{where}.rotation")
        return Frame.from_euler(origin, rotation.x, rotation.y, rotation.z)

    def to_dict(self) -> dict[str, object]:
        return {"origin": list(self.origin), "rotation": list(self.rotation)}

    @staticmethod
    def from_dict(data: Mapping[str, object], body: str) -> Placement:
        return Placement(
            origin=_triple(data.get("origin", [0, 0, 0]), body, "origin"),
            rotation=_triple(data.get("rotation", [0, 0, 0]), body, "rotation"),
        )


@dataclass
class Body:
    """One solid, built by its own ordered feature history."""

    id: str
    features: list[FeatureSpec] = field(default_factory=list)
    placement: Placement = field(default_factory=Placement)
    doc: str = ""

    def __post_init__(self) -> None:
        if not self.id.isidentifier():
            raise DocumentError(reason=f"body id {self.id!r} must be an identifier")

    # -- features ----------------------------------------------------------

    def feature(self, identifier: str) -> FeatureSpec:
        found = next((f for f in self.features if f.id == identifier), None)
        if found is None:
            raise UnknownReferenceError(
                kind="feature", identifier=identifier, referenced_by=self.id
            )
        return found

    def feature_index(self, identifier: str) -> int:
        for index, spec in enumerate(self.features):
            if spec.id == identifier:
                return index
        raise UnknownReferenceError(
            kind="feature", identifier=identifier, referenced_by=self.id
        )

    def add_feature(self, spec: FeatureSpec, at: int | None = None) -> None:
        if any(f.id == spec.id for f in self.features):
            raise DuplicateIdError(kind="feature", identifier=spec.id)
        if at is None:
            self.features.append(spec)
        else:
            self.features.insert(at, spec)

    def replace_feature(self, spec: FeatureSpec) -> None:
        self.features[self.feature_index(spec.id)] = spec

    def remove_feature(self, identifier: str) -> FeatureSpec:
        return self.features.pop(self.feature_index(identifier))

    def reorder_features(self, order: Sequence[str]) -> None:
        if sorted(order) != sorted(f.id for f in self.features):
            raise DocumentError(
                reason="a reorder must list every feature exactly once",
                path=f"bodies.{self.id}.features",
            )
        by_id = {f.id: f for f in self.features}
        self.features = [by_id[identifier] for identifier in order]

    def validate(self) -> None:
        seen: set[str] = set()
        for spec in self.features:
            if spec.id in seen:
                raise DuplicateIdError(kind="feature", identifier=spec.id)
            seen.add(spec.id)

    def parameter_dependencies(self) -> frozenset[str]:
        return self.placement.parameter_dependencies()

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "id": self.id,
            "features": [spec.to_dict() for spec in self.features],
        }
        if not self.placement.is_default:
            data["placement"] = self.placement.to_dict()
        if self.doc:
            data["doc"] = self.doc
        return data

    @staticmethod
    def from_dict(data: Mapping[str, object]) -> Body:
        try:
            identifier = str(data["id"])
        except KeyError:
            raise DocumentError(reason="body is missing 'id'", path="bodies") from None

        raw_features = data.get("features") or []
        if not isinstance(raw_features, Sequence) or isinstance(raw_features, (str, bytes)):
            raise DocumentError(
                reason="features must be a list", path=f"bodies.{identifier}.features"
            )
        raw_placement = data.get("placement")
        return Body(
            id=identifier,
            features=[FeatureSpec.from_dict(row) for row in raw_features],  # type: ignore[arg-type]
            placement=(
                Placement.from_dict(raw_placement, identifier)  # type: ignore[arg-type]
                if isinstance(raw_placement, Mapping)
                else Placement()
            ),
            doc=str(data.get("doc", "")),
        )


def _is_zero(value: Value) -> bool:
    return isinstance(value, (int, float)) and abs(float(value)) < 1e-12


def _triple(raw: object, body: str, field_name: str) -> tuple[Value, Value, Value]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        raise DocumentError(
            reason=f"{field_name} must be a list of three values",
            path=f"bodies.{body}.placement.{field_name}",
        )
    return (raw[0], raw[1], raw[2])  # type: ignore[return-value]


def identity_frame() -> Frame:
    return Frame(Vec3.zero(), Vec3(1, 0, 0), Vec3(0, 1, 0), Vec3(0, 0, 1))
