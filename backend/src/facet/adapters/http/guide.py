"""The agent-facing guide, served as Markdown at ``GET /api/mcp``.

Written for a language model that has just been handed a URL and nothing else.
That shapes it more than any style choice: it is task-ordered rather than
endpoint-ordered, every example is complete enough to POST as written, and the
worked example is a real part someone asked for rather than a cube.

It is deliberately not generated from the OpenAPI schema. A schema says what
fields exist; it cannot say that a selector is re-resolved on every rebuild, or
that a pocket cuts from its own sketch plane, or that deriving a dimension is
the difference between a model that survives an edit and one that does not.
Those are the facts an agent gets wrong, so they are what this spends its words
on. The schema is linked for the details.
"""

from __future__ import annotations

GUIDE = r"""# FacetCAD — a parametric CAD system you drive over HTTP

You are talking to a parametric solid modeller. You describe a part as a table
of named dimensions plus a short history of operations, and it builds the solid,
tells you the stable name of every face it made, and exports STL, STEP or 2D cut
paths.

Base URL for everything below: `/api`. Full schema at `/openapi.json`.

If your client speaks MCP, this same server exposes it at `/mcp` — 26 typed tools
over everything described here, so you can skip the URL building. `GET /mcp.json`
returns the client configuration for this deployment, addressed to whatever
hostname you reached it at, so nothing has to be checked out or guessed.

This page is for when your client does not speak MCP, or when you want to know
*why* a call behaves the way it does.

## The one rule

**Never write a number where a parameter would do.**

The point of this system is that changing one number rebuilds the part
correctly. A literal typed into a sketch or a feature is frozen at the value it
had when you typed it; everything derived from it silently stops agreeing. So:
measure once, name it, and derive the rest.

```json
{"name": "cav_w", "expr": "board_w + 2 * gap"}
```

Expressions take `+ - * /`, parentheses, `min max abs sqrt hypot`, and the
trigonometric functions in degree variants. `GET /api/expressions` lists them.

## Orient yourself in three calls

```
GET  /api/kernel          what the geometry engine can do
GET  /api/feature-types   which operations exist
GET  /api/projects        what is already here
```

## The document

One project is one document. It has five parts, in this order of dependency:

| part | what it is |
|---|---|
| `parameters` | named numbers and expressions — the sheet |
| `datums` | named planes, computed **only** from parameters and other datums |
| `sketches` | points, curves and closed loops, each attached to one datum |
| `bodies` | one solid each, with its own ordered feature history |
| `features` | pad, pocket, hole, thread, fillet, chamfer |

Create the whole thing in one call and it is built immediately:

```
POST /api/projects
{"id": "bracket", "name": "Bracket", "document": { ...the five parts... }}
```

Or build it up incrementally: `POST /parameters`, `PUT /sketches/{id}`,
`POST /features`, each of which rebuilds and reports what happened.

### Datums are computed, never picked

A datum is a plane defined by an origin, a normal and an in-plane X axis, each
computed from parameters and other datums. It is **never** derived from a face
you pointed at. This is what stops a sketch flipping over when a surface changes
sense during a rebuild — the failure that makes face-attached sketches fragile
in other systems.

You do not have to compute them by hand. Ask:

```
POST /api/projects/{id}/datums/for-face
{"tag": "shell/cap+", "point": [12.0, 8.0, 15.2]}
```

and you get back a datum whose offset is *the feature's own expression*:

```json
{"ok": true,
 "datum": {"id": "shell_cap_pos", "parent": "xy", "origin": [0, 0, "base_h"],
           "normal": [0, 0, 1]},
 "existing": null,
 "explanation": "the far cap of pad 'shell', which pads sketch 'base_outer' on datum 'xy' by 'base_h'",
 "at": {"u": 12.0, "v": 8.0},
 "size": {"u": "outer_w", "v": "outer_l", "uValue": 29.2, "vValue": 18.9}}
```

- `origin` holds `"base_h"`, not `15.2`. The datum follows the model.
- `existing` names a datum already on that plane — reuse it rather than adding a
  near-duplicate.
- `at` is your point in *that plane's* coordinates. Do not compute this yourself
  from the parent's numbers; for a face that stands on edge to its sketch they
  are coordinates on a different plane.
- `size` is the face's own extent, symbolically. `u: "outer_w"` means you can
  centre something on it with `outer_w / 2` and it stays centred forever.

Caps, sides and chamfers between two sides can all be derived. Cylinders,
helices and blend corners cannot, and say so with a reason.

### Sketches

```json
"base_outer": {
  "plane": "xy",
  "points": {"p0": [0, 0], "p1": ["outer_w", 0],
             "p2": ["outer_w", "outer_l"], "p3": [0, "outer_l"]},
  "curves": [
    {"id": "e0", "type": "line", "start": "p0", "end": "p1"},
    {"id": "e1", "type": "line", "start": "p1", "end": "p2"},
    {"id": "e2", "type": "line", "start": "p2", "end": "p3"},
    {"id": "e3", "type": "line", "start": "p3", "end": "p0"}
  ],
  "loops": [{"id": "outer", "curves": ["e0", "e1", "e2", "e3"]}]
}
```

Curve types are `line`, `arc` (start, end, `center`, optional `clockwise`) and
`circle` (`center`, `radius` — a loop on its own). Point coordinates may be
expressions.

**Curve ids matter.** A face swept from curve `e0` is named `feature/side[sketch.e0]`
forever. Rename the curve and every selector naming that face stops resolving.

### Features

Every feature needs an `id` — **unique across the whole document**, not just
within its body — and a `type`.

| type | required | notable options |
|---|---|---|
| `pad` | `profile`, `length` | `direction` (`+normal` / `-normal`), `midplane` |
| `pocket` | `profile`, `depth` | `direction`, `through_all` |
| `hole` | `at` (a `sketch.point`), and `standard` **or** `diameter` | `fit`, `depth` or `through_all`, `counterbore_diameter`, `counterbore_depth`, `direction` |
| `thread` | `at`, `standard`, `depth` | `internal` (default true), `modelled`, `hand` |
| `fillet` | `edges`, `radius` | `on_failure: skip` |
| `chamfer` | `edges`, `distance` | `on_failure: skip` |

Two things that catch people:

- **A pocket cuts from its own sketch plane.** There is no "start from this
  face" option. Put the sketch on the plane the cut enters through. Cutting
  `-normal` from the plane a pad grew `+normal` from drills away into empty space —
  the system refuses, but you have lost a round trip.
- **`direction` is always explicit and never inferred.** `+normal` follows the
  sketch datum's normal; `-normal` opposes it.

Threads are cosmetic by default: the hole is drilled at tap-drill size and the
designation is a note, which is what a machinist wants. Set `"modelled": true`
for real helical geometry, or `"modelled": "export"` to skip it on screen but
include it in the STL — a printed thread needs the geometry, and cutting it
costs seconds.

## Selectors — the part that makes this different

Faces have stable names derived from how they came to exist:

```
shell/cap+                     the far cap of pad 'shell'
shell/side[base_outer.e0]      the side swept from curve e0
cavity/floor                   the bottom of pocket 'cavity'
cavity/wall[base_cavity.e1]    a wall of that pocket
round/fillet[a ^ b]            the fillet that replaced the edge between a and b
```

An **edge** is named by the two faces it separates, written `a ^ b`. That is why
a fillet can be stated once and survive a rebuild:

```json
{"id": "soften", "type": "fillet", "radius": 2,
 "edges": "shell/cap+ ^ shell/side[*]"}
```

That means "every edge between the top cap and any side" — the whole top
perimeter, stated once, re-resolved on every rebuild. Syntax:

| form | meaning |
|---|---|
| `a ^ b` | edges between two face patterns |
| `a ^ */*` | between a and *anything* — use this when blends may have added faces |
| `a, b` | union (looser than `^`, so `a ^ b, c ^ d` is two selectors) |
| `*` | wildcard in any position |
| `dir=\|z` | keep only edges parallel to world Z |
| `dir=+z` | keep only faces whose normal points along world Z |

**Check a selector before you commit it:**

```
POST /api/projects/{id}/resolve
{"selector": "shell/cap+ ^ shell/side[*]"}
→ {"ok": true, "count": 4, "matched": ["shell/cap+ ^ shell/side[base_outer.e0]", ...]}
```

`GET /api/projects/{id}/topology` lists every tag that currently exists. When a
selector fails, that is the first thing to read.

### Blend order matters

A chamfer that meets a previously filleted edge **at a shared corner** makes the
kernel run the transition down the third edge at that corner — usually all the
way to the bottom of the part. Do chamfers first, then fillets, and they stay
where you put them.

## Worked example: a two-part enclosure

This is a real, built, verified part — an ESP-01 case whose base and lid slip
together. Copy it and change the four numbers at the top.

```json
POST /api/projects
{
  "id": "esp01", "name": "ESP-01 case",
  "document": {
    "schema": "cadsheet/1",
    "parameters": [
      {"name": "board_w", "value": 24.8, "group": "Board"},
      {"name": "board_l", "value": 14.5, "group": "Board"},
      {"name": "board_h", "value": 13.0, "group": "Board", "doc": "tallest component"},

      {"name": "wall",  "value": 1.6, "group": "Case"},
      {"name": "gap",   "value": 0.6, "group": "Case", "doc": "clearance around the board"},
      {"name": "fit",   "value": 0.2, "group": "Case", "doc": "lip-to-wall slip fit"},
      {"name": "lip_h", "value": 3.0, "group": "Case"},

      {"name": "cav_w",   "expr": "board_w + 2 * gap",         "group": "Derived"},
      {"name": "cav_l",   "expr": "board_l + 2 * gap",         "group": "Derived"},
      {"name": "cav_h",   "expr": "board_h + gap + lip_h",     "group": "Derived"},
      {"name": "outer_w", "expr": "cav_w + 2 * wall",          "group": "Derived"},
      {"name": "outer_l", "expr": "cav_l + 2 * wall",          "group": "Derived"},
      {"name": "base_h",  "expr": "cav_h + wall",              "group": "Derived"}
    ],
    "datums": {
      "cavity_top": {"type": "plane", "parent": "xy",
                     "origin": [0, 0, "base_h"], "normal": [0, 0, 1]}
    },
    "sketches": {
      "base_outer":  {"plane": "xy", "...": "rectangle 0,0 to outer_w,outer_l"},
      "base_cavity": {"plane": "cavity_top", "...": "rectangle wall,wall size cav_w,cav_l"},
      "lid_plate":   {"plane": "xy", "...": "rectangle 0,0 to outer_w,outer_l"},
      "lid_lip":     {"plane": "xy", "...": "rectangle wall+fit,wall+fit size cav_w-2*fit,cav_l-2*fit"}
    },
    "bodies": [
      {"id": "base", "features": [
        {"id": "shell",  "type": "pad",    "profile": "base_outer.outer",
         "length": "base_h", "direction": "+normal"},
        {"id": "cavity", "type": "pocket", "profile": "base_cavity.outer",
         "depth": "cav_h", "direction": "-normal"},
        {"id": "base_round", "type": "fillet", "radius": 1.5,
         "edges": "shell/side[*] dir=|z"}
      ]},
      {"id": "lid",
       "placement": {"origin": ["outer_w + 10", 0, 0], "rotation": [0, 0, 0]},
       "features": [
        {"id": "plate", "type": "pad", "profile": "lid_plate.outer",
         "length": "wall", "direction": "+normal"},
        {"id": "lip",   "type": "pad", "profile": "lid_lip.outer",
         "length": "lip_h", "direction": "-normal"},
        {"id": "lid_round", "type": "fillet", "radius": 1.5,
         "edges": "plate/side[*] dir=|z"}
      ]}
    ]
  }
}
```

Read what it teaches:

1. **Measured values and design choices are separate groups**, and everything
   else is derived. Change `board_w` and the cavity, the shell, the lid and the
   lip all move together.
2. **`cav_h` includes `lip_h`.** The lip hangs into the cavity, so without that
   term it steals the board's headroom — the model would build happily and the
   board would not fit.
3. **The lip pads `-normal`**, hanging below the lid's plate. Padding it
   `+normal` puts it inside the plate, and the two coplanar faces cannot be told
   apart.
4. **The two parts are separate bodies.** Bodies are never fused with each
   other, which is what lets them be separate printed parts — and later, an
   assembly. `placement` moves one aside for viewing; it does not affect the
   geometry.
5. **Each body's features are its own.** A second pad in the same body fuses
   onto what is there.

## A pattern for each thing you will be asked for

**A screw post** — a pad up from the floor, then a tapped hole down its middle.

```json
{"id": "post", "type": "pad", "profile": "posts.p1", "length": "post_h"},
{"id": "post_tap", "type": "thread", "at": "posts.centre1", "standard": "M2",
 "depth": "post_h - 1", "direction": "-normal"}
```

**A connector cutout** — a rectangle sketched on the wall it passes through.
Derive the datum from that wall rather than computing it:
`POST /datums/for-face {"tag": "shell/side[base_outer.e0]"}`, then pocket
`-normal` through `wall`.

**Board standoffs** — pads of `standoff_h` at the mounting-hole positions, with
the positions themselves parameters taken from the board's datasheet.

**Ventilation** — a row of slots is several pockets, or one sketch with several
loops. Keep the pitch a parameter so the count and spacing stay consistent.

**Rounded outside** — `fillet` on `"<pad>/side[*] dir=|z"` catches the four
upright corners and nothing else.

## Check your work

```
POST /api/projects/{id}/recompute
```

returns `ok`, and per feature a `status` (`built`, `cached`, `failed`,
`skipped`) with a structured error. **Read this after every edit.** A feature
that failed leaves the ones after it skipped.

```
GET /api/projects/{id}/topology    every face and edge tag that exists now
GET /api/projects/{id}/mesh        triangles, if you need to reason about size
```

## Getting it out

```
GET /api/projects/{id}/export?fmt=stl                 print it — every body
GET /api/projects/{id}/export?fmt=stl&body=lid        one part on its own
GET /api/projects/{id}/export?fmt=step&body=lid       hand it to another CAD system
GET /api/projects/{id}/export?fmt=csv                 the parameter sheet
POST /api/projects/{id}/import                        and back again
GET /api/projects/{id}/export/views?views=top,front,right    orthographic sections
GET /api/projects/{id}/export/cut?selector=lid/cap%2B         cut path of named faces
GET /api/projects/{id}/export/flat         every flat face, laid out
GET /api/projects/{id}/export/jointed?thickness=3            the same, finger-jointed
GET /api/projects/{id}/export/enclosure?thickness=3          a laser-cut box around it
```

`fit=outer` (the default) on `jointed` means the assembled part measures what
the model measures.

The sheet round-trips: export the CSV, edit it in a spreadsheet, `POST` it back
as `{"format": "csv", "body": "<the csv>"}`. Only parameters are touched — the
datums, sketches and history are left alone, so a round trip cannot damage the
parts a spreadsheet has no way to represent. A bad file is rejected whole, with
the row number; nothing is half-applied.

**Multi-part models: export each body separately.** Without `body=` the mesh
holds every body at its placement, which is right for looking at the assembly
and wrong for a print bed. `body=` gives you one part, named
`<project>-<body>.stl`. STEP holds a single solid, so it *requires* `body=` once
there is more than one and tells you the names.

## When it refuses

It refuses rather than guessing, and the message says what to do. The common
ones:

| message | what it means |
|---|---|
| `resolved to nothing (found 0)` | a selector matches no face. `GET /topology` and compare. |
| `expected 4, resolved 6` | the model changed shape under a selector. Narrow it, or accept the new count. |
| `the pocket removes no material` | almost always `direction`. The sketch is on the far side of the material. |
| `cannot deterministically order N fragments` | two faces are coincident, usually a pad grown inside another. |
| `produced N face(s) at a corner` | a blend meets another blend at a shared vertex. Chamfer before filleting. |
| `duplicate feature id` | ids are unique across the document, not per body. |

A refusal is information. It names the feature, the selector and the reason —
act on it rather than retrying the same call.
"""


def guide_markdown() -> str:
    return GUIDE
