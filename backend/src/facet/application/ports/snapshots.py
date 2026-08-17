"""The port for keeping built geometry between runs.

A rebuild is content-addressed already: the recompute engine hashes a feature's
spec, the parameters it reads, the frame it sits on and the key of the feature
before it. That hash is a perfectly good filename, so the only thing missing is
somewhere to put the bytes.

Deliberately the smallest interface that can serve that: get, put, forget. No
listing, no iteration, no transactions. A store that loses everything is still a
correct store — the document is the source of truth and the answer is always
recomputable — which is why every method here is allowed to fail silently and
why the engine treats a miss and an error identically.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SnapshotStore(Protocol):
    """Content-addressed storage for built geometry.

    Keys are opaque strings from the caller and already carry everything that
    identifies the content, so an implementation must not interpret them beyond
    making them safe to store under.
    """

    def load(self, key: str) -> bytes | None:
        """The bytes stored under ``key``, or None if there are none.

        Returns None rather than raising for anything recoverable — a missing
        file, a partial write, an unreadable directory. The caller's fallback is
        to rebuild, which is always available.
        """
        ...

    def save(self, key: str, blob: bytes) -> None:
        """Store ``blob`` under ``key``. Failure is not an error worth raising.

        A cache that cannot be written to should slow the next run down, not
        fail the current one.
        """
        ...

    def clear(self) -> None:
        """Forget everything."""
        ...
