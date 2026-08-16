"""Fastener size lookups.

Spot-checked against ISO 273 (clearance) and ISO 261 coarse pitch (tap drill),
because the whole point of naming a thread instead of typing a diameter is that
the number is one you would otherwise get wrong.
"""

from __future__ import annotations

import pytest

from facet.domain.errors import DocumentError
from facet.domain.standards import Fit, designations, hole_diameter, thread


@pytest.mark.parametrize(
    ("designation", "fit", "expected"),
    [
        ("M3", Fit.CLOSE, 3.2),
        ("M3", Fit.NORMAL, 3.4),
        ("M3", Fit.LOOSE, 3.6),
        ("M4", Fit.NORMAL, 4.5),
        ("M5", Fit.NORMAL, 5.5),
        ("M6", Fit.CLOSE, 6.4),
        ("M6", Fit.NORMAL, 6.6),
        ("M6", Fit.LOOSE, 7.0),
        ("M8", Fit.NORMAL, 9.0),
        ("M10", Fit.NORMAL, 11.0),
        ("M12", Fit.NORMAL, 13.5),
        ("M20", Fit.NORMAL, 22.0),
    ],
)
def test_clearance_holes_match_iso_273(designation: str, fit: str, expected: float) -> None:
    assert hole_diameter(designation, fit) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("designation", "expected"),
    [("M3", 2.5), ("M4", 3.3), ("M5", 4.2), ("M6", 5.0), ("M8", 6.75), ("M10", 8.5)],
)
def test_tap_drills_are_nominal_minus_pitch(designation: str, expected: float) -> None:
    assert hole_diameter(designation, Fit.TAPPED) == pytest.approx(expected)


def test_a_clearance_hole_is_always_larger_than_its_tap_drill() -> None:
    for designation in designations():
        tapped = hole_diameter(designation, Fit.TAPPED)
        assert tapped < hole_diameter(designation, Fit.CLOSE)


def test_fits_are_ordered_close_normal_loose() -> None:
    for designation in designations():
        close = hole_diameter(designation, Fit.CLOSE)
        normal = hole_diameter(designation, Fit.NORMAL)
        loose = hole_diameter(designation, Fit.LOOSE)
        assert close < normal < loose


def test_clearance_holes_exceed_the_nominal_diameter() -> None:
    """A clearance hole the bolt does not fit through would be useless."""
    for designation in designations():
        assert hole_diameter(designation, Fit.CLOSE) > thread(designation).nominal


def test_designations_are_case_insensitive() -> None:
    assert hole_diameter("m6", Fit.NORMAL) == hole_diameter("M6", Fit.NORMAL)


def test_fit_is_case_insensitive() -> None:
    assert hole_diameter("M6", "NORMAL") == hole_diameter("M6", Fit.NORMAL)


def test_the_default_fit_is_normal() -> None:
    assert hole_diameter("M6") == pytest.approx(6.6)


def test_an_unknown_thread_lists_the_known_ones() -> None:
    """Interpolating a plausible-looking wrong size would be worse than failing."""
    with pytest.raises(DocumentError) as excinfo:
        hole_diameter("M7", Fit.NORMAL)
    message = str(excinfo.value)
    assert "M7" in message
    assert "M6" in message and "M8" in message


def test_an_unknown_fit_is_rejected() -> None:
    with pytest.raises(DocumentError) as excinfo:
        hole_diameter("M6", "snug")
    assert "close" in str(excinfo.value)
