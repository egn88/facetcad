"""A safe arithmetic expression language for the parameter sheet.

Explicitly *not* built on :func:`eval`. Expressions arrive from documents that
may be shared between stations and driven by agents over the API, so the
evaluator is a hand-written parser over a closed grammar with a whitelisted
function table. There is no attribute access, no indexing, and no way to reach a
Python object.

Angles are degrees at this boundary, matching the document convention: ``sin(30)``
is 0.5. Radian variants are available as ``sin_r`` and friends for the rare case
where a formula is more natural in radians.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .errors import ExpressionError

# --------------------------------------------------------------------------
# AST
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Number:
    value: float


@dataclass(frozen=True, slots=True)
class Reference:
    name: str


@dataclass(frozen=True, slots=True)
class UnaryOp:
    op: str
    operand: Expression


@dataclass(frozen=True, slots=True)
class BinaryOp:
    op: str
    left: Expression
    right: Expression


@dataclass(frozen=True, slots=True)
class Call:
    name: str
    args: tuple[Expression, ...]


Expression = Number | Reference | UnaryOp | BinaryOp | Call


# --------------------------------------------------------------------------
# Function and constant tables
# --------------------------------------------------------------------------


def _degrees_wrapper(fn: Callable[[float], float]) -> Callable[[float], float]:
    return lambda x: fn(math.radians(x))


def _inverse_degrees(fn: Callable[[float], float]) -> Callable[[float], float]:
    return lambda x: math.degrees(fn(x))


FUNCTIONS: dict[str, tuple[Callable[..., float], int, int]] = {
    # name: (implementation, min_args, max_args)
    "abs": (abs, 1, 1),
    "sqrt": (math.sqrt, 1, 1),
    "floor": (lambda x: float(math.floor(x)), 1, 1),
    "ceil": (lambda x: float(math.ceil(x)), 1, 1),
    "round": (lambda x, n=0: round(x, int(n)), 1, 2),
    "min": (min, 1, 32),
    "max": (max, 1, 32),
    "sin": (_degrees_wrapper(math.sin), 1, 1),
    "cos": (_degrees_wrapper(math.cos), 1, 1),
    "tan": (_degrees_wrapper(math.tan), 1, 1),
    "asin": (_inverse_degrees(math.asin), 1, 1),
    "acos": (_inverse_degrees(math.acos), 1, 1),
    "atan": (_inverse_degrees(math.atan), 1, 1),
    "atan2": (lambda y, x: math.degrees(math.atan2(y, x)), 2, 2),
    "sin_r": (math.sin, 1, 1),
    "cos_r": (math.cos, 1, 1),
    "tan_r": (math.tan, 1, 1),
    "hypot": (math.hypot, 2, 8),
    "log": (math.log, 1, 2),
    "exp": (math.exp, 1, 1),
    "sign": (lambda x: float((x > 0) - (x < 0)), 1, 1),
    "clamp": (lambda x, lo, hi: max(lo, min(hi, x)), 3, 3),
    "if": (lambda c, a, b: a if c else b, 3, 3),
}

CONSTANTS: dict[str, float] = {
    "pi": math.pi,
    "e": math.e,
    "true": 1.0,
    "false": 0.0,
}

_BINARY_PRECEDENCE: dict[str, int] = {
    "<": 1, ">": 1, "<=": 1, ">=": 1, "==": 1, "!=": 1,
    "+": 2, "-": 2,
    "*": 3, "/": 3, "//": 3, "%": 3,
    "**": 5,
}
_RIGHT_ASSOCIATIVE = {"**"}
_UNARY_PRECEDENCE = 4


# --------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"\s*(?:"
    r"(?P<number>\d+\.\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?|\d+(?:[eE][+-]?\d+)?)"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<op><=|>=|==|!=|\*\*|//|[-+*/%<>(),])"
    r")"
)


def _tokenize(text: str, source: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    index = 0
    while index < len(text):
        match = _TOKEN_RE.match(text, index)
        if match is None:
            if not text[index:].strip():
                break
            raise ExpressionError(
                expression=source, reason=f"unexpected character {text[index]!r}"
            )
        kind = match.lastgroup
        assert kind is not None
        tokens.append((kind, match.group(kind)))
        index = match.end()
    return tokens


# --------------------------------------------------------------------------
# Parser (precedence climbing)
# --------------------------------------------------------------------------


class _Parser:
    def __init__(self, text: str) -> None:
        self.source = text
        self.tokens = _tokenize(text, text)
        self.pos = 0

    def _peek(self) -> tuple[str, str] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _fail(self, reason: str) -> ExpressionError:
        return ExpressionError(expression=self.source, reason=reason)

    def parse(self) -> Expression:
        if not self.tokens:
            raise self._fail("expression is empty")
        node = self._parse_binary(0)
        if self.pos != len(self.tokens):
            raise self._fail(f"unexpected trailing token {self.tokens[self.pos][1]!r}")
        return node

    def _parse_binary(self, min_precedence: int) -> Expression:
        left = self._parse_unary()
        while True:
            token = self._peek()
            if token is None or token[0] != "op":
                break
            op = token[1]
            precedence = _BINARY_PRECEDENCE.get(op)
            if precedence is None or precedence < min_precedence:
                break
            self.pos += 1
            next_min = precedence if op in _RIGHT_ASSOCIATIVE else precedence + 1
            right = self._parse_binary(next_min)
            left = BinaryOp(op=op, left=left, right=right)
        return left

    def _parse_unary(self) -> Expression:
        token = self._peek()
        if token is not None and token[0] == "op" and token[1] in {"-", "+"}:
            self.pos += 1
            operand = self._parse_binary(_UNARY_PRECEDENCE)
            return UnaryOp(op=token[1], operand=operand) if token[1] == "-" else operand
        return self._parse_primary()

    def _parse_primary(self) -> Expression:
        token = self._peek()
        if token is None:
            raise self._fail("unexpected end of expression")
        kind, text = token
        self.pos += 1

        if kind == "number":
            return Number(value=float(text))

        if kind == "name":
            if self._peek() == ("op", "("):
                return self._parse_call(text)
            lowered = text.lower()
            if lowered in CONSTANTS:
                return Number(value=CONSTANTS[lowered])
            return Reference(name=text)

        if text == "(":
            inner = self._parse_binary(0)
            self._expect(")")
            return inner

        raise self._fail(f"unexpected token {text!r}")

    def _parse_call(self, name: str) -> Expression:
        self._expect("(")
        args: list[Expression] = []
        if self._peek() != ("op", ")"):
            args.append(self._parse_binary(0))
            while self._peek() == ("op", ","):
                self.pos += 1
                args.append(self._parse_binary(0))
        self._expect(")")

        signature = FUNCTIONS.get(name.lower())
        if signature is None:
            raise self._fail(f"unknown function {name!r}")
        _, minimum, maximum = signature
        if not minimum <= len(args) <= maximum:
            expected = f"{minimum}" if minimum == maximum else f"{minimum}..{maximum}"
            raise self._fail(
                f"function {name!r} takes {expected} argument(s), got {len(args)}"
            )
        return Call(name=name.lower(), args=tuple(args))

    def _expect(self, char: str) -> None:
        token = self._peek()
        if token is None or token[1] != char:
            found = token[1] if token else "end of expression"
            raise self._fail(f"expected {char!r}, found {found!r}")
        self.pos += 1


def parse(text: str) -> Expression:
    """Parse an expression, raising :class:`ExpressionError` on bad syntax."""
    return _Parser(text).parse()


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------


def dependencies(node: Expression) -> frozenset[str]:
    """Every parameter name the expression reads. Drives the recompute DAG."""
    match node:
        case Reference(name=name):
            return frozenset({name})
        case UnaryOp(operand=operand):
            return dependencies(operand)
        case BinaryOp(left=left, right=right):
            return dependencies(left) | dependencies(right)
        case Call(args=args):
            return frozenset().union(*(dependencies(a) for a in args)) if args else frozenset()
        case _:
            return frozenset()


def evaluate(
    node: Expression,
    variables: Mapping[str, float],
    *,
    source: str = "",
    parameter: str | None = None,
) -> float:
    """Evaluate an expression against a variable table."""
    try:
        return _evaluate(node, variables)
    except ExpressionError:
        raise
    except ZeroDivisionError as exc:
        raise ExpressionError(
            expression=source or _render(node), reason="division by zero", parameter=parameter
        ) from exc
    except (ValueError, OverflowError) as exc:
        raise ExpressionError(
            expression=source or _render(node), reason=str(exc), parameter=parameter
        ) from exc


def _evaluate(node: Expression, variables: Mapping[str, float]) -> float:
    match node:
        case Number(value=value):
            return value
        case Reference(name=name):
            if name not in variables:
                raise ExpressionError(expression=name, reason=f"unknown name {name!r}")
            return float(variables[name])
        case UnaryOp(op=op, operand=operand):
            value = _evaluate(operand, variables)
            return -value if op == "-" else value
        case BinaryOp(op=op, left=left, right=right):
            return _apply_binary(op, _evaluate(left, variables), _evaluate(right, variables))
        case Call(name=name, args=args):
            fn, _, _ = FUNCTIONS[name]
            return float(fn(*(_evaluate(a, variables) for a in args)))
    raise ExpressionError(expression=repr(node), reason="unsupported expression node")


def _apply_binary(op: str, left: float, right: float) -> float:
    match op:
        case "+":
            return left + right
        case "-":
            return left - right
        case "*":
            return left * right
        case "/":
            return left / right
        case "//":
            return float(left // right)
        case "%":
            return left % right
        case "**":
            return float(left**right)
        case "<":
            return float(left < right)
        case ">":
            return float(left > right)
        case "<=":
            return float(left <= right)
        case ">=":
            return float(left >= right)
        case "==":
            return float(left == right)
        case "!=":
            return float(left != right)
    raise ExpressionError(expression=op, reason=f"unsupported operator {op!r}")


def _render(node: Expression) -> str:
    """Reconstruct source text, used in error messages."""
    match node:
        case Number(value=value):
            return repr(value)
        case Reference(name=name):
            return name
        case UnaryOp(op=op, operand=operand):
            return f"{op}{_render(operand)}"
        case BinaryOp(op=op, left=left, right=right):
            return f"({_render(left)} {op} {_render(right)})"
        case Call(name=name, args=args):
            return f"{name}({', '.join(_render(a) for a in args)})"
    return "<expr>"


def rename_reference(text: str, old: str, new: str) -> str:
    """Rewrite every reference to ``old`` as ``new``, preserving formatting.

    Renaming a parameter has to follow through into every expression that reads
    it, or the rename silently breaks the document. Doing that with a regular
    expression would be wrong in two ways: it would rewrite a function call that
    happens to share the name (a parameter called ``min`` is legal), and it
    would not know that ``pi`` is a constant rather than a reference.

    So the text is retokenised and only genuine reference tokens are spliced,
    which leaves the author's spacing and parentheses exactly as written.
    """
    if old == new:
        return text

    replacements: list[tuple[int, int]] = []
    for token in _tokenize_spans(text):
        if token.kind != "name" or token.text != old:
            continue
        if token.text.lower() in CONSTANTS:
            continue  # a constant, not a reference
        if token.followed_by_call:
            continue  # a function call that happens to share the name
        replacements.append((token.start, token.end))

    for start, end in reversed(replacements):
        text = text[:start] + new + text[end:]
    return text


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str
    text: str
    start: int
    end: int
    followed_by_call: bool


def _tokenize_spans(text: str) -> list[_Token]:
    """Tokenise, keeping source positions so text can be spliced precisely."""
    raw: list[tuple[str, str, int, int]] = []
    index = 0
    while index < len(text):
        match = _TOKEN_RE.match(text, index)
        if match is None:
            break
        kind = match.lastgroup
        if kind is None:
            break
        raw.append((kind, match.group(kind), match.start(kind), match.end(kind)))
        index = match.end()

    tokens: list[_Token] = []
    for position, (kind, value, start, end) in enumerate(raw):
        following = raw[position + 1] if position + 1 < len(raw) else None
        tokens.append(
            _Token(
                kind=kind,
                text=value,
                start=start,
                end=end,
                followed_by_call=following is not None
                and following[0] == "op"
                and following[1] == "(",
            )
        )
    return tokens


def evaluate_text(
    text: str, variables: Mapping[str, float], *, parameter: str | None = None
) -> float:
    """Parse and evaluate in one step."""
    try:
        node = parse(text)
    except ExpressionError as exc:
        raise ExpressionError(
            expression=exc.expression, reason=exc.reason, parameter=parameter
        ) from exc
    return evaluate(node, variables, source=text, parameter=parameter)
