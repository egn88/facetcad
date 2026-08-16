"""Geometric fingerprints — the safety net beneath provenance tags.

A tag says how a face was *born*. A fingerprint says what it currently *looks
like*: surface kind, area, centroid and normal, all expressed in the owning
feature's local frame so that moving or resizing the part does not invalidate
them.

Fingerprints are never the primary identity — provenance is. They serve two
narrower purposes:

1. **Disambiguation.** When provenance alone leaves two candidates, the stored
   fingerprint picks the one the document meant.
2. **Drift detection.** When a selector resolves by tag but the face's shape has
   changed beyond tolerance, that is worth reporting even though the rebuild can
   proceed — it is the early warning that a model is about to break.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .math3d import Vec3


class SurfaceKind:
    """Coarse surface classification, kernel-neutral.

    Open by design (see :class:`facet.domain.tags.Roles`) — a kernel that can
    distinguish more surface types may report them without editing this class.
    """

    PLANE = "plane"
    CYLINDER = "cylinder"
    CONE = "cone"
    SPHERE = "sphere"
    TORUS = "torus"
    FREEFORM = "freeform"
    UNKNOWN = "unknown"


#: Default tolerances for fingerprint comparison.
DEFAULT_LINEAR_TOL = 1e-4
DEFAULT_AREA_REL_TOL = 1e-3
DEFAULT_NORMAL_TOL = 1e-4


@dataclass(frozen=True, slots=True)
class FaceFingerprint:
    """A geometric summary of a face, in the owning feature's local frame."""

    surface: str
    area: float
    centroid: Vec3
    normal: Vec3

    def to_dict(self) -> dict[str, object]:
        return {
            "surface": self.surface,
            "area": round(self.area, 9),
            "centroid": [round(c, 9) for c in self.centroid],
            "normal": [round(c, 9) for c in self.normal],
        }

    @staticmethod
    def from_dict(data: dict[str, object]) -> FaceFingerprint:
        centroid = data["centroid"]
        normal = data["normal"]
        assert isinstance(centroid, (list, tuple)) and isinstance(normal, (list, tuple))
        return FaceFingerprint(
            surface=str(data.get("surface", SurfaceKind.UNKNOWN)),
            area=float(data["area"]),  # type: ignore[arg-type]
            centroid=Vec3(*(float(c) for c in centroid)),
            normal=Vec3(*(float(c) for c in normal)),
        )

    # -- comparison --------------------------------------------------------

    def matches(
        self,
        other: FaceFingerprint,
        *,
        linear_tol: float = DEFAULT_LINEAR_TOL,
        area_rel_tol: float = DEFAULT_AREA_REL_TOL,
        normal_tol: float = DEFAULT_NORMAL_TOL,
    ) -> bool:
        """True when two fingerprints plausibly describe the same face.

        Surface kind must agree exactly — a plane never becomes a cylinder
        through a parameter change, so a mismatch there is decisive.
        """
        if self.surface != other.surface:
            return False
        if not self.normal.is_close(other.normal, normal_tol):
            return False
        if not self.centroid.is_close(other.centroid, linear_tol):
            return False
        return self._area_matches(other.area, area_rel_tol)

    def _area_matches(self, area: float, rel_tol: float) -> bool:
        scale = max(abs(self.area), abs(area), 1e-12)
        return abs(self.area - area) / scale <= rel_tol

    def distance(self, other: FaceFingerprint) -> float:
        """A scalar dissimilarity, used to rank near-miss candidates.

        Returns ``inf`` for a surface-kind mismatch so such candidates can never
        win a nearest-match contest.
        """
        if self.surface != other.surface:
            return math.inf
        positional = (self.centroid - other.centroid).length()
        angular = (self.normal - other.normal).length()
        scale = max(abs(self.area), abs(other.area), 1e-12)
        areal = abs(self.area - other.area) / scale
        return positional + angular + areal

    def drift_from(self, other: FaceFingerprint) -> FingerprintDrift:
        """Describe how far this face has moved since the fingerprint was taken."""
        return FingerprintDrift(
            surface_changed=self.surface != other.surface,
            centroid_shift=(self.centroid - other.centroid).length(),
            normal_shift=(self.normal - other.normal).length(),
            area_ratio=(self.area / other.area) if other.area else math.inf,
        )


@dataclass(frozen=True, slots=True)
class EdgeFingerprint:
    """Geometric summary of an edge, in the owning feature's local frame.

    Edges get identity from their two adjacent faces, so this exists purely for
    filtering (``dir=+Z``) and for ranking near-miss candidates — never for
    identity.
    """

    curve: str
    length: float
    midpoint: Vec3
    direction: Vec3

    def to_dict(self) -> dict[str, object]:
        return {
            "curve": self.curve,
            "length": round(self.length, 9),
            "midpoint": [round(c, 9) for c in self.midpoint],
            "direction": [round(c, 9) for c in self.direction],
        }

    @staticmethod
    def from_dict(data: dict[str, object]) -> EdgeFingerprint:
        midpoint = data["midpoint"]
        direction = data["direction"]
        assert isinstance(midpoint, (list, tuple)) and isinstance(direction, (list, tuple))
        return EdgeFingerprint(
            curve=str(data.get("curve", CurveKind.UNKNOWN)),
            length=float(data["length"]),  # type: ignore[arg-type]
            midpoint=Vec3(*(float(c) for c in midpoint)),
            direction=Vec3(*(float(c) for c in direction)),
        )

    def is_parallel_to(self, axis: Vec3, tol: float = DEFAULT_NORMAL_TOL) -> bool:
        """Direction-agnostic parallelism — an edge has no inherent sense."""
        return abs(abs(self.direction.dot(axis)) - 1.0) <= tol


class CurveKind:
    LINE = "line"
    CIRCLE = "circle"
    ELLIPSE = "ellipse"
    BSPLINE = "bspline"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class FingerprintDrift:
    """How much a face changed between two rebuilds."""

    surface_changed: bool
    centroid_shift: float
    normal_shift: float
    area_ratio: float

    def is_significant(
        self, *, linear_tol: float = DEFAULT_LINEAR_TOL, normal_tol: float = DEFAULT_NORMAL_TOL
    ) -> bool:
        return (
            self.surface_changed
            or self.centroid_shift > linear_tol
            or self.normal_shift > normal_tol
        )

    def describe(self) -> str:
        if self.surface_changed:
            return "surface type changed"
        return (
            f"centroid moved {self.centroid_shift:.4g}mm, "
            f"normal rotated by {self.normal_shift:.4g}, "
            f"area x{self.area_ratio:.4g}"
        )
