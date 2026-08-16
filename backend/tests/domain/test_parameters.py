"""The parameter sheet: topological resolution, unit conversion, cycle reporting."""

from __future__ import annotations

import pytest

from facet.domain.errors import (
    CircularDependencyError,
    DocumentError,
    DuplicateIdError,
    UnknownParameterError,
)
from facet.domain.parameters import Parameter, ParameterSet


def sheet(*parameters: Parameter) -> ParameterSet:
    return ParameterSet(parameters)


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def test_literals_resolve_to_their_value() -> None:
    resolved = sheet(Parameter("plate_w", value=120.0)).resolve()
    assert resolved["plate_w"] == 120.0


def test_expressions_resolve_against_earlier_parameters() -> None:
    resolved = sheet(
        Parameter("plate_w", value=120.0),
        Parameter("plate_h", expr="plate_w * 0.6"),
    ).resolve()
    assert resolved["plate_h"] == pytest.approx(72.0)


def test_declaration_order_does_not_matter() -> None:
    """A dependency may be declared after its consumer; the sort handles it."""
    resolved = sheet(
        Parameter("area", expr="w * h"),
        Parameter("w", value=10.0),
        Parameter("h", expr="w / 2"),
    ).resolve()
    assert resolved["area"] == pytest.approx(50.0)


def test_deep_chains_resolve() -> None:
    parameters = [Parameter("p0", value=1.0)]
    parameters += [Parameter(f"p{i}", expr=f"p{i - 1} + 1") for i in range(1, 25)]
    assert sheet(*parameters).resolve()["p24"] == pytest.approx(25.0)


# --------------------------------------------------------------------------
# Units convert to canonical mm/deg on the way in
# --------------------------------------------------------------------------


def test_imperial_input_converts_to_millimetres() -> None:
    resolved = sheet(Parameter("bore", value=0.5, unit="in")).resolve()
    assert resolved["bore"] == pytest.approx(12.7)


def test_expressions_see_canonical_values() -> None:
    resolved = sheet(
        Parameter("bore", value=1.0, unit="in"),
        Parameter("clearance", expr="bore + 0.2"),
    ).resolve()
    assert resolved["clearance"] == pytest.approx(25.6)


def test_angles_convert_to_degrees() -> None:
    resolved = sheet(Parameter("sweep", value=1.0, unit="turn")).resolve()
    assert resolved["sweep"] == pytest.approx(360.0)


def test_unknown_unit_is_rejected() -> None:
    with pytest.raises(DocumentError):
        Parameter("x", value=1.0, unit="furlong")


# --------------------------------------------------------------------------
# Cycles are reported with the actual path
# --------------------------------------------------------------------------


def test_direct_cycle_is_detected() -> None:
    with pytest.raises(CircularDependencyError):
        sheet(Parameter("a", expr="a + 1")).resolve()


def test_indirect_cycle_reports_the_path() -> None:
    with pytest.raises(CircularDependencyError) as excinfo:
        sheet(
            Parameter("a", expr="b + 1"),
            Parameter("b", expr="c + 1"),
            Parameter("c", expr="a + 1"),
        ).resolve()
    cycle = excinfo.value.cycle
    assert set(cycle) == {"a", "b", "c"}
    assert "->" in str(excinfo.value)


def test_reference_to_a_missing_parameter_is_reported_with_its_referrer() -> None:
    with pytest.raises(UnknownParameterError) as excinfo:
        sheet(Parameter("a", expr="ghost * 2")).resolve()
    assert excinfo.value.name == "ghost"
    assert excinfo.value.referenced_by == "a"


# --------------------------------------------------------------------------
# Dependency queries drive incremental recompute
# --------------------------------------------------------------------------


def test_dependents_are_transitive() -> None:
    parameters = sheet(
        Parameter("w", value=10.0),
        Parameter("h", expr="w * 2"),
        Parameter("area", expr="w * h"),
        Parameter("unrelated", value=3.0),
    )
    assert parameters.dependents_of("w") == frozenset({"h", "area"})
    assert parameters.dependents_of("h") == frozenset({"area"})
    assert parameters.dependents_of("unrelated") == frozenset()


def test_dependencies_of_a_literal_are_empty() -> None:
    assert sheet(Parameter("w", value=1.0)).dependencies_of("w") == frozenset()


# --------------------------------------------------------------------------
# Sheet mechanics
# --------------------------------------------------------------------------


def test_duplicate_names_are_rejected() -> None:
    with pytest.raises(DuplicateIdError):
        sheet(Parameter("w", value=1.0), Parameter("w", value=2.0))


def test_a_parameter_needs_exactly_one_of_value_or_expr() -> None:
    with pytest.raises(DocumentError):
        Parameter("w")
    with pytest.raises(DocumentError):
        Parameter("w", value=1.0, expr="2")


def test_parameter_names_must_be_identifiers() -> None:
    with pytest.raises(DocumentError):
        Parameter("plate width", value=1.0)


def test_replace_updates_in_place_and_keeps_order() -> None:
    parameters = sheet(Parameter("w", value=10.0), Parameter("h", expr="w * 2"))
    parameters.replace("w", value=25.0)
    assert parameters.resolve()["h"] == pytest.approx(50.0)
    assert parameters.names == ("w", "h")


def test_grouping_for_the_ui() -> None:
    parameters = sheet(
        Parameter("w", value=1.0, group="Plate"),
        Parameter("h", value=2.0, group="Plate"),
        Parameter("d", value=3.0, group="Slot"),
    )
    groups = parameters.groups()
    assert [p.name for p in groups["Plate"]] == ["w", "h"]
    assert [p.name for p in groups["Slot"]] == ["d"]


def test_round_trips_through_the_document_form() -> None:
    original = sheet(
        Parameter("w", value=0.5, unit="in", group="Plate", doc="overall width"),
        Parameter("h", expr="w * 0.6"),
    )
    restored = ParameterSet.from_list(original.to_list())
    assert restored.resolve().as_dict() == original.resolve().as_dict()
    assert restored["w"].unit == "in"
    assert restored["w"].doc == "overall width"


def test_fingerprint_is_stable_and_order_independent() -> None:
    a = sheet(Parameter("w", value=1.0), Parameter("h", value=2.0)).resolve()
    b = sheet(Parameter("h", value=2.0), Parameter("w", value=1.0)).resolve()
    assert a.fingerprint() == b.fingerprint()


def test_fingerprint_changes_when_a_value_changes() -> None:
    a = sheet(Parameter("w", value=1.0)).resolve()
    b = sheet(Parameter("w", value=1.5)).resolve()
    assert a.fingerprint() != b.fingerprint()
