"""The expression language: correct arithmetic, degrees by default, no eval."""

from __future__ import annotations

import math

import pytest

from facet.domain.errors import ExpressionError
from facet.domain.expressions import dependencies, evaluate_text, parse


def ev(text: str, **variables: float) -> float:
    return evaluate_text(text, variables)


# --------------------------------------------------------------------------
# Arithmetic and precedence
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1", 1.0),
        ("1 + 2 * 3", 7.0),
        ("(1 + 2) * 3", 9.0),
        ("10 / 4", 2.5),
        ("10 // 4", 2.0),
        ("10 % 4", 2.0),
        ("2 ** 3 ** 2", 512.0),  # right associative
        ("-2 ** 2", -4.0),  # unary binds looser than **
        ("-(2 + 3)", -5.0),
        ("+7", 7.0),
        ("1e3", 1000.0),
        ("1.5e-2", 0.015),
        (".5", 0.5),
        ("2 < 3", 1.0),
        ("3 <= 3", 1.0),
        ("2 != 2", 0.0),
    ],
)
def test_arithmetic(text: str, expected: float) -> None:
    assert ev(text) == pytest.approx(expected)


def test_references_resolve_from_the_variable_table() -> None:
    assert ev("plate_w * 0.6", plate_w=120.0) == pytest.approx(72.0)


def test_nested_references() -> None:
    assert ev("a + b * c", a=1, b=2, c=3) == pytest.approx(7.0)


# --------------------------------------------------------------------------
# Functions — trigonometry is in degrees, matching the document convention
# --------------------------------------------------------------------------


def test_trigonometry_uses_degrees() -> None:
    assert ev("sin(30)") == pytest.approx(0.5)
    assert ev("cos(60)") == pytest.approx(0.5)
    assert ev("atan2(1, 1)") == pytest.approx(45.0)


def test_radian_variants_are_available() -> None:
    assert ev("sin_r(pi / 2)") == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("abs(-3)", 3.0),
        ("sqrt(16)", 4.0),
        ("min(3, 1, 2)", 1.0),
        ("max(3, 1, 2)", 3.0),
        ("floor(2.9)", 2.0),
        ("ceil(2.1)", 3.0),
        ("round(2.567, 2)", 2.57),
        ("hypot(3, 4)", 5.0),
        ("clamp(15, 0, 10)", 10.0),
        ("sign(-9)", -1.0),
        ("if(1 > 0, 10, 20)", 10.0),
        ("if(1 < 0, 10, 20)", 20.0),
        ("pi", math.pi),
    ],
)
def test_function_table(text: str, expected: float) -> None:
    assert ev(text) == pytest.approx(expected)


def test_functions_are_case_insensitive() -> None:
    assert ev("SQRT(9)") == ev("sqrt(9)")


# --------------------------------------------------------------------------
# Dependency extraction drives the recompute DAG
# --------------------------------------------------------------------------


def test_dependencies_are_extracted() -> None:
    assert dependencies(parse("a + b * max(c, 2)")) == frozenset({"a", "b", "c"})


def test_constants_are_not_dependencies() -> None:
    assert dependencies(parse("pi * r ** 2")) == frozenset({"r"})


def test_function_names_are_not_dependencies() -> None:
    assert dependencies(parse("sqrt(x)")) == frozenset({"x"})


# --------------------------------------------------------------------------
# Safety: the grammar is closed, there is no route to a Python object
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "__import__('os')",
        "().__class__",
        "open('/etc/passwd')",
        "a.b",
        "a[0]",
        "lambda: 1",
        "exec('x=1')",
        "1; 2",
        "x = 1",
    ],
)
def test_dangerous_input_is_rejected_at_parse_time(text: str) -> None:
    with pytest.raises(ExpressionError):
        parse(text)


@pytest.mark.parametrize(
    "text",
    ["", "   ", "1 +", "(1", "1)", "max()", "sqrt(1, 2)", "nosuchfn(1)", "1 ** ", "* 3"],
)
def test_malformed_expressions_are_rejected(text: str) -> None:
    with pytest.raises(ExpressionError):
        parse(text)


def test_unknown_name_fails_at_evaluation_with_a_useful_message() -> None:
    with pytest.raises(ExpressionError) as excinfo:
        evaluate_text("width * 2", {}, parameter="height")
    assert "width" in str(excinfo.value)


def test_division_by_zero_is_a_domain_error_not_a_crash() -> None:
    with pytest.raises(ExpressionError) as excinfo:
        evaluate_text("1 / 0", {}, parameter="ratio")
    assert "division by zero" in str(excinfo.value)
    assert excinfo.value.parameter == "ratio"


def test_domain_error_from_maths_is_wrapped() -> None:
    with pytest.raises(ExpressionError):
        evaluate_text("sqrt(-1)", {})
