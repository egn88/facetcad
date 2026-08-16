"""Threaded features.

Two claims are under test. The first is that a thread is *correct*: a tapped
hole is drilled at the tap-drill size, and the modelled form actually removes
material in the right places. The second matters more: a thread makes about a
hundred faces, and none of them may disturb the name of a face that has nothing
to do with the thread. If changing a pitch could renumber the top of the plate,
the whole naming scheme would be worth nothing.
"""

from __future__ import annotations

import copy

import pytest

pytest.importorskip("OCP", reason="requires the optional OCCT extra")

from facet.adapters.geometry.occt import OcctKernel
from facet.application.ports.geometry import Capability
from facet.application.recompute import (
    Detail,
    FeatureStatus,
    RecomputeEngine,
    recompute,
)
from facet.domain import standards
from facet.domain.document import Document
from facet.domain.selectors import FaceSelector

pytestmark = pytest.mark.occt

BLOCK: dict[str, object] = {
    "schema": "cadsheet/1",
    "project": "tapped block",
    "parameters": [
        {"name": "w", "value": 60.0},
        {"name": "h", "value": 40.0},
        {"name": "t", "value": 16.0},
        {"name": "deep", "value": 10.0},
    ],
    "datums": {"base": {"type": "plane", "origin": [0, 0, 0], "normal": [0, 0, 1]}},
    "sketches": {
        "outline": {
            "plane": "base",
            "points": {"p0": [0, 0], "p1": ["w", 0], "p2": ["w", "h"], "p3": [0, "h"]},
            "curves": [
                {"id": "bottom", "start": "p0", "end": "p1"},
                {"id": "right", "start": "p1", "end": "p2"},
                {"id": "top", "start": "p2", "end": "p3"},
                {"id": "left", "start": "p3", "end": "p0"},
            ],
            "loops": [{"id": "outer", "curves": ["bottom", "right", "top", "left"]}],
        },
        "holes": {
            "plane": "base",
            "points": {"h1": ["w / 2", "h / 2"]},
            "curves": [],
            "loops": [],
        },
    },
    "features": [{"id": "block", "type": "pad", "profile": "outline.outer", "length": "t"}],
}

TAPPED = {
    "id": "m6",
    "type": "thread",
    "at": "holes.h1",
    "standard": "M6",
    "depth": "deep",
    "direction": "+normal",
}


@pytest.fixture(scope="module")
def kernel() -> OcctKernel:
    return OcctKernel()


def build(kernel: OcctKernel, thread: dict | None = None, **values: float):
    data = copy.deepcopy(BLOCK)
    for row in data["parameters"]:  # type: ignore[index]
        if row["name"] in values:
            row["value"] = values[row["name"]]
    data["features"].append(copy.deepcopy(thread if thread is not None else TAPPED))
    return recompute(Document.from_dict(data), kernel)


def base_tags(result) -> list[str]:
    """Tags with ordinals stripped, so a split shows up as a changed count."""
    return sorted(str(f.tag).split("#")[0] for f in result.topology.faces)


def unrelated(result) -> list[str]:
    return sorted(t for t in base_tags(result) if not t.startswith("m6/"))


# -- cosmetic (the default) ------------------------------------------------


def test_a_thread_drills_at_the_tap_drill_size(kernel: OcctKernel) -> None:
    """5mm for an M6, not 6mm — the hole is what a tap is turned into."""
    result = build(kernel)
    assert result.ok, [str(o.error) for o in result.failures()]

    wall = FaceSelector.parse("m6/wall[*]").resolve(result.topology)
    assert len(wall) == 1
    expected = standards.thread("M6").tap_drill
    assert wall[0].fingerprint.centroid  # placed, not at the origin
    assert expected == pytest.approx(5.0)


def test_a_cosmetic_thread_costs_nothing_extra(kernel: OcctKernel) -> None:
    """No helix, so no faces beyond the bore and its floor."""
    result = build(kernel)
    assert not FaceSelector.parse("m6/thread[*]").candidates(result.topology)


def test_an_unknown_designation_lists_the_known_ones(kernel: OcctKernel) -> None:
    result = build(kernel, {**TAPPED, "standard": "M7"})
    assert not result.ok
    assert "M6" in str(result.failures()[0].error)


def test_a_thread_needs_a_designation(kernel: OcctKernel) -> None:
    spec = {k: v for k, v in TAPPED.items() if k != "standard"}
    result = build(kernel, spec)
    assert not result.ok
    assert "standard" in str(result.failures()[0].error)


def test_an_external_thread_must_be_modelled_to_mean_anything(kernel: OcctKernel) -> None:
    result = build(kernel, {**TAPPED, "internal": False})
    assert not result.ok
    assert "modelled" in str(result.failures()[0].error)


# -- modelled --------------------------------------------------------------


@pytest.fixture(scope="module")
def modelled(kernel: OcctKernel):
    return build(kernel, {**TAPPED, "modelled": True})


def test_the_kernel_declares_it_can_cut_threads(kernel: OcctKernel) -> None:
    assert Capability.THREAD in kernel.capabilities


def test_a_modelled_thread_builds(modelled) -> None:
    assert modelled.ok, [str(o.error) for o in modelled.failures()]


def test_every_thread_face_shares_one_tag(modelled) -> None:
    """Nobody selects flank number fifty-seven; they select the thread."""
    faces = FaceSelector.parse("m6/thread[*]").resolve(modelled.topology)
    assert len(faces) > 20
    assert {str(f.tag).split("#")[0] for f in faces} == {"m6/thread[holes.h1]"}


def test_a_thread_leaves_every_other_face_alone(kernel: OcctKernel, modelled) -> None:
    """The claim that makes threads safe to add to an existing part."""
    plain = build(kernel)
    assert unrelated(modelled) == unrelated(plain)
    for tag in unrelated(modelled):
        before = [f for f in plain.topology.faces if str(f.tag) == tag]
        after = [f for f in modelled.topology.faces if str(f.tag) == tag]
        assert len(before) == len(after) == 1, f"{tag} was split by the thread"


def test_a_short_thread_is_refused_with_the_minimum(kernel: OcctKernel) -> None:
    result = build(kernel, {**TAPPED, "modelled": True, "depth": 2.0})
    assert not result.ok
    assert "too short" in str(result.failures()[0].error)


def test_a_left_hand_thread_differs_from_a_right_hand_one(kernel: OcctKernel) -> None:
    right = build(kernel, {**TAPPED, "modelled": True})
    left = build(kernel, {**TAPPED, "modelled": True, "hand": "left"})
    assert left.ok and right.ok
    # Same tags, mirrored geometry: the fingerprints must not all coincide.
    assert base_tags(left) == base_tags(right)
    right_points = {f.fingerprint.centroid.rounded(6) for f in right.topology.faces}
    left_points = {f.fingerprint.centroid.rounded(6) for f in left.topology.faces}
    assert left_points != right_points


# -- the sweep -------------------------------------------------------------


@pytest.mark.parametrize(
    "values",
    [{"w": 75.0}, {"h": 55.0}, {"t": 20.0}, {"deep": 13.0}],
    ids=["wider", "deeper block", "thicker", "deeper thread"],
)
def test_no_unrelated_tag_moves_when_a_parameter_changes(
    kernel: OcctKernel, values: dict[str, float]
) -> None:
    """Change a dimension; everything that is not the thread stays put."""
    before = build(kernel, {**TAPPED, "modelled": True})
    after = build(kernel, {**TAPPED, "modelled": True}, **values)
    assert after.ok, [str(o.error) for o in after.failures()]
    assert unrelated(after) == unrelated(before)


def test_the_thread_selector_survives_a_size_change(kernel: OcctKernel) -> None:
    """A different screw changes the face count, and the selector still works."""
    m6 = build(kernel, {**TAPPED, "modelled": True})
    m8 = build(kernel, {**TAPPED, "modelled": True, "standard": "M8"})
    assert m8.ok, [str(o.error) for o in m8.failures()]
    assert FaceSelector.parse("m6/thread[*]").resolve(m6.topology)
    assert FaceSelector.parse("m6/thread[*]").resolve(m8.topology)
    assert unrelated(m6) == unrelated(m8)


def test_rebuilding_gives_the_same_names(kernel: OcctKernel) -> None:
    first = build(kernel, {**TAPPED, "modelled": True})
    second = build(kernel, {**TAPPED, "modelled": True})
    assert [str(f.tag) for f in first.topology.faces] == [
        str(f.tag) for f in second.topology.faces
    ]


# -- modelled for the file, skipped for the screen --------------------------


def test_export_only_threads_are_skipped_for_the_viewport(kernel: OcctKernel) -> None:
    """On screen a thread is a grey cylinder either way; the cut costs seconds.

    'export' is the setting for a printed part: the geometry has to be in the
    STL or the thread does not exist, but nothing is lost by leaving it out of
    the picture.
    """
    data = copy.deepcopy(BLOCK)
    data["features"].append({**TAPPED, "modelled": "export"})
    document = Document.from_dict(data)

    draft = recompute(document, kernel, Detail.DRAFT)
    full = recompute(document, kernel, Detail.FULL)

    assert draft.ok and full.ok
    assert not FaceSelector.parse("m6/thread[*]").candidates(draft.topology)
    assert FaceSelector.parse("m6/thread[*]").candidates(full.topology)


def test_export_only_does_not_disturb_any_other_face(kernel: OcctKernel) -> None:
    """The two rebuilds must agree on everything except the thread itself.

    If skipping the helix moved a face elsewhere, a selector written against
    the viewport would resolve differently in the exported file.
    """
    data = copy.deepcopy(BLOCK)
    data["features"].append({**TAPPED, "modelled": "export"})
    document = Document.from_dict(data)

    def others(detail: str) -> set[str]:
        result = recompute(document, kernel, detail)
        names = (str(f.tag).split("#")[0] for f in result.topology.faces)
        return {name for name in names if not name.startswith("m6/thread")}

    assert others(Detail.DRAFT) == others(Detail.FULL)


def test_the_two_detail_levels_cache_separately(kernel: OcctKernel) -> None:
    """A session alternates between them; one must not evict the other."""
    data = copy.deepcopy(BLOCK)
    data["features"].append({**TAPPED, "modelled": "export"})
    document = Document.from_dict(data)

    engine = RecomputeEngine(kernel)
    engine.recompute(document, Detail.FULL)
    engine.recompute(document, Detail.DRAFT)
    again = engine.recompute(document, Detail.FULL)

    outcomes = {o.id: o.status for o in again.outcomes}
    assert outcomes["m6"] == FeatureStatus.CACHED


def test_modelled_true_is_cut_at_either_detail(kernel: OcctKernel) -> None:
    """'true' means always, and stays meaning that."""
    data = copy.deepcopy(BLOCK)
    data["features"].append({**TAPPED, "modelled": True})
    document = Document.from_dict(data)
    for detail in (Detail.DRAFT, Detail.FULL):
        result = recompute(document, kernel, detail)
        assert FaceSelector.parse("m6/thread[*]").candidates(result.topology), detail


def test_an_unknown_modelled_value_says_what_is_allowed(kernel: OcctKernel) -> None:
    result = build(kernel, {**TAPPED, "modelled": "sometimes"})
    assert not result.ok
    message = str(result.failures()[0].error)
    assert "export" in message
