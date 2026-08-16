"""Laying a solid's faces out flat, for cutting.

Where :mod:`facet.application.enclosure` builds a *container* for a part,
this takes the part itself apart: every planar face becomes a panel, laid out
side by side on one sheet. A notched block gives ten panels, a wedge gives five.

Two things it deliberately does not do.

**It does not flatten curved faces.** A fillet or a bore has no development into
a plane, and projecting one anyway would produce a shape that is not the face.
Those are reported as skipped, by name, rather than dropped in silence.

**It does not add joints.** These are the faces as modelled, at their own size.
Butted together they enclose the original volume; they are a cutting list, not
an assembly. Finger joints belong to the enclosure generator, which knows the
material thickness because it was told.

Blend faces are excluded by default. A chamfer is a real planar face, but
nobody wants a 1mm sliver in a cutting list, and the tag says which faces those
are without any geometric guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from facet.domain.tags import FaceTag, Roles

from .ports.geometry import Arc2D, Line2D, Loop2D, Profile2D

#: Space left between panels on the sheet (mm).
SHEET_GAP = 8.0

#: Roles produced by a blend. Excluded unless asked for.
BLEND_ROLES = frozenset({Roles.FILLET, Roles.CHAMFER, Roles.CORNER})


@dataclass(frozen=True)
class FlattenResult:
    """The panels, plus an honest account of what could not be flattened."""

    panels: tuple[Profile2D, ...] = ()
    skipped: tuple[str, ...] = ()
    """Tags of faces that have no flat development, with the reason implied."""

    @property
    def is_empty(self) -> bool:
        return not self.panels


def is_blend(tag: FaceTag) -> bool:
    return tag.role in BLEND_ROLES


def lay_out(profiles: list[Profile2D], gap: float = SHEET_GAP) -> list[Profile2D]:
    """Move each panel so they sit in a row, left to right, none overlapping.

    Faces are modelled wherever they happen to be in space; flattened, several
    would land on top of each other. Laying them out is what makes the file
    cuttable as it stands.
    """
    placed: list[Profile2D] = []
    cursor = 0.0
    for profile in profiles:
        bounds = _bounds(profile)
        if bounds is None:
            continue
        min_x, min_y, max_x, _ = bounds
        placed.append(_moved(profile, cursor - min_x, -min_y))
        cursor += (max_x - min_x) + gap
    return placed


# --------------------------------------------------------------------------
# Moving a profile
# --------------------------------------------------------------------------


def _moved(profile: Profile2D, dx: float, dy: float) -> Profile2D:
    return replace(
        profile,
        loops=tuple(
            Loop2D(
                curves=tuple(_shift(curve, dx, dy) for curve in loop.curves),
                outer=loop.outer,
            )
            for loop in profile.loops
        ),
    )


def _shift(curve: Line2D | Arc2D, dx: float, dy: float) -> Line2D | Arc2D:
    """Translate a run, keeping which model edge it came from.

    The edge ref has to survive layout: it is how two panels that meet along an
    edge recognise each other when joints are cut.
    """
    if isinstance(curve, Line2D):
        return replace(
            curve,
            start=(curve.start[0] + dx, curve.start[1] + dy),
            end=(curve.end[0] + dx, curve.end[1] + dy),
        )
    return replace(curve, centre=(curve.centre[0] + dx, curve.centre[1] + dy))


def _bounds(profile: Profile2D) -> tuple[float, float, float, float] | None:
    xs: list[float] = []
    ys: list[float] = []
    for loop in profile.loops:
        for curve in loop.curves:
            if isinstance(curve, Line2D):
                xs.extend((curve.start[0], curve.end[0]))
                ys.extend((curve.start[1], curve.end[1]))
            else:
                xs.extend((curve.centre[0] - curve.radius, curve.centre[0] + curve.radius))
                ys.extend((curve.centre[1] - curve.radius, curve.centre[1] + curve.radius))
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))
