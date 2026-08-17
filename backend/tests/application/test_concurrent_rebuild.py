"""Two requests for the same project must not both rebuild it.

The frontend asks for /bodies and /topologies together and both need the same
rebuild. With no serialisation both missed a cold cache and both did the whole
thing, interleaved through the kernel — on a 35-feature document that turned an
11s rebuild into 26s on a live server, and the page 502'd.

The fix is not a faster rebuild, it is doing one instead of two.
"""

from __future__ import annotations

import threading
import time

from facet.adapters.geometry.fake import FakeKernel
from facet.application.recompute import Detail, RecomputeEngine
from facet.domain.document import Document

BLOCK: dict[str, object] = {
    "schema": "cadsheet/1",
    "project": "block",
    "parameters": [{"name": "w", "value": 40.0}, {"name": "t", "value": 10.0}],
    "datums": {"base": {"type": "plane", "origin": [0, 0, 0], "normal": [0, 0, 1]}},
    "sketches": {
        "outline": {
            "plane": "base",
            "points": {"a": [0, 0], "b": ["w", 0], "c": ["w", "w"], "d": [0, "w"]},
            "curves": [
                {"id": "s", "start": "a", "end": "b"},
                {"id": "e", "start": "b", "end": "c"},
                {"id": "n", "start": "c", "end": "d"},
                {"id": "w", "start": "d", "end": "a"},
            ],
            "loops": [{"id": "outer", "curves": ["s", "e", "n", "w"]}],
        }
    },
    "features": [{"id": "block", "type": "pad", "profile": "outline.outer", "length": "t"}],
}


class CountingKernel(FakeKernel):
    """Counts pads, so a duplicated rebuild is visible rather than merely slow."""

    def __init__(self) -> None:
        super().__init__()
        self.pads = 0
        self._count_lock = threading.Lock()

    def pad(self, request):  # type: ignore[no-untyped-def]
        with self._count_lock:
            self.pads += 1
        # Long enough that two unserialised rebuilds would certainly overlap.
        time.sleep(0.05)
        return super().pad(request)


def test_parallel_rebuilds_of_one_project_build_it_once() -> None:
    kernel = CountingKernel()
    engine = RecomputeEngine(kernel)
    document = Document.from_dict(BLOCK)
    results = []

    def rebuild() -> None:
        results.append(engine.recompute(document, Detail.DRAFT))

    threads = [threading.Thread(target=rebuild) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(results) == 4
    assert all(result.ok for result in results)
    # The first rebuilds; the other three find it cached.
    assert kernel.pads == 1, f"the pad was built {kernel.pads} times, not once"


def test_the_cache_survives_being_read_and_written_at_once() -> None:
    """The dict was mutated by two rebuilds concurrently as well."""
    engine = RecomputeEngine(CountingKernel())
    document = Document.from_dict(BLOCK)
    errors: list[str] = []

    def churn() -> None:
        try:
            for _ in range(5):
                engine.recompute(document, Detail.DRAFT)
                engine.invalidate()
        except Exception as caught:  # reported so every thread finishes
            errors.append(f"{type(caught).__name__}: {caught}")

    threads = [threading.Thread(target=churn) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []
