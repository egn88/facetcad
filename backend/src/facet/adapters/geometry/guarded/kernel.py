"""A geometry kernel that cannot freeze the server.

OpenCascade is a C++ library reached through Python bindings, and a call into it
holds the interpreter lock for its whole duration. Measured on this machine: a
boolean plus a fine mesh ran for 12.87 seconds and let another thread run three
times, against the ~1,280 turns it should have had. A ``SIGALRM`` set at 0.3s did
not fire until the call returned.

That rules out every in-process remedy. A timeout on a thread cannot interrupt
it, ``asyncio.wait_for`` cannot interrupt it, and a signal handler is Python code
that only runs between bytecodes — none of which happen while C++ has the lock.
Nothing short of another process can be stopped.

So the real kernel runs in a child, and this class is a stand-in that forwards to
it and gives up after a deadline. On a timeout the child is killed outright,
because a wedged worker cannot be asked politely, and a fresh one is started for
the next call. The parent process is never the one blocked, so the API keeps
answering — including the health check that a supervisor is watching.

The port was built for this. ``SolidHandle`` is an opaque id rather than a shape
precisely so the adapter could be moved out of process, and every request and
result across the port is a plain dataclass that pickles cleanly.
"""

from __future__ import annotations

import contextlib
import multiprocessing as mp
import os
import threading
from collections.abc import Callable
from multiprocessing.connection import Connection
from typing import Any

from facet.application.ports.geometry import (
    BlendRequest,
    BoundingBox,
    PadRequest,
    PocketRequest,
    SolidHandle,
    SolidResult,
    Tessellation,
    ThreadRequest,
)

from . import worker as worker_module

#: How long any one kernel call may take before the worker is killed.
#:
#: Generous on purpose. The slowest legitimate operation measured here is a
#: threaded rebuild at a few seconds, and a fine mesh over a boolean at thirteen;
#: a minute is far outside that, so anything reaching it is stuck rather than
#: busy. Too tight a deadline turns a slow part into a broken one.
DEFAULT_TIMEOUT = float(os.environ.get("FACET_GEOMETRY_TIMEOUT", "60"))

#: How long to wait for the child to import OpenCascade and report ready. The
#: import alone is seconds, and it is not the operation being timed.
STARTUP_TIMEOUT = 60.0

#: How long a request will wait for the worker to be free before giving up.
#:
#: There is one worker and one pipe, so geometry is strictly one at a time. Under
#: load that queue is where requests wait, and an unbounded one is how a server
#: falls over politely-looking: every client waits, nothing is refused, and the
#: queue grows until memory does. Refusing quickly is kinder than a reply nobody
#: is still waiting for.
DEFAULT_QUEUE_WAIT = float(os.environ.get("FACET_GEOMETRY_QUEUE_WAIT", "30"))


class KernelTimeout(RuntimeError):
    """A geometry operation exceeded its deadline and was killed.

    Distinct from a geometry *failure*: nothing is known about whether the
    operation would have succeeded, only that it stopped being worth waiting
    for. Callers should treat it as "try something simpler", not "this is
    impossible".
    """

    def __init__(self, method: str, seconds: float) -> None:
        super().__init__(
            f"geometry operation '{method}' exceeded {seconds:g}s and was stopped. "
            "The model may be too complex, or the operation may not terminate on "
            "this input. Nothing was changed."
        )
        self.method = method
        self.seconds = seconds


class KernelBusy(RuntimeError):
    """The geometry worker was occupied for too long to wait any longer.

    Not a failure of the request — it never ran. The distinction matters to a
    caller deciding whether to retry: this one is worth retrying, and a timeout
    or a geometry error is not.
    """

    def __init__(self, seconds: float) -> None:
        super().__init__(
            f"the geometry worker was busy for more than {seconds:g}s. "
            "Only one rebuild runs at a time; try again shortly."
        )
        self.seconds = seconds


class KernelRestarted(RuntimeError):
    """A handle was issued by a worker that no longer exists.

    Solids live in the worker's memory. When one is killed, every handle from it
    refers to nothing, and the caller has to rebuild rather than be handed a
    silently different shape.
    """


class GuardedKernel:
    """Runs a kernel in a child process, with a deadline on every call.

    Implements the geometry port by forwarding. It carries no geometry itself,
    which is what lets it survive the worker dying.
    """

    def __init__(
        self,
        kernel_name: str = "occt",
        *,
        timeout: float = DEFAULT_TIMEOUT,
        queue_wait: float = DEFAULT_QUEUE_WAIT,
        on_restart: Callable[[], None] | None = None,
    ) -> None:
        self._kernel_name = kernel_name
        self._timeout = timeout
        self._queue_wait = queue_wait
        # One worker, one pipe, and FastAPI runs sync endpoints in a threadpool:
        # without this two requests interleave their sends and each reads the
        # other's reply. Observed directly — four concurrent callers, four
        # protocol failures. The lock is not a throughput choice; the work was
        # already serial, because there is one worker.
        self._lock = threading.Lock()
        # Called when the worker is replaced, so caches holding now-dead handles
        # can be cleared. Without it a stale handle is retried forever.
        self._on_restart = on_restart
        self._process: mp.process.BaseProcess | None = None
        self._conn: Connection | None = None
        self._name = kernel_name
        self._capabilities: frozenset[str] = frozenset()
        #: Which worker a handle came from. Bumped when one is replaced — and
        #: bumped *there* rather than on the next start, because a handle is
        #: checked before the replacement worker is spawned. Bumping late let a
        #: dead handle through to a fresh worker, which numbers solids from s1
        #: again and so had a different shape under the same id.
        self._generation = 1

    def set_on_restart(self, callback: Callable[[], None]) -> None:
        """Register the cache-clearing hook.

        Set after construction because the thing that needs clearing — the
        service's per-project rebuild caches — is built around this kernel and
        cannot be passed to it.
        """
        self._on_restart = callback

    # -- identity ----------------------------------------------------------

    @property
    def name(self) -> str:
        self._ensure()
        return self._name

    @property
    def capabilities(self) -> frozenset[str]:
        self._ensure()
        return self._capabilities

    # -- the port ----------------------------------------------------------

    def pad(self, request: PadRequest) -> SolidResult:
        return self._tag(self._call("pad", request))

    def pocket(self, base: SolidHandle, request: PocketRequest) -> SolidResult:
        return self._tag(self._call("pocket", base, request))

    def fuse(self, base: SolidHandle, addition: SolidHandle) -> SolidResult:
        return self._tag(self._call("fuse", base, addition))

    def fillet(self, base: SolidHandle, request: BlendRequest) -> SolidResult:
        return self._tag(self._call("fillet", base, request))

    def chamfer(self, base: SolidHandle, request: BlendRequest) -> SolidResult:
        return self._tag(self._call("chamfer", base, request))

    def thread(self, base: SolidHandle, request: ThreadRequest) -> SolidResult:
        return self._tag(self._call("thread", base, request))

    def tessellate(self, solid: SolidHandle, tolerance: float = 0.1) -> Tessellation:
        return self._call("tessellate", solid, tolerance)

    def snapshot(self, solid: SolidHandle) -> bytes:
        return self._call("snapshot", solid)

    def restore(self, blob: bytes) -> SolidResult:
        """Rebuild a solid inside the worker from stored bytes.

        Tagged like any other result, and with the *current* generation — which
        is the point of restoring rather than reusing a handle. A snapshot
        outlives the worker that made it, so it is the one thing here that a
        restart does not invalidate.
        """
        return self._tag(self._call("restore", blob))

    def bounding_box(self, solid: SolidHandle) -> BoundingBox:
        return self._call("bounding_box", solid)

    def volume(self, solid: SolidHandle) -> float:
        return self._call("volume", solid)

    def export_brep(self, solid: SolidHandle, fmt: str) -> bytes:
        return self._call("export_brep", solid, fmt)

    def export_drawing(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("export_drawing", *args, **kwargs)

    def face_profile(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("face_profile", *args, **kwargs)

    def section_profile(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("section_profile", *args, **kwargs)

    def release(self, solid: SolidHandle) -> None:
        # A handle from a dead or replaced worker refers to memory that is
        # already gone, so releasing it is done, not failed.
        with contextlib.suppress(KernelTimeout, KernelRestarted):
            self._call("release", solid)

    # -- plumbing ----------------------------------------------------------

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if not self._lock.acquire(timeout=self._queue_wait):
            raise KernelBusy(self._queue_wait)
        try:
            return self._locked_call(method, *args, **kwargs)
        finally:
            self._lock.release()

    def _locked_call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        self._ensure()
        # Only now is the generation known to belong to a *live* worker, so this
        # is the only safe place to judge a handle against it.
        args = self._untag_all(args)
        conn = self._conn
        assert conn is not None
        try:
            conn.send((method, args, kwargs))
        except (BrokenPipeError, OSError):
            self._restart()
            raise KernelRestarted(
                f"the geometry worker died before '{method}' could be sent; rebuild to continue"
            ) from None

        if not conn.poll(self._timeout):
            # Nothing polite is possible: the child is inside C++ and will not
            # look at a pipe or a signal until it finishes, which is exactly
            # what it is failing to do.
            self._restart()
            raise KernelTimeout(method, self._timeout)

        try:
            ok, payload, _ = conn.recv()
        except (EOFError, OSError):
            self._restart()
            raise KernelRestarted(
                f"the geometry worker died during '{method}'; rebuild to continue"
            ) from None

        if ok:
            return payload
        raise _rebuild_error(payload)

    def _ensure(self) -> None:
        if self._process is not None and self._process.is_alive():
            return
        if self._process is not None:
            # A worker that died on its own — a segfault, an OOM kill — rather
            # than on a deadline we noticed. It still has to go through the
            # replacement path: that is what bumps the generation and clears the
            # caches, and skipping it let a handle from the dead worker pass the
            # staleness check and reach a fresh worker that had never issued it.
            # Seen in production as "unknown solid handle" on one rebuild, which
            # was luck — the same id in the new worker names a different solid,
            # so the alternative was exporting the wrong shape in silence.
            self._restart()
        self._start()

    def _start(self) -> None:
        # 'spawn', not 'fork': forking a process that has already loaded
        # OpenCascade and is running threads is a well-known way to deadlock in
        # the child, and this one exists to be reliable above all else.
        ctx = mp.get_context("spawn")
        parent, child = ctx.Pipe(duplex=True)
        process = ctx.Process(
            target=worker_module.main,
            args=(child, self._kernel_name),
            name="facet-geometry",
            daemon=True,
        )
        process.start()
        child.close()  # The parent must not hold the child's end, or EOF never arrives.

        if not parent.poll(STARTUP_TIMEOUT):
            process.kill()
            parent.close()
            raise KernelTimeout("startup", STARTUP_TIMEOUT)

        try:
            ok, marker, info = parent.recv()
        except EOFError:
            # The child died while starting. Under 'spawn' the commonest cause is
            # a parent whose __main__ cannot be re-imported — running from stdin,
            # say — which is worth naming rather than surfacing as a bare EOF.
            process.join(timeout=2)
            parent.close()
            raise RuntimeError(
                "the geometry worker exited during startup "
                f"(exit code {process.exitcode}). Its stderr is above."
            ) from None
        if not ok or marker != worker_module.READY:
            process.kill()
            parent.close()
            raise RuntimeError(f"the geometry worker failed to start: {marker!r}")

        self._process = process
        self._conn = parent
        self._name = str(info["name"])
        self._capabilities = frozenset(info["capabilities"])

    def _restart(self) -> None:
        """Kill the worker and forget it. The next call starts a fresh one."""
        if self._process is not None:
            self._process.kill()  # SIGKILL: terminate is a signal it cannot handle.
            self._process.join(timeout=5)
        if self._conn is not None:
            with contextlib.suppress(OSError):
                self._conn.close()
        self._process = None
        self._conn = None
        # Before anything else: every handle the old worker issued now refers to
        # memory that no longer exists, and the next worker will reuse the same
        # ids for different shapes.
        self._generation += 1
        # Anything caching a handle has to be told, or it hands one back forever.
        if self._on_restart is not None:
            self._on_restart()

    def close(self) -> None:
        if self._process is None:
            return
        try:
            if self._conn is not None:
                self._conn.send(worker_module.STOP)
                self._process.join(timeout=5)
        except (BrokenPipeError, OSError):
            pass
        finally:
            if self._process.is_alive():
                self._process.kill()
            self._process = None
            self._conn = None

    # -- handle generations ------------------------------------------------

    def _tag(self, result: Any) -> Any:
        """Stamp the current generation onto handles leaving this kernel."""
        if isinstance(result, SolidResult):
            return _replace_handle(result, self._stamp(result.solid))
        if isinstance(result, SolidHandle):
            return self._stamp(result)
        return result

    def _stamp(self, handle: SolidHandle) -> SolidHandle:
        return SolidHandle(id=f"{self._generation}:{handle.id}", kernel=handle.kernel)

    def _untag(self, handle: SolidHandle) -> SolidHandle:
        if self._stale(handle):
            raise KernelRestarted(
                "this solid was built by a geometry worker that has since been "
                "restarted, so it no longer exists. Rebuild from the document."
            )
        _, _, raw = handle.id.partition(":")
        return SolidHandle(id=raw, kernel=handle.kernel)

    def _untag_all(self, args: tuple[Any, ...]) -> tuple[Any, ...]:
        return tuple(self._untag(a) if isinstance(a, SolidHandle) else a for a in args)

    def _stale(self, handle: SolidHandle) -> bool:
        generation, sep, _ = handle.id.partition(":")
        return not sep or generation != str(self._generation)


def _replace_handle(result: SolidResult, handle: SolidHandle) -> SolidResult:
    return SolidResult(
        solid=handle,
        faces=result.faces,
        edges=result.edges,
        deleted=result.deleted,
    )


def _rebuild_error(described: tuple[str, str, dict[str, Any]]) -> BaseException:
    """Turn the worker's description back into the right exception type.

    The type matters: the HTTP layer maps a document error to 422 and a missing
    project to 404, and a geometry failure carries the feature id that the
    diagnostics panel shows. Flattening them all to RuntimeError would lose
    exactly the part that makes the errors useful.
    """
    from facet.domain import errors as domain_errors

    name, message, fields = described
    kind = getattr(domain_errors, name, None)
    if kind is not None and isinstance(kind, type) and issubclass(kind, Exception):
        try:
            return kind(**fields)  # type: ignore[call-arg]
        except TypeError:
            pass
    builtin = getattr(__builtins__, name, None) if isinstance(__builtins__, dict) is False else None
    if isinstance(builtin, type) and issubclass(builtin, BaseException):
        return builtin(message)
    return RuntimeError(f"{name}: {message}")
