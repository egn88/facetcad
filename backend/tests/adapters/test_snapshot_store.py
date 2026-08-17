"""The filesystem snapshot store.

Three properties, in order of how much they matter:

A half-written file must never read back as a whole one. The caller cannot tell
a truncated shape from a real one without doing the work it was trying to avoid,
so the write is atomic and this asserts it.

Nothing here may raise. The store is a cache in front of something always
recomputable, so a full disk, a read-only volume or a vanished directory has to
cost speed and not correctness.

And it must stay inside its budget, because it grows on every distinct edit of
every project and nothing else is going to clean up after it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from facet.adapters.persistence.snapshots import FilesystemSnapshotStore

KEY = "a" * 64
OTHER = "b" * 64


@pytest.fixture
def store(tmp_path: Path) -> FilesystemSnapshotStore:
    return FilesystemSnapshotStore(tmp_path / "snapshots")


# --------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------


def test_what_goes_in_comes_out(store: FilesystemSnapshotStore) -> None:
    store.save(KEY, b"geometry")
    assert store.load(KEY) == b"geometry"


def test_an_unknown_key_is_a_miss(store: FilesystemSnapshotStore) -> None:
    assert store.load(KEY) is None


def test_keys_do_not_collide(store: FilesystemSnapshotStore) -> None:
    store.save(KEY, b"first")
    store.save(OTHER, b"second")
    assert store.load(KEY) == b"first"
    assert store.load(OTHER) == b"second"


def test_saving_twice_keeps_the_later_one(store: FilesystemSnapshotStore) -> None:
    store.save(KEY, b"old")
    store.save(KEY, b"new")
    assert store.load(KEY) == b"new"


def test_clear_forgets_everything(store: FilesystemSnapshotStore) -> None:
    store.save(KEY, b"geometry")
    store.clear()
    assert store.load(KEY) is None


def test_binary_content_survives_unchanged(store: FilesystemSnapshotStore) -> None:
    """A B-rep is bytes, including the ones a text mode would rewrite."""
    blob = bytes(range(256)) + b"\r\n\x1a" + bytes(range(256))
    store.save(KEY, blob)
    assert store.load(KEY) == blob


# --------------------------------------------------------------------------
# Nothing partial, nothing left behind
# --------------------------------------------------------------------------


def test_no_temporary_files_are_left_behind(store: FilesystemSnapshotStore) -> None:
    store.save(KEY, b"geometry")
    assert not list(store.root.glob("**/*.part"))


def test_an_empty_blob_is_not_stored(store: FilesystemSnapshotStore) -> None:
    """Zero bytes is what a failed write looks like, so it is never a value."""
    store.save(KEY, b"")
    assert store.load(KEY) is None


def test_a_zero_length_file_reads_as_a_miss(store: FilesystemSnapshotStore) -> None:
    store.save(KEY, b"geometry")
    path = next(store.root.glob("**/*.bin"))
    path.write_bytes(b"")
    assert store.load(KEY) is None


# --------------------------------------------------------------------------
# Keys are not paths
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    ["../escape", "with/slash", "", "a" * 200, "with space", ".hidden"],
    ids=["traversal", "slash", "empty", "too long", "space", "leading dot"],
)
def test_a_key_that_is_not_a_digest_is_refused_quietly(
    store: FilesystemSnapshotStore, key: str
) -> None:
    store.save(key, b"geometry")
    assert store.load(key) is None
    assert not list(store.root.glob("**/*.bin"))


def test_nothing_is_written_outside_the_root(store: FilesystemSnapshotStore) -> None:
    store.save("../../escaped", b"geometry")
    assert not (store.root.parent.parent / "escaped.bin").exists()


# --------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------


def test_the_store_stays_inside_its_budget(tmp_path: Path) -> None:
    store = FilesystemSnapshotStore(tmp_path / "snapshots", budget_bytes=4000)
    for index in range(12):
        store.save(f"{index:064x}", b"x" * 1000)

    total = sum(path.stat().st_size for path in store.root.glob("**/*.bin"))
    assert total <= 4000


def test_eviction_drops_the_oldest_first(tmp_path: Path) -> None:
    store = FilesystemSnapshotStore(tmp_path / "snapshots", budget_bytes=3000)
    for index in range(3):
        key = f"{index:064x}"
        store.save(key, b"x" * 1000)
        # Ages them apart explicitly: three saves inside one filesystem
        # timestamp tick would make the order arbitrary and the test flaky.
        path = next(store.root.glob(f"**/{key}.bin"))
        os.utime(path, (1000 + index, 1000 + index))

    store.save(f"{9:064x}", b"x" * 1000)
    assert store.load(f"{0:064x}") is None
    assert store.load(f"{9:064x}") is not None


def test_a_blob_larger_than_the_budget_does_not_wedge_the_store(tmp_path: Path) -> None:
    """It cannot be kept, and trying must not loop or raise."""
    store = FilesystemSnapshotStore(tmp_path / "snapshots", budget_bytes=100)
    store.save(KEY, b"x" * 5000)
    store.save(OTHER, b"x" * 5000)
    assert sum(p.stat().st_size for p in store.root.glob("**/*.bin")) <= 5000


# --------------------------------------------------------------------------
# A store that cannot work is still a store
# --------------------------------------------------------------------------


def test_an_unusable_root_disables_the_cache_rather_than_failing(tmp_path: Path) -> None:
    """A file where the directory should be. Every call still answers."""
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("in the way")

    store = FilesystemSnapshotStore(blocked)
    store.save(KEY, b"geometry")
    assert store.load(KEY) is None
    store.clear()


def test_a_read_only_root_disables_writes_rather_than_failing(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    store = FilesystemSnapshotStore(root)
    root.chmod(0o500)
    try:
        store.save(KEY, b"geometry")
        assert store.load(KEY) is None
    finally:
        root.chmod(0o700)
