"""Generating a finger-jointed enclosure as flat panels.

Point a laser cutter at a part and what you usually want is not the part — it is
a box to put it in. This builds that box: six flat panels with interlocking
finger joints, laid out side by side on one sheet, ready to cut.

It is deliberately not a modelling feature. The panels are 2D from the start, so
this needs no kernel, runs instantly, and is testable as plain arithmetic. What
it shares with the rest of the system is the output type: :class:`Profile2D`,
the same thing a flattened face produces, so it reaches DXF and SVG through
exactly the same writers.

Conventions
-----------

*Outer dimensions.* ``width``, ``depth`` and ``height`` are the outside of the
assembled box, which is what a person measures and what a part has to fit
inside.

*Joint phase.* Every joint has two sides. One panel starts its edge with a tooth
at the outer line, the other starts with a gap — so the pair interlocks. Which
panel gets which is fixed by :data:`_PANELS` rather than decided per edge, so
the box always closes.

*Kerf.* A laser removes material as it cuts, so a tooth cut to nominal size ends
up loose. Every outward move is grown by half the kerf and every inward move
shrunk by the same, which makes the joint tight without changing the box's
outside dimensions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from facet.domain.errors import DocumentError
from facet.domain.math3d import Frame, Vec3

from .ports.geometry import Line2D, Loop2D, Profile2D

#: Gap between panels when they are laid out on one sheet (mm).
SHEET_GAP = 5.0


@dataclass(frozen=True)
class EnclosureSpec:
    """What the box should be. All lengths in millimetres."""

    width: float
    depth: float
    height: float
    thickness: float
    #: Target width of one tooth. The real width is adjusted so a whole number
    #: of teeth fits the edge.
    finger: float = 10.0
    #: Cut width of the laser. Half is added to every outward move.
    kerf: float = 0.15
    #: Space left around the part the box is built for.
    clearance: float = 0.0

    def validate(self) -> None:
        for name in ("width", "depth", "height", "thickness", "finger"):
            if getattr(self, name) <= 0:
                raise DocumentError(
                    reason=f"enclosure {name} must be positive, got {getattr(self, name):g}",
                    path="enclosure",
                )
        if self.kerf < 0:
            raise DocumentError(reason="kerf cannot be negative", path="enclosure")

        smallest = min(self.width, self.depth, self.height)
        if smallest <= 2 * self.thickness:
            raise DocumentError(
                reason=(
                    f"a {self.width:g}x{self.depth:g}x{self.height:g} box cannot be made "
                    f"from {self.thickness:g}mm material: the smallest dimension has to "
                    "exceed two thicknesses"
                ),
                path="enclosure",
            )
        if self.finger < self.thickness:
            raise DocumentError(
                reason=(
                    f"a {self.finger:g}mm finger in {self.thickness:g}mm material is "
                    "weaker than the material it joins; make the finger at least as "
                    "wide as the sheet is thick"
                ),
                path="enclosure",
            )


#: Which panels exist, their in-plane size, and the phase of each edge.
#:
#: Edges run bottom, right, top, left in the panel's own frame. ``True`` means
#: this panel starts that edge with a tooth; ``False`` means it starts with a
#: gap and the mating panel supplies the tooth. Every joint therefore pairs a
#: True edge with a False one, which is what makes the box close.
_PANELS: tuple[tuple[str, str, str, tuple[bool, bool, bool, bool]], ...] = (
    ("bottom", "width", "depth", (True, True, True, True)),
    ("top", "width", "depth", (True, True, True, True)),
    ("front", "width", "inner_height", (False, True, False, True)),
    ("back", "width", "inner_height", (False, True, False, True)),
    ("left", "inner_depth", "inner_height", (False, False, False, False)),
    ("right", "inner_depth", "inner_height", (False, False, False, False)),
)


def enclosure_panels(spec: EnclosureSpec) -> list[Profile2D]:
    """The six panels, laid out left to right on one sheet."""
    spec.validate()
    sizes = {
        "width": spec.width,
        "depth": spec.depth,
        "inner_depth": spec.depth - 2 * spec.thickness,
        "inner_height": spec.height - 2 * spec.thickness,
    }

    panels: list[Profile2D] = []
    offset = 0.0
    for name, across, up, phases in _PANELS:
        a, b = sizes[across], sizes[up]
        points = _outline(a, b, spec, phases)
        moved = [(x + offset, y) for x, y in points]
        panels.append(
            Profile2D(
                loops=(Loop2D(curves=tuple(_segments(moved))),),
                frame=Frame.from_origin_normal(Vec3(0.0, 0.0, 0.0), Vec3(0.0, 0.0, 1.0)),
                label=name,
            )
        )
        offset += a + SHEET_GAP
    return panels


def enclosure_for_bounds(
    minimum: tuple[float, float, float],
    maximum: tuple[float, float, float],
    thickness: float,
    finger: float = 10.0,
    kerf: float = 0.15,
    clearance: float = 2.0,
) -> EnclosureSpec:
    """A box that fits the given bounding box, with clearance and walls."""
    inner = [
        (hi - lo) + 2 * clearance for lo, hi in zip(minimum, maximum, strict=True)
    ]
    return EnclosureSpec(
        width=inner[0] + 2 * thickness,
        depth=inner[1] + 2 * thickness,
        height=inner[2] + 2 * thickness,
        thickness=thickness,
        finger=finger,
        kerf=kerf,
        clearance=clearance,
    )


# --------------------------------------------------------------------------
# Outline construction
# --------------------------------------------------------------------------


def _outline(
    width: float, height: float, spec: EnclosureSpec, phases: tuple[bool, bool, bool, bool]
) -> list[tuple[float, float]]:
    """One panel's closed outline, walked anticlockwise from the bottom left.

    The corners are the tricky part. Each edge begins and ends on the same phase
    (that is what the odd tooth count buys), so an edge is either "raised" at
    both ends — sitting on the outer line — or "recessed" at both ends, one
    thickness in. The panel's real corner is therefore the intersection of the
    two adjacent edges' *end* lines, not the corner of the nominal rectangle.

    Getting that wrong is what produced outlines that crossed themselves at the
    corners: each edge was walked between nominal corners, so a recessed edge
    left a stub hanging past the corner and the closing segment cut back across
    it. A laser follows that literally and drops the corner out as a loose
    triangle.
    """
    thickness = spec.thickness
    # How far each edge's ends sit in from the nominal rectangle.
    inset = [0.0 if raised else thickness for raised in phases]
    bottom, right, top, left = inset

    corners = [
        (left, bottom),
        (width - right, bottom),
        (width - right, height - top),
        (left, height - top),
    ]
    # Each edge alternates between the outer line and one thickness in, starting
    # on whichever its phase demands.
    depths = [
        (0.0, thickness) if raised else (thickness, 0.0) for raised in phases
    ]

    points: list[tuple[float, float]] = []
    for index in range(4):
        points.extend(
            _edge(
                corners[index],
                corners[(index + 1) % 4],
                spec,
                depths[index],
            )
        )
    return _without_repeats(points)


def _without_repeats(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Drop coincident neighbours, including where the outline closes.

    Adjacent edges meet exactly at a corner, so the walk naturally emits that
    point twice. Left in, it becomes a zero-length segment in the DXF — junk a
    controller may or may not tolerate, and noise in any check of the outline.
    """
    kept: list[tuple[float, float]] = []
    for point in points:
        if not kept or not _same(kept[-1], point):
            kept.append(point)
    while len(kept) > 1 and _same(kept[0], kept[-1]):
        kept.pop()
    return kept


def _same(a: tuple[float, float], b: tuple[float, float], tol: float = 1e-9) -> bool:
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def _edge(
    start: tuple[float, float],
    end: tuple[float, float],
    spec: EnclosureSpec,
    depths: tuple[float, float],
) -> list[tuple[float, float]]:
    """The zig-zag along one edge, from ``start`` up to but not including ``end``.

    ``depths`` is (depth of the first segment, depth of the others), each
    measured inward from the edge's outer line.
    """
    length = math.dist(start, end)
    if length <= 0:
        return []
    teeth = _tooth_count(length, spec.finger)
    step = length / teeth

    # Unit vector along the edge, and its inward normal for an anticlockwise walk.
    along = ((end[0] - start[0]) / length, (end[1] - start[1]) / length)
    inward = (-along[1], along[0])

    first, other = depths
    half = spec.kerf / 2.0
    points: list[tuple[float, float]] = []

    outer = min(first, other)
    for index in range(teeth):
        depth = first if index % 2 == 0 else other
        # Kerf: the segment standing proud grows and the recessed one shrinks,
        # so the pair presses together. Only the *internal* boundaries move —
        # the two ends of the edge are the panel's corners, and shifting those
        # would both resize the box and push a point past the corner, leaving a
        # diagonal that a laser cuts as written.
        grow = -half if depth == outer else half
        begin = index * step + (0.0 if index == 0 else grow)
        finish = (index + 1) * step - (0.0 if index == teeth - 1 else grow)

        points.append(_at(start, along, inward, begin, depth - first))
        points.append(_at(start, along, inward, finish, depth - first))
    return points


def _at(
    origin: tuple[float, float],
    along: tuple[float, float],
    inward: tuple[float, float],
    distance: float,
    depth: float,
) -> tuple[float, float]:
    return (
        origin[0] + along[0] * distance + inward[0] * depth,
        origin[1] + along[1] * distance + inward[1] * depth,
    )


def _tooth_count(length: float, finger: float) -> int:
    """An odd number of teeth, so an edge starts and ends on the same phase.

    Even counts leave one corner with a tooth and the other with a gap, which
    looks like a bug and fits like one.
    """
    count = max(3, round(length / finger))
    return count if count % 2 == 1 else count + 1


def _segments(points: list[tuple[float, float]]) -> list[Line2D]:
    return [
        Line2D(start=points[index], end=points[(index + 1) % len(points)])
        for index in range(len(points))
    ]
