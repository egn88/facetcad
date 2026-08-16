"""Feature specifications — the ordered history.

Features form a **linear history**, as in PartDesign or SolidWorks, rather than a
free DAG. Each feature consumes the solid the previous one produced. This is a
deliberate simplification: it matches how the tree reads in the UI, makes
reordering a first-class operation, and keeps "what does this feature see?"
answerable by looking one step back.

Specs are data only. Building them is the job of a handler in
:mod:`facet.application.features`, registered by type name so a new feature
never edits existing code.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .errors import DocumentError
from .selectors import FaceSelector
from .values import Value, dependencies_of_many


@dataclass(frozen=True)
class ProfileRef:
    """A reference to a closed loop within a sketch, written ``sketch.loop``."""

    sketch: str
    loop: str

    @staticmethod
    def parse(text: str) -> ProfileRef:
        parts = text.split(".")
        if len(parts) != 2 or not all(p.isidentifier() for p in parts):
            raise DocumentError(reason=f"profile must be written 'sketch.loop', got {text!r}")
        return ProfileRef(sketch=parts[0], loop=parts[1])

    def __str__(self) -> str:
        return f"{self.sketch}.{self.loop}"


@dataclass(frozen=True)
class FeatureSpec:
    """Common shape of every feature in the history.

    Type-specific fields live in ``options`` rather than in subclasses, so the
    document schema, the API and the registry all stay open to new feature types
    without a class hierarchy to extend in lockstep.
    """

    id: str
    type: str
    profile: ProfileRef | None = None
    options: Mapping[str, Value] = field(default_factory=dict)
    targets: Mapping[str, FaceSelector] = field(default_factory=dict)
    suppressed: bool = False
    doc: str = ""

    def __post_init__(self) -> None:
        if not self.id.isidentifier():
            raise DocumentError(reason=f"feature id {self.id!r} must be an identifier")
        if not self.type:
            raise DocumentError(reason=f"feature '{self.id}' has no type")

    # -- typed access ------------------------------------------------------

    def option(self, name: str, default: Value | None = None) -> Value:
        if name in self.options:
            return self.options[name]
        if default is not None:
            return default
        raise DocumentError(
            reason=f"feature '{self.id}' ({self.type}) is missing option '{name}'",
            path=f"features.{self.id}",
        )

    def flag(self, name: str, default: bool = False) -> bool:
        raw = self.options.get(name, default)
        return bool(raw)

    def integer(self, name: str, default: int) -> int:
        raw = self.options.get(name, default)
        try:
            return int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise DocumentError(
                reason=f"option '{name}' of feature '{self.id}' must be a whole number",
                path=f"features.{self.id}",
            ) from None

    def parameter_dependencies(self) -> frozenset[str]:
        return dependencies_of_many(dict(self.options))

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {"id": self.id, "type": self.type}
        if self.profile is not None:
            data["profile"] = str(self.profile)
        data.update({key: value for key, value in self.options.items()})
        if self.targets:
            data["targets"] = {
                name: selector.to_dict() for name, selector in self.targets.items()
            }
        if self.suppressed:
            data["suppressed"] = True
        if self.doc:
            data["doc"] = self.doc
        return data

    @staticmethod
    def from_dict(data: Mapping[str, object]) -> FeatureSpec:
        reserved = {"id", "type", "profile", "targets", "suppressed", "doc"}
        try:
            identifier = str(data["id"])
            kind = str(data["type"])
        except KeyError as exc:
            raise DocumentError(reason=f"feature is missing {exc}", path="features") from None

        raw_targets = data.get("targets") or {}
        targets = {
            str(name): _selector_from(value, identifier, str(name))
            for name, value in raw_targets.items()  # type: ignore[union-attr]
        }
        return FeatureSpec(
            id=identifier,
            type=kind,
            profile=ProfileRef.parse(str(data["profile"])) if data.get("profile") else None,
            options={
                key: value  # type: ignore[misc]
                for key, value in data.items()
                if key not in reserved
            },
            targets=targets,
            suppressed=bool(data.get("suppressed", False)),
            doc=str(data.get("doc", "")),
        )


def _selector_from(raw: object, feature: str, name: str) -> FaceSelector:
    """Accept either the shorthand string or the canonical structured form."""
    if isinstance(raw, str):
        return FaceSelector.parse(raw)
    if isinstance(raw, Mapping):
        return FaceSelector.from_dict(dict(raw))
    raise DocumentError(
        reason="a target must be a selector string or object",
        path=f"features.{feature}.targets.{name}",
    )
