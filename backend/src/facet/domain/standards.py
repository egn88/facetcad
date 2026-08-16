"""Standard fastener sizes.

The value of naming a thread rather than a diameter is that nobody remembers a
normal-fit clearance hole for an M6 is 6.6mm, and getting it wrong is the sort
of mistake you discover with a part in your hand.

Clearance diameters follow ISO 273 (medium series for "normal"). Tap-drill sizes
are for ISO 261 coarse pitch at roughly 100% thread engagement, which is
``nominal - pitch``.

The table is deliberately small and explicit. A designation that is not in it is
refused with the list of those that are, rather than being interpolated — a
plausible-looking hole of the wrong size is worse than an error.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import DocumentError


class Fit:
    """How the hole relates to the fastener that passes through it."""

    CLOSE = "close"
    NORMAL = "normal"
    LOOSE = "loose"
    TAPPED = "tapped"
    """Drilled for cutting a thread, not for clearance."""


@dataclass(frozen=True)
class Thread:
    designation: str
    nominal: float
    pitch: float
    close: float
    normal: float
    loose: float

    @property
    def tap_drill(self) -> float:
        """Coarse-pitch tap drill at ~100% engagement."""
        return round(self.nominal - self.pitch, 3)

    def clearance(self, fit: str) -> float:
        return {Fit.CLOSE: self.close, Fit.NORMAL: self.normal, Fit.LOOSE: self.loose}[fit]


#: ISO metric coarse threads, with ISO 273 clearance holes.
THREADS: dict[str, Thread] = {
    thread.designation: thread
    for thread in (
        Thread("M2", 2.0, 0.40, 2.2, 2.4, 2.6),
        Thread("M2.5", 2.5, 0.45, 2.7, 2.9, 3.1),
        Thread("M3", 3.0, 0.50, 3.2, 3.4, 3.6),
        Thread("M4", 4.0, 0.70, 4.3, 4.5, 4.8),
        Thread("M5", 5.0, 0.80, 5.3, 5.5, 5.8),
        Thread("M6", 6.0, 1.00, 6.4, 6.6, 7.0),
        Thread("M8", 8.0, 1.25, 8.4, 9.0, 10.0),
        Thread("M10", 10.0, 1.50, 10.5, 11.0, 12.0),
        Thread("M12", 12.0, 1.75, 13.0, 13.5, 14.5),
        Thread("M14", 14.0, 2.00, 15.0, 15.5, 16.5),
        Thread("M16", 16.0, 2.00, 17.0, 17.5, 18.5),
        Thread("M20", 20.0, 2.50, 21.0, 22.0, 24.0),
        Thread("M24", 24.0, 3.00, 25.0, 26.0, 28.0),
    )
}

FITS = (Fit.CLOSE, Fit.NORMAL, Fit.LOOSE, Fit.TAPPED)


def designations() -> tuple[str, ...]:
    return tuple(THREADS)


def thread(designation: str) -> Thread:
    found = THREADS.get(designation.strip().upper())
    if found is None:
        raise DocumentError(
            reason=(
                f"unknown thread {designation!r}; known sizes are "
                f"{', '.join(designations())}"
            )
        )
    return found


def hole_diameter(designation: str, fit: str = Fit.NORMAL) -> float:
    """The drilled diameter for a fastener size and fit."""
    cleaned = fit.strip().lower()
    if cleaned not in FITS:
        raise DocumentError(
            reason=f"unknown fit {fit!r}; expected one of {', '.join(FITS)}"
        )
    spec = thread(designation)
    return spec.tap_drill if cleaned == Fit.TAPPED else spec.clearance(cleaned)
