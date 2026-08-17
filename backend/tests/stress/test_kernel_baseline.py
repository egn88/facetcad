"""Names pinned to recorded values, not merely to each other.

The sweep battery next door asks whether a name moves when a *dimension*
changes. It cannot ask whether a name moved when the *kernel* changed, because
it compares one build against another build by the same code — shift every build
consistently and the sweeps agree with themselves and pass.

That gap is not hypothetical. A thread-tool optimisation was measured skipping
the intersection that seals the swept segments together; the cut then left a
1.8mm3 sliver and split ``cav/wall[cavity.wall_2]`` into a different number of
fragments — a moved ordinal on a wall the thread does not touch. Every sweep
still passed, because the baseline it compared against had moved too.

So the tags and volumes live in ``baseline.json``, checked in. A kernel change
that alters a name has to change that file, in a diff a reviewer can see and
argue with. Regenerate deliberately:

    FACET_UPDATE_BASELINE=1 pytest tests/stress/test_kernel_baseline.py

and then read the diff before committing it. An empty diff is the normal
outcome of an optimisation; a non-empty one is a claim that the geometry should
be different.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("OCP", reason="requires the optional OCCT extra")

from facet.adapters.geometry.occt import OcctKernel
from facet.application.recompute import Detail, RecomputeEngine, RecomputeResult
from facet.domain.document import Document

from .documents import SUITE, Case

pytestmark = pytest.mark.occt

BASELINE = Path(__file__).parent / "baseline.json"

UPDATING = os.environ.get("FACET_UPDATE_BASELINE") == "1"

#: Volumes are compared as a fraction, not to the last bit. A boolean is not
#: guaranteed bit-reproducible across OCCT builds, and pinning absolute digits
#: would make this fail on an upgrade for no reason. Loose enough to survive
#: that, four orders tighter than the 5e-6 the sliver above shifted things by.
VOLUME_TOLERANCE = 1e-9


def measure(kernel: OcctKernel, case: Case, detail: str) -> dict[str, Any]:
    """Everything about a build that a rename or a stray sliver would disturb."""
    document = Document.from_dict(case.document)
    result: RecomputeResult = RecomputeEngine(kernel).recompute(document, detail)
    assert result.ok, [f"{o.id}: {o.error}" for o in result.failures()]

    bodies: dict[str, Any] = {}
    for body in result.bodies:
        if body.solid is None:
            continue
        bodies[body.id] = {
            # Sorted, ordinals included: a split shows up as a changed list.
            "tags": sorted(str(face.tag) for face in body.topology.faces),
            "volume": kernel.volume(body.solid.handle),
        }
    return bodies


def record() -> dict[str, Any]:
    kernel = OcctKernel()
    return {
        f"{case.name}/{detail}": measure(kernel, case, detail)
        for case in SUITE
        for detail in (Detail.DRAFT, Detail.FULL)
    }


@pytest.fixture(scope="module")
def baseline() -> dict[str, Any]:
    if UPDATING:
        BASELINE.write_text(json.dumps(record(), indent=1, sort_keys=True) + "\n")
    if not BASELINE.exists():
        pytest.fail(
            f"{BASELINE.name} is missing. Create it with "
            "FACET_UPDATE_BASELINE=1 pytest tests/stress/test_kernel_baseline.py"
        )
    return json.loads(BASELINE.read_text())


@pytest.fixture(scope="module")
def kernel() -> OcctKernel:
    return OcctKernel()


CASES = [(case, detail) for case in SUITE for detail in (Detail.DRAFT, Detail.FULL)]
IDS = [f"{case.name}/{detail}" for case, detail in CASES]


@pytest.mark.parametrize(("case", "detail"), CASES, ids=IDS)
def test_every_name_is_the_one_that_was_recorded(
    kernel: OcctKernel, baseline: dict[str, Any], case: Case, detail: str
) -> None:
    key = f"{case.name}/{detail}"
    assert key in baseline, (
        f"no baseline for {key}; regenerate with FACET_UPDATE_BASELINE=1"
    )
    expected = baseline[key]
    actual = measure(kernel, case, detail)

    assert sorted(actual) == sorted(expected), f"{key}: the set of bodies changed"

    for body, recorded in expected.items():
        assert actual[body]["tags"] == recorded["tags"], (
            f"{key}, body {body}: a face name changed. If the geometry is meant "
            "to be different, regenerate the baseline and justify the diff."
        )


@pytest.mark.parametrize(("case", "detail"), CASES, ids=IDS)
def test_no_body_quietly_changes_volume(
    kernel: OcctKernel, baseline: dict[str, Any], case: Case, detail: str
) -> None:
    """The names can all hold while the solid is subtly wrong.

    A tool that fails to seal leaves a sliver behind and renames nothing. This
    is the assertion that catches it.
    """
    key = f"{case.name}/{detail}"
    expected = baseline[key]
    actual = measure(kernel, case, detail)

    for body, recorded in expected.items():
        was, is_now = recorded["volume"], actual[body]["volume"]
        assert is_now == pytest.approx(was, rel=VOLUME_TOLERANCE), (
            f"{key}, body {body}: volume moved from {was!r} to {is_now!r} "
            f"({abs(is_now - was) / was:.2e} relative) while every name held"
        )
