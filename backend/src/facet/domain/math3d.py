"""Pure geometric value objects.

Deliberately minimal and dependency-free: the domain must not import numpy or
any kernel type. These exist so the naming engine can reason about position and
orientation — canonical sort keys, fingerprints, datum frames — without ever
touching an adapter.

All lengths are millimetres and all angles are degrees at this boundary; the
document layer converts on the way in.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass

#: Lengths closer than this are treated as coincident (mm).
LINEAR_TOL = 1e-7
#: Direction components closer than this are treated as equal.
ANGULAR_TOL = 1e-9


@dataclass(frozen=True, slots=True)
class Vec2:
    x: float
    y: float

    def __iter__(self) -> Iterator[float]:
        yield self.x
        yield self.y

    def __add__(self, other: Vec2) -> Vec2:
        return Vec2(self.x + other.x, self.y + other.y)

    def __sub__(self, other: Vec2) -> Vec2:
        return Vec2(self.x - other.x, self.y - other.y)

    def __mul__(self, k: float) -> Vec2:
        return Vec2(self.x * k, self.y * k)

    def length(self) -> float:
        return math.hypot(self.x, self.y)

    def as_tuple(self) -> tuple[float, float]:
        return (self.x, self.y)


@dataclass(frozen=True, slots=True)
class Vec3:
    x: float
    y: float
    z: float

    @staticmethod
    def zero() -> Vec3:
        return Vec3(0.0, 0.0, 0.0)

    def __iter__(self) -> Iterator[float]:
        yield self.x
        yield self.y
        yield self.z

    def __add__(self, other: Vec3) -> Vec3:
        return Vec3(self.x + other.x, self.y + other.y, self.z + other.z)

    def __sub__(self, other: Vec3) -> Vec3:
        return Vec3(self.x - other.x, self.y - other.y, self.z - other.z)

    def __mul__(self, k: float) -> Vec3:
        return Vec3(self.x * k, self.y * k, self.z * k)

    def __neg__(self) -> Vec3:
        return Vec3(-self.x, -self.y, -self.z)

    def dot(self, other: Vec3) -> float:
        return self.x * other.x + self.y * other.y + self.z * other.z

    def cross(self, other: Vec3) -> Vec3:
        return Vec3(
            self.y * other.z - self.z * other.y,
            self.z * other.x - self.x * other.z,
            self.x * other.y - self.y * other.x,
        )

    def length(self) -> float:
        return math.sqrt(self.dot(self))

    def normalized(self) -> Vec3:
        n = self.length()
        if n < LINEAR_TOL:
            raise ValueError("cannot normalize a zero-length vector")
        return Vec3(self.x / n, self.y / n, self.z / n)

    def is_close(self, other: Vec3, tol: float = LINEAR_TOL) -> bool:
        return (
            abs(self.x - other.x) <= tol
            and abs(self.y - other.y) <= tol
            and abs(self.z - other.z) <= tol
        )

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)

    def rounded(self, ndigits: int = 9) -> Vec3:
        """Quantised copy, used to make hashes and sort keys reproducible."""
        return Vec3(round(self.x, ndigits), round(self.y, ndigits), round(self.z, ndigits))


@dataclass(frozen=True, slots=True)
class Frame:
    """A right-handed coordinate frame: origin plus orthonormal axes.

    Datum planes are frames. Crucially, a face's canonical sort key and its
    fingerprint are both expressed *in the owning feature's frame* rather than
    in world coordinates, so translating or resizing a part does not reshuffle
    the ordering of its faces.
    """

    origin: Vec3
    x_axis: Vec3
    y_axis: Vec3
    z_axis: Vec3

    @staticmethod
    def world() -> Frame:
        return Frame(
            origin=Vec3.zero(),
            x_axis=Vec3(1.0, 0.0, 0.0),
            y_axis=Vec3(0.0, 1.0, 0.0),
            z_axis=Vec3(0.0, 0.0, 1.0),
        )

    @staticmethod
    def from_origin_normal(origin: Vec3, normal: Vec3, x_hint: Vec3 | None = None) -> Frame:
        """Build a frame from an origin and a normal.

        ``x_hint`` is projected onto the plane to fix the in-plane rotation. When
        omitted a deterministic hint is chosen from the world axis least aligned
        with the normal — never a random or kernel-dependent choice, so the same
        inputs always yield the same frame.
        """
        z = normal.normalized()
        if x_hint is None:
            components = (
                (abs(z.x), Vec3(1, 0, 0)),
                (abs(z.y), Vec3(0, 1, 0)),
                (abs(z.z), Vec3(0, 0, 1)),
            )
            x_hint = min(components, key=lambda c: c[0])[1]
        projected = x_hint - z * z.dot(x_hint)
        if projected.length() < LINEAR_TOL:
            raise ValueError("x_hint is parallel to the normal; cannot define a frame")
        x = projected.normalized()
        y = z.cross(x)
        return Frame(origin=origin, x_axis=x, y_axis=y, z_axis=z)

    @staticmethod
    def from_euler(origin: Vec3, rx: float, ry: float, rz: float) -> Frame:
        """A frame rotated by intrinsic X, then Y, then Z (degrees).

        Euler angles are how a person describes an orientation — "lay it flat,
        then turn it 30 degrees" — and they are parameter-driven like every
        other number here, so a joint can later drive one directly.
        """
        sx, cx = math.sin(math.radians(rx)), math.cos(math.radians(rx))
        sy, cy = math.sin(math.radians(ry)), math.cos(math.radians(ry))
        sz, cz = math.sin(math.radians(rz)), math.cos(math.radians(rz))

        # R = Rz . Ry . Rx, taken column by column.
        return Frame(
            origin=origin,
            x_axis=Vec3(cz * cy, sz * cy, -sy),
            y_axis=Vec3(cz * sy * sx - sz * cx, sz * sy * sx + cz * cx, cy * sx),
            z_axis=Vec3(cz * sy * cx + sz * sx, sz * sy * cx - cz * sx, cy * cx),
        )

    def to_matrix(self) -> tuple[float, ...]:
        """Column-major 4x4, the layout WebGL and three.js expect."""
        return (
            self.x_axis.x, self.x_axis.y, self.x_axis.z, 0.0,
            self.y_axis.x, self.y_axis.y, self.y_axis.z, 0.0,
            self.z_axis.x, self.z_axis.y, self.z_axis.z, 0.0,
            self.origin.x, self.origin.y, self.origin.z, 1.0,
        )

    @property
    def is_identity(self) -> bool:
        return (
            self.origin.is_close(Vec3.zero())
            and self.x_axis.is_close(Vec3(1, 0, 0))
            and self.y_axis.is_close(Vec3(0, 1, 0))
            and self.z_axis.is_close(Vec3(0, 0, 1))
        )

    def to_local(self, point: Vec3) -> Vec3:
        """World point -> this frame's coordinates."""
        d = point - self.origin
        return Vec3(d.dot(self.x_axis), d.dot(self.y_axis), d.dot(self.z_axis))

    def to_world(self, point: Vec3) -> Vec3:
        return (
            self.origin
            + self.x_axis * point.x
            + self.y_axis * point.y
            + self.z_axis * point.z
        )

    def direction_to_world(self, direction: Vec3) -> Vec3:
        """Rotate a direction out of the frame, ignoring translation."""
        return (
            self.x_axis * direction.x + self.y_axis * direction.y + self.z_axis * direction.z
        )

    def direction_to_local(self, direction: Vec3) -> Vec3:
        """Rotate a direction into the frame, ignoring translation."""
        return Vec3(
            direction.dot(self.x_axis),
            direction.dot(self.y_axis),
            direction.dot(self.z_axis),
        )

    def point_at(self, uv: Vec2, offset: float = 0.0) -> Vec3:
        """Lift an in-plane (u, v) sketch coordinate into world space."""
        return self.to_world(Vec3(uv.x, uv.y, offset))

    def translated(self, delta: Vec3) -> Frame:
        return Frame(self.origin + delta, self.x_axis, self.y_axis, self.z_axis)

    def with_origin(self, origin: Vec3) -> Frame:
        return Frame(origin, self.x_axis, self.y_axis, self.z_axis)

    def flipped(self) -> Frame:
        """Same plane, reversed normal, still right-handed."""
        return Frame(self.origin, self.x_axis, -self.y_axis, -self.z_axis)
