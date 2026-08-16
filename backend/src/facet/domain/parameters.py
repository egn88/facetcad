"""The parameter sheet.

A parameter is either a literal value or an expression over other parameters.
Resolution is a topological walk that yields a flat ``name -> canonical float``
table; everything downstream — datums, sketches, feature specs — reads only that
table, so a parameter change has exactly one way to influence geometry.

Cycles are reported with the actual cycle path rather than a generic
"circular reference", because chasing one through a large sheet is otherwise
miserable.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace

from . import expressions, units
from .errors import (
    CircularDependencyError,
    DocumentError,
    DuplicateIdError,
    ExpressionError,
    UnknownParameterError,
)


@dataclass(frozen=True, slots=True)
class Parameter:
    """One row of the sheet.

    Exactly one of ``value`` or ``expr`` must be set. ``unit`` applies to a
    literal ``value``; expressions always operate on canonical numbers.
    """

    name: str
    value: float | None = None
    expr: str | None = None
    unit: str = "mm"
    group: str = ""
    doc: str = ""

    def __post_init__(self) -> None:
        if not self.name.isidentifier():
            raise DocumentError(reason=f"parameter name {self.name!r} must be an identifier")
        if (self.value is None) == (self.expr is None):
            raise DocumentError(
                reason=f"parameter '{self.name}' needs exactly one of 'value' or 'expr'",
                path=f"parameters.{self.name}",
            )
        units.quantity_of(self.unit)  # validates, raises DocumentError if unknown

    @property
    def is_computed(self) -> bool:
        return self.expr is not None

    @property
    def quantity(self) -> str:
        return units.quantity_of(self.unit)

    def canonical_literal(self) -> float:
        assert self.value is not None
        return units.to_canonical(self.value, self.unit)

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {"name": self.name}
        if self.expr is not None:
            data["expr"] = self.expr
        else:
            data["value"] = self.value
        if self.unit != "mm":
            data["unit"] = self.unit
        if self.group:
            data["group"] = self.group
        if self.doc:
            data["doc"] = self.doc
        return data

    @staticmethod
    def from_dict(data: Mapping[str, object]) -> Parameter:
        try:
            name = str(data["name"])
        except KeyError:
            raise DocumentError(reason="parameter is missing 'name'", path="parameters") from None
        raw_value = data.get("value")
        return Parameter(
            name=name,
            value=float(raw_value) if raw_value is not None else None,  # type: ignore[arg-type]
            expr=str(data["expr"]) if data.get("expr") is not None else None,
            unit=str(data.get("unit", "mm")),
            group=str(data.get("group", "")),
            doc=str(data.get("doc", "")),
        )


@dataclass(frozen=True, slots=True)
class ResolvedParameters:
    """The flat, canonical table every downstream consumer reads."""

    values: Mapping[str, float]
    order: tuple[str, ...] = field(default_factory=tuple)

    def __getitem__(self, name: str) -> float:
        try:
            return self.values[name]
        except KeyError:
            raise UnknownParameterError(name=name) from None

    def __contains__(self, name: str) -> bool:
        return name in self.values

    def __iter__(self) -> Iterator[str]:
        return iter(self.values)

    def as_dict(self) -> dict[str, float]:
        return dict(self.values)

    def fingerprint(self) -> tuple[tuple[str, float], ...]:
        """A hashable snapshot, used as part of a feature's cache key."""
        return tuple(sorted((name, round(v, 9)) for name, v in self.values.items()))


class ParameterSet:
    """An ordered collection of parameters that resolves to canonical values."""

    def __init__(self, parameters: Iterable[Parameter] = ()) -> None:
        self._parameters: dict[str, Parameter] = {}
        for parameter in parameters:
            self.add(parameter)

    # -- collection behaviour ---------------------------------------------

    def add(self, parameter: Parameter) -> None:
        if parameter.name in self._parameters:
            raise DuplicateIdError(kind="parameter", identifier=parameter.name)
        self._parameters[parameter.name] = parameter

    def replace(self, name: str, **changes: object) -> None:
        if name not in self._parameters:
            raise UnknownParameterError(name=name)
        self._parameters[name] = replace(self._parameters[name], **changes)  # type: ignore[arg-type]

    def rename(self, old: str, new: str) -> None:
        """Rename in place, keeping the row where the author put it.

        Order is part of how a sheet reads, so this rebuilds the mapping rather
        than deleting and appending, which would move the row to the bottom.
        """
        if old not in self._parameters:
            raise UnknownParameterError(name=old)
        if new in self._parameters:
            raise DuplicateIdError(kind="parameter", identifier=new)
        self._parameters = {
            (new if name == old else name): (
                replace(parameter, name=new) if name == old else parameter
            )
            for name, parameter in self._parameters.items()
        }

    def move(self, name: str, index: int) -> None:
        """Reposition a row within the sheet."""
        if name not in self._parameters:
            raise UnknownParameterError(name=name)
        order = [n for n in self._parameters if n != name]
        order.insert(max(0, min(index, len(order))), name)
        self._parameters = {n: self._parameters[n] for n in order}

    def remove(self, name: str) -> None:
        if name not in self._parameters:
            raise UnknownParameterError(name=name)
        del self._parameters[name]

    def __contains__(self, name: object) -> bool:
        return name in self._parameters

    def __getitem__(self, name: str) -> Parameter:
        try:
            return self._parameters[name]
        except KeyError:
            raise UnknownParameterError(name=name) from None

    def __len__(self) -> int:
        return len(self._parameters)

    def __iter__(self) -> Iterator[Parameter]:
        return iter(self._parameters.values())

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._parameters)

    def groups(self) -> dict[str, list[Parameter]]:
        grouped: dict[str, list[Parameter]] = {}
        for parameter in self._parameters.values():
            grouped.setdefault(parameter.group, []).append(parameter)
        return grouped

    # -- dependency analysis ----------------------------------------------

    def dependencies_of(self, name: str) -> frozenset[str]:
        parameter = self[name]
        if parameter.expr is None:
            return frozenset()
        try:
            return expressions.dependencies(expressions.parse(parameter.expr))
        except ExpressionError as exc:
            raise ExpressionError(
                expression=exc.expression, reason=exc.reason, parameter=name
            ) from exc

    def dependents_of(self, name: str) -> frozenset[str]:
        """Every parameter whose value would change if ``name`` changed.

        Transitive, so the recompute engine can dirty the right subtree in one
        call rather than iterating to a fixed point.
        """
        direct: dict[str, set[str]] = {p.name: set() for p in self}
        for parameter in self:
            for dependency in self.dependencies_of(parameter.name):
                if dependency in direct:
                    direct[dependency].add(parameter.name)

        reached: set[str] = set()
        frontier = [name]
        while frontier:
            current = frontier.pop()
            for consumer in direct.get(current, ()):
                if consumer not in reached:
                    reached.add(consumer)
                    frontier.append(consumer)
        return frozenset(reached)

    def evaluation_order(self) -> tuple[str, ...]:
        """Topologically sorted names, dependencies first."""
        WHITE, GREY, BLACK = 0, 1, 2
        colour = dict.fromkeys(self._parameters, WHITE)
        order: list[str] = []
        stack_path: list[str] = []

        def visit(name: str) -> None:
            if colour[name] == BLACK:
                return
            if colour[name] == GREY:
                cycle_start = stack_path.index(name)
                raise CircularDependencyError(cycle=tuple(stack_path[cycle_start:]))
            colour[name] = GREY
            stack_path.append(name)
            for dependency in sorted(self.dependencies_of(name)):
                if dependency not in self._parameters:
                    raise UnknownParameterError(name=dependency, referenced_by=name)
                visit(dependency)
            stack_path.pop()
            colour[name] = BLACK
            order.append(name)

        for name in self._parameters:
            visit(name)
        return tuple(order)

    # -- resolution --------------------------------------------------------

    def resolve(self) -> ResolvedParameters:
        """Evaluate every parameter into canonical units."""
        order = self.evaluation_order()
        values: dict[str, float] = {}
        for name in order:
            parameter = self._parameters[name]
            if parameter.expr is None:
                values[name] = parameter.canonical_literal()
            else:
                values[name] = expressions.evaluate_text(
                    parameter.expr, values, parameter=name
                )
        return ResolvedParameters(values=values, order=order)

    # -- serialisation -----------------------------------------------------

    def to_list(self) -> list[dict[str, object]]:
        return [p.to_dict() for p in self]

    @staticmethod
    def from_list(rows: Iterable[Mapping[str, object]]) -> ParameterSet:
        return ParameterSet(Parameter.from_dict(row) for row in rows)
