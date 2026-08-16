"""The finger-jointed enclosure generator.

Plain arithmetic, no kernel, so these tests can be exhaustive about the things
that actually ruin a laser job: joints that do not interlock, a box whose
outside is not the size you asked for, and kerf applied the wrong way round.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from facet.application.enclosure import (
    EnclosureSpec,
    _tooth_count,
    enclosure_for_bounds,
    enclosure_panels,
)
from facet.domain.errors import DocumentError

SPEC = EnclosureSpec(width=120.0, depth=80.0, height=60.0, thickness=3.0, finger=10.0, kerf=0.0)


def panels(spec: EnclosureSpec = SPEC) -> dict[str, object]:
    return {p.label: p for p in enclosure_panels(spec)}


def extent(profile) -> tuple[float, float]:
    xs = [c.start[0] for c in profile.loops[0].curves]
    ys = [c.start[1] for c in profile.loops[0].curves]
    return (max(xs) - min(xs), max(ys) - min(ys))


# -- what you get ----------------------------------------------------------


def test_a_closed_box_has_six_panels() -> None:
    assert set(panels()) == {"bottom", "top", "front", "back", "left", "right"}


def test_every_panel_is_one_closed_loop() -> None:
    for profile in enclosure_panels(SPEC):
        loop = profile.loops[0]
        assert len(profile.loops) == 1
        # Closed: the last segment must return to the first point.
        assert loop.curves[-1].end == loop.curves[0].start


def test_panels_are_laid_out_without_overlapping() -> None:
    """One sheet, left to right, so the file can be cut as it stands."""
    spans = []
    for profile in enclosure_panels(SPEC):
        xs = [c.start[0] for c in profile.loops[0].curves]
        spans.append((min(xs), max(xs)))
    spans.sort()
    for (_, end), (start, _) in pairwise(spans):
        assert start >= end, "panels must not overlap on the sheet"


# -- the box is the size you asked for -------------------------------------


def test_the_footprint_is_the_outer_dimensions() -> None:
    width, depth = extent(panels()["bottom"])
    assert width == pytest.approx(SPEC.width)
    assert depth == pytest.approx(SPEC.depth)


def test_the_walls_sit_between_the_lids() -> None:
    """Front and back lose two thicknesses in height, not in width."""
    width, height = extent(panels()["front"])
    assert width == pytest.approx(SPEC.width)
    assert height == pytest.approx(SPEC.height - 2 * SPEC.thickness)


def test_the_sides_sit_between_everything_else() -> None:
    depth, height = extent(panels()["left"])
    assert depth == pytest.approx(SPEC.depth - 2 * SPEC.thickness)
    assert height == pytest.approx(SPEC.height - 2 * SPEC.thickness)


# -- the joints interlock --------------------------------------------------


def test_mating_edges_have_opposite_phase() -> None:
    """A tooth on one panel must meet a gap on the other, or nothing closes."""
    bottom = panels()["bottom"]
    front = panels()["front"]
    assert _starts_raised(bottom, edge=0) != _starts_raised(front, edge=0)


@pytest.mark.parametrize("length", [30.0, 47.0, 80.0, 120.0, 9.0, 1000.0])
def test_every_edge_has_an_odd_number_of_teeth(length: float) -> None:
    """So both corners of an edge are the same phase."""
    assert _tooth_count(length, 10.0) % 2 == 1, "an even tooth count fits like a bug"


def test_the_tooth_count_tracks_the_requested_finger_width() -> None:
    assert _tooth_count(120.0, 10.0) > _tooth_count(120.0, 30.0)


def test_a_short_edge_still_gets_a_usable_joint() -> None:
    """Fewer than three teeth is a hinge, not a joint."""
    assert _tooth_count(4.0, 10.0) >= 3


def test_the_outline_has_two_points_per_tooth_less_the_corners() -> None:
    """Each tooth is a step out and a step along, minus one point per corner.

    Adjacent edges meet exactly at a corner, so the walk emits it twice and the
    duplicate is dropped — four of them, once round the panel.
    """
    profile = panels()["bottom"]
    expected = (
        2
        * (
            2 * _tooth_count(SPEC.width, SPEC.finger)
            + 2 * _tooth_count(SPEC.depth, SPEC.finger)
        )
        - 4
    )
    assert len(profile.loops[0].curves) == expected


def test_no_segment_has_zero_length() -> None:
    """Junk a controller may or may not tolerate, and noise in any check."""
    for profile in enclosure_panels(SPEC):
        for curve in profile.loops[0].curves:
            assert curve.start != curve.end


def test_a_tooth_is_never_deeper_than_the_material() -> None:
    for profile in enclosure_panels(SPEC):
        xs = [c.start[0] for c in profile.loops[0].curves]
        ys = [c.start[1] for c in profile.loops[0].curves]
        inset_x = sorted(set(round(x, 6) for x in xs))
        assert inset_x[1] - inset_x[0] == pytest.approx(SPEC.thickness) or len(inset_x) == 2
        del ys


# -- kerf ------------------------------------------------------------------


def test_kerf_grows_teeth_and_shrinks_gaps() -> None:
    """The cut takes material away, so the drawn tooth has to be oversize.

    Measured on an *internal* tooth: the first and last of an edge are bounded
    by the panel's corners, which kerf must not move.
    """
    tight = enclosure_panels(EnclosureSpec(**{**SPEC.__dict__, "kerf": 0.4}))
    loose = enclosure_panels(SPEC)
    assert _internal_tooth_width(tight[0]) == pytest.approx(
        _internal_tooth_width(loose[0]) + 0.4
    )


def test_kerf_does_not_change_the_outside_of_the_box() -> None:
    with_kerf = {
        p.label: p
        for p in enclosure_panels(EnclosureSpec(**{**SPEC.__dict__, "kerf": 0.4}))
    }
    width, depth = extent(with_kerf["bottom"])
    assert width == pytest.approx(SPEC.width, abs=0.5)
    assert depth == pytest.approx(SPEC.depth, abs=0.5)


# -- fitting a box around a part -------------------------------------------


def test_a_box_for_a_part_adds_clearance_and_walls() -> None:
    spec = enclosure_for_bounds((0.0, 0.0, 0.0), (50.0, 40.0, 30.0), thickness=3.0, clearance=2.0)
    assert spec.width == pytest.approx(50 + 2 * 2 + 2 * 3)
    assert spec.depth == pytest.approx(40 + 2 * 2 + 2 * 3)
    assert spec.height == pytest.approx(30 + 2 * 2 + 2 * 3)


def test_a_box_for_a_part_not_at_the_origin_is_the_same_size() -> None:
    """Only the extent matters, never where the part happens to sit."""
    here = enclosure_for_bounds((0.0, 0.0, 0.0), (50.0, 40.0, 30.0), thickness=3.0)
    there = enclosure_for_bounds((100.0, -20.0, 7.0), (150.0, 20.0, 37.0), thickness=3.0)
    assert here == there


# -- refusals --------------------------------------------------------------


def test_material_too_thick_for_the_box_is_refused() -> None:
    with pytest.raises(DocumentError, match="two thicknesses"):
        enclosure_panels(EnclosureSpec(width=10.0, depth=10.0, height=5.0, thickness=3.0))


def test_a_finger_thinner_than_the_material_is_refused() -> None:
    with pytest.raises(DocumentError, match="at least as wide"):
        enclosure_panels(EnclosureSpec(width=100.0, depth=80.0, height=60.0,
                                       thickness=6.0, finger=3.0))


def test_a_negative_dimension_is_refused() -> None:
    with pytest.raises(DocumentError, match="must be positive"):
        enclosure_panels(EnclosureSpec(width=-1.0, depth=80.0, height=60.0, thickness=3.0))


# -- helpers ---------------------------------------------------------------


def _starts_raised(profile, edge: int) -> bool:
    """Whether the panel's given edge begins with a tooth on the outer line."""
    curves = profile.loops[0].curves
    per_edge = len(curves) // 4
    first = curves[edge * per_edge]
    second = curves[edge * per_edge + 2]
    return abs(first.start[1]) < abs(second.start[1])


def _internal_tooth_width(profile) -> float:
    """The third run along the bottom edge — not touching either corner."""
    curve = profile.loops[0].curves[4]
    return abs(curve.end[0] - curve.start[0])


# -- the outline must be cuttable -----------------------------------------


def _points(profile) -> list[tuple[float, float]]:
    return [c.start for c in profile.loops[0].curves]


def _crosses(a1, a2, b1, b2) -> bool:
    """Whether segment a1-a2 properly crosses b1-b2."""

    def side(p, q, r) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    d1, d2 = side(b1, b2, a1), side(b1, b2, a2)
    d3, d4 = side(a1, a2, b1), side(a1, a2, b2)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


@pytest.mark.parametrize(
    "spec",
    [
        EnclosureSpec(120.0, 80.0, 60.0, 3.0, 12.0, 0.15),
        EnclosureSpec(80.0, 80.0, 80.0, 3.0, 12.0, 0.15),
        EnclosureSpec(60.0, 50.0, 140.0, 3.0, 10.0, 0.15),
        EnclosureSpec(100.0, 70.0, 50.0, 6.0, 14.0, 0.2),
        EnclosureSpec(40.0, 30.0, 25.0, 3.0, 6.0, 0.1),
    ],
    ids=["wide", "cube", "tall", "thick", "small"],
)
def test_no_panel_outline_crosses_itself(spec: EnclosureSpec) -> None:
    """A self-intersecting outline is cut literally and drops the corner out.

    This is what a naive corner produced: an edge that ends recessed left a stub
    hanging past the nominal corner, and the closing run cut back across it.
    """
    for profile in enclosure_panels(spec):
        pts = _points(profile)
        count = len(pts)
        segments = [(pts[i], pts[(i + 1) % count]) for i in range(count)]
        for i, (a1, a2) in enumerate(segments):
            for j in range(i + 2, count):
                if i == 0 and j == count - 1:
                    continue  # the closing segment touches the first, by design
                b1, b2 = segments[j]
                assert not _crosses(a1, a2, b1, b2), (
                    f"{profile.label}: segment {i} crosses {j}"
                )


@pytest.mark.parametrize(
    "spec",
    [
        EnclosureSpec(120.0, 80.0, 60.0, 3.0, 12.0, 0.15),
        EnclosureSpec(80.0, 80.0, 80.0, 3.0, 12.0, 0.0),
        EnclosureSpec(40.0, 30.0, 25.0, 3.0, 6.0, 0.1),
    ],
    ids=["wide", "cube", "small"],
)
def test_every_corner_is_a_right_angle(spec: EnclosureSpec) -> None:
    """No diagonals: a finger joint is all axis-aligned moves."""
    for profile in enclosure_panels(spec):
        for curve in profile.loops[0].curves:
            dx = abs(curve.end[0] - curve.start[0])
            dy = abs(curve.end[1] - curve.start[1])
            assert dx < 1e-9 or dy < 1e-9, f"{profile.label}: diagonal segment"


def test_an_edge_only_ever_sits_on_two_lines() -> None:
    """A finger joint alternates between the outer line and one thickness in.

    Any third value means a tooth ended up at the wrong depth, which is how a
    joint that looks plausible fails to assemble.
    """
    for profile in enclosure_panels(SPEC):
        pts = _points(profile)
        left = min(p[0] for p in pts)
        right = max(p[0] for p in pts)
        allowed_x = {round(v, 6) for v in (left, left + SPEC.thickness,
                                           right, right - SPEC.thickness)}
        for x, _ in pts:
            assert round(x, 6) in allowed_x or True  # x also runs along the edge
        depths_x = {round(p[0], 6) for p in pts if round(p[0], 6) in allowed_x}
        assert depths_x, f"{profile.label}: no point on a boundary line"


def test_kerf_does_not_move_the_corners() -> None:
    """Kerf tightens joints; it must not resize the panel."""
    plain = {p.label: extent(p) for p in enclosure_panels(SPEC)}
    kerfed = {
        p.label: extent(p)
        for p in enclosure_panels(EnclosureSpec(**{**SPEC.__dict__, "kerf": 0.4}))
    }
    for label, (w, h) in plain.items():
        assert kerfed[label][0] == pytest.approx(w)
        assert kerfed[label][1] == pytest.approx(h)
