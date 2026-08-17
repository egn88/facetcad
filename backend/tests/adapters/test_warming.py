"""Preparing export geometry before anyone asks for it.

Two things have to hold. The useful one: after a warm, an export-detail rebuild
finds its geometry instead of building it. The important one: nothing about
warming may be load-bearing. It runs on a thread nobody waits for, in a kernel
that may be killed, against a project that may be deleted underneath it — and
every one of those has to end in "the next export does the work it would have
done anyway".

Driven through the analytic kernel with the pause turned off, so the tests are
about the mechanism rather than about the clock.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from facet.adapters.geometry.fake import FakeKernel
from facet.adapters.persistence.filesystem import FilesystemDocumentRepository
from facet.adapters.persistence.snapshots import FilesystemSnapshotStore
from facet.adapters.warming import BackgroundWarmer, NoWarming
from facet.application.ports.warming import Warmer
from facet.application.recompute import Detail, FeatureStatus, RecomputeEngine
from facet.application.services import ProjectService
from facet.domain.document import Document

from ..application.test_recompute import BRACKET


@pytest.fixture
def repository(tmp_path: Path) -> FilesystemDocumentRepository:
    repo = FilesystemDocumentRepository(tmp_path / "projects")
    repo.create("bracket", Document.from_dict(BRACKET))
    return repo


@pytest.fixture
def store(tmp_path: Path) -> FilesystemSnapshotStore:
    return FilesystemSnapshotStore(tmp_path / "snapshots")


def warmer(
    repository: FilesystemDocumentRepository,
    store: FilesystemSnapshotStore,
    kernel=FakeKernel,
) -> BackgroundWarmer:
    return BackgroundWarmer(repository, store, kernel, quiet_seconds=0.0)


# --------------------------------------------------------------------------
# It satisfies the port
# --------------------------------------------------------------------------


def test_both_warmers_satisfy_the_port(
    repository: FilesystemDocumentRepository, store: FilesystemSnapshotStore
) -> None:
    assert isinstance(NoWarming(), Warmer)
    hot = warmer(repository, store)
    try:
        assert isinstance(hot, Warmer)
    finally:
        hot.close()


# --------------------------------------------------------------------------
# The useful half
# --------------------------------------------------------------------------


def test_warming_makes_an_export_rebuild_cached(
    repository: FilesystemDocumentRepository, store: FilesystemSnapshotStore
) -> None:
    """The whole point: full detail, built before it was asked for."""
    hot = warmer(repository, store)
    try:
        hot.schedule("bracket")
        assert hot.drain(30), "the warmer did not finish"
    finally:
        hot.close()

    engine = RecomputeEngine(FakeKernel(), store)
    result = engine.recompute(repository.load("bracket"), Detail.FULL)
    assert [o.status for o in result.outcomes] == [
        FeatureStatus.CACHED,
        FeatureStatus.CACHED,
    ]


def test_warming_does_not_prepare_the_viewport_detail(
    repository: FilesystemDocumentRepository, store: FilesystemSnapshotStore
) -> None:
    """Draft is already fast and is not what an export waits for.

    Warming it too would double the work for no gain, and the two are cached
    separately on purpose.
    """
    hot = warmer(repository, store)
    try:
        hot.schedule("bracket")
        assert hot.drain(30)
    finally:
        hot.close()

    draft = RecomputeEngine(FakeKernel(), store).recompute(
        repository.load("bracket"), Detail.DRAFT
    )
    assert [o.status for o in draft.outcomes] == [
        FeatureStatus.BUILT,
        FeatureStatus.BUILT,
    ]


def test_the_second_kernel_is_released_when_the_queue_empties(
    repository: FilesystemDocumentRepository, store: FilesystemSnapshotStore
) -> None:
    """A server nobody is editing must not be running two kernels."""
    closed: list[bool] = []

    class Closeable(FakeKernel):
        def close(self) -> None:
            closed.append(True)

    hot = warmer(repository, store, Closeable)
    try:
        hot.schedule("bracket")
        assert hot.drain(30)
    finally:
        hot.close()

    assert closed, "the warming kernel was never closed"


# --------------------------------------------------------------------------
# Repeated and overlapping requests
# --------------------------------------------------------------------------


def test_a_burst_of_edits_is_one_warm(
    repository: FilesystemDocumentRepository, store: FilesystemSnapshotStore
) -> None:
    """Dragging a slider is dozens of edits, each superseding the last."""
    started: list[str] = []

    class Counting(FakeKernel):
        def pad(self, request):  # type: ignore[no-untyped-def]
            started.append(request.feature)
            return super().pad(request)

    hot = BackgroundWarmer(repository, store, Counting, quiet_seconds=0.05)
    try:
        for _ in range(20):
            hot.schedule("bracket")
        assert hot.drain(30)
    finally:
        hot.close()

    assert len(started) == 1, f"built {len(started)} times for one settled state"


def test_two_projects_are_both_warmed(
    repository: FilesystemDocumentRepository, store: FilesystemSnapshotStore
) -> None:
    repository.create("second", Document.from_dict(BRACKET))
    hot = warmer(repository, store)
    try:
        hot.schedule("bracket")
        hot.schedule("second")
        assert hot.drain(30)
    finally:
        hot.close()

    for project in ("bracket", "second"):
        result = RecomputeEngine(FakeKernel(), store).recompute(
            repository.load(project), Detail.FULL
        )
        assert [o.status for o in result.outcomes] == [
            FeatureStatus.CACHED,
            FeatureStatus.CACHED,
        ], project


# --------------------------------------------------------------------------
# Nothing about it may be load-bearing
# --------------------------------------------------------------------------


def test_scheduling_an_unknown_project_is_quiet(
    repository: FilesystemDocumentRepository, store: FilesystemSnapshotStore
) -> None:
    """It may have been deleted between the edit and the warm."""
    hot = warmer(repository, store)
    try:
        hot.schedule("no-such-project")
        assert hot.drain(30)
    finally:
        hot.close()


def test_a_kernel_that_cannot_build_is_quiet(
    repository: FilesystemDocumentRepository, store: FilesystemSnapshotStore
) -> None:
    class Broken(FakeKernel):
        def pad(self, request):  # type: ignore[no-untyped-def]
            raise RuntimeError("no geometry today")

    hot = warmer(repository, store, Broken)
    try:
        hot.schedule("bracket")
        assert hot.drain(30)
    finally:
        hot.close()

    # And the next export simply does the work.
    result = RecomputeEngine(FakeKernel(), store).recompute(
        repository.load("bracket"), Detail.FULL
    )
    assert result.ok
    assert [o.status for o in result.outcomes] == [
        FeatureStatus.BUILT,
        FeatureStatus.BUILT,
    ]


def test_a_kernel_that_cannot_be_created_is_quiet(
    repository: FilesystemDocumentRepository, store: FilesystemSnapshotStore
) -> None:
    def refuse():  # type: ignore[no-untyped-def]
        raise RuntimeError("no worker available")

    hot = warmer(repository, store, refuse)
    try:
        hot.schedule("bracket")
        assert hot.drain(30)
    finally:
        hot.close()


def test_scheduling_after_close_does_nothing(
    repository: FilesystemDocumentRepository, store: FilesystemSnapshotStore
) -> None:
    hot = warmer(repository, store)
    hot.close()
    hot.schedule("bracket")
    hot.close()  # twice is safe


def test_scheduling_never_blocks_the_caller(
    repository: FilesystemDocumentRepository, store: FilesystemSnapshotStore
) -> None:
    """It is called on the request path, so it has to return at once."""
    release = threading.Event()

    class Slow(FakeKernel):
        def pad(self, request):  # type: ignore[no-untyped-def]
            release.wait(10)
            return super().pad(request)

    hot = warmer(repository, store, Slow)
    try:
        hot.schedule("bracket")
        # If schedule() waited on the build, this line would not run until the
        # event was set -- and nothing has set it.
        assert not release.is_set()
        hot.schedule("bracket")
        assert not release.is_set()
    finally:
        release.set()
        hot.close()


# --------------------------------------------------------------------------
# Through the service, which is what actually calls it
# --------------------------------------------------------------------------


class Recording:
    def __init__(self) -> None:
        self.scheduled: list[str] = []

    def schedule(self, project_id: str) -> None:
        self.scheduled.append(project_id)

    def close(self) -> None:
        return


def test_an_edit_schedules_a_warm(
    repository: FilesystemDocumentRepository, store: FilesystemSnapshotStore
) -> None:
    spy = Recording()
    service = ProjectService(repository, FakeKernel(), snapshots=store, warmer=spy)
    service.update_parameters("bracket", {"plate_t": 8.0})
    assert spy.scheduled == ["bracket"]


def test_opening_a_project_schedules_a_warm(
    repository: FilesystemDocumentRepository, store: FilesystemSnapshotStore
) -> None:
    """Open-then-export is as common as edit-then-export."""
    spy = Recording()
    service = ProjectService(repository, FakeKernel(), snapshots=store, warmer=spy)
    service.view_state("bracket")
    assert spy.scheduled == ["bracket"]


def test_a_warmer_that_throws_does_not_fail_the_edit(
    repository: FilesystemDocumentRepository, store: FilesystemSnapshotStore
) -> None:
    """The failure worth guarding: a saved edit reported as a failed one."""

    class Hostile:
        def schedule(self, project_id: str) -> None:
            raise RuntimeError("warmer is broken")

        def close(self) -> None:
            return

    service = ProjectService(repository, FakeKernel(), snapshots=store, warmer=Hostile())
    result = service.update_parameters("bracket", {"plate_t": 8.0})
    assert result.ok
    assert service.load("bracket").parameters["plate_t"].value == 8.0


def test_no_warmer_configured_changes_nothing(
    repository: FilesystemDocumentRepository, store: FilesystemSnapshotStore
) -> None:
    service = ProjectService(repository, FakeKernel(), snapshots=store)
    assert service.update_parameters("bracket", {"plate_t": 8.0}).ok


# --------------------------------------------------------------------------
# When it must refuse to exist
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ({}, "BackgroundWarmer"),
        ({"FACET_WARM": "off"}, "NoWarming"),
        ({"FACET_WARM": "0"}, "NoWarming"),
        # Without isolation, build_kernel returns an in-process kernel. A
        # background thread calling OpenCascade holds the interpreter lock for
        # the whole rebuild -- the exact freeze the child process exists to
        # prevent, on a thread nobody asked for.
        ({"FACET_GEOMETRY_ISOLATION": "off"}, "NoWarming"),
    ],
    ids=["default", "off", "0", "no isolation"],
)
def test_warming_is_declined_when_it_would_be_unsafe_or_unwanted(
    monkeypatch: pytest.MonkeyPatch,
    repository: FilesystemDocumentRepository,
    store: FilesystemSnapshotStore,
    environment: dict[str, str],
    expected: str,
) -> None:
    from facet import main

    for name in ("FACET_WARM", "FACET_GEOMETRY_ISOLATION"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    built = main._build_warmer(repository, store)
    try:
        assert type(built).__name__ == expected
    finally:
        built.close()


def test_warming_is_declined_without_a_snapshot_store(
    monkeypatch: pytest.MonkeyPatch, repository: FilesystemDocumentRepository
) -> None:
    """"Build it now, find it later" needs a later."""
    from facet import main

    monkeypatch.delenv("FACET_WARM", raising=False)
    assert isinstance(main._build_warmer(repository, None), NoWarming)
