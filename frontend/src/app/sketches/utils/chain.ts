/**
 * The chain model: a sketch as a sequence of "get to here, this way".
 *
 * Authoring a profile as three disconnected tables — points, then curves that
 * reference them, then a loop that references those — is a lot of bookkeeping
 * for what is really one continuous act of drawing. A chain collapses it: each
 * row is a destination and how you got there, and the points, curves and loop
 * are generated from it.
 *
 * This is a *view* over the document, not a new document format. The stored
 * sketch stays explicit points/curves/loops, which is what keeps it diffable
 * and what the naming engine roots face tags in. Chains are generated on save
 * and inferred on load, and a sketch that is not a single ordered loop simply
 * falls back to the tables.
 */

import type { SketchCurvePayload, SketchPayload } from '../../core/models/cad.models';

export type Join = 'line' | 'arc' | 'none';

export interface ChainRow {
  /** Destination, as a number or an expression. */
  u: string;
  v: string;
  /** How this row connects from the previous one. The first row is always 'none'. */
  join: Join;
  /** Optional curve name; blank falls back to `autoName`. */
  name: string;
  /** Arc centre, used only when `join` is 'arc'. */
  centerU: string;
  centerV: string;
  clockwise: boolean;

  /**
   * Ids allocated when the row is created and never changed afterwards.
   *
   * Deriving them from row position instead would renumber everything below an
   * inserted or deleted row, so `outline.c2` would start naming a different
   * curve — and every face tag and selector built on it would quietly move.
   * That is the exact failure this project exists to prevent, so generated
   * names are as immutable as typed ones.
   */
  pointId: string;
  autoName: string;
}

export interface Chain {
  rows: ChainRow[];
  /** How the last row joins back to the first; 'none' leaves the profile open. */
  close: Join;
  closeName: string;
  closeAutoName: string;
  closeCenterU: string;
  closeCenterV: string;
  closeClockwise: boolean;
}

export function emptyRow(join: Join, pointId: string, autoName: string): ChainRow {
  return {
    u: '0',
    v: '0',
    join,
    name: '',
    centerU: '0',
    centerV: '0',
    clockwise: false,
    pointId,
    autoName,
  };
}

/** The lowest `<prefix><n>` not already taken, so ids are never reused. */
export function nextId(prefix: string, taken: Iterable<string>): string {
  const used = new Set(taken);
  for (let index = 0; ; index++) {
    const candidate = `${prefix}${index}`;
    if (!used.has(candidate)) return candidate;
  }
}

export function usedPointIds(chain: Chain): string[] {
  return chain.rows.map((row) => row.pointId);
}

export function usedCurveIds(chain: Chain): string[] {
  return [
    ...chain.rows.map((row) => row.name.trim() || row.autoName),
    chain.closeName.trim() || chain.closeAutoName,
  ].filter(Boolean);
}

export function emptyChain(): Chain {
  return {
    // The first row never produces a curve — nothing arrives at it except the
    // closing segment — so it holds no generated curve name of its own.
    rows: [emptyRow('none', 'p0', '')],
    close: 'line',
    closeName: '',
    // One convention for every generated id: `cN`, allocated from the same
    // pool as the rest so it can never collide and never has to change.
    closeAutoName: 'c0',
    closeCenterU: '0',
    closeCenterV: '0',
    closeClockwise: false,
  };
}

// --------------------------------------------------------------------------
// Chain -> document
// --------------------------------------------------------------------------

/**
 * Generate the stored form: named points, curves between them, and one loop.
 *
 * Points are `p0`, `p1`, … and curves `c0`, `c1`, … — one convention, whether
 * an id was generated or typed, and identical to what the tables view
 * allocates. Names matter because a curve id ends up inside every face tag it
 * produces, so naming the two edges you intend to fillet is worth the
 * keystrokes even when the rest stay generated.
 */
export function chainToSketch(id: string, plane: string, chain: Chain): SketchPayload {
  const points: Record<string, (number | string)[]> = {};
  const curves: SketchCurvePayload[] = [];
  const loopCurves: string[] = [];

  chain.rows.forEach((row) => {
    points[row.pointId] = [numeric(row.u), numeric(row.v)];
  });

  const addSegment = (
    from: string,
    to: string,
    join: Join,
    name: string,
    autoName: string,
    centerU: string,
    centerV: string,
    clockwise: boolean,
  ): void => {
    if (join === 'none') return;
    const curveId = name.trim() || autoName;

    if (join === 'arc') {
      const centreId = `${curveId}_c`;
      points[centreId] = [numeric(centerU), numeric(centerV)];
      curves.push({
        id: curveId,
        type: 'arc',
        start: from,
        end: to,
        center: centreId,
        clockwise,
      });
    } else {
      curves.push({ id: curveId, type: 'line', start: from, end: to });
    }
    loopCurves.push(curveId);
  };

  chain.rows.forEach((row, index) => {
    if (index === 0) return;
    addSegment(
      chain.rows[index - 1].pointId, row.pointId, row.join,
      row.name, row.autoName, row.centerU, row.centerV, row.clockwise,
    );
  });

  if (chain.rows.length > 1) {
    addSegment(
      chain.rows[chain.rows.length - 1].pointId, chain.rows[0].pointId,
      chain.close, chain.closeName, chain.closeAutoName,
      chain.closeCenterU, chain.closeCenterV, chain.closeClockwise,
    );
  }

  // A loop only means anything once the profile closes; an open chain is still
  // a legitimate thing to be drawing, it just cannot be extruded yet.
  const closed = chain.close !== 'none' && loopCurves.length >= 3;
  return {
    id,
    plane,
    points,
    curves,
    loops: closed ? [{ id: 'outer', curves: loopCurves }] : [],
  };
}

// --------------------------------------------------------------------------
// Document -> chain
// --------------------------------------------------------------------------

/**
 * Recover a chain from a stored sketch, or null when it is not one.
 *
 * Returns null for anything the chain cannot faithfully represent — several
 * loops, curves outside the loop, a run that does not join end-to-start — so
 * the editor can fall back to the tables rather than silently discarding
 * geometry it failed to understand.
 */
export function sketchToChain(sketch: {
  points?: Record<string, unknown[]>;
  curves?: SketchCurvePayload[];
  loops?: { id: string; curves: string[] }[];
}): Chain | null {
  const curves = sketch.curves ?? [];
  const loops = sketch.loops ?? [];
  const points = sketch.points ?? {};
  if (loops.length !== 1 || curves.length === 0) return null;

  const byId = new Map(curves.map((c) => [c.id, c]));
  const ordered = loops[0].curves.map((id) => byId.get(id));
  if (ordered.some((c) => !c)) return null;

  const chainCurves = ordered as SketchCurvePayload[];
  // Every curve must hand over to the next, or this is not a single run.
  for (let i = 0; i < chainCurves.length; i++) {
    const next = chainCurves[(i + 1) % chainCurves.length];
    if (chainCurves[i].end !== next.start) return null;
  }
  if (chainCurves.some((c) => c.type === 'circle')) return null;

  const rows: ChainRow[] = [];
  const at = (id: string | undefined): [string, string] => {
    const value = id ? points[id] : undefined;
    return [String(value?.[0] ?? 0), String(value?.[1] ?? 0)];
  };

  chainCurves.forEach((curve, index) => {
    const [u, v] = at(curve.start);
    const previous = chainCurves[(index - 1 + chainCurves.length) % chainCurves.length];
    const [cu, cv] = at(previous.center);
    rows.push({
      u,
      v,
      join: index === 0 ? 'none' : (previous.type as Join),
      // The stored id is kept as the typed name so a round trip through the
      // editor never renames a curve, and never moves a face tag.
      name: index === 0 ? '' : previous.id,
      centerU: cu,
      centerV: cv,
      clockwise: previous.clockwise ?? false,
      pointId: curve.start ?? `p${index}`,
      autoName: index === 0 ? '' : previous.id,
    });
  });

  const last = chainCurves[chainCurves.length - 1];
  const [closeU, closeV] = at(last.center);
  return {
    rows,
    close: last.type as Join,
    closeName: last.id,
    closeAutoName: last.id,
    closeCenterU: closeU,
    closeCenterV: closeV,
    closeClockwise: last.clockwise ?? false,
  };
}

// --------------------------------------------------------------------------
// Undefined parameters
// --------------------------------------------------------------------------

const IDENTIFIER = /[A-Za-z_][A-Za-z0-9_]*/g;

/**
 * Names a chain refers to that are not parameters, functions or constants.
 *
 * Typing `plate_w` before that parameter exists is the natural order of work —
 * you know the shape before you have named every dimension — so the editor
 * offers to create what is missing rather than making you leave and come back.
 */
export function undefinedNames(
  chain: Chain,
  known: { parameters: string[]; functions: string[]; constants: string[] },
): string[] {
  const defined = new Set([...known.parameters, ...known.functions, ...known.constants]);
  const found = new Set<string>();

  const scan = (text: string): void => {
    for (const match of text.matchAll(IDENTIFIER)) {
      const name = match[0];
      // A name directly followed by '(' is a call, not a parameter.
      const after = text.slice(match.index + name.length).trimStart();
      if (after.startsWith('(')) continue;
      if (!defined.has(name)) found.add(name);
    }
  };

  for (const row of chain.rows) {
    scan(row.u);
    scan(row.v);
    if (row.join === 'arc') {
      scan(row.centerU);
      scan(row.centerV);
    }
  }
  if (chain.close === 'arc') {
    scan(chain.closeCenterU);
    scan(chain.closeCenterV);
  }
  return [...found].sort();
}

/** A cell that parses as a number is a literal; anything else is an expression. */
export function numeric(raw: string): number | string {
  const trimmed = raw.trim();
  const value = Number(trimmed);
  return trimmed !== '' && Number.isFinite(value) ? value : trimmed;
}
