# FacetCAD

Parameter-sheet driven parametric CAD with **deterministic topological naming**.

FacetCAD is a solid modeller you drive from a table of named dimensions: change a number
and the part rebuilds. It exists because that is precisely the workflow that breaks in
FreeCAD — faces there are referenced by kernel index, the indices move when a
spreadsheet-driven dimension changes, and a fillet quietly reattaches to a different edge.
Here every face is named after *how it came to exist*, so a fillet stated once survives
every rebuild.

```bash
docker compose up --build      # http://localhost:8080
```

## The problem this solves

FreeCAD's instability is **not** an OpenCascade defect. OCCT is deterministic. FreeCAD
breaks because it stores geometric references as raw kernel indices — `Face6`, `Edge12` —
into a shape whose enumeration is reshuffled by any upstream parameter change. Your fillet
was on edge 12; you widen the plate; edge 12 is now somewhere else.

That is a bookkeeping bug in the layer *above* the kernel, and it is fixable.

### Faces are named by provenance

A face is identified by **how it came to exist**, in names you chose:

```
base/cap+                  the top cap of feature 'base'
base/side[outline.left]    the side face swept from sketch curve 'left'
slot/floor                 the floor of pocket 'slot'
slot/wall[hole.c1]         the pocket wall swept from curve 'c1'
base/cap+#1                the second fragment, when a cut split the cap
```

Edges are **derived**, not tagged: an edge is the intersection of its two adjacent named
faces, written `base/cap+ ^ base/side[outline.left]` with the pair in canonical order. So
the naming problem is solved once, for faces, and edge stability — which is what fillets
depend on — follows for free.

### Documents store queries, not picks

```yaml
targets:
  edges: "base/cap+ ^ base/side[*]"     # the whole top perimeter, stated once
```

Re-evaluated on every rebuild. If a selector becomes ambiguous or loses its target, the
rebuild **stops and says why**:

```
feature f1 (fillet) — edges(between base/cap+ and base/side[*])
  expected 4 edges, resolved 3
  reason: face 'base/side[outline.left]' was consumed by feature 'slot_1'
```

Refusing to build is strictly better than lying. FreeCAD's real sin isn't failing — it's
silently moving your fillet and letting you find out at the printer.

### Sketches: a chain, not three tables

Drawing a profile is one continuous act, so the editor treats it that way. Each row is
"get to here, this way", and the points, curves and loop are generated from it:

```
from    u         v         join   name
start   0         0         —
p0      plate_w   0         line   bottom
p1      plate_w   plate_h   arc    (centre)
p2      0         plate_h   line   top
close   back to the first point     line
```

Names are optional — unnamed segments become `c0`, `c1`, … and points `p0`, `p1`, … which
is the same convention both editors use whether an id was generated or typed. Name the
ones you expect to reference later, because a curve id ends up inside every face tag it
produces: `base/side[outline.bottom]` reads better than `base/side[outline.c1]`.

Generated ids are allocated once and never renumbered or reused. Deriving them from row
position would mean deleting a row shifted every id below it, so `outline.c2` would start
naming a different curve and every selector built on it would quietly move — the exact
failure the naming system exists to prevent.

Referring to a parameter that does not exist yet is normal — you know the shape before
you have named every dimension — so the editor offers to create it in place rather than
sending you to the sheet and back.

The stored document stays explicit points, curves and loops; the chain is a view over it,
inferred on open and generated on save. A sketch that is not a single closed run (several
loops, a bare circle) falls back to the tables.

### Sketches: explicit coordinates, no solver

Every point is computed from parameters. There is no constraint solver, so there
is no solver branch that can flip between rebuilds and silently mirror a profile.

```yaml
points:
  a:  ["-half", "-r"]      # a capsule slot
  b:  ["half",  "-r"]
  rc: ["half",  0]         # arc centre
curves:
  - {id: bottom, start: a, end: b}
  - {id: right,  type: arc, start: b, end: c, center: rc}
  - {id: rim,    type: circle, center: m, radius: "bore_d / 2"}
```

Arcs reference named points exactly as lines do, so a loop closes by construction
rather than by you computing endpoints that happen to meet. The radius is derived
from the centre, and an arc whose two ends disagree about it is reported rather
than quietly averaged. A circle is closed already, so it forms a loop by itself.

Curved faces name like any other: that arc becomes `body/side[outline.right]`, the
circle becomes `hole/wall[bore.rim]`.

### Holes are placed, not swept

```yaml
- id: bolt
  type: hole
  at: holes.h1            # a named sketch point
  standard: M6            # or an explicit `diameter`
  fit: normal             # close | normal | loose | tapped
  through_all: true
  counterbore_diameter: 11
  counterbore_depth: 5
```

Naming a thread rather than a diameter matters because nobody remembers that a
normal-fit clearance hole for an M6 is 6.6mm, and finding out you were wrong
happens with a part in your hand. An unknown size is refused with the list of
known ones rather than interpolated.

The bore is tagged against the point that placed it — `bolt/wall[holes.h1]`,
`bolt/floor`, and for a counterbore `bolt/cbore[holes.h1]` and
`bolt/cbore_floor`. A counterbore is one entry in the history but two kernel
operations, and the bore it steps down to keeps its own name.

### Blends attach to edges, which is where this all pays off

```yaml
- id: corners
  type: fillet
  radius: rad
  edges: "base/side[*] dir=|z"        # the four upright corners
- id: soften
  type: chamfer
  distance: 2
  edges: "base/cap+ ^ */*"            # the whole top perimeter
  on_failure: skip                    # blends fail; a model may tolerate it
```

Edges are exactly where index-based references break down, so blends are the
sharpest test of the whole design. The blend face is then named after the edge
it replaced — `corners/fillet[base/side[outline.bottom] ^ base/side[outline.left]]`
— which needed no new naming concept, because that edge already had a stable
name made of its two adjacent faces. The names compose: chamfering a filleted
corner gives `soften/chamfer[corners/fillet[...] ^ base/cap+]`.

`on_failure: skip` exists because blend failure is genuinely kernel-bound. A
radius that does not fit is not a defect in the document, and a model should
survive one. The feature is reported as **bypassed** rather than silently
dropped.

### Directions are absolute by construction

Sketches attach **only to datum planes**, never to faces. A datum is computed from
parameters and other datums, never from picked topology, and extrusion direction carries
an explicit `+normal` / `-normal` sign. This designs out the whole class of flip and mirror
failures rather than patching it.

## Quick start

```bash
docker compose up --build
open http://localhost:8080          # click "New" → keep "example plate" ticked
```

Then edit `plate_w` in the sheet and watch every dependent dimension, the geometry and the
face names follow — while `slot/floor` stays `slot/floor`.

### Without Docker

```bash
# backend
cd backend && python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/uvicorn facet.main:app --reload      # :8000

# frontend
cd frontend && npm install && npm start           # :4200, proxied to :8000
```

## Using it

| | |
|---|---|
| **Sheet** | Every dimension is a number *or an expression* — `plate_w * 0.6` is as valid as `72` |
| **Click a face** | Reports its stable tag and copies the selector. The mouse is for *discovery*; the keyboard does the work |
| **No GPU?** | The viewport falls back to a CPU renderer automatically. Force it with `?render=software` |
| **Ctrl/⌘ K** | Selector console — ask what a selector matches before committing to it |
| **A E S P** | Add feature · edit the YAML source · sketches and datums · new parameter |
| **R F V C** | Reload · fit the view · show sketches · copy the selected tag |
| **Export** | STL and OBJ for printing, **STEP** for other CAD, DXF and SVG cut paths for a laser or router, CSV for the sheet, YAML for the whole project |
| **Import** | Edit the sheet in Excel or LibreOffice and import the CSV back |

Cloning a project onto another station is a file copy — documents are plain YAML. Each
one carries `schema: cadsheet/1`, the tag the format has had since before the project was
renamed; it stays, because renaming it would mean migrating every saved document for no
benefit.

## API

The UI has no privileged access: everything it does is a public endpoint, which is what
makes an MCP server a thin wrapper rather than a second implementation. Interactive docs
at `/docs`.

Two endpoints exist specifically so an agent can work without guessing:

```http
GET  /api/projects/{id}/topology     # every tag that currently exists
POST /api/projects/{id}/resolve      # what would this selector match?
```

Ask what exists, ask what a selector would match, *then* write it into the document.

```http
GET    /api/projects                        POST   /api/projects
GET    /api/projects/{id}/document          PUT    /api/projects/{id}/document
PATCH  /api/projects/{id}/parameters
POST   /api/projects/{id}/features          PATCH  .../features/{fid}
DELETE /api/projects/{id}/features/{fid}    POST   .../features/reorder
POST   /api/projects/{id}/recompute         GET    /api/projects/{id}/mesh
GET    /api/projects/{id}/export?fmt=       POST   /api/projects/{id}/import
GET    /api/projects/{id}/export/cut?selector=      .../export/views?views=
GET    /api/projects/{id}/export/flat               .../export/jointed?thickness=
GET    /api/projects/{id}/export/enclosure?thickness=
```

Bodies, sketches and datums are edited through endpoints of the same shape; `/openapi.json`
has the full set.

`/recompute` returns per-feature status. A failure halts the chain, marks later features
*skipped* rather than failed, and still returns the last good solid — so you see the part
as far as it got, with the culprit named.

## MCP server

Bundled. `docker compose up` and an agent can drive the whole system at
`http://localhost:8080/mcp` — no client-side install, nothing checked out, no subprocess.
An agent on another machine is a first-class user, which is the point.

The server hands out its own configuration, so connecting needs neither the repo
nor a guessed URL:

```bash
curl https://cad.example.com/mcp.json
```

```json
{
  "mcpServers": { "facet": { "type": "http", "url": "https://cad.example.com/mcp" } },
  "install": "claude mcp add --transport http facet https://cad.example.com/mcp",
  "guide": "https://cad.example.com/api/mcp"
}
```

It reads the hostname it was actually reached at, so a copy behind a different name tells
the truth about itself. Or skip the file entirely:

```bash
claude mcp add --transport http facet https://cad.example.com/mcp
```

`.mcp.json` is also committed at the repo root, for a client that reads one from a
checkout:

```json
{
  "mcpServers": {
    "facet": { "type": "http", "url": "http://localhost:8080/mcp" }
  }
}
```

Change the URL for a real deployment, and set `FACET_HOSTS` to the hostnames it answers
to:

```bash
FACET_HOSTS=cad.example.com docker compose up -d
```

That guard is DNS-rebinding protection: without it a page in someone's browser could
resolve your hostname to a server they control and then talk to yours. Loopback is always
allowed. `FACET_HOSTS=*` disables it, which is reasonable only behind a reverse proxy
that has already vetted the `Host` header.

> **There is no authentication, by design for now.** Anyone who can reach a deployment can
> read, edit and delete every project and drive every MCP tool. `FACET_HOSTS` is rebinding
> protection, not a login. That keeps it trivial to stand up locally, and it means the
> network is the entire security boundary: anywhere but a laptop or a private network, put
> an authenticating proxy in front of it. Real accounts, persistence in a database and
> tenant isolation are a later iteration, not something to assume is present.

There is also a stdio entry point for a local client that prefers spawning a process:

```bash
cd backend && .venv/bin/pip install -e ".[mcp]"
FACET_URL=http://localhost:8080/api .venv/bin/python -m facet.mcp
```

Both drive the same 26 tools. The extra is optional and the API boots without it — it
simply serves no MCP endpoint, which is what `FACET_INSTALL=.[occt]` gets you.

### Reading the docs instead

An agent with nothing but a fetch tool does not need MCP at all:

```
GET /api/mcp
```

is a Markdown guide written for one — task-ordered, with a worked two-part enclosure — and
its claims are tested against the running API, so it cannot quietly drift. Hand a model
that one URL and it can build.

## Architecture

Ports and adapters. Dependencies point inwards only; `domain` importing a kernel type is a
build failure.

```
adapters   http · mcp               →  driving
   application   use cases + PORTS
      domain     pure, zero deps
adapters   geometry/occt · geometry/fake · persistence · export   →  driven
```

The load-bearing rule: **kernels report provenance, they never assign names.** An adapter
says "this face was swept from curve `c1`"; deciding that a swept face is a pad *side* but
a pocket *wall* is a domain concern. That split is what keeps the naming engine
kernel-agnostic.

There are two geometry adapters on purpose. `geometry/fake` is an exact analytic kernel for
axis-aligned prismatic solids — not a stub. It proves the port is honest (a port with one
implementation is a guess), carries most of the suite in about eleven seconds without OCCT
installed at all, and answers "is this our naming layer or the kernel?" in one command.
Both adapters pass the same conformance suite.

See [ARCHITECTURE.md](ARCHITECTURE.md) and [DESIGN.md](DESIGN.md).

## Tests

```bash
cd backend && .venv/bin/python -m pytest        # 855 tests, ~1.5 min with OCCT installed
cd frontend && npm test                        # 25 tests, chain naming stability
```

Without the OCCT extra, 248 of those drop out and the remaining 596 — naming, selectors,
recompute, the whole domain — run on the analytic kernel in about eleven seconds.

The conformance suite runs twice — once per kernel — so OCCT and the analytic kernel are
proven interchangeable rather than merely intended to be.

The suite that decides whether the project succeeded is the **parameter sweep**: resize a
model across dozens of steps, rebuild each time, and assert every tag and every stored
selector still resolves identically. Any silent re-binding is a hard failure. It runs
against OCCT, because a sweep that only ever saw the analytic kernel would be proving the
wrong thing.

## Kernels

Two adapters implement the geometry port, and both pass the same conformance suite.

| | `occt` | `analytic` |
|---|---|---|
| Profiles | Any closed polygon, arcs, circles | Axis-aligned rectangles only |
| Curve types | line, arc, circle | line |
| Features | pad, pocket, hole, thread, fillet, chamfer | pad, pocket |
| Datum planes | Any orientation | Axis-aligned |
| Exports | STL, OBJ, **STEP**, DXF, SVG | STL, OBJ |
| Speed | Real geometry | Milliseconds per operation |

`occt` is the production kernel and is selected automatically when `cadquery-ocp` is
installed. `FACET_KERNEL=analytic` forces the other.

The analytic kernel refuses anything curved rather than approximating it, which is why
holes, threads and blends are OCCT's alone: a fake that quietly disagreed with the real
kernel would be worse than no fake at all.

`analytic` is not a stub — it is an exact occupancy-grid kernel. It exists so the naming
suite runs without OCCT in seconds, so the port cannot quietly grow OCCT-shaped
assumptions, and so a misbehaving model can be run against both to answer "is this our
naming layer or the kernel?" in one command.

The OCCT image is ~850 MB, most of it OpenCascade itself. The build strips the parts of
`cadquery-ocp` this project never imports — matplotlib, and every VTK library outside
OCP's own link chain — then proves the result can still cut a fillet and write STEP,
because falling back to the analytic kernel is silent and would otherwise just build a
simpler part. To skip OCCT altogether:

```bash
FACET_INSTALL=. docker compose build     # falls back to the analytic kernel
```

## State of the project

This is a working system with a large test suite behind it, not a released product. It
builds real parts and the naming holds under the sweeps — but there is no versioning
policy, nothing promised about document compatibility between commits, and no
authentication of any kind. Treat it as something to run yourself and read, not as
something to depend on.

Working today: parameter sheet with expressions and units, datums, explicit-coordinate
sketches with lines, arcs and circles, pad and pocket on arbitrary profiles at any
orientation, counterbored holes and tapped or modelled threads at ISO standard sizes,
fillet and chamfer through named-edge selectors with `on_failure: skip`, multiple bodies
with placements, provenance naming with split ordinals, selectors with fail-loud
diagnostics, incremental recompute with content-hash caching, STL/OBJ/STEP/CSV/YAML
export, DXF and SVG for cut paths, orthographic sections, flat and finger-jointed panels
and laser enclosures, CSV import, a 3D viewport with click-to-tag, an MCP server and the
agent guide at `/api/mcp`.

Not there: revolve, mirror and patterns; assembly as a joint tree with parameter-driven
kinematics — never a mate solver, which would reintroduce exactly the nondeterminism this
project exists to escape; dimensions and annotations on drawings; sheet metal. Geometry
also runs in-process, so an OCCT segfault on pathological input takes the API down with
it; isolating it in a worker pool is a known gap rather than a solved problem.

Countersinks need a cone, which the geometry port has no operation for yet — that is the
one piece of the hole feature deliberately left out rather than approximated.

Blending only *some* of the edges meeting at a corner used to be refused: OCCT builds a
transition patch there and attributes it to a vertex, which has no two-face name. Corners
are now named too — `a ^ b ^ c`, three or more faces, where an edge is two — so the patch
gets a stable tag like any other face. Arity is what distinguishes a corner from an edge,
so nothing about the existing grammar had to change.

Order matters when a fillet and a chamfer meet at one corner: state chamfers before
fillets. OCCT applies each blend to the solid the previous one left behind, and a fillet
that has already replaced an edge with a curved patch leaves the chamfer nothing flat to
bite on.

OCCT's blend operations genuinely fail on hard geometry, and that is kernel-bound rather
than something this layer can fix — which is what `on_failure: skip` is for.

## Licence

[MIT](LICENSE). Do what you like with it, including commercially; keep the notice, and
there is no warranty.

One caveat about the *image* rather than the source: it bundles OpenCascade, which is
LGPL-2.1. That is a licence on the binary you distribute, not on anything you write here
— but if you redistribute the image, the LGPL's terms come with it. Building with
`FACET_INSTALL=.` leaves OCCT out entirely.
