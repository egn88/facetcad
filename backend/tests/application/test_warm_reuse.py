"""Work that has already been done is not done again.

The engine and the service both cache, and both were being defeated by the same
mistake: the thing that makes a *cold* start cheap was reached for before the
thing that makes a *warm* one free. A restore is not free — it reads a file,
hands the bytes to the kernel, and has the kernel re-derive every face's
fingerprint before it will trust the stored names — and doing it on every
request cost 300ms against 55ms on a fourteen-body document, then threw away the
triangles too, because the restored solid arrives under a handle nobody has seen
before.

So these tests are about *not* working: no load from the store when memory has
the answer, no tessellation when the geometry has not changed, and no rebuild of
a prefix an earlier run already paid for.

Driven through the analytic kernel, which mints a fresh handle on restore
exactly as OpenCascade does, so the defect these guard against reproduces
without the OCCT extra installed.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from facet.adapters.geometry.fake import FakeKernel
from facet.adapters.persistence.filesystem import FilesystemDocumentRepository
from facet.application.ports.geometry import SolidHandle, Tessellation
from facet.application.recompute import Detail, FeatureStatus, RecomputeEngine
from facet.application.services import ProjectService
from facet.domain.document import Document

from .test_recompute import BRACKET
from .test_snapshots import MemoryStore, with_corner_pocket


class CountingKernel(FakeKernel):
    """Reports how much geometry it was actually asked to do."""

    def __init__(self) -> None:
        super().__init__()
        self.tessellations = 0
        self.restores = 0

    def tessellate(
        self, solid_handle: SolidHandle, tolerance: float = 0.1
    ) -> Tessellation:
        self.tessellations += 1
        return super().tessellate(solid_handle, tolerance)

    def restore(self, blob: bytes):  # type: ignore[no-untyped-def]
        self.restores += 1
        return super().restore(blob)


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def repository(tmp_path: Path) -> FilesystemDocumentRepository:
    repo = FilesystemDocumentRepository(tmp_path / "projects")
    repo.create("bracket", Document.from_dict(BRACKET))
    return repo


def service(
    repository: FilesystemDocumentRepository,
    store: MemoryStore,
    kernel: FakeKernel | None = None,
) -> ProjectService:
    return ProjectService(repository, kernel or CountingKernel(), snapshots=store)


# --------------------------------------------------------------------------
# The engine asks itself before it asks the disk
# --------------------------------------------------------------------------


def test_a_warm_engine_never_reads_the_store(store: MemoryStore) -> None:
    """The 300ms. A rebuild of a document this engine just built is free."""
    engine = RecomputeEngine(FakeKernel(), store)
    document = Document.from_dict(BRACKET)

    engine.recompute(document)
    settled = store.loads

    engine.recompute(document)
    engine.recompute(document)

    assert store.loads == settled


def test_a_warm_engine_never_restores(store: MemoryStore) -> None:
    """The half of it that costs the most: the kernel is not asked either."""
    kernel = CountingKernel()
    engine = RecomputeEngine(kernel, store)
    document = Document.from_dict(BRACKET)

    engine.recompute(document)
    engine.recompute(document)

    assert kernel.restores == 0


def test_a_cold_engine_still_restores(store: MemoryStore) -> None:
    """Which is the point of the store, and must survive the fix above."""
    document = Document.from_dict(BRACKET)
    RecomputeEngine(FakeKernel(), store).recompute(document)

    kernel = CountingKernel()
    result = RecomputeEngine(kernel, store).recompute(document)

    assert kernel.restores == 1
    assert [o.status for o in result.outcomes] == [
        FeatureStatus.CACHED,
        FeatureStatus.CACHED,
    ]


def test_a_remembered_feature_reports_its_face_count(store: MemoryStore) -> None:
    """A cached feature is still a diagnosable one.

    The count is what the panel shows, and reporting zero for a feature this
    process is holding the state of would make a warm rebuild look empty.
    """
    engine = RecomputeEngine(FakeKernel(), store)
    document = Document.from_dict(BRACKET)

    engine.recompute(document)
    warm = engine.recompute(document)

    assert [o.status for o in warm.outcomes] == [
        FeatureStatus.CACHED,
        FeatureStatus.CACHED,
    ]
    assert all(o.face_count > 0 for o in warm.outcomes)


def test_a_deeper_stored_state_still_wins_over_a_shallower_remembered_one(
    store: MemoryStore,
) -> None:
    """Depth decides, not which cache the state came from.

    After an edit early in a long history, the memory of the state before the
    edit is worth less than a snapshot of the state after it — so the search
    stays ordered by depth and consults memory only as the cheaper source at
    each one.
    """
    grown = with_corner_pocket()
    # One engine builds the whole three-feature history into the store.
    RecomputeEngine(FakeKernel(), store).recompute(grown)

    # Another knows only the two-feature prefix, in memory.
    engine = RecomputeEngine(FakeKernel(), store)
    engine.recompute(Document.from_dict(BRACKET))

    result = engine.recompute(grown)
    assert [o.status for o in result.outcomes] == [
        FeatureStatus.CACHED,
        FeatureStatus.CACHED,
        FeatureStatus.CACHED,
    ]


def test_an_edit_still_rebuilds_what_depends_on_it(store: MemoryStore) -> None:
    """The guard on all of the above: reuse must not outlive its key."""
    engine = RecomputeEngine(FakeKernel(), store)
    engine.recompute(Document.from_dict(BRACKET))

    changed = copy.deepcopy(BRACKET)
    for row in changed["parameters"]:  # type: ignore[index]
        if row["name"] == "plate_t":
            row["value"] = 12.0
            row.pop("expr", None)
    result = engine.recompute(Document.from_dict(changed))

    assert [o.status for o in result.outcomes] == [
        FeatureStatus.BUILT,
        FeatureStatus.BUILT,
    ]


# --------------------------------------------------------------------------
# Triangles belong to the geometry, not to the handle it arrived under
# --------------------------------------------------------------------------


def test_the_mesh_survives_a_restore(
    repository: FilesystemDocumentRepository, store: MemoryStore
) -> None:
    """A restored solid is the same shape, so it is the same triangles.

    Keyed on the kernel handle this could never hold: a restore issues a fresh
    id every time, so every request re-tessellated a body it had already
    tessellated. This is the assertion that the key describes the content.
    """
    kernel = CountingKernel()
    project = service(repository, store, kernel)

    project.view_state("bracket")
    settled = kernel.tessellations

    # Losing the engines is what a replaced geometry worker does, and it forces
    # the next rebuild back through the store.
    project.invalidate_caches()
    project.view_state("bracket")

    assert kernel.restores >= 1, "the rebuild did not go through the store"
    assert kernel.tessellations == settled


def test_a_warm_view_state_tessellates_nothing(
    repository: FilesystemDocumentRepository, store: MemoryStore
) -> None:
    kernel = CountingKernel()
    project = service(repository, store, kernel)

    project.view_state("bracket")
    settled = kernel.tessellations

    project.view_state("bracket")
    project.view_state("bracket")

    assert kernel.tessellations == settled


def test_an_edit_retessellates(
    repository: FilesystemDocumentRepository, store: MemoryStore
) -> None:
    """The other half: a mesh under a stale key would be the wrong picture."""
    kernel = CountingKernel()
    project = service(repository, store, kernel)

    project.view_state("bracket")
    settled = kernel.tessellations

    project.update_parameters("bracket", {"plate_t": 12.0})
    project.view_state("bracket")

    assert kernel.tessellations > settled


def test_the_two_detail_levels_do_not_evict_each_other(
    repository: FilesystemDocumentRepository, store: MemoryStore
) -> None:
    """A session alternates between drawing and exporting all day."""
    kernel = CountingKernel()
    project = service(repository, store, kernel)

    project.mesh("bracket", Detail.DRAFT)
    project.mesh("bracket", Detail.FULL)
    settled = kernel.tessellations

    project.mesh("bracket", Detail.DRAFT)
    project.mesh("bracket", Detail.FULL)

    assert kernel.tessellations == settled
