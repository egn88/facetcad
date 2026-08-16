"""Fingerprints disambiguate and detect drift; they never override provenance."""

from __future__ import annotations

import math

from facet.domain.fingerprint import FaceFingerprint, SurfaceKind
from facet.domain.math3d import Vec3

UP = Vec3(0, 0, 1)


def plane(area: float, centroid: Vec3, normal: Vec3 = UP) -> FaceFingerprint:
    return FaceFingerprint(SurfaceKind.PLANE, area, centroid, normal)


def test_identical_fingerprints_match() -> None:
    a = plane(100.0, Vec3(5, 5, 0))
    assert a.matches(plane(100.0, Vec3(5, 5, 0)))


def test_surface_kind_mismatch_is_decisive() -> None:
    """A plane never becomes a cylinder via a parameter change."""
    flat = plane(100.0, Vec3(5, 5, 0))
    round_ = FaceFingerprint(SurfaceKind.CYLINDER, 100.0, Vec3(5, 5, 0), Vec3(0, 0, 1))
    assert not flat.matches(round_)
    assert flat.distance(round_) == math.inf


def test_a_moved_face_does_not_match() -> None:
    assert not plane(100.0, Vec3(5, 5, 0)).matches(plane(100.0, Vec3(50, 5, 0)))


def test_a_reoriented_face_does_not_match() -> None:
    a = plane(100.0, Vec3(5, 5, 0), Vec3(0, 0, 1))
    b = plane(100.0, Vec3(5, 5, 0), Vec3(0, 1, 0))
    assert not a.matches(b)


def test_area_comparison_is_relative_not_absolute() -> None:
    """A 0.01mm^2 change is noise on a big face and decisive on a tiny one."""
    big = plane(10_000.0, Vec3(0, 0, 0))
    assert big.matches(plane(10_000.01, Vec3(0, 0, 0)))

    tiny = plane(0.02, Vec3(0, 0, 0))
    assert not tiny.matches(plane(0.01, Vec3(0, 0, 0)))


def test_distance_ranks_the_nearer_candidate_first() -> None:
    target = plane(100.0, Vec3(0, 0, 0))
    near = plane(100.0, Vec3(0.5, 0, 0))
    far = plane(100.0, Vec3(40, 0, 0))
    assert target.distance(near) < target.distance(far)


def test_round_trips_through_the_document_form() -> None:
    original = FaceFingerprint(SurfaceKind.CYLINDER, 12.5, Vec3(1, 2, 3), Vec3(0, 1, 0))
    assert FaceFingerprint.from_dict(original.to_dict()) == original


def test_drift_reports_a_shift_that_still_matches_by_tag() -> None:
    """Growing a face is legal, but worth telling the user about."""
    before = plane(100.0, Vec3(5, 5, 0))
    after = plane(400.0, Vec3(10, 10, 0))
    drift = after.drift_from(before)
    assert drift.is_significant()
    assert drift.area_ratio == 4.0
    assert "area x4" in drift.describe()


def test_no_drift_between_identical_faces() -> None:
    same = plane(100.0, Vec3(5, 5, 0))
    assert not same.drift_from(plane(100.0, Vec3(5, 5, 0))).is_significant()
