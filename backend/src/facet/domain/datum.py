"""Datum planes — the only thing a sketch may attach to.

The rule that removes relative-direction failures
--------------------------------------------------

A datum is defined by an origin, a normal and an in-plane X axis, each computed
**only from parameters and other datums**. It is never derived from picked
topology. Sketches attach to datums and nothing else.

The consequence is that no sketch orientation can ever flip because a face's
underlying surface changed sense during a rebuild — the failure mode that makes
face-attached sketches in FreeCAD so fragile. The cost is that datums must be
declared explicitly, which for a sheet-driven workflow is a feature.

A datum may be expressed in another datum's frame via ``parent``. That is still
deterministic, because the parent is itself parameter-derived; the chain
bottoms out at the world frame.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .errors import DocumentError, UnknownReferenceError
from .math3d import Frame, Vec3
from .parameters import ResolvedParameters
from .values import Value, dependencies_of_many, resolve_vec3


@dataclass(frozen=True)
class DatumPlane:
    """A named coordinate frame, computed from parameters."""

    id: str
    origin: tuple[Value, Value, Value] = (0.0, 0.0, 0.0)
    normal: tuple[Value, Value, Value] = (0.0, 0.0, 1.0)
    x_axis: tuple[Value, Value, Value] | None = None
    parent: str | None = None
    doc: str = ""

    def __post_init__(self) -> None:
        if not self.id.isidentifier():
            raise DocumentError(reason=f"datum id {self.id!r} must be an identifier")
        # A normal given as literal zeros can never define a plane, whatever the
        # parameters say, so it is worth catching before the geometry is built.
        literals = [v for v in self.normal if isinstance(v, (int, float))]
        if len(literals) == 3 and all(abs(float(v)) < 1e-12 for v in literals):
            raise DocumentError(
                reason=f"datum '{self.id}' has a zero-length normal",
                path=f"datums.{self.id}.normal",
            )

    def parameter_dependencies(self) -> frozenset[str]:
        values = [*self.origin, *self.normal, *(self.x_axis or ())]
        return dependencies_of_many(values)

    def resolve(
        self, parameters: ResolvedParameters, frames: Mapping[str, Frame]
    ) -> Frame:
        """Compute the world frame for this datum."""
        where = f"datums.{self.id}"
        origin = resolve_vec3(self.origin, parameters, where=f"{where}.origin")
        normal = resolve_vec3(self.normal, parameters, where=f"{where}.normal")
        x_hint = (
            resolve_vec3(self.x_axis, parameters, where=f"{where}.x_axis")
            if self.x_axis is not None
            else None
        )

        if normal.length() < 1e-9:
            raise DocumentError(reason="datum normal cannot be zero-length", path=where)

        if self.parent is not None:
            parent_frame = frames.get(self.parent)
            if parent_frame is None:
                raise UnknownReferenceError(
                    kind="datum", identifier=self.parent, referenced_by=self.id
                )
            origin = parent_frame.to_world(origin)
            normal = parent_frame.direction_to_world(normal)
            if x_hint is not None:
                x_hint = parent_frame.direction_to_world(x_hint)

        try:
            return Frame.from_origin_normal(origin, normal, x_hint)
        except ValueError as exc:
            raise DocumentError(reason=str(exc), path=where) from exc

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "type": "plane",
            "origin": list(self.origin),
            "normal": list(self.normal),
        }
        if self.x_axis is not None:
            data["x_axis"] = list(self.x_axis)
        if self.parent is not None:
            data["parent"] = self.parent
        if self.doc:
            data["doc"] = self.doc
        return data

    @staticmethod
    def from_dict(identifier: str, data: Mapping[str, object]) -> DatumPlane:
        kind = str(data.get("type", "plane"))
        if kind != "plane":
            raise DocumentError(
                reason=f"unsupported datum type {kind!r}; only 'plane' is available",
                path=f"datums.{identifier}",
            )
        return DatumPlane(
            id=identifier,
            origin=_triple(data.get("origin", [0, 0, 0]), identifier, "origin"),
            normal=_triple(data.get("normal", [0, 0, 1]), identifier, "normal"),
            x_axis=(
                _triple(data["x_axis"], identifier, "x_axis")
                if data.get("x_axis") is not None
                else None
            ),
            parent=str(data["parent"]) if data.get("parent") else None,
            doc=str(data.get("doc", "")),
        )


def _triple(raw: object, identifier: str, field_name: str) -> tuple[Value, Value, Value]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        raise DocumentError(
            reason=f"{field_name} must be a list of three values",
            path=f"datums.{identifier}.{field_name}",
        )
    return (raw[0], raw[1], raw[2])  # type: ignore[return-value]


@dataclass(frozen=True)
class DatumSet:
    """The document's datums, resolved in dependency order."""

    planes: dict[str, DatumPlane] = field(default_factory=dict)

    #: Always available, so a document need not declare the obvious.
    WORLD_PLANES = ("xy", "xz", "yz")

    def resolve_all(self, parameters: ResolvedParameters) -> dict[str, Frame]:
        frames: dict[str, Frame] = _standard_frames()
        for identifier in self._ordered_ids():
            frames[identifier] = self.planes[identifier].resolve(parameters, frames)
        return frames

    def _ordered_ids(self) -> list[str]:
        """Topological order over the ``parent`` relation."""
        WHITE, GREY, BLACK = 0, 1, 2
        colour = dict.fromkeys(self.planes, WHITE)
        order: list[str] = []
        path: list[str] = []

        def visit(identifier: str) -> None:
            if colour.get(identifier, BLACK) == BLACK:
                return
            if colour[identifier] == GREY:
                cycle = " -> ".join([*path[path.index(identifier):], identifier])
                raise DocumentError(reason=f"circular datum parent chain: {cycle}")
            colour[identifier] = GREY
            path.append(identifier)
            parent = self.planes[identifier].parent
            if parent is not None and parent in self.planes:
                visit(parent)
            path.pop()
            colour[identifier] = BLACK
            order.append(identifier)

        for identifier in self.planes:
            visit(identifier)
        return order


def _standard_frames() -> dict[str, Frame]:
    """The three world planes, available in every document without declaration."""
    return {
        "xy": Frame.world(),
        "xz": Frame.from_origin_normal(Vec3.zero(), Vec3(0, -1, 0), Vec3(1, 0, 0)),
        "yz": Frame.from_origin_normal(Vec3.zero(), Vec3(1, 0, 0), Vec3(0, 1, 0)),
    }
