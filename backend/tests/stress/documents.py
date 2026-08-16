"""Documents for the parameter-sweep battery.

Each one exercises a different corner of the feature set, and every dimension
that could plausibly move is a parameter — because a constant cannot drift, and
drift is what these tests are looking for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _rect(name: str, w: str, h: str, x: str = "0", y: str = "0") -> dict[str, Any]:
    return {
        "plane": "xy",
        "points": {
            "p0": [x, y],
            "p1": [f"{x} + {w}", y],
            "p2": [f"{x} + {w}", f"{y} + {h}"],
            "p3": [x, f"{y} + {h}"],
        },
        "curves": [
            {"id": "bottom", "type": "line", "start": "p0", "end": "p1"},
            {"id": "right", "type": "line", "start": "p1", "end": "p2"},
            {"id": "top", "type": "line", "start": "p2", "end": "p3"},
            {"id": "left", "type": "line", "start": "p3", "end": "p0"},
        ],
        "loops": [{"id": "outer", "curves": ["bottom", "right", "top", "left"]}],
    }


PLATE_WITH_POCKET: dict[str, Any] = {
    "schema": "cadsheet/1",
    "project": "plate with a pocket",
    "parameters": [
        {"name": "w", "value": 90.0},
        {"name": "h", "value": 60.0},
        {"name": "t", "value": 10.0},
        {"name": "slot_w", "value": 30.0},
        {"name": "slot_h", "value": 14.0},
        {"name": "slot_d", "value": 4.0},
    ],
    # A sketch for a subtractive feature sits on the face it enters through, so
    # the cut runs down into material exactly as a real operation would.
    "datums": {"top": {"type": "plane", "origin": [0, 0, "t"], "normal": [0, 0, 1]}},
    "sketches": {
        "outline": _rect("outline", "w", "h"),
        "slot": {**_rect("slot", "slot_w", "slot_h", "w / 4", "h / 3"), "plane": "top"},
    },
    "features": [
        {"id": "base", "type": "pad", "profile": "outline.outer", "length": "t"},
        {
            "id": "slot",
            "type": "pocket",
            "profile": "slot.outer",
            "depth": "slot_d",
            "direction": "-normal",
        },
    ],
}


BLENDED: dict[str, Any] = {
    "schema": "cadsheet/1",
    "project": "rounded and bevelled",
    "parameters": [
        {"name": "w", "value": 80.0},
        {"name": "h", "value": 55.0},
        {"name": "t", "value": 12.0},
        {"name": "rad", "value": 5.0},
        {"name": "bevel", "value": 1.5},
    ],
    "datums": {},
    "sketches": {"outline": _rect("outline", "w", "h")},
    "features": [
        {"id": "base", "type": "pad", "profile": "outline.outer", "length": "t"},
        {"id": "corners", "type": "fillet", "radius": "rad", "edges": "base/side[*] dir=|z"},
        {"id": "lip", "type": "chamfer", "distance": "bevel", "edges": "base/cap+ ^ */*"},
    ],
}


DRILLED: dict[str, Any] = {
    "schema": "cadsheet/1",
    "project": "drilled and tapped",
    "parameters": [
        {"name": "w", "value": 70.0},
        {"name": "h", "value": 50.0},
        {"name": "t", "value": 14.0},
        {"name": "tap_deep", "value": 9.0},
        {"name": "cbore_d", "value": 11.0},
        {"name": "cbore_deep", "value": 4.0},
    ],
    "datums": {"top": {"type": "plane", "origin": [0, 0, "t"], "normal": [0, 0, 1]}},
    "sketches": {
        "outline": _rect("outline", "w", "h"),
        "holes": {
            "plane": "top",
            "points": {"a": ["w / 4", "h / 2"], "b": ["3 * w / 4", "h / 2"]},
            "curves": [],
            "loops": [],
        },
    },
    "features": [
        {"id": "base", "type": "pad", "profile": "outline.outer", "length": "t"},
        {
            "id": "bolt",
            "type": "hole",
            "at": "holes.a",
            "standard": "M6",
            "fit": "normal",
            "through_all": True,
            "direction": "-normal",
            "counterbore_diameter": "cbore_d",
            "counterbore_depth": "cbore_deep",
        },
        {
            "id": "tap",
            "type": "thread",
            "at": "holes.b",
            "standard": "M5",
            "depth": "tap_deep",
            "direction": "-normal",
        },
    ],
}


CURVED: dict[str, Any] = {
    "schema": "cadsheet/1",
    "project": "arcs and a bore",
    "parameters": [
        {"name": "span", "value": 60.0},
        {"name": "rad", "value": 15.0},
        {"name": "t", "value": 8.0},
        {"name": "bore", "value": 6.0},
    ],
    "datums": {},
    "sketches": {
        "capsule": {
            "plane": "xy",
            "points": {
                "l": [0, 0],
                "r": ["span", 0],
                "lt": [0, "rad"],
                "lb": [0, "0 - rad"],
                "rt": ["span", "rad"],
                "rb": ["span", "0 - rad"],
            },
            "curves": [
                {"id": "top", "type": "line", "start": "lt", "end": "rt"},
                {"id": "cap_r", "type": "arc", "start": "rt", "end": "rb", "center": "r"},
                {"id": "bottom", "type": "line", "start": "rb", "end": "lb"},
                {"id": "cap_l", "type": "arc", "start": "lb", "end": "lt", "center": "l"},
            ],
            "loops": [{"id": "outer", "curves": ["top", "cap_r", "bottom", "cap_l"]}],
        },
        "hole": {
            "plane": "xy",
            "points": {"c": ["span / 2", 0]},
            "curves": [{"id": "ring", "type": "circle", "center": "c", "radius": "bore"}],
            "loops": [{"id": "outer", "curves": ["ring"]}],
        },
    },
    "features": [
        {"id": "body", "type": "pad", "profile": "capsule.outer", "length": "t"},
        {
            "id": "bore",
            "type": "pocket",
            "profile": "hole.outer",
            "depth": "t",
            "direction": "+normal",
        },
    ],
}


TWO_BODIES: dict[str, Any] = {
    "schema": "cadsheet/1",
    "project": "two bodies",
    "parameters": [
        {"name": "w", "value": 60.0},
        {"name": "h", "value": 45.0},
        {"name": "t", "value": 8.0},
        {"name": "pin_r", "value": 7.0},
        {"name": "pin_len", "value": 30.0},
        {"name": "gap", "value": 95.0},
    ],
    "datums": {},
    "sketches": {
        "plate": _rect("plate", "w", "h"),
        "pin": {
            "plane": "xy",
            "points": {"c": [0, 0]},
            "curves": [{"id": "rim", "type": "circle", "center": "c", "radius": "pin_r"}],
            "loops": [{"id": "outer", "curves": ["rim"]}],
        },
    },
    "bodies": [
        {
            "id": "plate",
            "features": [
                {"id": "slab", "type": "pad", "profile": "plate.outer", "length": "t"},
                {
                    "id": "round",
                    "type": "fillet",
                    "radius": 4.0,
                    "edges": "slab/side[*] dir=|z",
                },
            ],
        },
        {
            "id": "pin",
            "placement": {"origin": ["gap", 0, 0], "rotation": [0, 0, 0]},
            "features": [
                {"id": "shaft", "type": "pad", "profile": "pin.outer", "length": "pin_len"},
                {
                    "id": "lead",
                    "type": "chamfer",
                    "distance": 1.0,
                    "edges": "shaft/cap+ ^ */*",
                },
            ],
        },
    ],
}


#: A non-convex outline with two acute corners, plus a fillet and chamfer that
#: share a face — the shape that surfaced the blend-order problem.
WEDGE_AND_NOTCH: dict[str, Any] = {
    "schema": "cadsheet/1",
    "project": "wedge and notch",
    "parameters": [
        {"name": "span", "value": 40.0},
        {"name": "rise", "value": 15.0},
        {"name": "t", "value": 10.0},
        {"name": "rad", "value": 2.0},
        {"name": "bevel", "value": 1.0},
        {"name": "block", "value": 10.0},
    ],
    "datums": {},
    "sketches": {
        "wedge": {
            "plane": "xy",
            "points": {"a": [0, 0], "b": ["span", 0], "c": ["span / 2", "rise"]},
            "curves": [
                {"id": "c0", "type": "line", "start": "a", "end": "b"},
                {"id": "c1", "type": "line", "start": "b", "end": "c"},
                {"id": "c2", "type": "line", "start": "c", "end": "a"},
            ],
            "loops": [{"id": "outer", "curves": ["c0", "c1", "c2"]}],
        },
        "notched": {
            "plane": "xy",
            "points": {
                "p0": ["span + 20", 0],
                "p1": ["span + 20 + block / 3", 0],
                "p2": ["span + 20 + block / 3", "block / 3"],
                "p3": ["span + 20 + 2 * block / 3", "block / 3"],
                "p4": ["span + 20 + 2 * block / 3", 0],
                "p5": ["span + 20 + block", 0],
                "p6": ["span + 20 + block", "block"],
                "p7": ["span + 20", "block"],
            },
            "curves": [
                {"id": "e0", "type": "line", "start": "p0", "end": "p1"},
                {"id": "e1", "type": "line", "start": "p1", "end": "p2"},
                {"id": "e2", "type": "line", "start": "p2", "end": "p3"},
                {"id": "e3", "type": "line", "start": "p3", "end": "p4"},
                {"id": "e4", "type": "line", "start": "p4", "end": "p5"},
                {"id": "e5", "type": "line", "start": "p5", "end": "p6"},
                {"id": "e6", "type": "line", "start": "p6", "end": "p7"},
                {"id": "e7", "type": "line", "start": "p7", "end": "p0"},
            ],
            "loops": [
                {"id": "outer", "curves": ["e0", "e1", "e2", "e3", "e4", "e5", "e6", "e7"]}
            ],
        },
    },
    "features": [
        {"id": "notch", "type": "pad", "profile": "notched.outer", "length": "block"},
        {"id": "wedge", "type": "pad", "profile": "wedge.outer", "length": "t"},
        # Chamfers before fillets: they must not meet at a shared vertex.
        {
            "id": "bevel",
            "type": "chamfer",
            "distance": "bevel",
            "edges": "wedge/cap+ ^ wedge/side[wedge.c1], wedge/cap+ ^ wedge/side[wedge.c2]",
        },
        {
            "id": "soft",
            "type": "fillet",
            "radius": "rad",
            "edges": "wedge/cap+ ^ wedge/side[wedge.c0]",
        },
    ],
}


@dataclass(frozen=True)
class Case:
    """One document, and what may be done to it.

    ``sweep`` must stay inside the regime the model is valid over: a pocket made
    deep enough to break through legitimately changes the topology, and that is
    a different question from drift.

    ``dimensions`` are the subset for which zero is meaningless — a length, a
    radius, a depth. A *position* may perfectly well be zero, so it is excluded
    from the degenerate-input check rather than being asserted to fail.
    """

    name: str
    document: dict[str, Any]
    sweep: tuple[str, ...]
    dimensions: tuple[str, ...]


SUITE: tuple[Case, ...] = (
    Case(
        "plate with a pocket",
        PLATE_WITH_POCKET,
        ("w", "h", "t", "slot_w", "slot_h", "slot_d"),
        ("w", "h", "t", "slot_w", "slot_h", "slot_d"),
    ),
    Case("blended", BLENDED, ("w", "h", "t", "rad", "bevel"), ("w", "h", "t", "rad", "bevel")),
    Case(
        "drilled and tapped",
        DRILLED,
        ("w", "h", "t", "tap_deep", "cbore_d", "cbore_deep"),
        ("w", "h", "t", "tap_deep", "cbore_d", "cbore_deep"),
    ),
    Case("curved", CURVED, ("span", "rad", "t", "bore"), ("span", "rad", "t", "bore")),
    Case(
        "two bodies",
        TWO_BODIES,
        ("w", "h", "t", "pin_r", "pin_len", "gap"),
        # `gap` is where the pin sits, not how big anything is: zero is a
        # position, and a valid one.
        ("w", "h", "t", "pin_r", "pin_len"),
    ),
    Case(
        "wedge and notch",
        WEDGE_AND_NOTCH,
        ("span", "rise", "t", "rad", "bevel", "block"),
        ("span", "rise", "t", "rad", "bevel", "block"),
    ),
)
