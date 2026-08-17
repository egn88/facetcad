"""Filesystem snapshot store — one file per content hash.

Writes are atomic (temporary file, then rename) for the same reason document
saves are: an interrupted write must not leave a half a solid behind that reads
back as a plausible one. Here it matters more than usual, because the caller
cannot tell a truncated shape from a real one without doing the work it was
trying to avoid.

The directory is disposable by design. Deleting it costs the next open a
rebuild and nothing else, which is what makes it safe to cap the size and evict
the oldest entries rather than reason about which are still reachable.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from pathlib import Path

#: Keys are hex digests with a little structure around them; anything else is a
#: caller bug rather than something to sanitise into a surprising filename.
_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: How much geometry to keep before evicting the oldest. A body of a large model
#: is under a megabyte, so this holds a few hundred of them — generous next to
#: the seconds each one saves, small next to any disk it would live on.
DEFAULT_BUDGET_BYTES = 512 * 1024 * 1024


class FilesystemSnapshotStore:
    """Stores snapshots as ``<root>/<key[:2]>/<key>.bin``.

    Sharded on the first two characters of the hash. One flat directory works
    until it holds tens of thousands of files, at which point listing it — which
    eviction has to do — gets slow on exactly the systems where this matters
    most.
    """

    def __init__(
        self, root: Path | str, budget_bytes: int = DEFAULT_BUDGET_BYTES
    ) -> None:
        self._root = Path(root)
        self._budget = budget_bytes
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError:
            # A read-only or missing volume disables the cache rather than the
            # server. Every load will miss and every save will no-op.
            self._usable = False
        else:
            self._usable = True

    @property
    def root(self) -> Path:
        return self._root

    # -- the port ----------------------------------------------------------

    def load(self, key: str) -> bytes | None:
        path = self._path(key)
        if path is None:
            return None
        try:
            blob = path.read_bytes()
        except OSError:
            return None
        if not blob:
            return None
        # Touched so eviction can tell what is still in use. Best-effort: a
        # store on a filesystem that refuses this still works, it just evicts
        # by age of write rather than age of use.
        with contextlib.suppress(OSError):
            os.utime(path)
        return blob

    def save(self, key: str, blob: bytes) -> None:
        path = self._path(key)
        if path is None or not blob:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Same directory as the target, so the rename cannot cross devices.
            handle, temporary = tempfile.mkstemp(dir=path.parent, suffix=".part")
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(blob)
                os.replace(temporary, path)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise
        except OSError:
            return
        self._evict()

    def clear(self) -> None:
        if not self._usable:
            return
        for path in self._root.glob("*/*.bin"):
            with contextlib.suppress(OSError):
                path.unlink()

    # -- internals ---------------------------------------------------------

    def _path(self, key: str) -> Path | None:
        if not self._usable or not _SAFE_KEY.match(key):
            return None
        return self._root / key[:2] / f"{key}.bin"

    def _evict(self) -> None:
        """Drop the least recently used entries until back inside the budget.

        Only walks the tree when it has to, because the common case is a store
        well under budget and this runs after every save.
        """
        try:
            entries = [
                (path, path.stat()) for path in self._root.glob("*/*.bin")
            ]
        except OSError:
            return
        total = sum(stat.st_size for _, stat in entries)
        if total <= self._budget:
            return

        entries.sort(key=lambda item: item[1].st_mtime)
        for path, stat in entries:
            if total <= self._budget:
                return
            try:
                path.unlink()
            except OSError:
                continue
            total -= stat.st_size
