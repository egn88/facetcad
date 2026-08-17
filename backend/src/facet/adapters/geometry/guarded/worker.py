"""The child process that actually touches OpenCascade.

It owns the real kernel and every shape in it. Nothing here is clever: read a
method name and its arguments, call it, send back what came out. The value is
entirely in *where* it runs — a process the parent can kill.
"""

from __future__ import annotations

import faulthandler
import os
import sys
from multiprocessing.connection import Connection
from typing import Any

#: Sent when the worker is ready to take calls, so the parent does not start a
#: timeout clock while OCP is still importing — that import alone is seconds.
READY = "__ready__"

#: Asks the worker to exit cleanly.
STOP = "__stop__"


def serve(conn: Connection, kernel_name: str) -> None:
    """Answer calls on ``conn`` until told to stop, or killed.

    Killed is the expected ending. A worker that has wedged inside OpenCascade
    cannot be asked to stop — that is the whole reason it lives out here — so
    the parent sends SIGKILL and starts another.
    """
    # A killed worker leaves no traceback, and "it stopped" is a poor bug
    # report. This makes the kernel dump the C-level stack on a fatal signal,
    # so at least the operation that wedged is identifiable afterwards.
    faulthandler.enable(file=sys.stderr, all_threads=True)

    kernel = _build(kernel_name)
    conn.send((True, READY, {"name": kernel.name, "capabilities": set(kernel.capabilities)}))

    while True:
        try:
            message = conn.recv()
        except (EOFError, KeyboardInterrupt):
            return  # The parent went away; so do we.

        if message == STOP:
            return

        method, args, kwargs = message
        try:
            result = getattr(kernel, method)(*args, **kwargs)
        except BaseException as caught:
            # Deliberately BaseException, and deliberately not re-raised: a
            # geometry error is an ordinary outcome here and belongs back with
            # the caller, where the feature id and selector are known. Letting
            # it kill the worker would turn a bad fillet into a lost session.
            conn.send((False, _describe(caught), None))
            continue
        conn.send((True, result, None))


def _build(name: str) -> Any:
    if name == "occt":
        from facet.adapters.geometry.occt import OcctKernel

        return OcctKernel()
    from facet.adapters.geometry.fake import FakeKernel

    return FakeKernel()


def _describe(caught: BaseException) -> tuple[str, str, dict[str, Any]]:
    """Enough to rebuild the exception on the other side.

    The exception object itself is not sent: the domain errors carry structured
    fields the API turns into its response, and pickling an arbitrary exception
    is a good way to fail at the worst moment.
    """
    fields: dict[str, Any] = {}
    for key, value in vars(caught).items():
        if isinstance(value, (str, int, float, bool, type(None), tuple, list)):
            fields[key] = value
    return (type(caught).__name__, str(caught), fields)


def main(conn: Connection, kernel_name: str) -> None:
    """Entry point for the spawned process."""
    # The worker must never inherit the parent's idea of being the server.
    os.environ.setdefault("FACET_WORKER", "1")
    serve(conn, kernel_name)
