"""Turning a click into numbers a document can hold, and naming those numbers.

Locating a point already gave the caller an offset from every datum. The next
thing they do with it is declare a datum there, and a literal offset makes that
datum a dead number: change the plate thickness and the datum stays put while
the material moves, so the hole drilled on it comes out in the wrong place.

Naming the parameter that already resolves to the offset is what closes that
gap. The datum is then still computed from parameters, which is the rule the
whole system rests on — see :mod:`facet.domain.datum`.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from facet.adapters.geometry.fake import FakeKernel
from facet.adapters.persistence.filesystem import FilesystemDocumentRepository
from facet.application.services import ProjectService
from facet.domain.datum import DatumPlane
from facet.domain.document import Document

from .test_recompute import BRACKET

#: The bracket is 6mm thick and its ``top`` datum sits at ``plate_t``, so a
#: click on the top face is the exact case the feature exists for.
PLATE_T = 6.0

#: The centre of the top face, in world coordinates.
TOP_FACE = (60.0, 36.0, PLATE_T)


def service_with(tmp_path: Path, *extra: dict[str, object]) -> ProjectService:
    """The bracket, optionally carrying extra parameters, ready to locate on."""
    data = copy.deepcopy(BRACKET)
    data["parameters"] = [*data["parameters"], *extra]  # type: ignore[misc]

    repository = FilesystemDocumentRepository(tmp_path)
    service = ProjectService(repository, FakeKernel())
    repository.create("bracket", Document.from_dict(data))
    return service


def rows(service: ProjectService, point: tuple[float, float, float]) -> dict[str, dict]:
    return {row["datum"]: row for row in service.locate("bracket", point)}


# --------------------------------------------------------------------------
# Naming the offset
# --------------------------------------------------------------------------


def test_an_offset_that_matches_a_parameter_reports_its_name(tmp_path: Path) -> None:
    """The whole point: a datum written against ``plate_t`` follows the model."""
    found = rows(service_with(tmp_path), TOP_FACE)
    assert found["base"]["offset"] == pytest.approx(PLATE_T)
    assert found["base"]["offsetParameter"] == "plate_t"
    assert found["xy"]["offsetParameter"] == "plate_t"


def test_an_offset_that_matches_nothing_reports_no_parameter(tmp_path: Path) -> None:
    """Silence is the honest answer; a near miss would be worse than none."""
    found = rows(service_with(tmp_path), (60.0, 36.0, 3.3))
    assert found["base"]["offset"] == pytest.approx(3.3)
    assert found["base"]["offsetParameter"] is None


def test_a_zero_offset_reports_no_parameter(tmp_path: Path) -> None:
    """A point already on the plane needs no parameter at all.

    A sheet can easily hold a parameter that happens to resolve to zero, and
    naming it here would put a meaningless dependency into every datum created
    from a click on a plane the user is already standing on.
    """
    found = rows(service_with(tmp_path, {"name": "shim", "value": 0.0}), TOP_FACE)
    assert found["top"]["offset"] == pytest.approx(0.0)
    assert found["top"]["offsetParameter"] is None


def test_several_matching_parameters_always_resolve_to_the_same_one(
    tmp_path: Path,
) -> None:
    """Two parameters of equal value must not make the answer flap.

    A caller comparing two locates, or a test asserting on one, needs the same
    click to produce the same document every time.
    """
    service = service_with(
        tmp_path,
        {"name": "zz_thickness", "value": PLATE_T},
        {"name": "aa_thickness", "value": PLATE_T},
    )
    answers = {rows(service, TOP_FACE)["base"]["offsetParameter"] for _ in range(5)}
    assert answers == {"aa_thickness"}


def test_a_negative_offset_names_the_parameter_it_negates(tmp_path: Path) -> None:
    """Looking down at the base from the ``top`` datum is the same thickness.

    The name alone is reported; the sign is already in ``offset``, so the
    caller negates rather than being handed an expression it did not ask for.
    """
    found = rows(service_with(tmp_path), (60.0, 36.0, 0.0))
    assert found["top"]["offset"] == pytest.approx(-PLATE_T)
    assert found["top"]["offsetParameter"] == "plate_t"


def test_an_exact_match_beats_a_negated_one(tmp_path: Path) -> None:
    """A parameter that is genuinely negative should be named as it stands.

    Otherwise a document holding both a drop and a thickness would have its
    signs quietly swapped, and the caller would negate a name that was already
    pointing the right way.
    """
    service = service_with(tmp_path, {"name": "aa_drop", "value": -PLATE_T})
    assert rows(service, (60.0, 36.0, 0.0))["top"]["offsetParameter"] == "aa_drop"
    assert rows(service, TOP_FACE)["base"]["offsetParameter"] == "plate_t"


# --------------------------------------------------------------------------
# What locate already promised
# --------------------------------------------------------------------------


def test_the_located_coordinates_are_unchanged(tmp_path: Path) -> None:
    """The new field is an addition; nothing that existed may have moved."""
    found = service_with(tmp_path).locate("bracket", TOP_FACE)
    by_datum = {row["datum"]: row for row in found}

    assert by_datum["base"]["u"] == pytest.approx(60.0)
    assert by_datum["base"]["v"] == pytest.approx(36.0)
    assert by_datum["base"]["offset"] == pytest.approx(PLATE_T)

    # Nearest plane first, so the obvious choice is still the default one.
    offsets = [abs(float(row["offset"])) for row in found]
    assert offsets == sorted(offsets)

    # Still no topology in the answer: a click is numbers, not a reference.
    expected = {"datum", "u", "v", "offset", "offsetParameter"}
    assert all(set(row) == expected for row in found)


def test_a_datum_written_from_the_named_offset_tracks_the_model(
    tmp_path: Path,
) -> None:
    """The failure this exists to prevent, end to end.

    Declaring the datum with the literal offset would leave it at 6mm; written
    against the name it moves with the sheet, which is the only reason a click
    is allowed anywhere near datum creation.
    """
    service = service_with(tmp_path)
    named = rows(service, TOP_FACE)["base"]["offsetParameter"]

    result = service.put_datum(
        "bracket", DatumPlane(id="cap", origin=(0, 0, named), normal=(0, 0, 1))
    )
    assert result.frames["cap"].origin.z == pytest.approx(PLATE_T)

    moved = service.update_parameters("bracket", {"plate_t": 11.0})
    assert moved.frames["cap"].origin.z == pytest.approx(11.0)
