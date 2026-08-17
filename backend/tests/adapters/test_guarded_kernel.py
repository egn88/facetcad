"""A wedged geometry call must not take the server with it.

The premise, measured rather than assumed: OpenCascade holds the interpreter
lock for the whole of a call. A 12.87s operation let another thread run three
times out of a possible ~1,280, and a SIGALRM set at 0.3s did not fire until it
returned. So a timeout on a thread cannot interrupt one, asyncio cannot, and a
signal handler — being Python that only runs between bytecodes — cannot either.

That leaves killing a process, which is what this adapter is for.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("OCP", reason="requires the optional OCCT extra")

from facet.adapters.geometry.guarded import (
    GuardedKernel,
    KernelBusy,
    KernelRestarted,
    KernelTimeout,
)
from facet.application.ports.geometry import (
    BlendRequest,
    Capability,
    CurveType,
    PadRequest,
    Profile,
    ProfileCurve,
)
from facet.domain.errors import FeatureBuildError
from facet.domain.math3d import Frame, Vec2

pytestmark = pytest.mark.occt


def square(size: float = 40.0) -> Profile:
    corners = [(0, 0), (size, 0), (size, size), (0, size)]
    curves = tuple(
        ProfileCurve(
            id=chr(97 + i),
            type=CurveType.LINE,
            start=Vec2(*corners[i]),
            end=Vec2(*corners[(i + 1) % 4]),
        )
        for i in range(4)
    )
    return Profile(sketch="s", loop="outer", frame=Frame.world(), curves=curves)


@pytest.fixture
def kernel():
    guarded = GuardedKernel("occt", timeout=30.0)
    yield guarded
    guarded.close()


def rounded_block(kernel: GuardedKernel):
    """A solid with curved faces, so tessellation tolerance actually costs."""
    pad = kernel.pad(PadRequest(feature="p", profile=square(), length=20.0))
    edges = tuple(e.ref for e in pad.edges)[:4]
    return kernel.fillet(pad.solid, BlendRequest(feature="f", edges=edges, size=8.0))


# -- it is a real kernel ---------------------------------------------------


def test_it_reports_the_kernel_behind_it(kernel: GuardedKernel) -> None:
    assert kernel.name == "occt"
    assert Capability.PAD in kernel.capabilities


def test_geometry_survives_the_process_boundary(kernel: GuardedKernel) -> None:
    result = kernel.pad(PadRequest(feature="p", profile=square(20.0), length=10.0))
    assert len(result.faces) == 6
    assert kernel.volume(result.solid) == pytest.approx(4000.0)
    assert kernel.tessellate(result.solid).triangle_count == 12


def test_a_geometry_error_comes_back_as_itself(kernel: GuardedKernel) -> None:
    """Not flattened to RuntimeError.

    The HTTP layer maps a document error to 422 and the diagnostics panel reads
    the feature id off it. Losing the type would lose the useful half.
    """
    pad = kernel.pad(PadRequest(feature="p", profile=square(20.0), length=10.0))
    with pytest.raises(FeatureBuildError) as caught:
        kernel.fillet(pad.solid, BlendRequest(feature="f", edges=("nonexistent",), size=1.0))
    # The structured fields survive too — the diagnostics panel names the
    # feature from them, and a bare message would leave it with nothing.
    assert caught.value.feature == "f"
    assert "nonexistent" in str(caught.value)


# -- the point of it -------------------------------------------------------


def test_a_call_that_will_not_finish_is_stopped() -> None:
    guarded = GuardedKernel("occt", timeout=0.5)
    try:
        solid = rounded_block(guarded).solid
        started = time.monotonic()
        with pytest.raises(KernelTimeout) as caught:
            # Fine enough to run for seconds on curved faces.
            guarded.tessellate(solid, 1e-7)
        elapsed = time.monotonic() - started
        # Stopped on the deadline, not after the work finished.
        assert elapsed < 2.0, f"took {elapsed:.2f}s to give up on a 0.5s deadline"
        assert "tessellate" in str(caught.value)
        assert "Nothing was changed" in str(caught.value)
    finally:
        guarded.close()


def test_a_handle_from_a_killed_worker_is_refused_not_reused() -> None:
    """The dangerous case, and the reason handles carry a generation.

    A replacement worker numbers its solids from the start again, so the id that
    named a killed worker's solid names a *different* shape in the new one.
    Silently accepting it would export the wrong part.
    """
    guarded = GuardedKernel("occt", timeout=0.5)
    try:
        stale = rounded_block(guarded).solid
        with pytest.raises(KernelTimeout):
            guarded.tessellate(stale, 1e-7)

        with pytest.raises(KernelRestarted):
            guarded.tessellate(stale, 0.1)
    finally:
        guarded.close()


def test_work_continues_after_a_worker_is_killed() -> None:
    guarded = GuardedKernel("occt", timeout=0.5)
    try:
        with pytest.raises(KernelTimeout):
            guarded.tessellate(rounded_block(guarded).solid, 1e-7)

        # A fresh build, on the replacement worker, from scratch.
        rebuilt = guarded.pad(PadRequest(feature="p", profile=square(20.0), length=10.0))
        assert guarded.volume(rebuilt.solid) == pytest.approx(4000.0)
    finally:
        guarded.close()


def test_the_restart_hook_fires_so_caches_can_be_dropped() -> None:
    """Caches hold handles. They have to be told, or they hand back dead ones."""
    called: list[int] = []
    guarded = GuardedKernel("occt", timeout=0.5, on_restart=lambda: called.append(1))
    try:
        with pytest.raises(KernelTimeout):
            guarded.tessellate(rounded_block(guarded).solid, 1e-7)
        assert called == [1]
    finally:
        guarded.close()


def test_releasing_a_stale_handle_is_not_an_error() -> None:
    """Freeing memory that is already gone is done, not failed."""
    guarded = GuardedKernel("occt", timeout=0.5)
    try:
        stale = rounded_block(guarded).solid
        with pytest.raises(KernelTimeout):
            guarded.tessellate(stale, 1e-7)
        guarded.release(stale)  # must not raise
    finally:
        guarded.close()


# -- concurrency -----------------------------------------------------------


def test_concurrent_callers_do_not_corrupt_the_protocol() -> None:
    """One worker, one pipe, and FastAPI serves sync endpoints from a threadpool.

    Without serialising, two requests interleave their writes and each reads the
    other's reply. Measured before the lock existed: four concurrent callers,
    four protocol failures. This is the regression test for that.
    """
    import threading

    guarded = GuardedKernel("occt", timeout=30.0)
    try:
        assert guarded.name  # start the worker before the threads race for it
        failures: list[str] = []
        wrong: list[tuple[float, float]] = []

        def hammer(size: float) -> None:
            expected = size * size * 10.0
            try:
                for _ in range(6):
                    result = guarded.pad(
                        PadRequest(feature="p", profile=square(size), length=10.0)
                    )
                    # Each thread checks it got *its own* answer back. A crossed
                    # reply shows up here as another thread's volume.
                    got = guarded.volume(result.solid)
                    if abs(got - expected) > 1.0:
                        wrong.append((expected, got))
            except Exception as caught:  # reported, not raised, so all threads finish
                failures.append(f"{type(caught).__name__}: {caught}")

        threads = [threading.Thread(target=hammer, args=(s,)) for s in (10.0, 20.0, 30.0, 40.0)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)

        assert failures == []
        assert wrong == []
    finally:
        guarded.close()


def test_waiting_too_long_for_a_busy_worker_is_refused_not_queued() -> None:
    """An unbounded queue is how a server falls over while looking healthy."""
    import threading

    guarded = GuardedKernel("occt", timeout=30.0, queue_wait=0.1)
    try:
        solid = rounded_block(guarded).solid
        busy: list[BaseException] = []
        started = threading.Event()

        def occupy() -> None:
            started.set()
            guarded.tessellate(solid, 1e-7)  # seconds of work

        holder = threading.Thread(target=occupy)
        holder.start()
        started.wait(timeout=5)
        time.sleep(0.2)  # let it actually take the lock

        try:
            guarded.volume(solid)
        except KernelBusy as caught:
            busy.append(caught)
        holder.join(timeout=120)

        assert busy, "a caller waited indefinitely instead of being refused"
        assert "one rebuild runs at a time" in str(busy[0])
    finally:
        guarded.close()


def test_a_worker_that_dies_on_its_own_also_invalidates_its_handles() -> None:
    """Not every death is a deadline we noticed.

    A segfault or an OOM kill leaves the process gone with no timeout involved,
    and the next call simply starts a replacement. That path skipped the
    generation bump, so a handle from the dead worker passed the staleness check
    and reached a worker that had never issued it — observed in production as
    'unknown solid handle'. The dangerous version is quieter: the replacement
    numbers solids from s1 again, so the same id can name a *different* solid.
    """
    cleared: list[int] = []
    guarded = GuardedKernel("occt", timeout=30.0, on_restart=lambda: cleared.append(1))
    try:
        result = guarded.pad(PadRequest(feature="p", profile=square(20.0), length=10.0))

        # Kill it the way the OS would, with no deadline and no notice.
        guarded._process.kill()  # type: ignore[union-attr]
        guarded._process.join(timeout=5)  # type: ignore[union-attr]

        with pytest.raises(KernelRestarted):
            guarded.volume(result.solid)
        assert cleared == [1], "the caches were not told the worker had been replaced"

        # And the replacement is usable.
        rebuilt = guarded.pad(PadRequest(feature="p", profile=square(30.0), length=10.0))
        assert guarded.volume(rebuilt.solid) == pytest.approx(9000.0)
    finally:
        guarded.close()
