"""Unit handling.

The canonical internal units are **millimetres** and **degrees**. Everything
inside the domain, every kernel call and every stored fingerprint uses them.
A parameter may declare a different input unit, in which case its literal value
is converted on the way in — so typing ``0.5 in`` yields 12.7 internally and
every downstream formula stays unit-free.

Expressions are therefore always evaluated over canonical numbers. That keeps
the expression language simple (no dimensional analysis) at the cost of
requiring the document to be explicit about a parameter's input unit.
"""

from __future__ import annotations

import math

from .errors import DocumentError


class Quantity:
    LENGTH = "length"
    ANGLE = "angle"
    SCALAR = "scalar"


#: unit name -> (quantity, factor to canonical)
_UNITS: dict[str, tuple[str, float]] = {
    # length -> mm
    "mm": (Quantity.LENGTH, 1.0),
    "cm": (Quantity.LENGTH, 10.0),
    "m": (Quantity.LENGTH, 1000.0),
    "um": (Quantity.LENGTH, 0.001),
    "in": (Quantity.LENGTH, 25.4),
    "ft": (Quantity.LENGTH, 304.8),
    "mil": (Quantity.LENGTH, 0.0254),
    # angle -> deg
    "deg": (Quantity.ANGLE, 1.0),
    "rad": (Quantity.ANGLE, 180.0 / math.pi),
    "turn": (Quantity.ANGLE, 360.0),
    # dimensionless
    "": (Quantity.SCALAR, 1.0),
    "x": (Quantity.SCALAR, 1.0),
    "count": (Quantity.SCALAR, 1.0),
}

CANONICAL = {Quantity.LENGTH: "mm", Quantity.ANGLE: "deg", Quantity.SCALAR: ""}


def known_units() -> tuple[str, ...]:
    return tuple(sorted(u for u in _UNITS if u))


def quantity_of(unit: str) -> str:
    return _lookup(unit)[0]


def to_canonical(value: float, unit: str) -> float:
    """Convert a value in ``unit`` into mm / deg / dimensionless."""
    return value * _lookup(unit)[1]


def from_canonical(value: float, unit: str) -> float:
    """Convert a canonical value back into ``unit``, for display."""
    return value / _lookup(unit)[1]


def _lookup(unit: str) -> tuple[str, float]:
    key = (unit or "").strip().lower()
    try:
        return _UNITS[key]
    except KeyError:
        raise DocumentError(
            reason=f"unknown unit {unit!r}; expected one of {', '.join(known_units())}"
        ) from None
