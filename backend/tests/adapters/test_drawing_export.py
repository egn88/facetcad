"""DXF and SVG writing.

Pure geometry in, text out — no kernel involved, so these run everywhere and
pin the file dialects precisely. The point of the format tests is that a file
which *looks* right but a machine refuses is the expensive kind of wrong.
"""

from __future__ import annotations

import re

import pytest

from facet.adapters.export.drawing import export_drawing
from facet.application.ports.geometry import Arc2D, Line2D, Loop2D, Profile2D
from facet.domain.errors import DocumentError

SQUARE = Profile2D(
    loops=(
        Loop2D(
            curves=(
                Line2D((0.0, 0.0), (40.0, 0.0)),
                Line2D((40.0, 0.0), (40.0, 25.0)),
                Line2D((40.0, 25.0), (0.0, 25.0)),
                Line2D((0.0, 25.0), (0.0, 0.0)),
            )
        ),
    ),
    label="plate/cap+",
)

WITH_HOLE = Profile2D(
    loops=(
        SQUARE.loops[0],
        Loop2D(
            curves=(Arc2D(centre=(20.0, 12.5), radius=4.0, start_angle=0.0, end_angle=0.0),),
            outer=False,
        ),
    ),
    label="drilled",
)


def dxf(*profiles: Profile2D) -> str:
    return export_drawing(profiles, "dxf").decode("ascii")


def pairs(text: str) -> list[tuple[str, str]]:
    """DXF is a flat list of (group code, value) lines."""
    lines = text.splitlines()
    return list(zip(lines[0::2], lines[1::2], strict=True))


# -- DXF -------------------------------------------------------------------


def test_a_dxf_has_the_sections_a_reader_expects() -> None:
    text = dxf(SQUARE)
    assert text.startswith("0\nSECTION\n")
    assert text.rstrip().endswith("EOF")
    assert "ENTITIES" in text
    assert "ENDSEC" in text


def test_dxf_group_codes_come_in_pairs() -> None:
    """An odd line count is the classic way to write a file nothing will open."""
    assert len(dxf(WITH_HOLE).splitlines()) % 2 == 0


def test_lines_become_dxf_line_entities() -> None:
    codes = pairs(dxf(SQUARE))
    assert sum(1 for code, value in codes if code == "0" and value == "LINE") == 4


def test_a_closed_arc_becomes_a_circle_not_a_zero_length_arc() -> None:
    text = dxf(WITH_HOLE)
    assert "CIRCLE" in text
    assert "\nARC\n" not in text


def test_a_clockwise_arc_is_written_counter_clockwise() -> None:
    """DXF arcs only run counter-clockwise, so the ends swap instead."""
    arc = Arc2D(centre=(0.0, 0.0), radius=5.0, start_angle=90.0, end_angle=0.0, ccw=False)
    codes = pairs(dxf(Profile2D(loops=(Loop2D(curves=(arc,)),), label="a")))
    start = next(v for c, v in codes if c == "50")
    end = next(v for c, v in codes if c == "51")
    assert float(start) == pytest.approx(0.0)
    assert float(end) == pytest.approx(90.0)


def test_each_profile_gets_its_own_layer() -> None:
    text = dxf(SQUARE, WITH_HOLE)
    assert "plate_cap+" in text  # '/' is not legal in an R12 layer name
    assert "drilled" in text


def test_layer_names_are_made_unique() -> None:
    same = Profile2D(loops=SQUARE.loops, label="face")
    text = dxf(same, same)
    assert "face_2" in text


def test_an_empty_drawing_is_still_a_valid_file() -> None:
    text = export_drawing((), "dxf").decode("ascii")
    assert "EOF" in text
    assert len(text.splitlines()) % 2 == 0


# -- SVG -------------------------------------------------------------------


def svg(*profiles: Profile2D) -> str:
    return export_drawing(profiles, "svg").decode("utf-8")


def test_svg_declares_millimetres() -> None:
    text = svg(SQUARE)
    assert 'width="' in text and "mm" in text
    assert "viewBox=" in text


def test_svg_flips_the_y_axis_once_for_the_whole_drawing() -> None:
    """Model y points up, SVG y points down; the flip belongs in one place."""
    assert svg(SQUARE).count("scale(1,-1)") == 1


def test_svg_emits_one_path_per_loop() -> None:
    assert svg(WITH_HOLE).count("<path") == 2


def test_a_full_circle_is_two_half_arcs() -> None:
    """A single 360-degree SVG arc is degenerate and draws nothing."""
    text = svg(WITH_HOLE)
    path = re.findall(r'<path d="([^"]+)"', text)[1]
    assert path.count(" A ") == 2


def test_svg_escapes_a_label() -> None:
    tricky = Profile2D(loops=SQUARE.loops, label='a<b&c"')
    assert "&lt;" in svg(tricky) and "&amp;" in svg(tricky)


def test_the_drawing_is_not_clipped_by_its_own_bounds() -> None:
    """A margin, so a cut path on the edge is not lost to rounding."""
    text = svg(SQUARE)
    width = float(re.search(r'width="([\d.]+)mm"', text).group(1))
    assert width > 40.0


# -- shared ----------------------------------------------------------------


def test_an_unknown_format_says_what_is_available() -> None:
    with pytest.raises(DocumentError) as caught:
        export_drawing((SQUARE,), "pdf")
    assert "dxf" in str(caught.value) and "svg" in str(caught.value)


def test_the_format_may_be_written_with_a_dot_or_in_caps() -> None:
    assert export_drawing((SQUARE,), ".DXF").startswith(b"0")
