"""Shared profile builders for kernel conformance tests."""

from __future__ import annotations

from facet.application.ports.geometry import (
    CurveType,
    Profile,
    ProfileCurve,
)
from facet.domain.math3d import Frame, Vec2, Vec3

#: The four sides of a rectangular profile, in loop order.
RECT_CURVE_IDS = ("bottom", "right", "top", "left")


def rectangle(
    sketch: str,
    width: float,
    height: float,
    *,
    x0: float = 0.0,
    y0: float = 0.0,
    z: float = 0.0,
    loop: str = "outer",
) -> Profile:
    """A closed axis-aligned rectangle on a plane parallel to XY at height ``z``."""
    corners = [
        Vec2(x0, y0),
        Vec2(x0 + width, y0),
        Vec2(x0 + width, y0 + height),
        Vec2(x0, y0 + height),
    ]
    curves = tuple(
        ProfileCurve(
            id=RECT_CURVE_IDS[index],
            type=CurveType.LINE,
            start=corners[index],
            end=corners[(index + 1) % 4],
        )
        for index in range(4)
    )
    frame = Frame.world().with_origin(Vec3(0.0, 0.0, z))
    return Profile(sketch=sketch, loop=loop, frame=frame, curves=curves)
