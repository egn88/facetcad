"""Running an OpenCascade boolean exactly once.

``BRepAlgoAPI_Cut(a, b)`` is not a constructor that prepares an operation — it
*performs* it, and returns with ``IsDone()`` already true and ``Shape()``
already populated. The conventional-looking follow-up ``Build()`` therefore ran
the whole boolean a second time. Worse, ``SetRunParallel`` was being set
*between* the two, so the only run that produced the result anyone waited for
was the serial one. Every cut and fuse in this kernel cost double.

Arguments are supplied through the list API instead, which leaves ``Build()`` as
the single point where the work happens — and lets the parallel flag apply to
it.
"""

from __future__ import annotations

from typing import Protocol

from OCP.TopoDS import TopoDS_Shape
from OCP.TopTools import TopTools_ListOfShape


class BooleanOperation(Protocol):
    """The slice of ``BRepAlgoAPI_BooleanOperation`` this helper drives.

    Structural rather than a base class, for the same reason the geometry ports
    are: nothing here imports an OCCT type in order to be recognised by it.
    """

    def SetArguments(self, shapes: TopTools_ListOfShape) -> None: ...
    def SetTools(self, shapes: TopTools_ListOfShape) -> None: ...
    def SetRunParallel(self, parallel: bool) -> None: ...
    def Build(self) -> None: ...


def boolean[Operation: BooleanOperation](
    operation: Operation, base: TopoDS_Shape, tool: TopoDS_Shape
) -> Operation:
    """Configure ``operation`` with one argument and one tool, and run it.

    The operation is returned rather than its shape: callers need the history
    map to attribute faces, and building the shape again to get it would undo
    the point of this module. Generic, so a caller gets back the concrete type
    it passed in and ``Modified`` and ``Shape`` still resolve on it.
    """
    arguments = TopTools_ListOfShape()
    arguments.Append(base)
    tools = TopTools_ListOfShape()
    tools.Append(tool)
    operation.SetArguments(arguments)
    operation.SetTools(tools)
    operation.SetRunParallel(True)
    operation.Build()
    return operation
