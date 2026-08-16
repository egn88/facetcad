"""Sketch curves: lines, arcs and circles, all from explicit coordinates.

Arcs reference named points exactly as lines do, so a loop closes by
construction rather than by the author computing endpoints that happen to meet.
The radius is derived from the centre, and an arc whose two ends disagree about
it is reported rather than quietly averaged — the same fail-loud rule the rest
of the system follows.
"""

from __future__ import annotations

import math

import pytest

from facet.domain.errors import DocumentError
from facet.domain.parameters import Parameter, ParameterSet
from facet.domain.sketch import CurveKinds, Sketch, SketchCurve, SketchLoop, SketchPoint


def params(**values: float) -> object:
    return ParameterSet(Parameter(name, value=value) for name, value in values.items()).resolve()


# --------------------------------------------------------------------------
# Declaration-time validation
# --------------------------------------------------------------------------


def test_a_line_needs_both_endpoints() -> None:
    with pytest.raises(DocumentError) as excinfo:
        SketchCurve(id="l", type=CurveKinds.LINE, start="p0")
    assert "end" in str(excinfo.value)


def test_an_arc_needs_a_centre() -> None:
    with pytest.raises(DocumentError) as excinfo:
        SketchCurve(id="a", type=CurveKinds.ARC, start="p0", end="p1")
    assert "center" in str(excinfo.value)


def test_a_circle_needs_a_centre() -> None:
    with pytest.raises(DocumentError):
        SketchCurve(id="c", type=CurveKinds.CIRCLE)


def test_a_circle_may_not_have_endpoints() -> None:
    """It closes on itself, so endpoints would be contradictory."""
    with pytest.raises(DocumentError) as excinfo:
        SketchCurve(id="c", type=CurveKinds.CIRCLE, center="m", start="p0", end="p1")
    assert "closed already" in str(excinfo.value)


def test_an_unknown_curve_type_is_rejected() -> None:
    with pytest.raises(DocumentError) as excinfo:
        SketchCurve(id="s", type="spline", start="p0", end="p1")
    assert "line, arc, circle" in str(excinfo.value)


def test_a_circle_is_closed_and_a_line_is_not() -> None:
    assert SketchCurve(id="c", type=CurveKinds.CIRCLE, center="m").is_closed
    assert not SketchCurve(id="l", start="p0", end="p1").is_closed


# --------------------------------------------------------------------------
# A rounded slot: lines joined by arcs
# --------------------------------------------------------------------------


def slot_sketch() -> Sketch:
    """A capsule: two straight flanks closed by semicircular ends."""
    return Sketch(
        id="slot",
        plane="xy",
        points=(
            SketchPoint("a", ("-half", "-r")),
            SketchPoint("b", ("half", "-r")),
            SketchPoint("c", ("half", "r")),
            SketchPoint("d", ("-half", "r")),
            SketchPoint("right_c", ("half", 0.0)),
            SketchPoint("left_c", ("-half", 0.0)),
        ),
        curves=(
            SketchCurve(id="bottom", start="a", end="b"),
            SketchCurve(id="right", type=CurveKinds.ARC, start="b", end="c", center="right_c"),
            SketchCurve(id="top", start="c", end="d"),
            SketchCurve(id="left", type=CurveKinds.ARC, start="d", end="a", center="left_c"),
        ),
        loops=(SketchLoop(id="outer", curves=("bottom", "right", "top", "left")),),
    )


def test_a_rounded_slot_resolves() -> None:
    resolved = slot_sketch().resolve_loop("outer", params(half=20.0, r=5.0))
    assert [c.id for c in resolved] == ["bottom", "right", "top", "left"]
    assert [c.type for c in resolved] == ["line", "arc", "line", "arc"]


def test_arc_radius_is_derived_from_the_centre() -> None:
    resolved = slot_sketch().resolve_loop("outer", params(half=20.0, r=5.0))
    arcs = [c for c in resolved if c.type == CurveKinds.ARC]
    assert all(arc.radius == pytest.approx(5.0) for arc in arcs)


def test_arc_radius_follows_a_parameter_change() -> None:
    """The whole point: geometry driven from the sheet."""
    wide = slot_sketch().resolve_loop("outer", params(half=40.0, r=12.0))
    arcs = [c for c in wide if c.type == CurveKinds.ARC]
    assert all(arc.radius == pytest.approx(12.0) for arc in arcs)


def test_the_slot_loop_is_closed() -> None:
    resolved = slot_sketch().resolve_loop("outer", params(half=20.0, r=5.0))
    for index, curve in enumerate(resolved):
        following = resolved[(index + 1) % len(resolved)]
        assert curve.end is not None and following.start is not None
        assert (curve.end - following.start).length() == pytest.approx(0.0, abs=1e-9)


def test_an_arc_whose_ends_disagree_about_the_radius_is_refused() -> None:
    """Silently averaging would give geometry nobody asked for."""
    sketch = Sketch(
        id="bad",
        plane="xy",
        points=(
            SketchPoint("s", (10.0, 0.0)),
            SketchPoint("e", (0.0, 25.0)),  # 25 from centre, not 10
            SketchPoint("m", (0.0, 0.0)),
            SketchPoint("t", (-10.0, 0.0)),
        ),
        curves=(
            SketchCurve(id="arc", type=CurveKinds.ARC, start="s", end="e", center="m"),
            SketchCurve(id="l1", start="e", end="t"),
            SketchCurve(id="l2", start="t", end="s"),
        ),
        loops=(SketchLoop(id="outer", curves=("arc", "l1", "l2")),),
    )
    with pytest.raises(DocumentError) as excinfo:
        sketch.resolve_loop("outer", params())
    message = str(excinfo.value)
    assert "inconsistent" in message
    assert "10" in message and "25" in message


def test_an_arc_centred_on_its_start_point_is_refused() -> None:
    sketch = Sketch(
        id="bad",
        plane="xy",
        points=(
            SketchPoint("s", (0.0, 0.0)),
            SketchPoint("e", (0.0, 0.0)),
            SketchPoint("m", (0.0, 0.0)),
            SketchPoint("t", (5.0, 5.0)),
        ),
        curves=(
            SketchCurve(id="arc", type=CurveKinds.ARC, start="s", end="e", center="m"),
            SketchCurve(id="l1", start="e", end="t"),
            SketchCurve(id="l2", start="t", end="s"),
        ),
        loops=(SketchLoop(id="outer", curves=("arc", "l1", "l2")),),
    )
    with pytest.raises(DocumentError) as excinfo:
        sketch.resolve_loop("outer", params())
    assert "centre" in str(excinfo.value)


def test_sweep_direction_is_explicit() -> None:
    clockwise = SketchCurve(
        id="a", type=CurveKinds.ARC, start="p0", end="p1", center="m", clockwise=True
    )
    assert clockwise.clockwise is True
    default = SketchCurve(id="b", type=CurveKinds.ARC, start="p0", end="p1", center="m")
    assert default.clockwise is False


# --------------------------------------------------------------------------
# Circles form a loop on their own
# --------------------------------------------------------------------------


def circle_sketch(radius: object = 8.0) -> Sketch:
    return Sketch(
        id="bore",
        plane="xy",
        points=(SketchPoint("m", ("cx", "cy")),),
        curves=(SketchCurve(id="rim", type=CurveKinds.CIRCLE, center="m", radius=radius),),
        loops=(SketchLoop(id="outer", curves=("rim",)),),
    )


def test_a_single_circle_is_a_valid_loop() -> None:
    resolved = circle_sketch().resolve_loop("outer", params(cx=30.0, cy=20.0))
    assert len(resolved) == 1
    assert resolved[0].type == CurveKinds.CIRCLE
    assert resolved[0].radius == pytest.approx(8.0)
    assert resolved[0].center is not None
    assert resolved[0].center.x == pytest.approx(30.0)


def test_a_circle_radius_can_be_an_expression() -> None:
    resolved = circle_sketch(radius="bore_d / 2").resolve_loop(
        "outer", ParameterSet([
            Parameter("cx", value=0.0), Parameter("cy", value=0.0),
            Parameter("bore_d", value=17.0),
        ]).resolve()
    )
    assert resolved[0].radius == pytest.approx(8.5)


def test_a_circle_with_a_non_positive_radius_is_refused() -> None:
    with pytest.raises(DocumentError) as excinfo:
        circle_sketch(radius=0.0).resolve_loop("outer", params(cx=0.0, cy=0.0))
    assert "positive radius" in str(excinfo.value)


def test_mixing_a_circle_with_other_curves_is_refused() -> None:
    """A circle already encloses an area; adding more curves is meaningless."""
    sketch = Sketch(
        id="bad",
        plane="xy",
        points=(SketchPoint("m", (0.0, 0.0)), SketchPoint("p", (5.0, 0.0))),
        curves=(
            SketchCurve(id="rim", type=CurveKinds.CIRCLE, center="m", radius=3.0),
            SketchCurve(id="l", start="p", end="p"),
        ),
        loops=(SketchLoop(id="outer", curves=("rim", "l")),),
    )
    with pytest.raises(DocumentError) as excinfo:
        sketch.resolve_loop("outer", params())
    assert "forms a loop by itself" in str(excinfo.value)


def test_an_open_loop_of_two_lines_is_refused() -> None:
    """Two straight segments cannot enclose an area."""
    sketch = Sketch(
        id="bad",
        plane="xy",
        points=(SketchPoint("a", (0.0, 0.0)), SketchPoint("b", (10.0, 0.0))),
        curves=(
            SketchCurve(id="l1", start="a", end="b"),
            SketchCurve(id="l2", start="b", end="a"),
        ),
        loops=(SketchLoop(id="outer", curves=("l1", "l2")),),
    )
    with pytest.raises(DocumentError) as excinfo:
        sketch.resolve_loop("outer", params())
    assert "at least 3" in str(excinfo.value)


def test_an_empty_loop_is_refused() -> None:
    with pytest.raises(DocumentError):
        SketchLoop(id="outer", curves=())


# --------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------


def test_curves_round_trip_through_the_document_form() -> None:
    original = slot_sketch()
    restored = Sketch.from_dict("slot", original.to_dict())
    assert [c.type for c in restored.curves] == [c.type for c in original.curves]
    assert [c.center for c in restored.curves] == [c.center for c in original.curves]


def test_sweep_direction_round_trips() -> None:
    sketch = Sketch(
        id="s",
        plane="xy",
        points=(SketchPoint("p0", (0.0, 0.0)),),
        curves=(
            SketchCurve(
                id="a", type=CurveKinds.ARC, start="p0", end="p0", center="p0", clockwise=True
            ),
        ),
        loops=(),
    )
    restored = Sketch.from_dict("s", sketch.to_dict())
    assert restored.curves[0].clockwise is True


def test_a_circle_round_trips_with_its_radius() -> None:
    restored = Sketch.from_dict("bore", circle_sketch(radius="d / 2").to_dict())
    assert restored.curves[0].radius == "d / 2"
    assert restored.curves[0].type == CurveKinds.CIRCLE


def test_geometry_from_a_slot_matches_hand_computed_values() -> None:
    """Sanity-check the resolved coordinates rather than only their structure."""
    resolved = slot_sketch().resolve_loop("outer", params(half=30.0, r=6.0))
    bottom = resolved[0]
    assert bottom.start is not None and bottom.end is not None
    assert bottom.start.x == pytest.approx(-30.0)
    assert bottom.start.y == pytest.approx(-6.0)
    assert bottom.end.x == pytest.approx(30.0)

    right = resolved[1]
    assert right.center is not None
    assert right.center.x == pytest.approx(30.0)
    assert right.center.y == pytest.approx(0.0)
    assert right.radius == pytest.approx(6.0)
    # The capsule's straight flanks are 2*half long and the ends are semicircles.
    assert math.isclose(right.radius * 2, 12.0)
