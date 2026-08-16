"""Canonical ordering of split-face fragments.

When a later feature cuts one logical face into several fragments, each fragment
needs a stable ``#n`` suffix. The ordering must be a property of the *geometry*,
never of the kernel's internal face enumeration — that enumeration is exactly
what makes FreeCAD's references drift.

The rule
--------

Fragments are ordered by their centroid expressed in the owning feature's local
frame, sorted lexicographically on (u, v, w). Because the key is local, widening
a plate moves every centroid but preserves their relative order, so ``#0`` stays
``#0``.

Sort keys are **quantised** to a tolerance grid before comparison. Without this,
a difference of 1e-13 in u would decide the ordering and the least floating-point
perturbation would swap two fragments. Quantising makes such a difference a tie,
so the next coordinate decides instead.

When two fragments remain tied after quantisation, the ordering genuinely cannot
be determined and :class:`AmbiguousSplitError` is raised rather than a coin being
tossed. That is the fail-loud rule applied to split ordering.

Single fragments are never suffixed: a face that was not split keeps its plain
tag, so ``base/cap+`` only becomes ``base/cap+#0`` when a split actually occurs.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass

from .errors import AmbiguousSplitError
from .math3d import Vec3
from .tags import FaceTag

#: Centroids closer than this in every coordinate cannot be ordered (mm).
#: Chosen well above floating-point noise but far below any real feature size.
ORDERING_TOL = 1e-6


@dataclass(frozen=True, slots=True)
class Fragment[T]:
    """A candidate piece of a split face, with its centroid in local coordinates."""

    centroid: Vec3
    payload: T


def _quantised_key(centroid: Vec3, tolerance: float) -> tuple[int, int, int]:
    return (
        round(centroid.x / tolerance),
        round(centroid.y / tolerance),
        round(centroid.z / tolerance),
    )


def canonical_order(
    centroids: Sequence[Vec3], *, tolerance: float = ORDERING_TOL, tag: FaceTag | None = None
) -> list[int]:
    """Return indices of ``centroids`` in canonical order.

    Raises :class:`AmbiguousSplitError` when two centroids are indistinguishable
    at the given tolerance, since any order chosen for them would be arbitrary
    and therefore unstable across rebuilds.
    """
    if len(centroids) <= 1:
        return list(range(len(centroids)))

    keys = [_quantised_key(c, tolerance) for c in centroids]
    order = sorted(range(len(centroids)), key=lambda i: keys[i])

    for previous, current in itertools.pairwise(order):
        if keys[previous] == keys[current]:
            separation = _max_component_delta(centroids[previous], centroids[current])
            raise AmbiguousSplitError(
                tag=str(tag) if tag is not None else "<unnamed>",
                candidates=len(centroids),
                separation=separation,
                tolerance=tolerance,
            )
    return order


def _max_component_delta(a: Vec3, b: Vec3) -> float:
    delta = a - b
    return max(abs(delta.x), abs(delta.y), abs(delta.z))


def assign_ordinals[T](
    base_tag: FaceTag,
    fragments: Sequence[Fragment[T]],
    *,
    tolerance: float = ORDERING_TOL,
) -> list[tuple[FaceTag, T]]:
    """Tag each fragment of a split face.

    A single fragment keeps ``base_tag`` unchanged — no gratuitous ``#0``. Two or
    more receive ``#0``, ``#1``, ... in canonical order.
    """
    if not fragments:
        return []
    if len(fragments) == 1:
        return [(base_tag.without_ordinal(), fragments[0].payload)]

    order = canonical_order(
        [f.centroid for f in fragments], tolerance=tolerance, tag=base_tag
    )
    stripped = base_tag.without_ordinal()
    return [
        (stripped.with_ordinal(ordinal), fragments[index].payload)
        for ordinal, index in enumerate(order)
    ]
