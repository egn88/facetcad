"""A geometry kernel isolated in a child process, with a deadline per call.

OpenCascade holds the interpreter lock for the whole of a call, so nothing
in-process can interrupt one. This adapter puts the kernel somewhere that can be
killed, which is the only thing that works.
"""

from .kernel import (
    DEFAULT_TIMEOUT,
    GuardedKernel,
    KernelBusy,
    KernelRestarted,
    KernelTimeout,
)

__all__ = [
    "DEFAULT_TIMEOUT",
    "GuardedKernel",
    "KernelBusy",
    "KernelRestarted",
    "KernelTimeout",
]
