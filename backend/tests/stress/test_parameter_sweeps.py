"""The battery this project exists to pass.

The FreeCAD failure this replaces is not that a rebuild crashes — it is that it
*succeeds* and produces something subtly different: a fillet on a new edge, a
pocket in a face that used to be somewhere else, a hole that moved because an
index shifted. So the assertion here is not "it still builds". It is that after
a dimension changes, **every name is the one it was**.

Each document is swept parameter by parameter and then several at once, and the
full set of face tags is compared against the baseline. A single changed,
missing or extra tag fails the test, because a single one is enough to send a
selector somewhere else on the next rebuild.

Sweeps stay inside the range the model is valid over. A pocket made deep enough
to break through legitimately changes the topology; that is a different question
from drift, and conflating them would make the battery meaningless.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

pytest.importorskip("OCP", reason="requires the optional OCCT extra")

from facet.adapters.geometry.occt import OcctKernel
from facet.application.recompute import RecomputeResult, recompute
from facet.domain.document import Document

from .documents import SUITE, TAPPED_CAVITY, Case

pytestmark = pytest.mark.occt

#: Multipliers applied to one parameter at a time. Deliberately modest and
#: awkward — round numbers can hide an ordering bug that ties would expose.
MULTIPLIERS = (0.83, 1.0, 1.27)

#: Every document, with the one that cuts a helix marked. A sweep over it is
#: tens of real thread cuts, so `-m "not slow"` leaves it out while iterating.
#: Marked here rather than per test, because all seven tests below sweep it.
CASES = [
    pytest.param(
        case,
        id=case.name,
        marks=[pytest.mark.slow] if case.name == "tapped cavity" else [],
    )
    for case in SUITE
]


@pytest.fixture(scope="module")
def kernel() -> OcctKernel:
    return OcctKernel()


def build(kernel: OcctKernel, document: dict[str, Any], **values: float) -> RecomputeResult:
    data = copy.deepcopy(document)
    for row in data["parameters"]:
        if row["name"] in values:
            row["value"] = values[row["name"]]
    return recompute(Document.from_dict(data), kernel)


def tags(result: RecomputeResult) -> list[str]:
    """Every face name in the model, ordinals and all, in a stable order."""
    return sorted(
        str(face.tag)
        for body in result.bodies
        if body.solid is not None
        for face in body.solid.topology.faces
    )


def value_of(document: dict[str, Any], name: str) -> float:
    return float(next(r for r in document["parameters"] if r["name"] == name)["value"])


def describe(result: RecomputeResult) -> list[str]:
    return [f"{o.id}: {o.error}" for o in result.failures()]


# --------------------------------------------------------------------------
# One parameter at a time
# --------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES)
def test_the_baseline_builds(
    kernel: OcctKernel, case: Case
) -> None:
    result = build(kernel, case.document)
    assert result.ok, describe(result)
    assert tags(result), f"{case.name} produced no named faces"


@pytest.mark.parametrize("case", CASES)
def test_no_name_moves_when_one_parameter_changes(
    kernel: OcctKernel, case: Case
) -> None:
    """The whole point, stated once per document."""
    baseline = tags(build(kernel, case.document))

    for parameter in case.sweep:
        original = value_of(case.document, parameter)
        for multiplier in MULTIPLIERS:
            result = build(kernel, case.document, **{parameter: original * multiplier})
            assert result.ok, f"{case.name}: {parameter} x{multiplier} — {describe(result)}"
            assert tags(result) == baseline, (
                f"{case.name}: changing {parameter} to {original * multiplier:g} moved a name"
            )


@pytest.mark.parametrize("case", CASES)
def test_no_name_moves_when_every_parameter_changes_at_once(
    kernel: OcctKernel, case: Case
) -> None:
    """Interactions, not just one dimension at a time.

    Each parameter gets a different multiplier so the model is stretched out of
    proportion rather than scaled — scaling can mask an ordering that depends on
    relative size.
    """
    baseline = tags(build(kernel, case.document))
    spread = (0.88, 1.19, 0.94, 1.31, 1.07, 0.91)

    changes = {
        parameter: value_of(case.document, parameter) * spread[index % len(spread)]
        for index, parameter in enumerate(case.sweep)
    }
    result = build(kernel, case.document, **changes)
    assert result.ok, f"{case.name}: {describe(result)}"
    assert tags(result) == baseline, f"{case.name}: a combined change moved a name"


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES)
def test_rebuilding_the_same_document_is_identical(
    kernel: OcctKernel, case: Case
) -> None:
    """Same input, same output — including the order faces are reported in."""
    first = build(kernel, case.document)
    second = build(kernel, case.document)
    assert [str(f.tag) for f in first.topology.faces] == [
        str(f.tag) for f in second.topology.faces
    ]


@pytest.mark.parametrize("case", CASES)
def test_a_round_trip_through_a_parameter_returns_the_same_model(
    kernel: OcctKernel, case: Case
) -> None:
    """Change a dimension and change it back; nothing may be left behind."""
    baseline = tags(build(kernel, case.document))
    parameter = case.sweep[0]
    original = value_of(case.document, parameter)

    build(kernel, case.document, **{parameter: original * 1.4})
    restored = build(kernel, case.document, **{parameter: original})
    assert tags(restored) == baseline, f"{case.name}: a round trip through {parameter} drifted"


# --------------------------------------------------------------------------
# Selectors, which are what the names are for
# --------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES)
def test_every_selector_in_the_document_keeps_resolving(
    kernel: OcctKernel, case: Case
) -> None:
    """A stable name is only worth having if the selector using it still hits.

    Every feature that carries a selector is re-resolved after the sweep, and a
    failure surfaces as a failed feature — which is what makes this stronger
    than comparing tags alone.
    """
    for parameter in case.sweep:
        original = value_of(case.document, parameter)
        for multiplier in (0.8, 1.3):
            result = build(kernel, case.document, **{parameter: original * multiplier})
            assert result.ok, (
                f"{case.name}: a selector stopped resolving when {parameter} became "
                f"{original * multiplier:g} — {describe(result)}"
            )


# --------------------------------------------------------------------------
# Degenerate input is refused, not silently mis-built
# --------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES)
def test_a_zero_dimension_fails_loudly(
    kernel: OcctKernel, case: Case
) -> None:
    """Zero is where a kernel is most likely to return something plausible.

    The requirement is only that it does not build *and claim success*; which
    feature reports the problem is up to the model.
    """
    for parameter in case.dimensions:
        result = build(kernel, case.document, **{parameter: 0.0})
        assert not result.ok, f"{case.name}: {parameter}=0 built without complaint"


# --------------------------------------------------------------------------
# A modelled thread, next to faces that can split
# --------------------------------------------------------------------------


def _without_thread(result: RecomputeResult) -> list[str]:
    """Every name the threaded feature did not make, ordinals stripped.

    Stripped rather than kept, so a wall that split into a different number of
    fragments shows up as a changed *count* of the same name — which is the
    shape the failure actually took.

    The whole ``m8`` feature is excluded, not only its flanks. A helix that runs
    all the way round cuts the bore wall into a spiral, and the spiral is one
    face per turn — so ``m8/wall[bores.tap]`` counts turns just as honestly as
    the flanks do, and for the same reason. It was constant here only while the
    groove was not reaching round.
    """
    return sorted(
        str(face.tag).split("#")[0]
        for body in result.bodies
        if body.solid is not None
        for face in body.solid.topology.faces
        if not str(face.tag).startswith("m8/")
    )


@pytest.fixture(scope="module")
def cavity_without_thread(kernel: OcctKernel) -> list[str]:
    """Built once. Every rebuild of this document cuts a real helix."""
    return _without_thread(build(kernel, TAPPED_CAVITY))


@pytest.mark.slow
@pytest.mark.parametrize("deep", [6.0, 12.7, 15.0], ids=lambda v: f"{v:g}mm")
def test_a_deeper_thread_leaves_the_cavity_alone(
    kernel: OcctKernel, cavity_without_thread: list[str], deep: float
) -> None:
    """Change how far the helix reaches; the cavity keeps its wall count.

    The thread's own fragments are expected to change — a longer thread has more
    turns and honestly more flanks, which is why ``tap_deep`` is not in this
    document's sweep. What must not change is anything else, and "anything else"
    here includes a pocket wall and two blended uprights that *can* split.

    ``test_threads.py`` makes the same claim against a bare pad. Six faces, none
    of which can split, so the assertion passed without ever being tested.

    Note what this still cannot see. Like every test in this module it compares
    two builds by the same kernel, so a change that moves *both* leaves it
    green — verified against the thread-tool regression described in
    ``test_kernel_baseline.py``, which this test does not catch and that one
    does. The two are complements, not overlapping.
    """
    result = build(kernel, TAPPED_CAVITY, tap_deep=deep)

    assert result.ok, f"tap_deep={deep:g}: {describe(result)}"
    assert _without_thread(result) == cavity_without_thread, (
        f"a thread {deep:g}mm deep renamed or re-split a face that is not the thread"
    )
