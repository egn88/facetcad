# Parametric CAD — Design Document

A web-based, dockerized parametric CAD system driven by a parameter sheet and a linear
feature history, built to eliminate the topological-naming instability that makes
FreeCAD's spreadsheet workflow brittle.

## 0. Decisions locked

| Decision | Choice |
|---|---|
| Geometry kernel | OpenCascade via `cadquery-ocp` (Python bindings) |
| Backend | Python 3.12 + FastAPI |
| Sketching | Explicit parametric coordinates — **no 2D constraint solver** |
| Frontend | React + three.js (driven directly, not via react-three-fiber) |
| Sheet export | Parameter table (CSV/JSON) **and** 2D technical drawings (DXF/SVG) |
| First milestone | Thin vertical slice: pad + pocket, with web UI, viewer and docker from day one |
| Assembly | Deferred; when built, joint-tree kinematics only (Tier A) — never a mate solver |

## 1. The core problem and its solution

FreeCAD's instability is **not** an OpenCascade defect. OCCT is deterministic. FreeCAD
breaks because it persists geometric references as raw kernel indices — `Face6`,
`Edge12` — into a shape whose enumeration is reshuffled by any upstream parameter
change. Your fillet was on edge 12; you widen the plate; edge 12 is now somewhere else,
and the fillet silently moves.

That is a bookkeeping bug in the layer *above* the kernel. Fixing it is the entire
point of this project. Three mechanisms, layered:

### 1.1 Provenance tags

Every OCCT operation exposes history maps — `Generated(shape)`, `Modified(shape)`,
`IsDeleted(shape)`. Every operation in this system is wrapped so that each resulting
face carries a **semantic tag rooted in user-chosen names**, never in kernel indices.

Tag grammar:

```
<feature_id>/<role>[<source>]
```

Examples:

```
base_plate/cap+
base_plate/cap-
base_plate/side[outline.left]
slot_1/floor
slot_1/wall[slot_profile.front]
f1/fillet[base_plate/cap+ ^ base_plate/side[outline.left]]
```

Propagation rules per operation:

- **Pad (prism):** `Generated(edge)` maps each profile curve to the side face it swept.
  Caps get `cap+` / `cap-` by the explicit direction sign.
- **Boolean cut (pocket/hole):** for each face `F` of the base solid, `Modified(F)`
  returns its survivors, which inherit `F`'s tag. For each face `G` of the tool solid,
  `Modified(G)` returns faces re-rooted under the cutting feature's id.
  `IsDeleted(F)` retires a tag and **records why** — so a later selector failure can
  say "this face was consumed by feature `slot_1`" instead of just failing.
- **Splits:** when one face becomes several, disambiguate with an ordinal derived from
  a *canonical geometric sort* — centroid projected into the owning feature's local
  frame, sorted lexicographically. Deterministic, and stable under small parameter
  changes: `base_plate/cap+#0`, `base_plate/cap+#1`.
- **Fillet/chamfer:** `Generated(edge)` yields the blend face; `Modified(face)` carries
  the neighbours forward.

### 1.2 Edges and vertices are derived, not tagged

An edge is identified as **the intersection of its two adjacent named faces**; a vertex
as the intersection of three. Sorted, so the identity is canonical:

```
base_plate/cap+  ^  base_plate/side[outline.bottom]
```

This means the naming problem only has to be solved **once, for faces**. Edge and
vertex stability then follows for free — which matters enormously, because edges are
what fillets and chamfers attach to, and edges are exactly where FreeCAD's naming is
weakest.

### 1.3 Selectors: the document stores queries, never picks

The document never contains "face 6". It contains a query that is re-evaluated on every
rebuild:

```
faces(feature=base_plate, role=cap, side=+)
faces(tag="base_plate/side[outline.left]")
edges(between=["base_plate/cap+", "base_plate/side[*]"])   # whole top perimeter
edges(feature=slot_1, role=floor_perimeter)
faces(dir=+Z, feature=base_plate)                          # geometric filter, last resort
```

Selectors are deterministic, human-readable, and **typeable** — which is precisely what
the keyboard-first and MCP goals require.

### 1.4 Fail loudly — the rule that makes it trustworthy

Each selector caches, alongside itself, the cardinality and geometric fingerprint
(area, centroid in the owning feature's local frame, normal) of what it last resolved
to. On rebuild, resolution proceeds in order:

1. Exact tag match
2. Tag pattern match
3. Fingerprint nearest-match within tolerance
4. Geometric filter

If the result is ambiguous, or the cardinality changed, the rebuild **raises a
structured error and refuses to guess**:

```
feature f1 (fillet) — selector edges(between=["base_plate/cap+","base_plate/side[*]"])
  expected 4 edges, resolved 3
  missing: base_plate/cap+ ^ base_plate/side[outline.left]
  reason:  face base_plate/side[outline.left] was deleted by feature slot_1
```

FreeCAD's real sin is not failing — it is silently reattaching a fillet to the wrong
edge and letting you find out at the printer. **Refusing to build is strictly better
than lying.** The quality of these diagnostics is the product.

## 2. Directions are absolute by construction

Relative-direction weirdness is designed out rather than patched:

- **Sketches attach only to datum planes. Never to faces.**
- A datum is `origin + x_axis + normal`, computed **only from parameters and other
  datums** — never from picked topology.
- Extrusion direction is the datum normal with an **explicit sign** (`+normal` /
  `-normal`), never inferred.
- Any transform is expressed in the global frame or in a *named* datum's frame.

Cost: you must declare datums explicitly. Given that the whole point is to work from
the table, that is a feature rather than a tax. It eliminates the entire class of
flip/mirror failures.

## 3. Document model

One project = one git-diffable YAML document. Import/export/clone is then a file copy.

```yaml
schema: cadsheet/1
project: bracket
units: {length: mm, angle: deg}

parameters:
  - {name: plate_w,  value: 120,          group: Plate, doc: overall width}
  - {name: plate_h,  expr: plate_w * 0.6, group: Plate}
  - {name: plate_t,  value: 6,            group: Plate}
  - {name: slot_d,   value: 2.5,          group: Slot}

datums:
  base: {type: plane, origin: [0, 0, 0], x: [1, 0, 0], normal: [0, 0, 1]}
  top:  {type: plane, parent: base, origin: [0, 0, plate_t], x: [1, 0, 0], normal: [0, 0, 1]}

sketches:
  outline:
    plane: datums.base
    points:
      p0: [0, 0]
      p1: [plate_w, 0]
      p2: [plate_w, plate_h]
      p3: [0, plate_h]
    curves:
      - {id: bottom, type: line, from: p0, to: p1}
      - {id: right,  type: line, from: p1, to: p2}
      - {id: top,    type: line, from: p2, to: p3}
      - {id: left,   type: line, from: p3, to: p0}
    loops:
      - {id: outer, curves: [bottom, right, top, left]}

features:
  - id: base_plate
    type: pad
    profile: sketches.outline.outer
    length: plate_t
    direction: "+normal"

  - id: slot_1
    type: pocket
    profile: sketches.slot.outer
    depth: slot_d
    start: 'faces(feature=base_plate, role=cap, side=+)'
```

`features` is an **ordered linear history**, as in PartDesign / SolidWorks — not a free
DAG. Simpler to reason about, matches user expectation, and reordering is a first-class
operation.

### Blend order

Fillets and chamfers can share a face, but **not a vertex**. Where a chamfer
meets a previously filleted edge at a shared corner, OCCT cannot close the two
against each other and resolves it by running the transition down the third edge
at that vertex — usually an upright one, all the way to the bottom face.

The naming engine reports this correctly rather than hiding it: the resulting
patch is tagged `bevel/corner[part/cap- ^ ...]`, and a tag naming `cap-` on what
should be a top-edge chamfer is the tell.

Measured behaviour, on both a rectangular and a triangular part:

| what | result |
|---|---|
| chamfer any edges, no prior fillet | stays at the top |
| fillet an edge, chamfer one that shares no vertex with it | stays at the top |
| fillet an edge, chamfer one sharing **one** vertex | one face runs down |
| fillet an edge, chamfer ones sharing **two** vertices | two faces run down |
| **chamfer first, then fillet** | **stays at the top, no transition patches** |

So the rule is: **do the chamfers first, then the fillets.** Naming several
specific edges in one feature needs the comma union — `a ^ b, c ^ d` — which is
why an edge selector unions at a looser precedence than `^`.

### Bodies

A document holds one or more **bodies**, the same convention FreeCAD's PartDesign uses.
A body is one solid with its own linear feature history and its own placement:

```yaml
bodies:
  - id: plate
    features:
      - {id: base_plate, type: pad, profile: sketches.outline.outer, length: plate_t}
  - id: pin
    placement: {origin: [stand_off, 0, 0], rotation: [0, 0, 0]}
    features:
      - {id: shaft, type: pad, profile: sketches.pin.outer, length: pin_len}
```

Two rules make this worth the extra nesting:

- **A body is exactly one solid.** A second pad inside a body *fuses* onto what is
  already there, rather than replacing it. Before bodies existed, only the last solid
  survived to the viewport.
- **Bodies are never fused with each other.** They stay separate solids, which is what
  an assembly needs: a joint has to be able to move a part, and you cannot move half of
  a fused blob.

`placement` positions a body for display and export only — it is *not* applied to the
modelled geometry. Rotating a body must never perturb the centroids that canonical
ordinals and fingerprints are computed from, or a placement change could silently
renumber faces. That is the whole disease this project exists to cure.

A single-body document is still written with a flat `features:` list, so the simple case
stays simple and every earlier document loads unchanged (as body `main`).

### Expressions

A small safe evaluator over parameters: arithmetic, `min`/`max`/`abs`/`sqrt`,
trigonometry with degree variants, conditionals. Dependency graph with cycle detection
via topological sort. Everything stored internally in mm/degrees; display units are a
presentation concern.

## 4. Recompute engine

- Each feature node caches `input_hash -> (shape, tag_map, tessellation)`.
- `input_hash` = hash of resolved parameter values + feature spec + upstream shape hash.
- A parameter change dirties **only** downstream nodes. Everything else is served from
  cache, including the tessellation — so panning and typing never re-run geometry.
- Recompute is a pure function of the document. Errors are **per-node**: the model still
  renders up to the last successfully built feature, with the failure point marked.
- Geometry runs in a **subprocess pool with timeouts**. OCCT can segfault on pathological
  input; a crash must not take the API down.

## 5. API — designed first, UI is just a client

The React app makes no privileged calls. Everything it can do is a public endpoint,
which makes the MCP server a thin wrapper rather than a parallel implementation.

```
GET    /api/projects
POST   /api/projects
GET    /api/projects/{id}/document
PUT    /api/projects/{id}/document          # whole-document replace (import / paste YAML)
PATCH  /api/projects/{id}/parameters        # {plate_w: 130}
POST   /api/projects/{id}/features
PATCH  /api/projects/{id}/features/{fid}
DELETE /api/projects/{id}/features/{fid}
POST   /api/projects/{id}/features/reorder
POST   /api/projects/{id}/recompute         -> per-feature status + structured errors
GET    /api/projects/{id}/mesh              -> triangles + exact edge polylines + tags
GET    /api/projects/{id}/topology          -> every current face/edge tag
POST   /api/projects/{id}/resolve           -> {selector} : preview what it matches
POST   /api/projects/{id}/export            -> stl | 3mf | step | dxf | svg | csv | json
```

`/topology` and `/resolve` make the system **self-describing**. An MCP agent (or a
human) can ask "what faces exist right now?" and "what would this selector match?"
*before* committing a change. That is the difference between an API an agent can use
effectively and one it can only guess at.

## 6. Frontend

React, grouped by feature. Panels:

- **Parameter sheet** — editable grid, formula bar, groups, units, live highlighting of
  dependents when a cell is focused.
- **Feature tree** — ordered, drag-to-reorder, per-node error badges.
- **3D viewport** — three.js driven directly from a single imperative component.
  Shaded faces plus the kernel's *exact* edge polylines, so it reads as CAD rather than
  as a mesh. Click a face → its stable tag is displayed and the matching selector is
  copied to the clipboard.

  react-three-fiber was tried first and removed. It only configures its canvas once
  `ResizeObserver` reports a non-zero size, so where that observer is silent the
  viewport stays blank with no error at all; and r3f v8 pins `react <19`, which would
  hold the UI back later. Owning the render loop also suits a CAD viewport, where
  picking has to resolve to a face tag rather than to a generic mesh.
- **Document view** — raw YAML, round-trips through the same API.
- **Diagnostics** — structured rebuild errors with jump-to-feature.

Keyboard-first: a command palette (`Ctrl+K`) drives the same API commands the mouse
does. The mouse's job is *discovery* — clicking a face to learn its name — not
authoring.

## 7. Deployment

`docker compose` with three services:

- `api` — FastAPI + OCP, multi-stage build (the OCP wheel is large; keep it out of the
  final layer's build tooling)
- `web` — React build served by nginx
- `db` — SQLite on a volume for v1, behind SQLAlchemy so Postgres is a config change

Documents live as files on a volume; the database only holds project metadata. That
keeps "clone a project onto another station" as simple as copying one text file.

## 8. Testing strategy: parameter-sweep stability tests

This is the test suite that decides whether the project succeeds.

For every fixture model, sweep each parameter across its plausible range (and a few
degenerate ones), rebuild, and **assert that every selector still resolves to the same
tags with the same cardinality**. Property-based, not example-based. Any silent
re-binding is a hard failure.

Conventional unit tests cover the expression engine, the tag propagation rules per
operation, and the export writers.

## 8b. Getting geometry out

Five exports, and it is worth being clear which answers which question.

| Export | Question it answers |
|---|---|
| **STL / 3MF** | Print it. |
| **STEP** | Hand it to another CAD system. |
| **Views** | Orthographic sections, for a setup drawing. |
| **Cut** (from a selection) | Cut *these faces*, named by selector. |
| **Faces** | Cut every flat face of the part, plain. |
| **Joined** | Cut every flat face *and* the finger joints between them. |
| **Box** | Cut a container that this part fits inside. |

The last three are easy to confuse. **Faces** takes the part apart into panels
with plain edges. **Joined** does the same and cuts interlocking teeth into the
edges the faces share, so the modelled shape can be assembled. **Box** ignores
the part's shape entirely and builds a rectangular enclosure around its bounding
box.

Joined is where the naming engine pays off outside the model: every edge reports
the same ref to *both* faces that meet along it, so two panels recognise each
other as a mating pair with no geometric matching, and the phase is settled by
sorting their tags — stable across rebuilds because tags are.

A joint is only cut where it will survive. The outline is checked for
self-intersection afterwards, and a panel that fails goes out plain with the
reason: recesses that would meet through a panel narrower than twice the
material, a run shorter than its own teeth, a corner gone wrong. A cutting list
that looks right and falls apart on the bed is the worst outcome available.

## 9. Roadmap

| Milestone | Content | Est. |
|---|---|---|
| **M0** ✅ Skeleton | Repo layout, docker-compose, FastAPI + React hello, project CRUD, document load/save round-trip | ~1 wk |
| **M1** ✅ Geometry core | Parameters + expressions, datums, explicit sketches, **pad + pocket only**, provenance tagging through prism and boolean, selector resolution, fail-loud diagnostics, recompute cache, STL + STEP export | ~3 wk |
| **M2** ✅ Viewer | Tessellation endpoint with tags, three.js viewport, orbit/shaded/edges, click-face→selector, last-good-state error rendering | ~1.5 wk |
| **M3** ✅ Sheet UX | Parameter grid + formula bar, feature tree with reorder, command palette, document import/export | ~2 wk |
| **M4** Feature set | Hole (with standard sizes), fillet, chamfer, threads — revolve, mirror and patterns outstanding | ~2 wk |
| **M5** ✅ 2D output | Face cut paths, orthographic sections, DXF + SVG, laser enclosure and jointed faces | ~2 wk |
| **M6** MCP + docs | OpenAPI polish, MCP server wrapping the API, worked examples | ~1 wk |
| *Later* | Assembly Tier A (joint tree), drawing dimensions/annotations, sheet metal | — |

Multi-body landed early, out of order: it turned out to be the fix for "only the last
solid renders", and it is the foundation Tier A needs anyway.

## 10. Assembly — deferred, and deliberately scoped

Two very different products share this name:

**Tier A — joint tree with driven parameters (~1–2 weeks).** Parts placed by explicit
transforms — which bodies already have, as `placement`, driven by parameters like
everything else. A joint becomes a rule that computes a placement instead of the user
typing one. A joint is `{type: revolute|prismatic|fixed, parent, child, axis: <datum>,
value: <parameter>}`. Restricted to a **tree** (no closed loops), motion is pure forward
kinematics — matrix multiplication down the chain. Drag a parameter, the arm swings.
Fully deterministic, no solver, and it reuses the parameter system that already exists.
This delivers ~90% of "join parts through a virtual axis and watch them move".

**Tier B — mate constraint solver (months).** Coincident/tangent/distance mates, DOF
analysis, redundancy detection, closed kinematic loops. A nonlinear solve over SE(3)
with Jacobians, and it reintroduces exactly the nondeterminism this project exists to
escape.

**Build Tier A. Do not build Tier B.** If one closed loop is ever genuinely needed, add
a targeted loop-closure solver for that case alone.

## 11. Known risks

| Risk | Mitigation |
|---|---|
| **OCCT fillet fragility** — fillets fail on hard geometry, and ours will fail identically because it is the same kernel | Apply fillets last, per-edge with named selectors, and support an explicit `on_failure: skip` flag so one bad blend doesn't kill the rebuild |
| OCCT segfaults on pathological input | Geometry in a subprocess pool with timeouts |
| Split-face ordinals drifting under parameter change | Canonical geometric sort in the owning feature's local frame; guarded by the parameter-sweep test suite |
| Tag explosion on complex boolean chains | Tags are hierarchical and lazily rendered; `/topology` paginates |
| Large docker image from the OCP wheel | Multi-stage build |
| Scope creep into a general CAD system | The linear history + explicit-coordinate sketch decisions are load-bearing simplifications. Defend them. |

## 12. Framing

Much of the geometry layer already exists in `build123d` / OCCT. What exists nowhere is:
stable named topology, incremental recompute, a table-driven parameter UI, and a clean
self-describing API. So this project is best understood as **a stable-naming parametric
layer plus a web IDE on top of OCCT** — not as a CAD system. That framing keeps us out
of the parts nobody should rewrite.

Prior art worth watching: Zoo's KCL (text-based cloud CAD), CadQuery/build123d,
FreeCAD 1.0's partial toponaming mitigation.
