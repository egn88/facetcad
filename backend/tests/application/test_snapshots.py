"""Geometry that survives the process that built it.

A rebuild is already content-addressed, so the only thing between a warm rebuild
and a cold one is somewhere to put the bytes. Measured on a 35-feature document:
warm 2.5ms, cold 2.5s, and a restart threw the difference away every time.

What has to be true for that to be worth having is not "it is faster". It is
that a restored model is *the same model* — same names, same ordinals, same
solid — because everything downstream stores selectors against those names. So
most of this file is about equality, and the rest is about the failure modes
degrading to a rebuild rather than to a wrong answer.

Driven through the analytic kernel, so it runs without the OCCT extra. The port
conformance suite asserts the same identity properties against both adapters.
"""

from __future__ import annotations

import copy

import pytest

from facet.adapters.geometry.fake import FakeKernel
from facet.application.recompute import (
    Detail,
    FeatureStatus,
    RecomputeEngine,
    RecomputeResult,
)
from facet.domain.document import Document

from .test_recompute import BRACKET


class MemoryStore:
    """A snapshot store that keeps everything in a dict.

    Standing in for the filesystem so these tests are about the engine. The
    filesystem adapter's own behaviour is in ``tests/adapters``.
    """

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.loads = 0
        self.saves = 0

    def load(self, key: str) -> bytes | None:
        self.loads += 1
        return self.blobs.get(key)

    def save(self, key: str, blob: bytes) -> None:
        self.saves += 1
        self.blobs[key] = blob

    def clear(self) -> None:
        self.blobs.clear()


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


def build(store: MemoryStore | None, document: Document, detail: str = Detail.DRAFT):
    """A rebuild in a *fresh* engine, which is what a new process amounts to."""
    return RecomputeEngine(FakeKernel(), store).recompute(document, detail)


def bracket(**changes: float) -> Document:
    data = copy.deepcopy(BRACKET)
    for row in data["parameters"]:  # type: ignore[index]
        if row["name"] in changes:
            row["value"] = changes[row["name"]]
            row.pop("expr", None)
    return Document.from_dict(data)


#: A second pocket, in a corner the first one does not reach. Appending a
#: *duplicate* of the existing slot would remove no material and be refused,
#: which is correct behaviour and a useless fixture.
CORNER_SKETCH: dict[str, object] = {
    "plane": "top",
    "points": {
        "r0": [8.0, 8.0],
        "r1": [28.0, 8.0],
        "r2": [28.0, 24.0],
        "r3": [8.0, 24.0],
    },
    "curves": [
        {"id": "d0", "start": "r0", "end": "r1"},
        {"id": "d1", "start": "r1", "end": "r2"},
        {"id": "d2", "start": "r2", "end": "r3"},
        {"id": "d3", "start": "r3", "end": "r0"},
    ],
    "loops": [{"id": "outer", "curves": ["d0", "d1", "d2", "d3"]}],
}

CORNER_POCKET: dict[str, object] = {
    "id": "corner",
    "type": "pocket",
    "profile": "corner.outer",
    "depth": "slot_d",
    "direction": "-normal",
}


def with_corner_pocket() -> Document:
    data = copy.deepcopy(BRACKET)
    data["sketches"]["corner"] = CORNER_SKETCH  # type: ignore[index]
    data["features"].append(copy.deepcopy(CORNER_POCKET))  # type: ignore[attr-defined]
    return Document.from_dict(data)


def tags(result: RecomputeResult) -> dict[str, list[str]]:
    return {
        body.id: sorted(str(face.tag) for face in body.topology.faces)
        for body in result.bodies
        if body.solid is not None
    }


def statuses(result: RecomputeResult) -> list[str]:
    return [o.status for o in result.outcomes]


# --------------------------------------------------------------------------
# The point
# --------------------------------------------------------------------------


def test_a_second_process_reuses_what_the_first_built(store: MemoryStore) -> None:
    first = build(store, bracket())
    assert statuses(first) == [FeatureStatus.BUILT, FeatureStatus.BUILT]

    second = build(store, bracket())
    assert statuses(second) == [FeatureStatus.CACHED, FeatureStatus.CACHED]


def test_a_restored_model_has_the_same_names(store: MemoryStore) -> None:
    """The assertion the whole feature stands on."""
    first = build(store, bracket())
    second = build(store, bracket())

    assert tags(second) == tags(first)
    assert tags(second)  # and it is not vacuously empty


def test_a_restored_model_has_the_same_edges(store: MemoryStore) -> None:
    """Edges are derived from face names, so they are a second check on refs."""
    first = build(store, bracket())
    second = build(store, bracket())

    def edges(result: RecomputeResult) -> list[str]:
        return sorted(str(e.tag) for e in result.bodies[0].topology.edges)

    assert edges(second) == edges(first)


def test_nothing_is_reused_without_a_store() -> None:
    assert statuses(build(None, bracket())) == [
        FeatureStatus.BUILT,
        FeatureStatus.BUILT,
    ]
    assert statuses(build(None, bracket())) == [
        FeatureStatus.BUILT,
        FeatureStatus.BUILT,
    ]


# --------------------------------------------------------------------------
# Invalidation — the half that has to be right
# --------------------------------------------------------------------------


def test_a_changed_parameter_is_not_served_from_a_snapshot(store: MemoryStore) -> None:
    """The failure that would matter: yesterday's geometry under today's numbers."""
    thin = build(store, bracket(plate_t=6.0))
    thick = build(store, bracket(plate_t=11.0))

    assert statuses(thick) == [FeatureStatus.BUILT, FeatureStatus.BUILT]
    assert thick.bodies[0].solid is not None
    assert _height(thick) != _height(thin)
    assert _height(thick) == pytest.approx(11.0)


def _height(result: RecomputeResult) -> float:
    centroids = [
        face.fingerprint.centroid.z for face in result.bodies[0].topology.faces
    ]
    return max(centroids)


def test_a_snapshot_is_kept_per_detail_level(store: MemoryStore) -> None:
    """Draft and full are different solids and must not answer for each other."""
    build(store, bracket(), Detail.DRAFT)
    full = build(store, bracket(), Detail.FULL)

    assert statuses(full) == [FeatureStatus.BUILT, FeatureStatus.BUILT]
    assert len(store.blobs) == 2


def test_appending_a_feature_resumes_from_the_stored_prefix(store: MemoryStore) -> None:
    """The common edit: everything before the new feature is already known."""
    build(store, bracket())
    grown = build(store, with_corner_pocket())

    assert grown.ok, [f"{o.id}: {o.error}" for o in grown.failures()]
    assert statuses(grown) == [
        FeatureStatus.CACHED,
        FeatureStatus.CACHED,
        FeatureStatus.BUILT,
    ]


def test_resuming_gives_the_same_names_as_a_full_rebuild(store: MemoryStore) -> None:
    """Resuming skips the naming engine's earlier passes, so this is not free.

    Split ordinals are resolved in the *owning* feature's frame. A restored
    prefix has run none of its features, so those frames have to be registered
    anyway — otherwise a new feature splitting one of their faces orders the
    fragments in the world frame and numbers them differently than a full
    rebuild would.
    """
    document = with_corner_pocket()

    build(store, bracket())  # seed the two-feature prefix
    resumed = build(store, document)
    fresh = build(None, document)

    assert resumed.ok, [f"{o.id}: {o.error}" for o in resumed.failures()]
    assert tags(resumed) == tags(fresh)


# --------------------------------------------------------------------------
# Everything that can go wrong is a rebuild, not a wrong answer
# --------------------------------------------------------------------------


def test_a_corrupt_snapshot_is_ignored(store: MemoryStore) -> None:
    build(store, bracket())
    for key in store.blobs:
        store.blobs[key] = b"shredded"

    result = build(store, bracket())
    assert statuses(result) == [FeatureStatus.BUILT, FeatureStatus.BUILT]
    assert result.ok


def test_a_truncated_snapshot_is_ignored(store: MemoryStore) -> None:
    build(store, bracket())
    for key, blob in list(store.blobs.items()):
        store.blobs[key] = blob[: len(blob) // 2]

    result = build(store, bracket())
    assert statuses(result) == [FeatureStatus.BUILT, FeatureStatus.BUILT]
    assert tags(result) == tags(build(None, bracket()))


def test_a_snapshot_from_another_kernel_is_ignored(store: MemoryStore) -> None:
    """Two kernels do not agree on refs, so one's bytes are the other's garbage."""
    build(store, bracket())
    engine = RecomputeEngine(FakeKernel(), store)
    # The key is namespaced by kernel name, so a different name simply misses;
    # this asserts the namespacing exists rather than that a mix-up is survived.
    object.__setattr__(engine, "_kernel", _RenamedKernel())
    result = engine.recompute(bracket())
    assert statuses(result) == [FeatureStatus.BUILT, FeatureStatus.BUILT]


class _RenamedKernel(FakeKernel):
    @property
    def name(self) -> str:
        return "not-the-analytic-one"


def test_a_failed_rebuild_is_not_stored(store: MemoryStore) -> None:
    """A history that stopped early never reached the state the key describes."""
    data = copy.deepcopy(BRACKET)
    data["features"].append(  # type: ignore[attr-defined]
        {
            "id": "impossible",
            "type": "pocket",
            "profile": "hole.outer",
            "depth": -5.0,
            "direction": "-normal",
        }
    )
    result = build(store, Document.from_dict(data))

    assert not result.ok
    assert store.saves == 0
    assert not store.blobs


def test_a_store_that_never_answers_costs_only_speed(store: MemoryStore) -> None:
    class Amnesiac(MemoryStore):
        def save(self, key: str, blob: bytes) -> None:
            self.saves += 1  # accepted and discarded, as a full store would

    forgetful = Amnesiac()
    first = build(forgetful, bracket())
    second = build(forgetful, bracket())

    assert first.ok and second.ok
    assert tags(second) == tags(first)
    assert statuses(second) == [FeatureStatus.BUILT, FeatureStatus.BUILT]
