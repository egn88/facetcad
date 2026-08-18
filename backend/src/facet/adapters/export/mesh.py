"""Mesh and sheet exporters.

The mesh writers work from a :class:`Tessellation`, not from a kernel shape, so
they are kernel-agnostic by construction: any adapter that can tessellate can
export STL and OBJ without implementing anything extra. That is the ISP split
from ARCHITECTURE.md paying off — printing works on day one with the analytic
kernel, and unchanged when OCCT arrives.
"""

from __future__ import annotations

import csv
import io
import json
import struct
from collections.abc import Mapping, Sequence

from facet.application.ports.geometry import Tessellation
from facet.domain.document import Document
from facet.domain.math3d import Vec3
from facet.domain.parameters import ResolvedParameters


def stl_binary(mesh: Tessellation, name: str = "facet") -> bytes:
    """Binary STL — the format every slicer accepts."""
    triangles = _triangles(mesh)
    buffer = io.BytesIO()
    header = f"facet {name}".encode()[:80].ljust(80, b"\0")
    buffer.write(header)
    buffer.write(struct.pack("<I", len(triangles)))
    for a, b, c in triangles:
        normal = _face_normal(a, b, c)
        buffer.write(struct.pack("<3f", *normal.as_tuple()))
        for vertex in (a, b, c):
            buffer.write(struct.pack("<3f", *vertex.as_tuple()))
        buffer.write(struct.pack("<H", 0))
    return buffer.getvalue()


def stl_ascii(mesh: Tessellation, name: str = "facet") -> bytes:
    lines = [f"solid {name}"]
    for a, b, c in _triangles(mesh):
        normal = _face_normal(a, b, c)
        lines.append(f"  facet normal {normal.x:.6e} {normal.y:.6e} {normal.z:.6e}")
        lines.append("    outer loop")
        for vertex in (a, b, c):
            lines.append(f"      vertex {vertex.x:.6e} {vertex.y:.6e} {vertex.z:.6e}")
        lines.append("    endloop")
        lines.append("  endfacet")
    lines.append(f"endsolid {name}")
    return "\n".join(lines).encode()


def obj_text(mesh: Tessellation, name: str = "facet") -> bytes:
    """OBJ keeps per-face grouping, so the tags survive into the exported file."""
    lines = [f"# facet {name}", "o " + name]
    for index in range(mesh.vertex_count):
        x, y, z = mesh.positions[index * 3 : index * 3 + 3]
        lines.append(f"v {x:.6f} {y:.6f} {z:.6f}")

    ranges = {r.ref: r for r in mesh.face_ranges}
    for ref, face_range in ranges.items():
        lines.append(f"g {ref}")
        span = mesh.indices[face_range.start : face_range.start + face_range.count]
        for offset in range(0, len(span), 3):
            a, b, c = span[offset : offset + 3]
            lines.append(f"f {a + 1} {b + 1} {c + 1}")
    return "\n".join(lines).encode()


def _triangles(mesh: Tessellation) -> list[tuple[Vec3, Vec3, Vec3]]:
    vertices = [
        Vec3(mesh.positions[i * 3], mesh.positions[i * 3 + 1], mesh.positions[i * 3 + 2])
        for i in range(mesh.vertex_count)
    ]
    return [
        (
            vertices[mesh.indices[offset]],
            vertices[mesh.indices[offset + 1]],
            vertices[mesh.indices[offset + 2]],
        )
        for offset in range(0, len(mesh.indices), 3)
    ]


def _face_normal(a: Vec3, b: Vec3, c: Vec3) -> Vec3:
    normal = (b - a).cross(c - a)
    return normal.normalized() if normal.length() > 1e-12 else Vec3(0.0, 0.0, 0.0)


# --------------------------------------------------------------------------
# The parameter sheet itself
# --------------------------------------------------------------------------


def parameters_csv(document: Document, resolved: ResolvedParameters | None) -> bytes:
    """The sheet as CSV, including each row's computed value."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["name", "group", "value", "expr", "unit", "resolved_mm_deg", "doc"])
    for parameter in document.parameters:
        computed = resolved[parameter.name] if resolved and parameter.name in resolved else ""
        writer.writerow(
            [
                parameter.name,
                parameter.group,
                "" if parameter.value is None else parameter.value,
                parameter.expr or "",
                parameter.unit,
                computed,
                parameter.doc,
            ]
        )
    return buffer.getvalue().encode()


def parameters_json(document: Document, resolved: ResolvedParameters | None) -> bytes:
    payload = {
        "project": document.name,
        "parameters": document.parameters.to_list(),
        "resolved": resolved.as_dict() if resolved else {},
    }
    return json.dumps(payload, indent=2).encode()


def topology_json(topology: Mapping[str, object]) -> bytes:
    """Every current face and edge tag — the discovery surface for agents.

    Takes the payload rather than a :class:`TopologyIndex` because a document
    is more than one body, and an index is one solid's worth of naming. The
    caller assembles the document-wide answer; this only writes it out.
    """
    return json.dumps(topology, indent=2).encode()


#: Formats this adapter can write, and their MIME types.
MESH_FORMATS: dict[str, str] = {
    "stl": "model/stl",
    "stl-ascii": "model/stl",
    "obj": "text/plain",
}

SHEET_FORMATS: dict[str, str] = {
    "csv": "text/csv",
    "json": "application/json",
    "yaml": "application/x-yaml",
    "topology": "application/json",
}


#: Formats that need a B-rep kernel; guarded by Capability.BREP_EXPORT.
BREP_FORMATS: dict[str, str] = {
    "step": "application/step",
}


def supported_formats() -> Sequence[str]:
    return (*MESH_FORMATS, *SHEET_FORMATS, *BREP_FORMATS)
