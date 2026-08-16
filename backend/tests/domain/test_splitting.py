"""Split-fragment ordering must be a property of geometry, not of enumeration.

The decisive test here is :func:`test_ordering_survives_parameter_sweep`: it
mimics widening a plate and asserts that the fragment which was ``#0`` stays
``#0`` throughout. That is precisely the guarantee FreeCAD fails to provide.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from facet.domain.errors import AmbiguousSplitError
from facet.domain.math3d import Vec3
from facet.domain.splitting import (
    ORDERING_TOL,
    Fragment,
    assign_ordinals,
    canonical_order,
)
from facet.domain.tags import FaceTag

CAP = FaceTag.parse("base/cap+")


# --------------------------------------------------------------------------
# Ordering is deterministic and independent of input order
# --------------------------------------------------------------------------


def test_order_is_independent_of_presentation_order() -> None:
    """The kernel may hand us fragments in any order; the result must not care."""
    a, b, c = Vec3(60, 20, 0), Vec3(10, 20, 0), Vec3(35, 5, 0)
    forwards = [a, b, c]
    backwards = [c, b, a]

    ordered_forwards = [forwards[i].as_tuple() for i in canonical_order(forwards)]
    ordered_backwards = [backwards[i].as_tuple() for i in canonical_order(backwards)]
    assert ordered_forwards == ordered_backwards


def test_order_is_lexicographic_on_local_coordinates() -> None:
    centroids = [Vec3(10, 50, 0), Vec3(10, 20, 0), Vec3(5, 99, 0)]
    assert canonical_order(centroids) == [2, 1, 0]


@given(
    st.lists(
        st.tuples(
            st.integers(min_value=-500, max_value=500),
            st.integers(min_value=-500, max_value=500),
        ),
        min_size=2,
        max_size=8,
        unique=True,
    )
)
@settings(max_examples=200)
def test_ordering_is_a_total_order_over_distinct_centroids(points: list[tuple[int, int]]) -> None:
    centroids = [Vec3(float(x), float(y), 0.0) for x, y in points]
    order = canonical_order(centroids)
    assert sorted(order) == list(range(len(centroids)))
    keys = [(centroids[i].x, centroids[i].y) for i in order]
    assert keys == sorted(keys)


# --------------------------------------------------------------------------
# The guarantee that matters: stability under parameter change
# --------------------------------------------------------------------------


def test_ordering_survives_parameter_sweep() -> None:
    """Widening a plate moves every centroid but must not reshuffle ordinals.

    Two pockets sit at 20% and 70% of the plate width. As ``plate_w`` sweeps,
    both centroids move, yet the left fragment must remain ``#0`` at every step.
    """
    previous: list[str] | None = None
    for plate_w in range(50, 400, 7):
        width = float(plate_w)
        fragments = [
            # deliberately presented right-to-left, the order a kernel might use
            Fragment(centroid=Vec3(width * 0.70, 15.0, 0.0), payload="right"),
            Fragment(centroid=Vec3(width * 0.20, 15.0, 0.0), payload="left"),
        ]
        tagged = assign_ordinals(CAP, fragments)
        labels = [f"{tag}={payload}" for tag, payload in tagged]

        assert labels == ["base/cap+#0=left", "base/cap+#1=right"]
        if previous is not None:
            assert labels == previous
        previous = labels


def test_ordering_is_stable_under_rigid_translation_of_the_whole_face() -> None:
    """Local-frame keys mean moving the part cannot reorder its own fragments."""
    base = [Vec3(10, 20, 0), Vec3(60, 20, 0), Vec3(35, 80, 0)]
    reference = canonical_order(base)
    for shift in (Vec3(1000, 0, 0), Vec3(0, -750, 0), Vec3(-3, 12, 44)):
        moved = [c + shift for c in base]
        assert canonical_order(moved) == reference


def test_floating_point_noise_does_not_flip_the_order() -> None:
    """Sub-tolerance jitter on the leading coordinate must be treated as a tie.

    Without quantisation the 1e-12 difference in x would decide the ordering and
    the slightest numeric change would swap the two fragments.
    """
    centroids = [Vec3(10.0, 90.0, 0.0), Vec3(10.0 + 1e-12, 5.0, 0.0)]
    # x ties after quantisation, so y decides: the y=5 fragment sorts first.
    assert canonical_order(centroids) == [1, 0]


# --------------------------------------------------------------------------
# Ordinals are only introduced when a split actually happened
# --------------------------------------------------------------------------


def test_an_unsplit_face_keeps_its_plain_tag() -> None:
    tagged = assign_ordinals(CAP, [Fragment(centroid=Vec3(1, 2, 3), payload="only")])
    assert [str(tag) for tag, _ in tagged] == ["base/cap+"]


def test_no_fragments_yields_nothing() -> None:
    assert assign_ordinals(CAP, []) == []


def test_a_split_face_gains_sequential_ordinals() -> None:
    fragments = [
        Fragment(centroid=Vec3(30, 0, 0), payload="c"),
        Fragment(centroid=Vec3(10, 0, 0), payload="a"),
        Fragment(centroid=Vec3(20, 0, 0), payload="b"),
    ]
    tagged = assign_ordinals(CAP, fragments)
    assert [(str(tag), payload) for tag, payload in tagged] == [
        ("base/cap+#0", "a"),
        ("base/cap+#1", "b"),
        ("base/cap+#2", "c"),
    ]


def test_reassigning_ordinals_replaces_rather_than_stacks() -> None:
    already_suffixed = FaceTag.parse("base/cap+#7")
    tagged = assign_ordinals(
        already_suffixed,
        [Fragment(Vec3(0, 0, 0), "a"), Fragment(Vec3(10, 0, 0), "b")],
    )
    assert [str(tag) for tag, _ in tagged] == ["base/cap+#0", "base/cap+#1"]


# --------------------------------------------------------------------------
# Genuine ambiguity fails loudly
# --------------------------------------------------------------------------


def test_indistinguishable_centroids_raise_rather_than_guess() -> None:
    centroids = [Vec3(10.0, 20.0, 0.0), Vec3(10.0, 20.0, 0.0)]
    with pytest.raises(AmbiguousSplitError) as excinfo:
        canonical_order(centroids, tag=CAP)
    error = excinfo.value
    assert error.tag == "base/cap+"
    assert error.candidates == 2
    assert "anchor" in str(error)


def test_separation_just_below_tolerance_is_ambiguous() -> None:
    centroids = [Vec3(0.0, 0.0, 0.0), Vec3(ORDERING_TOL / 10, 0.0, 0.0)]
    with pytest.raises(AmbiguousSplitError):
        canonical_order(centroids, tag=CAP)


def test_separation_well_above_tolerance_is_fine() -> None:
    centroids = [Vec3(0.0, 0.0, 0.0), Vec3(ORDERING_TOL * 1000, 0.0, 0.0)]
    assert canonical_order(centroids, tag=CAP) == [0, 1]


def test_ambiguity_error_reports_the_actual_separation() -> None:
    centroids = [Vec3(0.0, 0.0, 0.0), Vec3(0.0, 1e-9, 0.0)]
    with pytest.raises(AmbiguousSplitError) as excinfo:
        canonical_order(centroids, tag=CAP)
    assert excinfo.value.separation == pytest.approx(1e-9)
