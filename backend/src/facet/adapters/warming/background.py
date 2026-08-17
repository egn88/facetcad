"""Rebuilding at export detail, on a thread, in a kernel of its own.

Why a second kernel rather than the one already running: geometry is serialised.
There is one worker, one pipe and one lock, so warming through the foreground
kernel would not be background work at all — it would put a nine-second rebuild
in front of the next click.

Why that is safe here, when a worker *pool* was measured and rejected: the two
kernels never exchange a handle. A handle names memory inside one worker, which
is why sharding solids across several was a much larger problem than it looked.
These two exchange snapshot *bytes* through the content-addressed store, and
bytes do not care which process wrote them.

What it costs: a second OpenCascade process while a warm is running. It is
spawned when there is something to warm and closed when the queue empties, so
steady-state memory is unchanged and the ~1s of importing OCP is paid on a
thread nobody is waiting for.

Everything here fails silently on purpose. A warm that does not happen costs the
next export the rebuild it would have done anyway.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from facet.application.ports.geometry import GeometryKernel
from facet.application.ports.repository import DocumentRepository
from facet.application.ports.snapshots import SnapshotStore
from facet.application.recompute import Detail, RecomputeEngine

logger = logging.getLogger(__name__)

#: How long to wait for quiet before starting. Dragging a parameter slider is
#: dozens of edits in a couple of seconds, and each supersedes the last — so the
#: useful moment to start is when they stop, not when they begin.
DEFAULT_QUIET_SECONDS = 3.0


class BackgroundWarmer:
    """Keeps export-detail geometry ready for recently-edited projects.

    One thread, one project at a time, latest request wins. A project scheduled
    while another is building is queued; a project scheduled twice is queued
    once, because the second request wants the same end state as the first.
    """

    def __init__(
        self,
        repository: DocumentRepository,
        snapshots: SnapshotStore,
        kernel: Callable[[], GeometryKernel],
        *,
        quiet_seconds: float = DEFAULT_QUIET_SECONDS,
    ) -> None:
        self._repository = repository
        self._snapshots = snapshots
        self._make_kernel = kernel
        self._quiet = quiet_seconds

        self._lock = threading.Lock()
        #: Projects waiting, in insertion order and each at most once.
        self._pending: dict[str, None] = {}
        self._wake = threading.Event()
        self._closed = False
        self._idle = threading.Event()
        self._idle.set()
        self._thread: threading.Thread | None = None
        #: Built on first use and dropped when the queue empties, so a server
        #: nobody is editing runs one OpenCascade process, not two.
        self._kernel: GeometryKernel | None = None

    # -- the port ----------------------------------------------------------

    def schedule(self, project_id: str) -> None:
        with self._lock:
            if self._closed:
                return
            self._pending[project_id] = None
            self._idle.clear()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._serve, name="facet-warmer", daemon=True
                )
                self._thread.start()
        self._wake.set()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._pending.clear()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        self._release()

    # -- for tests ---------------------------------------------------------

    def drain(self, timeout: float = 60.0) -> bool:
        """Wait until nothing is queued or building. Returns False on timeout."""
        return self._idle.wait(timeout)

    # -- the thread --------------------------------------------------------

    def _serve(self) -> None:
        while True:
            # Wait for work, then wait for quiet. A burst of edits lands in the
            # queue and only the state after the last one is worth building.
            if not self._wake.wait(timeout=30.0):
                if self._drop_through():
                    return
                continue
            self._wake.clear()
            if self._quiet:
                # Any new request during the pause re-sets the event, so this
                # restarts the countdown rather than racing it.
                while self._wake.wait(timeout=self._quiet):
                    self._wake.clear()

            while True:
                project = self._take()
                if project is None:
                    break
                self._warm(project)

            with self._lock:
                if not self._pending:
                    self._release()
                    self._idle.set()
                    if self._closed:
                        return

    def _drop_through(self) -> bool:
        """Nothing for a while: let the thread go and release the kernel."""
        with self._lock:
            if self._pending:
                return False
            self._release()
            self._idle.set()
            self._thread = None
            return True

    def _take(self) -> str | None:
        """The oldest waiting project, left in the queue until it is done.

        Left in, so a request arriving while it builds is folded into the one
        already running rather than queueing a second identical build.
        """
        with self._lock:
            if self._closed or not self._pending:
                return None
            return next(iter(self._pending))

    def _warm(self, project_id: str) -> None:
        with self._lock:
            self._pending.pop(project_id, None)
        try:
            document = self._repository.load(project_id)
        except Exception:
            # Deleted, renamed, or mid-write. Nothing to warm and nothing wrong.
            logger.debug("warming skipped: %s could not be loaded", project_id)
            return
        try:
            kernel = self._ensure_kernel()
            RecomputeEngine(kernel, self._snapshots).recompute(document, Detail.FULL)
        except Exception:
            # A geometry failure, a timeout, a killed worker. The document is
            # unaffected -- nothing here writes one -- and the next export does
            # the rebuild it would have done anyway.
            logger.debug("warming %s did not finish", project_id, exc_info=True)
            self._release()

    def _ensure_kernel(self) -> GeometryKernel:
        if self._kernel is None:
            self._kernel = self._make_kernel()
        return self._kernel

    def _release(self) -> None:
        kernel = self._kernel
        self._kernel = None
        if kernel is None:
            return
        closer = getattr(kernel, "close", None)
        if closer is None:
            return
        try:
            closer()
        except Exception:  # pragma: no cover - shutdown is best effort
            logger.debug("warming kernel did not close cleanly", exc_info=True)


class NoWarming:
    """The do-nothing warmer, for when it is turned off or unavailable."""

    def schedule(self, project_id: str) -> None:
        return

    def close(self) -> None:
        return
