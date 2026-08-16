"""Writing 2D profiles out as DXF and SVG.

These are the files a CNC router, a laser cutter or a waterjet actually eats.
Both writers are pure functions over :class:`Profile2D`, with no kernel types in
sight, so they are testable without OCCT and reusable by any adapter that can
produce a flattened face.

Two deliberate choices:

* **Arcs stay arcs.** A controller cuts a real arc smoothly and a chain of
  chords audibly. Flattening everything to polylines would be simpler here and
  worse everywhere downstream.
* **DXF is written as R12.** It is the plainest dialect there is — no classes,
  no object section, no handles — and every machine and every CAM package on
  earth reads it. Later dialects buy features this file does not need.

Millimetres throughout, matching the rest of the document.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

from facet.application.ports.geometry import Arc2D, Curve2D, Line2D, Loop2D, Profile2D
from facet.domain.errors import DocumentError

#: Formats this module can write, for capability checks and error messages.
DRAWING_FORMATS = ("dxf", "svg")


def export_drawing(profiles: Sequence[Profile2D], fmt: str, *, title: str = "") -> bytes:
    """Write one or more profiles in ``fmt``."""
    normalized = fmt.lower().lstrip(".")
    if normalized == "dxf":
        return _dxf(profiles).encode("ascii", errors="replace")
    if normalized == "svg":
        return _svg(profiles, title=title).encode("utf-8")
    raise DocumentError(
        reason=(
            f"unsupported drawing format {fmt!r}; this build writes "
            f"{', '.join(DRAWING_FORMATS)}"
        ),
        path="export",
    )


# --------------------------------------------------------------------------
# DXF (AutoCAD R12 ASCII)
# --------------------------------------------------------------------------


def _dxf(profiles: Sequence[Profile2D]) -> str:
    out: list[str] = []

    def pair(code: int, value: object) -> None:
        out.append(str(code))
        out.append(f"{value:.6f}" if isinstance(value, float) else str(value))

    layers = _layer_names(profiles)

    # R12 tolerates a missing TABLES section, but naming the layers up front is
    # what lets an operator switch cut order on the machine.
    pair(0, "SECTION")
    pair(2, "TABLES")
    pair(0, "TABLE")
    pair(2, "LAYER")
    pair(70, len(layers))
    for name in layers:
        pair(0, "LAYER")
        pair(2, name)
        pair(70, 0)
        pair(62, 7)
        pair(6, "CONTINUOUS")
    pair(0, "ENDTAB")
    pair(0, "ENDSEC")

    pair(0, "SECTION")
    pair(2, "ENTITIES")
    for profile, layer in zip(profiles, layers, strict=True):
        for loop in profile.loops:
            for curve in loop.curves:
                _dxf_curve(pair, curve, layer)
    pair(0, "ENDSEC")
    pair(0, "EOF")
    return "\n".join(out) + "\n"


def _dxf_curve(pair, curve: Curve2D, layer: str) -> None:
    if isinstance(curve, Line2D):
        pair(0, "LINE")
        pair(8, layer)
        pair(10, float(curve.start[0]))
        pair(20, float(curve.start[1]))
        pair(30, 0.0)
        pair(11, float(curve.end[0]))
        pair(21, float(curve.end[1]))
        pair(31, 0.0)
        return

    if curve.full_circle:
        pair(0, "CIRCLE")
        pair(8, layer)
        pair(10, float(curve.centre[0]))
        pair(20, float(curve.centre[1]))
        pair(30, 0.0)
        pair(40, float(curve.radius))
        return

    # DXF arcs are always counter-clockwise from start to end, so a clockwise
    # arc is written by swapping its ends rather than by negating anything.
    start, end = _dxf_angles(curve)
    pair(0, "ARC")
    pair(8, layer)
    pair(10, float(curve.centre[0]))
    pair(20, float(curve.centre[1]))
    pair(30, 0.0)
    pair(40, float(curve.radius))
    pair(50, start)
    pair(51, end)


def _dxf_angles(arc: Arc2D) -> tuple[float, float]:
    if arc.ccw:
        return (arc.start_angle % 360.0, arc.end_angle % 360.0)
    return (arc.end_angle % 360.0, arc.start_angle % 360.0)


def _layer_names(profiles: Sequence[Profile2D]) -> list[str]:
    """One layer per profile, from its label, unique and DXF-safe."""
    names: list[str] = []
    taken: set[str] = set()
    for index, profile in enumerate(profiles):
        base = _sanitize_layer(profile.label) or f"profile_{index}"
        name = base
        suffix = 1
        while name in taken:
            suffix += 1
            name = f"{base}_{suffix}"
        taken.add(name)
        names.append(name)
    return names


#: DXF R12 layer names forbid these; everything else we pass through.
_LAYER_FORBIDDEN = set('<>/\\":;?*|=`,^[]# ')


def _sanitize_layer(text: str) -> str:
    cleaned = "".join("_" if ch in _LAYER_FORBIDDEN else ch for ch in text.strip())
    return cleaned[:31]


# --------------------------------------------------------------------------
# SVG
# --------------------------------------------------------------------------

#: Laser and router software conventionally reads hairline strokes as "cut".
_STROKE = "#000000"
_STROKE_WIDTH = 0.1
_MARGIN = 5.0


def _svg(profiles: Sequence[Profile2D], *, title: str = "") -> str:
    box = _bounds(profiles)
    if box is None:
        width = height = 10.0
        min_x = min_y = 0.0
    else:
        min_x, min_y, max_x, max_y = box
        width = max(max_x - min_x, 1e-6)
        height = max(max_y - min_y, 1e-6)

    min_x -= _MARGIN
    min_y -= _MARGIN
    width += 2 * _MARGIN
    height += 2 * _MARGIN

    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
            f'width="{width:.4f}mm" height="{height:.4f}mm" '
            f'viewBox="0 0 {width:.4f} {height:.4f}">'
        ),
    ]
    if title:
        out.append(f"  <title>{_escape(title)}</title>")
    # SVG's y axis points down and the model's points up. One flip on the group
    # keeps every path below in model coordinates, so nothing else has to care.
    out.append(
        f'  <g transform="translate({-min_x:.6f},{max_y + _MARGIN if box else height:.6f}) '
        f'scale(1,-1)" fill="none" stroke="{_STROKE}" '
        f'stroke-width="{_STROKE_WIDTH}">'
    )
    for profile in profiles:
        if profile.label:
            out.append(f"    <g id=\"{_escape(profile.label)}\">")
        for loop in profile.loops:
            path = _svg_path(loop)
            if path:
                out.append(f'      <path d="{path}"/>')
        if profile.label:
            out.append("    </g>")
    out.append("  </g>")
    out.append("</svg>")
    return "\n".join(out) + "\n"


def _svg_path(loop: Loop2D) -> str:
    if not loop.curves:
        return ""
    parts: list[str] = []
    cursor: tuple[float, float] | None = None

    for curve in loop.curves:
        start, end = _endpoints(curve)
        if cursor is None or not _close(cursor, start):
            parts.append(f"M {start[0]:.6f} {start[1]:.6f}")
        if isinstance(curve, Line2D):
            parts.append(f"L {end[0]:.6f} {end[1]:.6f}")
        elif curve.full_circle:
            # SVG has no circle command inside a path, so a full circle is two
            # half arcs; one arc of 360° is degenerate and renders as nothing.
            cx, cy = curve.centre
            r = curve.radius
            parts.append(f"M {cx + r:.6f} {cy:.6f}")
            parts.append(f"A {r:.6f} {r:.6f} 0 0 1 {cx - r:.6f} {cy:.6f}")
            parts.append(f"A {r:.6f} {r:.6f} 0 0 1 {cx + r:.6f} {cy:.6f}")
            end = (cx + r, cy)
        else:
            sweep = 1 if curve.ccw else 0
            large = 1 if _swept_angle(curve) > 180.0 else 0
            parts.append(
                f"A {curve.radius:.6f} {curve.radius:.6f} 0 {large} {sweep} "
                f"{end[0]:.6f} {end[1]:.6f}"
            )
        cursor = end

    return " ".join(parts)


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# --------------------------------------------------------------------------
# Shared geometry helpers
# --------------------------------------------------------------------------


def _endpoints(curve: Curve2D) -> tuple[tuple[float, float], tuple[float, float]]:
    if isinstance(curve, Line2D):
        return curve.start, curve.end
    return (_on_arc(curve, curve.start_angle), _on_arc(curve, curve.end_angle))


def _on_arc(arc: Arc2D, degrees: float) -> tuple[float, float]:
    radians = math.radians(degrees)
    return (
        arc.centre[0] + arc.radius * math.cos(radians),
        arc.centre[1] + arc.radius * math.sin(radians),
    )


def _swept_angle(arc: Arc2D) -> float:
    delta = (arc.end_angle - arc.start_angle) if arc.ccw else (arc.start_angle - arc.end_angle)
    return delta % 360.0


def _close(a: tuple[float, float], b: tuple[float, float], tol: float = 1e-7) -> bool:
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def _bounds(
    profiles: Iterable[Profile2D],
) -> tuple[float, float, float, float] | None:
    xs: list[float] = []
    ys: list[float] = []
    for profile in profiles:
        for loop in profile.loops:
            for curve in loop.curves:
                if isinstance(curve, Line2D):
                    xs.extend((curve.start[0], curve.end[0]))
                    ys.extend((curve.start[1], curve.end[1]))
                else:
                    # An arc can bulge past its endpoints, so bound it by the
                    # circle. Slightly generous, never wrong.
                    xs.extend((curve.centre[0] - curve.radius, curve.centre[0] + curve.radius))
                    ys.extend((curve.centre[1] - curve.radius, curve.centre[1] + curve.radius))
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))
