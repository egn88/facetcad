"""Finger joints cut into a part's own faces.

The third of the three laser exports, and the one that leans hardest on the
naming engine: two panels know they are a mating pair because the edge between
their faces reports the same ref to both, and the phase is decided by sorting
their tags — no geometry matching, and stable across rebuilds because tags are.
"""

from __future__ import annotations

import copy
from itertools import pairwise

import pytest

pytest.importorskip("OCP", reason="requires the optional OCCT extra")

from facet.adapters.geometry.occt import OcctKernel
from facet.adapters.persistence.filesystem import FilesystemDocumentRepository
from facet.application.jointed import JointSpec, joint_faces
from facet.application.ports.geometry import Line2D, Loop2D, Profile2D
from facet.application.services import ProjectService
from facet.domain.document import Document
from facet.domain.errors import DocumentError

from .test_flatten import PART

pytestmark = pytest.mark.occt


@pytest.fixture(scope="module")
def service(tmp_path_factory) -> ProjectService:
    folder = tmp_path_factory.mktemp("jointed")
    repository = FilesystemDocumentRepository(folder)
    api = ProjectService(repository, OcctKernel())
    repository.create("part", Document.from_dict(copy.deepcopy(PART)))
    return api


def crosses(first: Line2D, second: Line2D) -> bool:
    def side(p, q, r) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    a1, a2, b1, b2 = first.start, first.end, second.start, second.end
    d1, d2 = side(b1, b2, a1), side(b1, b2, a2)
    d3, d4 = side(a1, a2, b1), side(a1, a2, b2)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


# -- what comes out --------------------------------------------------------


def test_joints_are_cut_into_shared_edges(service: ProjectService) -> None:
    result = service.jointed_faces("part", thickness=1.0, finger=3.0)
    assert result.joints > 20


def test_every_face_still_appears(service: ProjectService) -> None:
    """A panel that cannot be jointed is emitted plain, never dropped."""
    plain = service.flat_faces("part")
    jointed = service.jointed_faces("part", thickness=1.0, finger=3.0)
    assert len(jointed.panels) == len(plain.panels)


@pytest.mark.parametrize(
    ("thickness", "finger"),
    [(1.0, 3.0), (2.0, 5.0), (3.0, 8.0), (0.6, 2.0)],
    ids=["1mm", "2mm", "3mm", "thin"],
)
def test_no_panel_outline_crosses_itself(
    service: ProjectService, thickness: float, finger: float
) -> None:
    """The check that catches every way a joint can go wrong at once."""
    for profile in service.jointed_faces("part", thickness, finger).panels:
        curves = [c for c in profile.loops[0].curves if isinstance(c, Line2D)]
        for i, first in enumerate(curves):
            for j in range(i + 2, len(curves)):
                if i == 0 and j == len(curves) - 1:
                    continue
                assert not crosses(first, curves[j]), f"{profile.label}: {i} x {j}"


def test_a_panel_too_narrow_for_the_material_says_so(service: ProjectService) -> None:
    """Recesses that would meet in the middle produce a part that falls apart."""
    result = service.jointed_faces("part", thickness=3.0, finger=8.0)
    assert any("too narrow" in note for note in result.plain)


def test_thicker_material_joints_fewer_edges(service: ProjectService) -> None:
    thin = service.jointed_faces("part", thickness=0.6, finger=2.0)
    thick = service.jointed_faces("part", thickness=3.0, finger=8.0)
    assert thick.joints < thin.joints
    assert len(thick.plain) >= len(thin.plain)


def test_the_result_survives_a_parameter_change(service: ProjectService) -> None:
    """The cutting list tracks the model, joints and all."""
    before = service.jointed_faces("part", thickness=1.0, finger=3.0)
    service.update_parameters("part", {"span": 46.0})
    after = service.jointed_faces("part", thickness=1.0, finger=3.0)
    assert {p.label for p in after.panels} == {p.label for p in before.panels}
    service.update_parameters("part", {"span": 40.0})


# -- the pairing rule ------------------------------------------------------


def square(label: str, size: float, edges: tuple[str, str, str, str]) -> Profile2D:
    corners = [(0.0, 0.0), (size, 0.0), (size, size), (0.0, size)]
    return Profile2D(
        loops=(
            Loop2D(
                curves=tuple(
                    Line2D(start=corners[i], end=corners[(i + 1) % 4], edge=edges[i])
                    for i in range(4)
                )
            ),
        ),
        label=label,
    )


def test_two_panels_sharing_an_edge_take_opposite_phases() -> None:
    """Otherwise the joint does not interlock — teeth would meet teeth."""
    a = square("a/face", 40.0, ("shared", "", "", ""))
    b = square("b/face", 40.0, ("shared", "", "", ""))
    result = joint_faces({a.label: a, b.label: b}, JointSpec(3.0, 10.0, 0.0))

    first, second = result.panels
    assert _starts_raised(first) != _starts_raised(second)


def test_an_edge_touching_only_one_panel_gets_no_joint() -> None:
    """A free edge has nothing to interlock with."""
    only = square("a/face", 40.0, ("lonely", "", "", ""))
    result = joint_faces({only.label: only}, JointSpec(3.0, 10.0, 0.0))
    assert result.joints == 0


def test_the_phase_does_not_depend_on_the_order_panels_are_given() -> None:
    a = square("a/face", 40.0, ("shared", "", "", ""))
    b = square("b/face", 40.0, ("shared", "", "", ""))
    forwards = joint_faces({a.label: a, b.label: b}, JointSpec(3.0, 10.0, 0.0))
    backwards = joint_faces({b.label: b, a.label: a}, JointSpec(3.0, 10.0, 0.0))
    assert [p.label for p in forwards.panels] == [p.label for p in backwards.panels]
    assert _starts_raised(forwards.panels[0]) == _starts_raised(backwards.panels[0])


# -- refusals --------------------------------------------------------------


def test_material_thicker_than_the_finger_is_refused() -> None:
    face = square("a/face", 40.0, ("shared", "", "", ""))
    with pytest.raises(DocumentError, match="at least as wide"):
        joint_faces({face.label: face}, JointSpec(thickness=10.0, finger=3.0))


def test_a_negative_kerf_is_refused() -> None:
    face = square("a/face", 40.0, ("shared", "", "", ""))
    with pytest.raises(DocumentError, match="kerf"):
        joint_faces({face.label: face}, JointSpec(3.0, 10.0, kerf=-0.1))


def test_zero_thickness_is_refused() -> None:
    face = square("a/face", 40.0, ("shared", "", "", ""))
    with pytest.raises(DocumentError, match="thickness"):
        joint_faces({face.label: face}, JointSpec(thickness=0.0))


# -- helpers ---------------------------------------------------------------


def _starts_raised(profile: Profile2D) -> bool:
    """Whether the panel's first jointed run begins on the outer line."""
    curves = [c for c in profile.loops[0].curves if isinstance(c, Line2D)]
    ys = [c.start[1] for c in curves]
    return abs(curves[0].start[1] - min(ys)) < abs(curves[2].start[1] - min(ys))


def test_no_segment_has_zero_length(service: ProjectService) -> None:
    for profile in service.jointed_faces("part", 1.0, 3.0).panels:
        for curve in profile.loops[0].curves:
            if isinstance(curve, Line2D):
                assert curve.start != curve.end


def test_panels_do_not_overlap_after_jointing(service: ProjectService) -> None:
    spans = []
    for panel in service.jointed_faces("part", 1.0, 3.0).panels:
        xs = [c.start[0] for c in panel.loops[0].curves if isinstance(c, Line2D)]
        spans.append((min(xs), max(xs)))
    spans.sort()
    for (_, end), (start, _) in pairwise(spans):
        assert start >= end - 1e-6


# -- sizing the teeth ------------------------------------------------------


def teeth_on(profile: Profile2D) -> int:
    """Count the direction reversals on the panel's first jointed run."""
    curves = [c for c in profile.loops[0].curves if isinstance(c, Line2D)]
    return sum(1 for c in curves if abs(c.start[0] - c.end[0]) < 1e-9)


def test_a_fixed_tooth_count_ignores_the_edge_length(service: ProjectService) -> None:
    """What keeps a small face and a large one both looking like joints.

    With a fixed finger width a short edge gets the minimum three teeth and a
    long one gets a comb; asking for a count instead makes them consistent.
    """
    five = service.jointed_faces("part", thickness=1.0, teeth=5)
    assert five.joints > 0
    for profile in five.panels:
        curves = [c for c in profile.loops[0].curves if isinstance(c, Line2D)]
        assert curves


def test_an_even_tooth_count_is_refused() -> None:
    face = square("a/face", 40.0, ("shared", "", "", ""))
    with pytest.raises(DocumentError, match="odd number"):
        joint_faces({face.label: face}, JointSpec(3.0, teeth=4))


def test_fewer_than_three_teeth_is_refused() -> None:
    face = square("a/face", 40.0, ("shared", "", "", ""))
    with pytest.raises(DocumentError, match="at least three"):
        joint_faces({face.label: face}, JointSpec(3.0, teeth=1))


def test_the_recess_depth_can_differ_from_the_thickness() -> None:
    """For a deliberately proud or recessed fit."""
    a = square("a/face", 40.0, ("shared", "", "", ""))
    b = square("b/face", 40.0, ("shared", "", "", ""))
    shallow = joint_faces({a.label: a, b.label: b}, JointSpec(3.0, 10.0, 0.0, depth=1.0))
    deep = joint_faces({a.label: a, b.label: b}, JointSpec(3.0, 10.0, 0.0, depth=5.0))
    assert _recess_depth(shallow.panels[1]) == pytest.approx(1.0)
    assert _recess_depth(deep.panels[1]) == pytest.approx(5.0)


def test_a_zero_depth_is_refused() -> None:
    face = square("a/face", 40.0, ("shared", "", "", ""))
    with pytest.raises(DocumentError, match="depth must be positive"):
        joint_faces({face.label: face}, JointSpec(3.0, depth=0.0))


# -- per-face overrides ----------------------------------------------------


def test_an_override_changes_that_face_and_its_partner() -> None:
    """An edge is shared, so an override on one face has to apply to both.

    Anything else cuts teeth of two different widths along the same joint.
    """
    a = square("a/face", 60.0, ("shared", "", "", ""))
    b = square("b/face", 60.0, ("shared", "", "", ""))
    plain = joint_faces({a.label: a, b.label: b}, JointSpec(3.0, 20.0, 0.0))
    forced = joint_faces(
        {a.label: a, b.label: b}, JointSpec(3.0, 20.0, 0.0, overrides={"b/face": 5.0})
    )
    assert teeth_on(forced.panels[0]) > teeth_on(plain.panels[0])
    assert teeth_on(forced.panels[1]) > teeth_on(plain.panels[1])


def test_two_conflicting_overrides_resolve_the_same_way_for_both_faces() -> None:
    """The tie-break has to be a rule, not whichever face is processed first."""
    a = square("a/face", 60.0, ("shared", "", "", ""))
    b = square("b/face", 60.0, ("shared", "", "", ""))
    spec = JointSpec(3.0, 20.0, 0.0, overrides={"a/face": 5.0, "b/face": 12.0})
    result = joint_faces({a.label: a, b.label: b}, spec)
    assert teeth_on(result.panels[0]) == teeth_on(result.panels[1])


def test_an_override_narrower_than_the_material_is_refused() -> None:
    face = square("a/face", 40.0, ("shared", "", "", ""))
    with pytest.raises(DocumentError, match="at least as wide"):
        joint_faces({face.label: face}, JointSpec(3.0, 10.0, overrides={"a/face": 1.0}))


def test_an_override_on_an_unrelated_face_changes_nothing() -> None:
    a = square("a/face", 60.0, ("shared", "", "", ""))
    b = square("b/face", 60.0, ("shared", "", "", ""))
    spec = JointSpec(3.0, 20.0, 0.0)
    elsewhere = JointSpec(3.0, 20.0, 0.0, overrides={"z/face": 5.0})
    assert teeth_on(joint_faces({a.label: a, b.label: b}, spec).panels[0]) == teeth_on(
        joint_faces({a.label: a, b.label: b}, elsewhere).panels[0]
    )


def _recess_depth(profile: Profile2D) -> float:
    """How far the recessed segments sit in from the panel's outer line."""
    curves = [c for c in profile.loops[0].curves if isinstance(c, Line2D)]
    ys = sorted({round(c.start[1], 6) for c in curves})
    return ys[1] - ys[0]


# -- seeing through a blend ------------------------------------------------


def test_a_chamfered_edge_still_joins_the_faces_it_bevels_between() -> None:
    """A blend is left out of the cutting list, but its neighbours still meet.

    A chamfer between two faces is a bevel a millimetre wide; the panels either
    side of it are neighbours in every sense that matters to a joint. Without
    this, a chamfered part comes out with plain edges exactly where the chamfers
    were — which is how a wedge ended up unjoinable along two of its sides.
    """
    a = square("a/face", 40.0, ("bevelled", "", "", ""))
    b = square("b/face", 40.0, ("other_side", "", "", ""))
    panels = {a.label: a, b.label: b}

    # The chamfer sits between them, and is not in the cutting list.
    adjacency = {
        "bevelled": ("a/face", "ch/chamfer[a/face ^ b/face]"),
        "other_side": ("b/face", "ch/chamfer[a/face ^ b/face]"),
    }
    result = joint_faces(panels, JointSpec(3.0, 10.0, 0.0), adjacency=adjacency)
    assert result.joints == 2
    assert _starts_raised(result.panels[0]) != _starts_raised(result.panels[1])


def test_a_blend_touching_three_panels_is_left_alone() -> None:
    """A corner patch has no single partner to join to, so it is not guessed at."""
    faces = {
        label: square(label, 40.0, (edge, "", "", ""))
        for label, edge in (("a/face", "e1"), ("b/face", "e2"), ("c/face", "e3"))
    }
    adjacency = {
        "e1": ("a/face", "x/corner[..]"),
        "e2": ("b/face", "x/corner[..]"),
        "e3": ("c/face", "x/corner[..]"),
    }
    assert joint_faces(faces, JointSpec(3.0, 10.0, 0.0), adjacency=adjacency).joints == 0


def test_an_edge_already_shared_keeps_its_own_pairing() -> None:
    """Bridging must not steal an edge that two panels already share."""
    a = square("a/face", 40.0, ("shared", "", "", ""))
    b = square("b/face", 40.0, ("shared", "", "", ""))
    adjacency = {"shared": ("a/face", "b/face")}
    direct = joint_faces({a.label: a, b.label: b}, JointSpec(3.0, 10.0, 0.0))
    bridged = joint_faces(
        {a.label: a, b.label: b}, JointSpec(3.0, 10.0, 0.0), adjacency=adjacency
    )
    assert direct.joints == bridged.joints


def test_adjacency_is_optional() -> None:
    """Callers that have no blend information still get direct pairings."""
    a = square("a/face", 40.0, ("shared", "", "", ""))
    b = square("b/face", 40.0, ("shared", "", "", ""))
    assert joint_faces({a.label: a, b.label: b}, JointSpec(3.0, 10.0, 0.0)).joints == 2


# -- shallow corners -------------------------------------------------------


def test_a_short_run_between_recessed_neighbours_does_not_spike() -> None:
    """Where two runs meet at a shallow angle the mitre is unbounded.

    Measured on a real part: a 1mm inset against a 1.1mm run put the mitred
    corner 2mm past the end of it, and the closing segment then cut back across
    the panel. Beyond the mitre limit the corner is bevelled instead, the same
    fix a stroke renderer applies for the same reason.
    """
    corners = [(0.0, 0.0), (40.0, 0.0), (40.0, 25.0), (0.0, 25.0), (-0.4, 1.1)]
    profile = Profile2D(
        loops=(
            Loop2D(
                curves=tuple(
                    Line2D(
                        start=corners[i],
                        end=corners[(i + 1) % len(corners)],
                        edge=f"e{i}",
                    )
                    for i in range(len(corners))
                )
            ),
        ),
        label="a/face",
    )
    partner = square("b/face", 40.0, ("e0", "e1", "e2", "e3"))
    result = joint_faces(
        {profile.label: profile, partner.label: partner}, JointSpec(1.0, 3.0, 0.0)
    )
    for panel in result.panels:
        curves = [c for c in panel.loops[0].curves if isinstance(c, Line2D)]
        for i, first in enumerate(curves):
            for j in range(i + 2, len(curves)):
                if i == 0 and j == len(curves) - 1:
                    continue
                assert not crosses(first, curves[j]), f"{panel.label}: {i} x {j}"


# -- which side of the material the model is on ----------------------------


def envelope(panels: tuple[Profile2D, ...], label: str) -> tuple[float, float]:
    profile = next(p for p in panels if p.label == label)
    curves = [c for c in profile.loops[0].curves if isinstance(c, Line2D)]
    xs = [c.start[0] for c in curves]
    ys = [c.start[1] for c in curves]
    return (max(xs) - min(xs), max(ys) - min(ys))


def test_fitting_outer_keeps_the_assembly_the_size_of_the_model(
    service: ProjectService,
) -> None:
    """A 100mm box should measure 100mm when it is built.

    Fitting outer, every tooth stops at its own face's boundary and only the
    recesses move inward, so the assembled envelope is the modelled solid.
    """
    outer = service.jointed_faces("part", thickness=1.0, finger=3.0, fit="outer")
    plain = service.flat_faces("part")
    for panel in outer.panels:
        width, height = envelope(outer.panels, panel.label)
        flat_w, flat_h = envelope(plain.panels, panel.label)
        assert width <= flat_w + 1e-9
        assert height <= flat_h + 1e-9


def test_fitting_inner_stands_a_thickness_proud(service: ProjectService) -> None:
    """Treating the solid as the cavity makes the assembly larger, deliberately."""
    outer = service.jointed_faces("part", thickness=1.0, finger=3.0, fit="outer")
    inner = service.jointed_faces("part", thickness=1.0, finger=3.0, fit="inner")

    grew = [
        label
        for label in (p.label for p in outer.panels)
        if envelope(inner.panels, label) != envelope(outer.panels, label)
    ]
    assert grew, "fitting inner has to differ somewhere, or the flag means nothing"
    for label in grew:
        assert envelope(inner.panels, label) >= envelope(outer.panels, label)


def test_outer_is_the_default(service: ProjectService) -> None:
    """The size a person states is the one they can measure afterwards."""
    default = service.jointed_faces("part", thickness=1.0, finger=3.0)
    outer = service.jointed_faces("part", thickness=1.0, finger=3.0, fit="outer")
    assert [envelope(default.panels, p.label) for p in default.panels] == [
        envelope(outer.panels, p.label) for p in outer.panels
    ]


def test_both_fits_still_interlock() -> None:
    """Changing which side the model is on must not break the joint itself."""
    for fit in ("outer", "inner"):
        a = square("a/face", 40.0, ("shared", "", "", ""))
        b = square("b/face", 40.0, ("shared", "", "", ""))
        result = joint_faces({a.label: a, b.label: b}, JointSpec(3.0, 10.0, 0.0, fit=fit))
        assert _starts_raised(result.panels[0]) != _starts_raised(result.panels[1]), fit


def test_an_unknown_fit_is_refused() -> None:
    face = square("a/face", 40.0, ("shared", "", "", ""))
    with pytest.raises(DocumentError, match="fit must be"):
        joint_faces({face.label: face}, JointSpec(3.0, 10.0, fit="middle"))


@pytest.mark.parametrize("fit", ["outer", "inner"], ids=["outer", "inner"])
def test_neither_fit_makes_a_panel_cross_itself(service: ProjectService, fit: str) -> None:
    for profile in service.jointed_faces("part", 1.0, 3.0, fit=fit).panels:
        curves = [c for c in profile.loops[0].curves if isinstance(c, Line2D)]
        for i, first in enumerate(curves):
            for j in range(i + 2, len(curves)):
                if i == 0 and j == len(curves) - 1:
                    continue
                assert not crosses(first, curves[j]), f"{profile.label}: {i} x {j}"
