"""Cutting finger joints into a part's own faces.

Three exports sit next to each other and it is worth being clear which is which:

* **faces** (:mod:`facet.application.flatten`) — every planar face, flat, with
  plain edges. What you want when the panels are not being joined to each other.
* **box** (:mod:`facet.application.enclosure`) — a rectangular finger-jointed
  container *for* the part, sized from its bounding box.
* **this** — the part's *own* faces, with finger joints cut into the edges they
  share, so the modelled shape can be laser-cut and assembled.

The naming engine is what makes the third one possible. Every edge of the solid
is reported with the same ref to both faces that meet along it, so two panels
know they are a pair without any geometric matching, and the phase can be
assigned canonically — the face whose tag sorts first supplies the teeth. That
is stable across rebuilds because tags are.

Inner or outer
--------------

A joint has to decide which side of the material the modelled surface is on, and
the two answers differ by a thickness at every edge.

``outer`` — the default — puts the modelled solid on the *outside*: every tooth
stops at the face's own boundary and every recess is cut inward from it, so the
assembled box measures what the model measures. That is what a person means by
"a 100mm box".

``inner`` treats the solid as the cavity: one panel of each pair stands a
thickness proud, so the assembly comes out a thickness larger at every joint and
the *inside* matches the model. That is what you want when the part being
enclosed is the thing you modelled.

Only straight edges take a joint. A finger joint along an arc is a different
construction, and a panel whose outline contains one is emitted plain rather
than mangled.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from facet.domain.errors import DocumentError

from .ports.geometry import Arc2D, Curve2D, Line2D, Loop2D, Profile2D

#: Below this, two points are the same point.
_TOL = 1e-7

#: How far a mitred corner may travel from the vertex, as a multiple of the
#: inset. The same idea as a stroke renderer's mitre limit, and for the same
#: reason: at a shallow angle the mitre is unbounded.
MITRE_LIMIT = 2.0

#: The modelled solid is the outside of the assembly.
OUTER = "outer"
#: The modelled solid is the cavity the assembly encloses.
INNER = "inner"


@dataclass(frozen=True)
class JointSpec:
    """How the joints are cut. All lengths in millimetres.

    Two ways to size the teeth, because a part rarely has faces of one size.
    Give a ``finger`` width and each edge gets as many teeth as fit; give
    ``teeth`` instead and every edge gets that many however long it is, which is
    what keeps a 12mm face and a 120mm face both looking like joints rather than
    one hinge and one comb.

    ``overrides`` sets a finger width per face tag for the awkward ones. An edge
    is shared by two faces, so an override on either applies to both — anything
    else would cut teeth that do not mate.
    """

    thickness: float
    finger: float = 10.0
    kerf: float = 0.15
    #: Fixed number of teeth per edge, overriding ``finger`` when set.
    teeth: int | None = None
    #: How deep a recess is cut. Defaults to the material thickness, which is
    #: what makes a joint sit flush; a different value is for a deliberate
    #: proud or recessed fit.
    depth: float | None = None
    #: Face tag -> finger width, for faces the global setting does not suit.
    overrides: Mapping[str, float] = field(default_factory=dict)
    #: ``"outer"`` if the modelled solid is the outside of the assembly,
    #: ``"inner"`` if it is the cavity. See the module docstring.
    fit: str = OUTER

    @property
    def recess(self) -> float:
        return self.thickness if self.depth is None else self.depth

    def validate(self) -> None:
        if self.thickness <= 0:
            raise DocumentError(
                reason=f"material thickness must be positive, got {self.thickness:g}",
                path="joints",
            )
        if self.recess <= 0:
            raise DocumentError(
                reason=f"joint depth must be positive, got {self.recess:g}", path="joints"
            )
        if self.teeth is not None and (self.teeth < 3 or self.teeth % 2 == 0):
            raise DocumentError(
                reason=(
                    f"a joint needs an odd number of teeth, at least three, got "
                    f"{self.teeth}. An even count leaves one corner with a tooth and "
                    "the other with a gap."
                ),
                path="joints",
            )
        for width in (self.finger, *self.overrides.values()):
            if width < self.thickness:
                raise DocumentError(
                    reason=(
                        f"a {width:g}mm finger in {self.thickness:g}mm material is "
                        "weaker than the material it joins; make the finger at least "
                        "as wide as the sheet is thick"
                    ),
                    path="joints",
                )
        if self.kerf < 0:
            raise DocumentError(reason="kerf cannot be negative", path="joints")
        if self.fit not in (OUTER, INNER):
            raise DocumentError(
                reason=f"fit must be {OUTER!r} or {INNER!r}, got {self.fit!r}",
                path="joints",
            )

    def finger_for(self, pair: tuple[str, str]) -> float:
        """The finger width for an edge, honouring either face's override.

        The two panels must agree or the joint will not mate, so a single rule
        decides: an override on either face wins, and if both have one the face
        whose tag sorts first does — the same tie-break the phase uses.
        """
        for label in pair:
            if label in self.overrides:
                return self.overrides[label]
        return self.finger


@dataclass(frozen=True)
class JointedResult:
    panels: tuple[Profile2D, ...] = ()
    #: Panels emitted without joints, each as "label: reason".
    plain: tuple[str, ...] = ()
    #: How many edges actually received a joint.
    joints: int = 0


def joint_faces(
    panels: dict[str, Profile2D],
    spec: JointSpec,
    adjacency: Mapping[str, tuple[str, str]] | None = None,
) -> JointedResult:
    """Cut finger joints into every edge shared by two of ``panels``.

    ``adjacency`` is the model's full edge-to-face map, blends included. Without
    it an edge against an excluded fillet or chamfer reads as a free edge and
    gets no joint, which leaves a bevelled part with plain sides where the bevel
    was — see :func:`_shared_edges`.
    """
    spec.validate()

    shared = _shared_edges(panels, adjacency or {})
    out: list[Profile2D] = []
    plain: list[str] = []
    joints = 0

    for label in sorted(panels):
        profile = panels[label]
        outer = next((loop for loop in profile.loops if loop.outer), None)
        if outer is None or not outer.curves:
            plain.append(f"{label}: nothing to cut")
            out.append(profile)
            continue
        if any(isinstance(curve, Arc2D) for curve in outer.curves):
            # A finger joint along an arc is a different construction; a panel
            # with one is better plain than mangled.
            plain.append(f"{label}: the outline curves")
            out.append(profile)
            continue

        runs = _runs(outer.curves)
        pairs = [shared.get(run.edge) for run in runs]
        phases = [
            _phase(label, pair) if _jointable(run, spec) else None
            for run, pair in zip(runs, pairs, strict=True)
        ]
        fingers = [
            spec.finger_for(pair) if pair else spec.finger for pair in pairs
        ]
        if not any(phase is not None for phase in phases):
            plain.append(f"{label}: no edge long enough to joint")
            out.append(profile)
            continue

        boundary = _jointed_outline(runs, phases, fingers, spec)
        if _self_intersects(boundary):
            # The recesses met in the middle: the panel is too narrow for the
            # material it would be joined with. Emitting the tangle anyway would
            # cut a part that falls apart on the bed, so the face goes out plain
            # and says why.
            plain.append(
                f"{label}: too narrow for {spec.thickness:g}mm joints"
            )
            out.append(profile)
            continue

        joints += sum(1 for phase in phases if phase is not None)
        out.append(
            replace(
                profile,
                loops=(
                    Loop2D(curves=tuple(boundary), outer=True),
                    *(loop for loop in profile.loops if not loop.outer),
                ),
            )
        )

    return JointedResult(panels=tuple(out), plain=tuple(plain), joints=joints)


def _self_intersects(boundary: list[Line2D]) -> bool:
    """Whether a closed outline crosses itself.

    Cheap (the outlines are tens of segments) and the only check that catches
    every way a joint can go wrong at once — overlapping recesses, a spike at a
    corner, a run shorter than its own teeth.
    """
    count = len(boundary)
    for i in range(count):
        for j in range(i + 2, count):
            if i == 0 and j == count - 1:
                continue  # neighbours around the close
            if _crosses(boundary[i], boundary[j]):
                return True
    return False


def _crosses(first: Line2D, second: Line2D) -> bool:
    def side(p, q, r) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    a1, a2 = first.start, first.end
    b1, b2 = second.start, second.end
    d1, d2 = side(b1, b2, a1), side(b1, b2, a2)
    d3, d4 = side(a1, a2, b1), side(a1, a2, b2)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


# --------------------------------------------------------------------------
# Pairing panels up
# --------------------------------------------------------------------------


def _shared_edges(
    panels: dict[str, Profile2D], adjacency: Mapping[str, tuple[str, str]]
) -> dict[str, tuple[str, str]]:
    """Model edge ref -> the two panel labels that meet along it.

    Two edges get a pair. The plain case is an edge whose faces are both in the
    cutting list. The other is an edge against a face that was left out — a
    fillet or a chamfer — where the two panels either side of the blend are
    still neighbours in every sense that matters to a joint, just separated by a
    bevel a millimetre wide.

    Without the second case a chamfered part comes out with plain edges exactly
    where the chamfers were, which is how a wedge ended up unjoinable along two
    of its three sides.
    """
    on_panel: dict[str, set[str]] = {}
    for label, profile in panels.items():
        for loop in profile.loops:
            for curve in loop.curves:
                if curve.edge:
                    on_panel.setdefault(curve.edge, set()).add(label)

    shared = {
        ref: (first, second)
        for ref, labels in on_panel.items()
        if len(labels) == 2
        for first, second in [tuple(sorted(labels))]
    }

    for ref, pair in _across_blends(on_panel, adjacency, panels).items():
        shared.setdefault(ref, pair)
    return shared


def _across_blends(
    on_panel: dict[str, set[str]],
    adjacency: Mapping[str, tuple[str, str]],
    panels: dict[str, Profile2D],
) -> dict[str, tuple[str, str]]:
    """Pair up the panels either side of a face that was left out.

    A blend face is bounded by the two panels it bevels between. Each of those
    panels has its own edge against the blend, at slightly different places —
    so the two edges are given the *same* pair, and the phase rule then hands
    one of them the teeth exactly as it would for a shared edge.

    Only a blend with exactly two neighbours in the cutting list qualifies. A
    corner patch touches three or more and there is no single partner to join
    to, so those are left alone rather than guessed at.
    """
    if not adjacency:
        return {}

    # Which panels sit around each face that did not make the list.
    neighbours: dict[str, set[str]] = {}
    edges_of: dict[str, list[str]] = {}
    for ref, (first, second) in adjacency.items():
        for absent, present in ((first, second), (second, first)):
            if absent in panels or present not in panels:
                continue
            neighbours.setdefault(absent, set()).add(present)
            edges_of.setdefault(absent, []).append(ref)

    bridged: dict[str, tuple[str, str]] = {}
    for absent, around in neighbours.items():
        if len(around) != 2:
            continue
        pair = tuple(sorted(around))
        for ref in edges_of[absent]:
            # An edge already shared by two panels keeps that pairing.
            if len(on_panel.get(ref, ())) < 2:
                bridged[ref] = pair  # type: ignore[assignment]
    return bridged


def _jointable(run: _Run, spec: JointSpec) -> bool:
    """Whether a run is long enough to be worth a joint.

    Three teeth is the minimum that reads as a joint rather than a hinge, and
    each has to be at least as wide as the material is thick. Both panels
    sharing an edge measure the same length, so they agree without talking.
    """
    return math.dist(run.start, run.end) >= 3 * spec.recess


def _phase(label: str, pair: tuple[str, str] | None) -> bool | None:
    """Whether this panel supplies the teeth on a shared edge.

    Decided by sorting the two tags, so both panels reach the same answer
    independently and the joint always interlocks. Tags are stable across
    rebuilds, so the phase is too.
    """
    if pair is None:
        return None
    return label == pair[0]


# --------------------------------------------------------------------------
# The outline
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Run:
    """One straight stretch of the boundary, and the model edge behind it."""

    start: tuple[float, float]
    end: tuple[float, float]
    edge: str


def _runs(curves: tuple[Curve2D, ...]) -> list[_Run]:
    """Merge consecutive collinear runs that came from the same model edge."""
    runs: list[_Run] = []
    for curve in curves:
        assert isinstance(curve, Line2D)
        if runs and runs[-1].edge == curve.edge and curve.edge:
            runs[-1] = _Run(runs[-1].start, curve.end, curve.edge)
        else:
            runs.append(_Run(curve.start, curve.end, curve.edge))
    return runs


def _jointed_outline(
    runs: list[_Run],
    phases: list[bool | None],
    fingers: list[float],
    spec: JointSpec,
) -> list[Line2D]:
    """Walk the boundary, cutting teeth into every run that has a phase.

    Corners are where this gets interesting. A run that begins and ends recessed
    pulls its corners in by one thickness, so the corner is the intersection of
    the two neighbouring *offset* lines rather than the modelled vertex.

    That mitre is right at a convex corner: the material inside it belongs to
    the mating panel, and running out to the modelled vertex would collide with
    it. At a *reflex* corner the two offset lines move apart instead, and their
    intersection flies off behind the boundary as a spike. There the corner is
    bevelled: each run stops at its own offset from the vertex and a short step
    joins the two.
    """
    inward = _inward_sign(runs)
    count = len(runs)
    insets = [0.0 if phase is None or phase else spec.recess for phase in phases]

    # Per run: where it starts and where it ends, after the corners are resolved.
    begins: list[tuple[float, float]] = [(0.0, 0.0)] * count
    ends: list[tuple[float, float]] = [(0.0, 0.0)] * count
    for index in range(count):
        nxt = (index + 1) % count
        leaving, arriving = _corner(
            runs[index], insets[index], runs[nxt], insets[nxt], inward
        )
        ends[index] = leaving
        begins[nxt] = arriving

    out: list[Line2D] = []
    for index in range(count):
        run, phase = runs[index], phases[index]
        start, end = begins[index], ends[index]
        if phase is None:
            if not _same(start, end):
                out.append(Line2D(start=start, end=end, edge=run.edge))
        else:
            out.extend(_teeth(start, end, phase, fingers[index], spec, inward, run.edge))

        # The bevel step across a reflex corner, when there is one.
        following = begins[(index + 1) % count]
        if not _same(end, following):
            out.append(Line2D(start=end, end=following, edge=""))
    return out


def _teeth(
    start: tuple[float, float],
    end: tuple[float, float],
    tooth_first: bool,
    finger: float,
    spec: JointSpec,
    inward: float,
    edge: str,
) -> list[Line2D]:
    """The zig-zag along one jointed run."""
    length = math.dist(start, end)
    if length <= _TOL:
        return []

    along = ((end[0] - start[0]) / length, (end[1] - start[1]) / length)
    normal = (-along[1] * inward, along[0] * inward)

    teeth = spec.teeth or _tooth_count(length, finger)
    step = length / teeth
    half = spec.kerf / 2.0
    # Where the run's ends sit relative to the modelled boundary.
    #
    # Fitting outer, both panels of a pair are measured from that boundary and
    # only their recesses move inward, so the assembly's envelope is the solid.
    # Fitting inner, the recessed panel is measured from a thickness *proud* of
    # it, which pushes the envelope out by a thickness at every joint and leaves
    # the cavity matching the solid instead.
    base = 0.0 if (spec.fit == OUTER or tooth_first) else spec.recess

    points: list[tuple[float, float]] = []
    for index in range(teeth):
        raised = (index % 2 == 0) == tooth_first
        depth = (0.0 if raised else spec.recess) - base
        # Kerf moves internal boundaries only: the ends are the panel's corners,
        # and shifting those would resize the panel.
        grow = -half if raised else half
        begin = index * step + (0.0 if index == 0 else grow)
        finish = (index + 1) * step - (0.0 if index == teeth - 1 else grow)
        points.append(_offset(start, along, normal, begin, depth))
        points.append(_offset(start, along, normal, finish, depth))

    return [
        Line2D(start=points[i], end=points[i + 1], edge=edge)
        for i in range(len(points) - 1)
        if not _same(points[i], points[i + 1])
    ]


def _tooth_count(length: float, finger: float) -> int:
    """An odd number, so a run starts and ends on the same phase."""
    count = max(3, round(length / finger))
    return count if count % 2 == 1 else count + 1


def _corner(
    first: _Run,
    first_inset: float,
    second: _Run,
    second_inset: float,
    inward: float,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Where the first run ends and the second begins.

    The same point at a convex corner (a mitre), two points at a reflex one (a
    bevel), because mitring a reflex corner sends the intersection off behind
    the boundary as a spike.
    """
    a_point, a_dir = _offset_line(first, first_inset, inward)
    b_point, b_dir = _offset_line(second, second_inset, inward)
    vertex = first.end

    cross = a_dir[0] * b_dir[1] - a_dir[1] * b_dir[0]
    convex = cross * inward > 1e-9

    if convex:
        dx = b_point[0] - a_point[0]
        dy = b_point[1] - a_point[1]
        t = (dx * b_dir[1] - dy * b_dir[0]) / cross
        mitred = (a_point[0] + a_dir[0] * t, a_point[1] + a_dir[1] * t)
        reach = max(first_inset, second_inset, _TOL)
        if math.dist(mitred, vertex) <= MITRE_LIMIT * reach:
            return (mitred, mitred)
        # Too far. Where two runs meet at a shallow angle the mitre of their
        # offset lines races away from the corner: a 1mm inset against a 1.1mm
        # run produced a point 2mm past the end of it, and the closing segment
        # then cut back across the panel. Bevel instead, as a stroke renderer
        # does for the same reason.

    # Reflex, shallow, or the two runs are parallel: step across instead.
    leaving = _push(vertex, a_dir, first_inset, inward)
    arriving = _push(vertex, b_dir, second_inset, inward)
    return (leaving, arriving)


def _push(
    point: tuple[float, float], along: tuple[float, float], inset: float, inward: float
) -> tuple[float, float]:
    normal = (-along[1] * inward, along[0] * inward)
    return (point[0] + normal[0] * inset, point[1] + normal[1] * inset)


def _offset_line(
    run: _Run, inset: float, inward: float
) -> tuple[tuple[float, float], tuple[float, float]]:
    length = math.dist(run.start, run.end)
    if length <= _TOL:
        return (run.start, (1.0, 0.0))
    along = ((run.end[0] - run.start[0]) / length, (run.end[1] - run.start[1]) / length)
    normal = (-along[1] * inward, along[0] * inward)
    point = (run.start[0] + normal[0] * inset, run.start[1] + normal[1] * inset)
    return (point, along)


def _inward_sign(runs: list[_Run]) -> float:
    """+1 when the boundary runs anticlockwise, -1 when it runs clockwise.

    A face's outer wire is not guaranteed either way — it depends on which side
    of the surface the solid is on — so the direction that points *into* the
    panel has to be derived rather than assumed.
    """
    twice_area = 0.0
    for run in runs:
        twice_area += run.start[0] * run.end[1] - run.end[0] * run.start[1]
    return 1.0 if twice_area >= 0 else -1.0


def _offset(
    origin: tuple[float, float],
    along: tuple[float, float],
    normal: tuple[float, float],
    distance: float,
    depth: float,
) -> tuple[float, float]:
    return (
        origin[0] + along[0] * distance + normal[0] * depth,
        origin[1] + along[1] * distance + normal[1] * depth,
    )


def _same(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return abs(a[0] - b[0]) <= _TOL and abs(a[1] - b[1]) <= _TOL
