"""The port for getting work done before it is asked for.

Exporting a document rebuilds it at full detail, which is the expensive one: the
threads a viewport skips are cut, and on a 35-feature model that is seconds. The
snapshot store already makes the *second* export instant. This is about the
first.

Deliberately the narrowest possible interface — name a project, and the
implementation may or may not have something ready later. No result, no future,
no progress. A caller must be correct whether the warming happened, was
superseded, failed, or was never attempted, because it only ever saves time and
must never be load-bearing.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Warmer(Protocol):
    """Prepares a project's export-detail geometry out of band."""

    def schedule(self, project_id: str) -> None:
        """Note that this project would be worth having ready.

        Must return immediately and must never raise. Called on the request path,
        so anything slow or fragile here is worse than the cold rebuild it is
        trying to avoid. Repeated calls for the same project are expected — an
        edit and the refresh that follows it are two — and collapsing them is the
        implementation's business.
        """
        ...

    def close(self) -> None:
        """Stop, releasing whatever is held. Safe to call twice."""
        ...
