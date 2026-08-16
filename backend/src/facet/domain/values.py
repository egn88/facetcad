"""Document values: a literal number, or an expression over parameters.

Every dimension in the document is a :data:`Value`. Writing ``6`` and writing
``"plate_t"`` are the same kind of thing, which is what makes the parameter
sheet the single source of truth — there is no second, non-parametric way to
state a dimension.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from . import expressions
from .errors import DocumentError, ExpressionError
from .math3d import Vec2, Vec3
from .parameters import ResolvedParameters

#: A number, or an expression to evaluate against the parameter table.
Value = float | int | str


def resolve(value: Value, parameters: ResolvedParameters, *, where: str = "") -> float:
    """Evaluate a document value to a canonical number."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return expressions.evaluate_text(value, parameters.values)
        except ExpressionError as exc:
            raise ExpressionError(
                expression=exc.expression, reason=exc.reason, parameter=where or None
            ) from exc
    raise DocumentError(
        reason=f"expected a number or expression, got {type(value).__name__}", path=where
    )


def resolve_vec3(
    values: Sequence[Value], parameters: ResolvedParameters, *, where: str = ""
) -> Vec3:
    if len(values) != 3:
        raise DocumentError(reason=f"expected 3 components, got {len(values)}", path=where)
    x, y, z = (resolve(v, parameters, where=f"{where}[{i}]") for i, v in enumerate(values))
    return Vec3(x, y, z)


def resolve_vec2(
    values: Sequence[Value], parameters: ResolvedParameters, *, where: str = ""
) -> Vec2:
    if len(values) != 2:
        raise DocumentError(reason=f"expected 2 components, got {len(values)}", path=where)
    u, v = (resolve(value, parameters, where=f"{where}[{i}]") for i, value in enumerate(values))
    return Vec2(u, v)


def dependencies(value: Value) -> frozenset[str]:
    """Parameter names a value reads, for dirty propagation."""
    if isinstance(value, str):
        try:
            return expressions.dependencies(expressions.parse(value))
        except ExpressionError:
            return frozenset()
    return frozenset()


def dependencies_of_many(values: Sequence[Value] | Mapping[str, Value]) -> frozenset[str]:
    items = values.values() if isinstance(values, Mapping) else values
    result: frozenset[str] = frozenset()
    for value in items:
        result |= dependencies(value)
    return result
