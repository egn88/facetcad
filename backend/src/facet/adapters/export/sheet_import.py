"""Reading the parameter sheet back from CSV.

The counterpart to :func:`facet.adapters.export.mesh.parameters_csv`, and the
reason the sheet UI does not need to become a spreadsheet: export the table,
edit it in Excel or LibreOffice Calc where that work is genuinely pleasant, and
import it again.

Only the parameter table is affected. Datums, sketches and the feature history
are untouched, so a round trip cannot damage the parts of the document a
spreadsheet has no way to represent.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping

from facet.domain.document import Document
from facet.domain.errors import DocumentError
from facet.domain.parameters import Parameter, ParameterSet

REQUIRED_COLUMN = "name"
#: Columns produced by the exporter. Anything else is ignored on import.
KNOWN_COLUMNS = {"name", "group", "value", "expr", "unit", "resolved_mm_deg", "doc"}


def parameters_from_csv(document: Document, text: str) -> Document:
    """Return ``document`` with its parameter table replaced from ``text``.

    Rejects the whole file rather than importing it partially: a sheet that is
    half-applied is far harder to reason about than one that was refused with a
    row number.
    """
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise DocumentError(reason="the CSV is empty", path="parameters")

    headers = {(name or "").strip().lower() for name in reader.fieldnames}
    if REQUIRED_COLUMN not in headers:
        raise DocumentError(
            reason=(
                f"the CSV needs a '{REQUIRED_COLUMN}' column; found "
                f"{', '.join(sorted(h for h in headers if h)) or 'nothing'}"
            ),
            path="parameters",
        )

    parameters = ParameterSet()
    seen: set[str] = set()

    for line, raw in enumerate(reader, start=2):  # row 1 is the header
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        name = row.get("name", "")
        if not name:
            continue  # tolerate trailing blank lines a spreadsheet often adds
        if name in seen:
            raise DocumentError(
                reason=f"parameter '{name}' appears more than once", path=f"csv:row {line}"
            )
        seen.add(name)
        parameters.add(_row_to_parameter(row, name, line))

    if len(parameters) == 0:
        raise DocumentError(reason="the CSV contained no parameter rows", path="parameters")

    rebuilt = Document(
        name=document.name,
        schema=document.schema,
        parameters=parameters,
        datums=document.datums,
        sketches=document.sketches,
        # Every body, not just the first: a spreadsheet round trip must not
        # quietly discard the rest of the model.
        bodies=document.bodies,
    )
    # Fail here rather than at build time, so a bad import never lands on disk.
    rebuilt.validate()
    return rebuilt


def _row_to_parameter(row: Mapping[str, str], name: str, line: int) -> Parameter:
    expression = row.get("expr", "")
    literal = row.get("value", "")

    if expression and literal:
        raise DocumentError(
            reason=(
                f"parameter '{name}' has both a value and an expression; leave one of the "
                "two cells empty"
            ),
            path=f"csv:row {line}",
        )
    if not expression and not literal:
        raise DocumentError(
            reason=f"parameter '{name}' has neither a value nor an expression",
            path=f"csv:row {line}",
        )

    value: float | None = None
    if literal:
        try:
            value = float(literal)
        except ValueError:
            raise DocumentError(
                reason=(
                    f"parameter '{name}' has value {literal!r}, which is not a number. "
                    "Put formulas in the 'expr' column instead."
                ),
                path=f"csv:row {line}",
            ) from None

    return Parameter(
        name=name,
        value=value,
        expr=expression or None,
        unit=row.get("unit") or "mm",
        group=row.get("group", ""),
        doc=row.get("doc", ""),
    )
