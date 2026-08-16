"""Deriving a datum from a face, without ever pointing at the face.

Putting a hole on top of a pad means sketching on the plane of ``base/cap+``.
Typed by hand that plane is a literal number, and the number is right exactly
once: change the plate thickness and the datum stays behind while the material
moves, so the hole comes out in the wrong place.

The document already knows where that face is. ``base/cap+`` is the far cap of
the pad that extrudes sketch ``outline``, which sits on datum ``base``, by
``plate_t``. Reading that back gives a datum whose offset is still the
*expression*, so it is computed from parameters like every other datum and the
rule in :mod:`facet.domain.datum` is honoured rather than worked around.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from pathlib import Path

import pytest

from facet.adapters.geometry.fake import FakeKernel
from facet.adapters.persistence.filesystem import FilesystemDocumentRepository
from facet.application.datum_proposal import propose_datum_for_face
from facet.application.services import ProjectService
from facet.domain.datum import DatumPlane
from facet.domain.document import Document
from facet.domain.errors import UnknownReferenceError
from facet.domain.math3d import Frame, Vec3

from .test_recompute import BRACKET

#: The bracket's plate is 6mm thick, and its pad states that as 'plate_t'.
PLATE_T = 6.0

#: Its pocket is 2mm deep, cut downwards from the 'top' datum.
SLOT_D = 2.0


def bracket(**pad: object) -> dict[str, object]:
    """The bracket document, with the options of its base pad overridden."""
    data = copy.deepcopy(BRACKET)
    features: list[dict[str, object]] = data["features"]  # type: ignore[assignment]
    features[0] = {**features[0], **pad}
    return data


def service_for(tmp_path: Path, document: dict[str, object]) -> ProjectService:
    repository = FilesystemDocumentRepository(tmp_path)
    service = ProjectService(repository, FakeKernel())
    repository.create("bracket", Document.from_dict(document))
    return service


def datum_of(service: ProjectService, tag: str) -> dict[str, object]:
    """The proposed datum payload, asserting the plane was derivable at all."""
    proposed = service.datum_for_face("bracket", tag)
    assert proposed.ok, proposed.reason
    assert proposed.datum is not None
    return dict(proposed.datum)


# --------------------------------------------------------------------------
# A pad's caps
# --------------------------------------------------------------------------


def test_a_pads_far_cap_offsets_by_the_length_expression_not_its_value(
    tmp_path: Path,
) -> None:
    """The offset must arrive as 'plate_t', never as 6.

    A datum holding the number is a dead datum: it was right when it was made
    and wrong after the next edit. Carrying the expression is the entire reason
    this is derived from the document rather than measured off the solid.
    """
    proposed = datum_of(service_for(tmp_path, bracket()), "base/cap+")
    assert proposed["parent"] == "base"
    assert proposed["origin"] == [0, 0, "plate_t"]
    assert proposed["normal"] == [0, 0, 1]


def test_a_pads_near_cap_lies_on_the_sketchs_datum_itself(tmp_path: Path) -> None:
    """A pad grows away from its sketch, so the cap left behind is the sketch.

    Offsetting it by the length as well would put the datum through the far cap
    and every hole placed on it into thin air.
    """
    proposed = datum_of(service_for(tmp_path, bracket()), "base/cap-")
    assert proposed["parent"] == "base"
    assert proposed["origin"] == [0, 0, 0]


def test_a_pad_grown_against_its_normal_swaps_which_cap_is_the_far_one(
    tmp_path: Path,
) -> None:
    """Cap sign follows the face's own normal, not the extrusion direction.

    The naming engine decides cap+ from the outward normal against the sketch
    plane, so a '-normal' pad calls its far cap 'cap-' and leaves 'cap+' on the
    sketch. Guessing from the direction alone would put both planes one length
    out, in opposite directions.
    """
    service = service_for(tmp_path, bracket(direction="-normal"))
    assert datum_of(service, "base/cap-")["origin"] == [0, 0, "-plate_t"]
    assert datum_of(service, "base/cap+")["origin"] == [0, 0, 0]


def test_a_length_given_as_a_number_is_negated_as_a_number(tmp_path: Path) -> None:
    """A literal stays a literal; only its sign changes.

    Turning 6 into the string '-6' would work by accident and read as an
    expression the author never wrote.
    """
    service = service_for(tmp_path, bracket(length=6.0, direction="-normal"))
    assert datum_of(service, "base/cap-")["origin"] == [0, 0, -6.0]


def test_a_negated_expression_negates_the_whole_expression(tmp_path: Path) -> None:
    """'-(plate_t + 2)' and '-plate_t + 2' are eight millimetres apart.

    A leading minus binds tighter than '+' in the expression grammar, so the
    obvious string concatenation would negate only the first term and the datum
    would land above the sketch instead of below it. This asserts on the plane
    the document actually resolves to, because that is the only check that
    catches a string which parses but means something else.
    """
    service = service_for(tmp_path, bracket(length="plate_t + 2", direction="-normal"))
    proposed = datum_of(service, "base/cap-")
    assert proposed["origin"] == [0, 0, "-(plate_t + 2)"]

    result = service.put_datum("bracket", _plane_from(proposed))
    assert result.frames["base_cap_neg"].origin.z == pytest.approx(-(PLATE_T + 2))


def test_a_midplane_pad_reaches_half_its_length_each_way(tmp_path: Path) -> None:
    """Both caps exist, so neither one lies on the sketch.

    A midplane pad is the case where cap sign genuinely cannot come from the
    direction, since the solid grows both ways; answering as if it were
    one-sided would be wrong by a full half-length.
    """
    service = service_for(tmp_path, bracket(midplane=True))
    assert datum_of(service, "base/cap+")["origin"] == [0, 0, "plate_t / 2"]
    assert datum_of(service, "base/cap-")["origin"] == [0, 0, "-(plate_t / 2)"]


# --------------------------------------------------------------------------
# What a cut leaves behind
# --------------------------------------------------------------------------


def test_a_pockets_floor_offsets_by_its_depth_in_the_direction_it_cuts(
    tmp_path: Path,
) -> None:
    """The floor is as far below the sketch as the pocket is deep.

    The parent is the pocket's own sketch datum rather than the world, so the
    two offsets compose and moving 'top' moves the floor with it.
    """
    proposed = datum_of(service_for(tmp_path, bracket()), "slot/floor")
    assert proposed["parent"] == "top"
    assert proposed["origin"] == [0, 0, "-slot_d"]


def test_a_pockets_ceiling_is_the_sketchs_datum_itself(tmp_path: Path) -> None:
    """A cut starts at its sketch, so the ceiling needs no offset at all."""
    proposed = datum_of(service_for(tmp_path, bracket()), "slot/ceiling")
    assert proposed["parent"] == "top"
    assert proposed["origin"] == [0, 0, 0]


def test_a_hole_derives_from_the_sketch_it_is_placed_on(tmp_path: Path) -> None:
    """A hole names its sketch through 'at' rather than through a profile.

    Same plane, different spelling in the document; missing this would refuse
    every hole in the model for no reason the user could act on.
    """
    data = bracket()
    features: list[dict[str, object]] = data["features"]  # type: ignore[assignment]
    features.append(
        {"id": "bolt", "type": "hole", "at": "hole.q0", "diameter": 5.0, "depth": "slot_d"}
    )
    proposed = datum_of(service_for(tmp_path, data), "bolt/floor")
    assert proposed["parent"] == "top"
    assert proposed["origin"] == [0, 0, "-slot_d"]


def test_a_hole_drilled_through_everything_has_no_derivable_floor(
    tmp_path: Path,
) -> None:
    """How deep a through-all cut goes is decided by the solid, not the sheet.

    There is no expression to hand back, so the honest answer is to say why
    rather than to invent one from the material that happens to be there today.
    """
    data = bracket()
    features: list[dict[str, object]] = data["features"]  # type: ignore[assignment]
    features.append(
        {"id": "bolt", "type": "hole", "at": "hole.q0", "diameter": 5.0, "through_all": True}
    )
    refused = service_for(tmp_path, data).datum_for_face("bracket", "bolt/floor")
    assert not refused.ok
    assert refused.datum is None
    assert "through_all" in (refused.reason or "")


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def test_a_side_face_gets_the_plane_it_lies_in_not_its_sketch(tmp_path: Path) -> None:
    """A side stands on edge to its sketch, and is answerable anyway.

    Offering the sketch's plane here would be confidently wrong — ninety degrees
    from the face the user pointed at — which is exactly how this read as the
    tool silently choosing a different face. The plane through the curve and the
    sweep direction is the face itself, and every term of it is written down.
    """
    proposed = service_for(tmp_path, bracket()).datum_for_face(
        "bracket", "base/side[outline.left]"
    )
    assert proposed.ok, proposed.reason
    assert proposed.datum is not None
    # Not parallel to the sketch: a side's normal lies *in* the sketch plane.
    assert list(proposed.datum["normal"])[2] == 0


def test_a_role_the_feature_never_produces_is_refused_by_name(tmp_path: Path) -> None:
    """A pad has no floor. Saying which faces it does have is the useful part."""
    refused = service_for(tmp_path, bracket()).datum_for_face("bracket", "base/floor")
    assert not refused.ok
    assert "cap+" in (refused.reason or "")


def test_a_tag_naming_a_feature_the_document_does_not_have_is_rejected(
    tmp_path: Path,
) -> None:
    """An unknown feature is a bad question, not a face that is merely awkward.

    It is reported as an unknown reference — the same error every other lookup
    raises — rather than as a refusal, so a caller cannot mistake a typo for a
    face it simply has to place by hand.
    """
    service = service_for(tmp_path, bracket())
    with pytest.raises(UnknownReferenceError):
        service.datum_for_face("bracket", "ghost/cap+")


# --------------------------------------------------------------------------
# Reusing what is already there
# --------------------------------------------------------------------------


def test_a_datum_already_on_that_plane_is_offered_for_reuse(tmp_path: Path) -> None:
    """The bracket's 'top' datum is the pad's far cap, written another way.

    Compared in world space rather than by matching the text: 'top' is declared
    on the world frame at [0, 0, plate_t] and the proposal hangs off 'base', yet
    they are one plane. Missing that is how a document ends up with six datums
    on the top of one plate.
    """
    proposed = service_for(tmp_path, bracket()).datum_for_face("bracket", "base/cap+")
    assert proposed.existing == "top"


def test_no_datum_is_offered_when_none_describes_the_plane(tmp_path: Path) -> None:
    """The pocket floor is 4mm up and nothing is declared there."""
    proposed = service_for(tmp_path, bracket()).datum_for_face("bracket", "slot/floor")
    assert proposed.ok
    assert proposed.existing is None


# --------------------------------------------------------------------------
# The proposed id
# --------------------------------------------------------------------------


def test_the_proposed_id_is_an_identifier_and_is_the_same_every_time(
    tmp_path: Path,
) -> None:
    """Asking twice must not be a way of creating two datums.

    The id has to be a valid identifier because a datum's id is one, and it has
    to be stable because "propose, then PUT" is the flow this exists to support
    — a varying id would quietly pile up near-duplicate planes.
    """
    service = service_for(tmp_path, bracket())
    proposed = {datum_of(service, "base/cap+")["id"] for _ in range(5)}
    assert proposed == {"base_cap_pos"}
    assert "base_cap_pos".isidentifier()

    # The sign is spelled out rather than dropped: the two caps are opposite
    # planes and must never collapse onto one name.
    assert datum_of(service, "base/cap-")["id"] == "base_cap_neg"


def test_an_id_taken_by_a_different_plane_gains_a_suffix(tmp_path: Path) -> None:
    """Proposing an id that is already in use would silently move that datum."""
    data = bracket()
    datums: dict[str, object] = data["datums"]  # type: ignore[assignment]
    datums["base_cap_pos"] = {"type": "plane", "origin": [0, 0, 99], "normal": [0, 0, 1]}
    assert datum_of(service_for(tmp_path, data), "base/cap+")["id"] == "base_cap_pos_2"


def test_an_id_already_describing_that_plane_is_kept(tmp_path: Path) -> None:
    """Re-proposing after an edit must land on the datum it made last time.

    Suffixing here would answer a second datum on a plane that already has
    exactly the right one, which is the failure the suffix exists to avoid.
    """
    data = bracket()
    datums: dict[str, object] = data["datums"]  # type: ignore[assignment]
    datums["base_cap_pos"] = {
        "type": "plane",
        "origin": [0, 0, "plate_t"],
        "normal": [0, 0, 1],
    }
    proposed = service_for(tmp_path, data).datum_for_face("bracket", "base/cap+")
    assert proposed.datum is not None
    assert proposed.datum["id"] == "base_cap_pos"
    assert proposed.existing == "base_cap_pos"


def test_a_fragment_of_a_split_face_proposes_the_face_it_came_from(
    tmp_path: Path,
) -> None:
    """Fragments are coplanar with the face that was split, so the ordinal is noise.

    Letting it through would make '#1' and '#2' propose two ids for one plane.
    """
    service = service_for(tmp_path, bracket())
    assert datum_of(service, "base/cap+#1") == datum_of(service, "base/cap+")


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------


def test_a_datum_taken_from_the_proposal_follows_the_parameter_sheet(
    tmp_path: Path,
) -> None:
    """The failure the whole feature exists to prevent, start to finish.

    Written with the literal the plane would freeze at 6mm while the plate grew
    to 11mm. Written with the expression the datum moves with the pad, so a hole
    sketched on it stays on top of the material.
    """
    service = service_for(tmp_path, bracket())
    proposed = datum_of(service, "base/cap+")

    result = service.put_datum("bracket", _plane_from(proposed))
    assert result.frames["base_cap_pos"].origin.z == pytest.approx(PLATE_T)

    moved = service.update_parameters("bracket", {"plate_t": 11.0})
    assert moved.frames["base_cap_pos"].origin.z == pytest.approx(11.0)


def test_the_explanation_names_the_feature_the_sketch_and_the_offset(
    tmp_path: Path,
) -> None:
    """One sentence a person can check against the model tree.

    The point of the answer is that the user can agree with it before it becomes
    a datum, which needs the three things they would look up themselves.
    """
    proposed = service_for(tmp_path, bracket()).datum_for_face("bracket", "slot/floor")
    assert proposed.explanation == (
        "the floor of pocket 'slot', which cuts sketch 'hole' on datum 'top' by "
        "'-slot_d'"
    )


def _plane_from(proposed: dict[str, object]) -> DatumPlane:
    """The proposal, read as a datum exactly as the PUT endpoint would."""
    return DatumPlane.from_dict(str(proposed["id"]), {**proposed, "type": "plane"})


# -- faces that stand on edge to their sketch -------------------------------


SIDES: dict[str, object] = {
    "schema": "cadsheet/1",
    "parameters": [
        {"name": "left", "value": 0.0},
        {"name": "right", "value": 40.0},
        {"name": "front", "value": 0.0},
        {"name": "back", "value": 25.0},
        {"name": "tall", "value": 10.0},
    ],
    "datums": {},
    "sketches": {
        "plan": {
            "plane": "xy",
            "points": {
                "a": ["left", "front"],
                "b": ["right", "front"],
                "c": ["right", "back"],
                "d": ["left", "back"],
            },
            "curves": [
                {"id": "e0", "type": "line", "start": "a", "end": "b"},
                {"id": "e1", "type": "line", "start": "b", "end": "c"},
                {"id": "e2", "type": "line", "start": "c", "end": "d"},
                {"id": "e3", "type": "line", "start": "d", "end": "a"},
            ],
            "loops": [{"id": "outer", "curves": ["e0", "e1", "e2", "e3"]}],
        },
        "round": {
            "plane": "xy",
            "points": {"o": [20, 12]},
            "curves": [{"id": "rim", "type": "circle", "center": "o", "radius": 5}],
            "loops": [{"id": "outer", "curves": ["rim"]}],
        },
    },
    "features": [
        {"id": "block", "type": "pad", "profile": "plan.outer", "length": "tall"},
        {
            "id": "bore",
            "type": "pocket",
            "profile": "round.outer",
            "depth": "tall",
            "direction": "+normal",
        },
    ],
}


def sides_document() -> Document:
    return Document.from_dict(copy.deepcopy(SIDES))


def frame_of(datum: Mapping[str, object]) -> Frame:
    """Put a proposed datum into the document and read back the frame it makes.

    Asserting on the resolved frame rather than on the expression text is the
    only check that catches a plane which is symbolic, plausible and wrong.
    """
    data = copy.deepcopy(SIDES)
    entry: dict[str, object] = {
        "type": "plane",
        "origin": datum["origin"],
        "normal": datum["normal"],
    }
    if datum.get("x_axis") is not None:
        entry["x_axis"] = datum["x_axis"]
    if datum.get("parent"):
        entry["parent"] = datum["parent"]
    data["datums"] = {str(datum["id"]): entry}  # type: ignore[index]

    document = Document.from_dict(data)
    return document.datums.resolve_all(document.parameters.resolve())[str(datum["id"])]


def test_a_side_face_gives_the_plane_that_contains_it() -> None:
    """The face a user picks is the plane they get, not one perpendicular to it.

    A side is swept along the sketch's normal, so its plane holds the curve and
    that normal. Offering the sketch's own plane instead is what made this feel
    like the tool silently picking a different face.
    """
    proposal = propose_datum_for_face(sides_document(), "block/side[plan.e0]")
    assert proposal.ok, proposal.reason

    frame = frame_of(proposal.datum)  # type: ignore[arg-type]
    # e0 runs along y = front, so the face it leaves is the xz plane there.
    assert frame.z_axis.is_close(Vec3(0.0, -1.0, 0.0), tol=1e-9)
    assert abs(frame.origin.y) < 1e-9


def test_a_side_normal_points_out_of_the_material() -> None:
    """Facing into the solid would mirror every sketch drawn on the datum."""
    document = sides_document()
    for curve, outward in (
        ("e0", Vec3(0.0, -1.0, 0.0)),
        ("e1", Vec3(1.0, 0.0, 0.0)),
        ("e2", Vec3(0.0, 1.0, 0.0)),
        ("e3", Vec3(-1.0, 0.0, 0.0)),
    ):
        proposal = propose_datum_for_face(document, f"block/side[plan.{curve}]")
        assert proposal.ok, proposal.reason
        assert frame_of(proposal.datum).z_axis.is_close(outward, tol=1e-9), curve  # type: ignore[arg-type]


def test_a_side_datum_names_the_parameters_its_curve_was_drawn_from() -> None:
    """Symbolic, so the plane moves when the sheet does."""
    proposal = propose_datum_for_face(sides_document(), "block/side[plan.e1]")
    assert proposal.ok
    text = str(proposal.datum)  # type: ignore[arg-type]
    assert "right" in text
    assert "front" in text


def test_a_side_plane_follows_the_parameter_that_placed_it() -> None:
    """The whole reason for deriving it instead of writing a number down."""
    proposal = propose_datum_for_face(sides_document(), "block/side[plan.e1]")
    assert proposal.ok
    before = frame_of(proposal.datum)  # type: ignore[arg-type]

    widened = copy.deepcopy(SIDES)
    for row in widened["parameters"]:  # type: ignore[index]
        if row["name"] == "right":
            row["value"] = 65.0
    data = copy.deepcopy(widened)
    datum = dict(proposal.datum)  # type: ignore[arg-type]
    entry: dict[str, object] = {
        "type": "plane",
        "origin": datum["origin"],
        "normal": datum["normal"],
    }
    if datum.get("x_axis") is not None:
        entry["x_axis"] = datum["x_axis"]
    if datum.get("parent"):
        entry["parent"] = datum["parent"]
    data["datums"] = {str(datum["id"]): entry}
    document = Document.from_dict(data)
    after = document.datums.resolve_all(document.parameters.resolve())[str(datum["id"])]

    assert after.origin.x == pytest.approx(65.0)
    assert before.origin.x == pytest.approx(40.0)


def test_u_runs_along_the_curve() -> None:
    """So a sketch on the face reads the way the face looks."""
    proposal = propose_datum_for_face(sides_document(), "block/side[plan.e0]")
    assert proposal.ok
    frame = frame_of(proposal.datum)  # type: ignore[arg-type]
    assert abs(abs(frame.x_axis.x) - 1.0) < 1e-9


def test_each_side_gets_its_own_datum_id() -> None:
    """Four sides share a role; collapsing them would offer one plane for four."""
    document = sides_document()
    ids = {
        propose_datum_for_face(document, f"block/side[plan.{curve}]").datum["id"]  # type: ignore[index]
        for curve in ("e0", "e1", "e2", "e3")
    }
    assert len(ids) == 4


def test_a_side_swept_from_a_circle_is_refused_for_being_curved() -> None:
    """A bore wall is a cylinder; there is no plane to hand back."""
    proposal = propose_datum_for_face(sides_document(), "bore/wall[round.rim]")
    assert not proposal.ok
    assert "cylinder" in (proposal.reason or "")


def test_a_fillet_refusal_points_at_what_can_be_derived() -> None:
    """A refusal a user can act on beats one they can only be annoyed by."""
    data = copy.deepcopy(SIDES)
    data["features"].append(  # type: ignore[attr-defined]
        {
            "id": "soften",
            "type": "fillet",
            "radius": 2.0,
            "edges": "block/cap+ ^ block/side[plan.e0]",
        }
    )
    proposal = propose_datum_for_face(
        Document.from_dict(data), "soften/fillet[block/cap+ ^ block/side[plan.e0]]"
    )
    assert not proposal.ok
    reason = proposal.reason or ""
    assert "cylinder" in reason
    assert "pick one of those" in reason


# -- chamfers between two derivable faces ----------------------------------


def chamfered() -> Document:
    """The block, with a chamfer across two of its vertical sides."""
    data = copy.deepcopy(SIDES)
    data["features"].append(  # type: ignore[attr-defined]
        {
            "id": "bevel",
            "type": "chamfer",
            "distance": 2.0,
            "edges": "block/side[plan.e0] ^ block/side[plan.e1]",
        }
    )
    return Document.from_dict(data)


def test_a_chamfer_between_two_derivable_faces_is_derivable_too() -> None:
    """A chamfer names the edge it replaced, and an edge names its two faces.

    So "where is this chamfer?" becomes "where are those?", which this module
    already answers — and the answer recurses without any new machinery.
    """
    proposal = propose_datum_for_face(
        chamfered(), "bevel/chamfer[block/side[plan.e0] ^ block/side[plan.e1]]"
    )
    assert proposal.ok, proposal.reason


def test_a_chamfer_normal_bisects_the_faces_it_bevels_between() -> None:
    """Anything else is not the chamfer, however plausible the plane looks."""
    document = chamfered()
    chamfer = propose_datum_for_face(
        document, "bevel/chamfer[block/side[plan.e0] ^ block/side[plan.e1]]"
    )
    first = propose_datum_for_face(document, "block/side[plan.e0]")
    second = propose_datum_for_face(document, "block/side[plan.e1]")
    assert chamfer.ok and first.ok and second.ok

    bisector = (
        _frame_in(chamfered(), first.datum).z_axis  # type: ignore[arg-type]
        + _frame_in(chamfered(), second.datum).z_axis  # type: ignore[arg-type]
    ).normalized()
    assert _frame_in(chamfered(), chamfer.datum).z_axis.is_close(bisector, tol=1e-9)  # type: ignore[arg-type]


def test_a_chamfer_sits_one_setback_off_the_corner() -> None:
    """Two setbacks would land past the bevel entirely, on nothing."""
    document = chamfered()
    proposal = propose_datum_for_face(
        document, "bevel/chamfer[block/side[plan.e0] ^ block/side[plan.e1]]"
    )
    assert proposal.ok
    frame = _frame_in(chamfered(), proposal.datum)  # type: ignore[arg-type]

    # The corner the two curves share, and the setback stated by the feature.
    corner = Vec3(40.0, 0.0, 0.0)
    assert abs((frame.origin - corner).dot(frame.z_axis)) == pytest.approx(
        2.0 / math.sqrt(2.0), abs=1e-9
    )


def test_a_chamfer_against_a_cap_is_refused_with_the_reason() -> None:
    """It leaves the sketch's plane, and the sheet does not state its angle."""
    data = copy.deepcopy(SIDES)
    data["features"].append(  # type: ignore[attr-defined]
        {
            "id": "lip",
            "type": "chamfer",
            "distance": 1.0,
            "edges": "block/cap+ ^ block/side[plan.e0]",
        }
    )
    proposal = propose_datum_for_face(
        Document.from_dict(data), "lip/chamfer[block/cap+ ^ block/side[plan.e0]]"
    )
    assert not proposal.ok
    assert "out of the sketch's plane" in (proposal.reason or "")


def test_a_chamfer_whose_parent_is_curved_names_that_parent() -> None:
    """A refusal that says which face is the problem can be acted on."""
    data = copy.deepcopy(SIDES)
    data["features"].append(  # type: ignore[attr-defined]
        {
            "id": "lip",
            "type": "chamfer",
            "distance": 1.0,
            "edges": "bore/wall[round.rim] ^ block/side[plan.e0]",
        }
    )
    proposal = propose_datum_for_face(
        Document.from_dict(data), "lip/chamfer[bore/wall[round.rim] ^ block/side[plan.e0]]"
    )
    assert not proposal.ok
    assert "bore/wall[round.rim]" in (proposal.reason or "")


def _frame_in(document: Document, datum: Mapping[str, object]) -> Frame:
    """Resolve a proposed datum inside a document that already holds the rest."""
    entry: dict[str, object] = {
        "type": "plane",
        "origin": datum["origin"],
        "normal": datum["normal"],
    }
    if datum.get("x_axis") is not None:
        entry["x_axis"] = datum["x_axis"]
    if datum.get("parent"):
        entry["parent"] = datum["parent"]
    data = document.to_dict()
    data["datums"] = {**(data.get("datums") or {}), str(datum["id"]): entry}
    rebuilt = Document.from_dict(data)
    return rebuilt.datums.resolve_all(rebuilt.parameters.resolve())[str(datum["id"])]


# -- a clicked point, on the plane it was clicked on ------------------------


def test_a_click_comes_back_in_the_derived_plane_s_own_coordinates() -> None:
    """Reading them off the parent is the trap this exists to close.

    A cap's datum is parallel to its parent, so the two agree and nothing looks
    wrong. A side stands on edge to it, and the parent's numbers are then
    coordinates on a different plane entirely — plausible, and wrong.
    """
    document = sides_document()
    proposal = propose_datum_for_face(document, "block/side[plan.e0]", (12.0, 0.0, 4.0))
    assert proposal.ok
    assert proposal.at is not None

    frame = frame_of(proposal.datum)  # type: ignore[arg-type]
    lifted = frame.to_world(Vec3(proposal.at[0], proposal.at[1], 0.0))
    assert lifted.is_close(Vec3(12.0, 0.0, 4.0), tol=1e-6)


def test_the_origin_of_a_side_datum_is_a_corner_of_the_face() -> None:
    """So a point placed at 0,0 lands somewhere a person can see and reason from.

    Relative coordinates are only useful if the origin is somewhere obvious;
    the start of the curve the face was swept from is the natural choice.
    """
    proposal = propose_datum_for_face(sides_document(), "block/side[plan.e0]")
    assert proposal.ok
    frame = frame_of(proposal.datum)  # type: ignore[arg-type]
    assert frame.origin.is_close(Vec3(0.0, 0.0, 0.0), tol=1e-9)


def test_a_side_face_lies_in_the_positive_quadrant_of_its_datum() -> None:
    """u along the face, v up it, both counting from the corner at 0,0."""
    proposal = propose_datum_for_face(sides_document(), "block/side[plan.e0]")
    assert proposal.ok
    frame = frame_of(proposal.datum)  # type: ignore[arg-type]
    # The far top corner of that side: 40 along the curve, 10 up the pad.
    assert frame.to_local(Vec3(40.0, 0.0, 10.0)).is_close(Vec3(40.0, 10.0, 0.0), tol=1e-9)


def test_a_chamfer_reads_the_same_way_a_side_does() -> None:
    """u across the bevel, v up the part — not rotated ninety degrees.

    A face that plainly is not rotated should not sketch as though it were.
    """
    document = chamfered()
    proposal = propose_datum_for_face(
        document,
        "bevel/chamfer[block/side[plan.e0] ^ block/side[plan.e1]]",
        (39.0, 1.0, 7.0),
    )
    assert proposal.ok
    assert proposal.at is not None
    # v is the height up the 10mm block, so it tracks z rather than the plan.
    assert proposal.at[1] == pytest.approx(7.0, abs=1e-6)


def test_no_point_means_no_coordinates() -> None:
    """A face picked from a list says which face, never where on it."""
    proposal = propose_datum_for_face(sides_document(), "block/side[plan.e0]")
    assert proposal.ok
    assert proposal.at is None


# -- the face's own size, for centring on it -------------------------------


def test_a_side_face_reports_its_width_and_height() -> None:
    """So a hole can be centred on it without measuring anything by hand."""
    proposal = propose_datum_for_face(sides_document(), "block/side[plan.e0]")
    assert proposal.ok
    assert proposal.size is not None
    # e0 runs the 40mm width of the plan; the pad is 10 tall.
    assert proposal.size["uValue"] == pytest.approx(40.0)
    assert proposal.size["vValue"] == pytest.approx(10.0)


def test_the_reported_size_is_an_expression_not_a_measurement() -> None:
    """Centring with `w / 2` has to stay centred when the part changes."""
    proposal = propose_datum_for_face(sides_document(), "block/side[plan.e0]")
    assert proposal.ok
    assert proposal.size is not None
    assert not isinstance(proposal.size["u"], float)
    assert "right" in str(proposal.size["u"]) or "left" in str(proposal.size["u"])
    assert proposal.size["v"] == "tall"


def test_a_chamfer_reports_the_width_of_its_bevel() -> None:
    """Two millimetres off each of two square faces leaves 2*sqrt(2) across."""
    proposal = propose_datum_for_face(
        chamfered(), "bevel/chamfer[block/side[plan.e0] ^ block/side[plan.e1]]"
    )
    assert proposal.ok
    assert proposal.size is not None
    assert proposal.size["uValue"] == pytest.approx(2.0 * math.sqrt(2.0), abs=1e-4)
    assert proposal.size["vValue"] == pytest.approx(10.0)


def test_a_cap_reports_no_size() -> None:
    """Its extent is the whole profile, which is not one number."""
    proposal = propose_datum_for_face(sides_document(), "block/cap+")
    assert proposal.ok
    assert proposal.size is None


# -- reuse means the same frame, not merely the same plane -----------------


def test_a_datum_turned_within_the_plane_is_not_reused() -> None:
    """Two datums on one plane but rotated are different sketching frames.

    Reusing one while reporting coordinates for the other puts a point at
    numbers that are correct in a frame nobody is using — which is exactly how
    a point placed on a chamfer ended up off the face.
    """
    proposal = propose_datum_for_face(sides_document(), "block/side[plan.e0]")
    assert proposal.ok
    datum = dict(proposal.datum)  # type: ignore[arg-type]

    turned = copy.deepcopy(SIDES)
    turned["datums"] = {  # type: ignore[index]
        "turned": {
            "type": "plane",
            "origin": datum["origin"],
            "normal": datum["normal"],
            # Same plane, u running up it instead of along it.
            "x_axis": [0, 0, 1],
            "parent": datum["parent"],
        }
    }
    again = propose_datum_for_face(Document.from_dict(turned), "block/side[plan.e0]")
    assert again.ok
    # It may still reuse some *other* datum on that plane — 'xz' genuinely is
    # this face's plane, correctly oriented. What it must not do is reuse the
    # turned one.
    assert again.existing != "turned"


def test_a_datum_with_the_same_frame_is_still_reused() -> None:
    """Tightening the rule must not stop it doing its job."""
    proposal = propose_datum_for_face(sides_document(), "block/side[plan.e0]")
    assert proposal.ok
    datum = dict(proposal.datum)  # type: ignore[arg-type]

    already = copy.deepcopy(SIDES)
    already["datums"] = {  # type: ignore[index]
        "mine": {
            "type": "plane",
            "origin": datum["origin"],
            "normal": datum["normal"],
            "x_axis": datum["x_axis"],
            "parent": datum["parent"],
        }
    }
    again = propose_datum_for_face(Document.from_dict(already), "block/side[plan.e0]")
    assert again.ok
    assert again.existing == "mine"


def test_a_reused_datum_reports_coordinates_in_its_own_frame() -> None:
    """Whichever datum is actually sketched on is the one u,v describe."""
    proposal = propose_datum_for_face(sides_document(), "block/side[plan.e0]")
    assert proposal.ok
    datum = dict(proposal.datum)  # type: ignore[arg-type]

    already = copy.deepcopy(SIDES)
    already["datums"] = {  # type: ignore[index]
        "mine": {
            "type": "plane",
            "origin": datum["origin"],
            "normal": datum["normal"],
            "x_axis": datum["x_axis"],
            "parent": datum["parent"],
        }
    }
    document = Document.from_dict(already)
    again = propose_datum_for_face(document, "block/side[plan.e0]", (12.0, 0.0, 4.0))
    assert again.existing == "mine"
    assert again.at is not None

    frame = document.datums.resolve_all(document.parameters.resolve())["mine"]
    assert frame.to_world(Vec3(again.at[0], again.at[1], 0.0)).is_close(
        Vec3(12.0, 0.0, 4.0), tol=1e-6
    )
