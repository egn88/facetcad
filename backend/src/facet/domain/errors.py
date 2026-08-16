"""Domain error hierarchy.

Every failure carries enough structure for the UI and the API to render an
actionable diagnostic. The project's central promise is that a rebuild never
silently guesses; that promise is only as good as the errors raised here, so
these types deliberately carry *why* alongside *what*.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class FacetCADError(Exception):
    """Base class for every domain-level failure."""

    def as_dict(self) -> dict[str, object]:
        return {"kind": type(self).__name__, "message": str(self)}


# --------------------------------------------------------------------------
# Expressions and parameters
# --------------------------------------------------------------------------


@dataclass
class ExpressionError(FacetCADError):
    """An expression could not be parsed or evaluated."""

    expression: str
    reason: str
    parameter: str | None = None

    def __str__(self) -> str:
        where = f" for parameter '{self.parameter}'" if self.parameter else ""
        return f"invalid expression{where}: {self.expression!r} — {self.reason}"

    def as_dict(self) -> dict[str, object]:
        return {
            **super().as_dict(),
            "expression": self.expression,
            "reason": self.reason,
            "parameter": self.parameter,
        }


@dataclass
class CircularDependencyError(FacetCADError):
    """Parameters reference each other in a cycle."""

    cycle: tuple[str, ...]

    def __str__(self) -> str:
        return "circular parameter dependency: " + " -> ".join([*self.cycle, self.cycle[0]])

    def as_dict(self) -> dict[str, object]:
        return {**super().as_dict(), "cycle": list(self.cycle)}


@dataclass
class UnknownParameterError(FacetCADError):
    name: str
    referenced_by: str | None = None

    def __str__(self) -> str:
        where = f" (referenced by '{self.referenced_by}')" if self.referenced_by else ""
        return f"unknown parameter '{self.name}'{where}"

    def as_dict(self) -> dict[str, object]:
        return {**super().as_dict(), "name": self.name, "referenced_by": self.referenced_by}


# --------------------------------------------------------------------------
# Naming and selection — the heart of the system
# --------------------------------------------------------------------------


@dataclass
class TagSyntaxError(FacetCADError):
    """A tag or selector shorthand string is malformed."""

    text: str
    reason: str

    def __str__(self) -> str:
        return f"malformed tag {self.text!r}: {self.reason}"

    def as_dict(self) -> dict[str, object]:
        return {**super().as_dict(), "text": self.text, "reason": self.reason}


@dataclass
class SelectorResolutionError(FacetCADError):
    """A selector did not resolve to the geometry the document expects.

    This is the error that replaces FreeCAD's silent re-binding. It reports the
    expectation, what was actually found, and — where the history knows —
    the feature responsible for the discrepancy.
    """

    selector: str
    expected: int | None
    actual: int
    feature: str | None = None
    missing: tuple[str, ...] = ()
    unexpected: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    def __str__(self) -> str:
        head = f"selector {self.selector} "
        if self.expected is None:
            head += f"resolved to nothing (found {self.actual})"
        else:
            head += f"expected {self.expected} result(s), resolved {self.actual}"
        parts = [head]
        if self.missing:
            parts.append("  missing: " + ", ".join(self.missing))
        if self.unexpected:
            parts.append("  unexpected: " + ", ".join(self.unexpected))
        parts.extend(f"  reason: {r}" for r in self.reasons)
        return "\n".join(parts)

    def as_dict(self) -> dict[str, object]:
        return {
            **super().as_dict(),
            "selector": self.selector,
            "expected": self.expected,
            "actual": self.actual,
            "feature": self.feature,
            "missing": list(self.missing),
            "unexpected": list(self.unexpected),
            "reasons": list(self.reasons),
        }


@dataclass
class AmbiguousSplitError(FacetCADError):
    """Split-face ordinals could not be assigned deterministically.

    Raised when two sibling fragments have centroids too close to order
    reliably, which would make the ``#n`` suffix unstable across a rebuild.
    The fix is an explicit anchor on the offending face.
    """

    tag: str
    candidates: int
    separation: float
    tolerance: float

    def __str__(self) -> str:
        return (
            f"cannot deterministically order {self.candidates} fragments of '{self.tag}': "
            f"closest centroid separation {self.separation:.6g} is below the ordering "
            f"tolerance {self.tolerance:.6g}. Pin this face with an explicit anchor."
        )

    def as_dict(self) -> dict[str, object]:
        return {
            **super().as_dict(),
            "tag": self.tag,
            "candidates": self.candidates,
            "separation": self.separation,
            "tolerance": self.tolerance,
        }


# --------------------------------------------------------------------------
# Document structure
# --------------------------------------------------------------------------


@dataclass
class DocumentError(FacetCADError):
    """The document is structurally invalid."""

    reason: str
    path: str | None = None

    def __str__(self) -> str:
        where = f" at {self.path}" if self.path else ""
        return f"invalid document{where}: {self.reason}"

    def as_dict(self) -> dict[str, object]:
        return {**super().as_dict(), "reason": self.reason, "path": self.path}


@dataclass
class DuplicateIdError(FacetCADError):
    kind: str
    identifier: str

    def __str__(self) -> str:
        return f"duplicate {self.kind} id '{self.identifier}'"

    def as_dict(self) -> dict[str, object]:
        return {**super().as_dict(), "kind": self.kind, "identifier": self.identifier}


@dataclass
class UnknownReferenceError(FacetCADError):
    kind: str
    identifier: str
    referenced_by: str | None = None

    def __str__(self) -> str:
        where = f" (referenced by '{self.referenced_by}')" if self.referenced_by else ""
        return f"unknown {self.kind} '{self.identifier}'{where}"

    def as_dict(self) -> dict[str, object]:
        return {
            **super().as_dict(),
            "kind": self.kind,
            "identifier": self.identifier,
            "referenced_by": self.referenced_by,
        }


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------


@dataclass
class FeatureBuildError(FacetCADError):
    """A feature failed to build. Upstream features remain valid."""

    feature: str
    reason: str
    cause: FacetCADError | None = None

    def __str__(self) -> str:
        base = f"feature '{self.feature}' failed: {self.reason}"
        return f"{base}\n{self.cause}" if self.cause else base

    def as_dict(self) -> dict[str, object]:
        return {
            **super().as_dict(),
            "feature": self.feature,
            "reason": self.reason,
            "cause": self.cause.as_dict() if self.cause else None,
        }


@dataclass
class CapabilityError(FacetCADError):
    """The configured kernel cannot perform the requested operation."""

    capability: str
    kernel: str
    available: tuple[str, ...] = field(default_factory=tuple)

    def __str__(self) -> str:
        return (
            f"kernel '{self.kernel}' does not support '{self.capability}' "
            f"(available: {', '.join(self.available) or 'none'})"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            **super().as_dict(),
            "capability": self.capability,
            "kernel": self.kernel,
            "available": list(self.available),
        }
