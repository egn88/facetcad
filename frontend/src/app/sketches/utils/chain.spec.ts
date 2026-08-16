/**
 * The chain model's correctness guarantee.
 *
 * The tests that matter most are the stability ones. A generated curve id ends
 * up inside every face tag it produces, so if editing a chain renumbers ids,
 * `base/side[outline.c2]` silently starts naming a different face and every
 * selector built on it moves with it. That is the precise failure this whole
 * project exists to prevent, so it is pinned here rather than left to review.
 */

import { describe, expect, it } from 'vitest';

import {
  type Chain,
  chainToSketch,
  emptyChain,
  emptyRow,
  nextId,
  sketchToChain,
  undefinedNames,
  usedCurveIds,
  usedPointIds,
} from './chain';

/** A closed triangle with generated ids, as the editor would build it. */
function triangle(): Chain {
  const chain = emptyChain();
  chain.rows = [
    // Row 0 owns no curve: nothing arrives at it but the closing segment.
    { ...emptyRow('none', 'p0', ''), u: '0', v: '0' },
    { ...emptyRow('line', 'p1', 'c1'), u: 'w', v: '0' },
    { ...emptyRow('line', 'p2', 'c2'), u: 'w', v: 'h' },
  ];
  return chain;
}

const VOCABULARY = {
  parameters: ['w', 'h'],
  functions: ['min', 'max', 'sqrt'],
  constants: ['pi', 'e'],
};

// --------------------------------------------------------------------------
// Generation
// --------------------------------------------------------------------------

describe('chainToSketch', () => {
  it('generates points, curves and a closing loop', () => {
    const sketch = chainToSketch('outline', 'base', triangle());

    expect(Object.keys(sketch.points)).toEqual(['p0', 'p1', 'p2']);
    expect(sketch.curves.map((c) => c.id)).toEqual(['c1', 'c2', 'c0']);
    expect(sketch.loops).toEqual([{ id: 'outer', curves: ['c1', 'c2', 'c0'] }]);
  });

  it('joins each row to the one before it', () => {
    const sketch = chainToSketch('outline', 'base', triangle());
    expect(sketch.curves[0]).toMatchObject({ start: 'p0', end: 'p1' });
    expect(sketch.curves[1]).toMatchObject({ start: 'p1', end: 'p2' });
    expect(sketch.curves[2]).toMatchObject({ start: 'p2', end: 'p0' });
  });

  it('keeps coordinates as expressions when they are not numbers', () => {
    const sketch = chainToSketch('outline', 'base', triangle());
    expect(sketch.points['p1']).toEqual(['w', 0]);
    expect(sketch.points['p2']).toEqual(['w', 'h']);
  });

  it('prefers a typed name over the generated one', () => {
    const chain = triangle();
    chain.rows[1].name = 'bottom';
    chain.closeName = 'left';

    const sketch = chainToSketch('outline', 'base', chain);
    expect(sketch.curves.map((c) => c.id)).toEqual(['bottom', 'c2', 'left']);
    expect(sketch.loops[0].curves).toEqual(['bottom', 'c2', 'left']);
  });

  it('gives an arc its own centre point, named after the curve', () => {
    const chain = triangle();
    chain.rows[2].join = 'arc';
    chain.rows[2].name = 'roundtop';
    chain.rows[2].centerU = 'w / 2';
    chain.rows[2].centerV = 'h';

    const sketch = chainToSketch('outline', 'base', chain);
    const arc = sketch.curves.find((c) => c.id === 'roundtop');
    expect(arc).toMatchObject({ type: 'arc', center: 'roundtop_c' });
    expect(sketch.points['roundtop_c']).toEqual(['w / 2', 'h']);
  });

  it('leaves an unclosed chain without a loop', () => {
    const chain = triangle();
    chain.close = 'none';
    const sketch = chainToSketch('outline', 'base', chain);
    expect(sketch.loops).toEqual([]);
    expect(sketch.curves).toHaveLength(2);
  });

  it('does not invent a loop from fewer than three curves', () => {
    const chain = emptyChain();
    chain.rows = [emptyRow('none', 'p0', ''), emptyRow('line', 'p1', 'c1')];
    expect(chainToSketch('s', 'base', chain).loops).toEqual([]);
  });
});

// --------------------------------------------------------------------------
// Stability — the guarantee that matters
// --------------------------------------------------------------------------

describe('generated ids are stable', () => {
  it('does not renumber remaining rows when one is deleted', () => {
    const chain = triangle();
    chain.rows.push({ ...emptyRow('line', 'p3', 'c3'), u: '0', v: 'h' });
    const before = chainToSketch('outline', 'base', chain).curves.map((c) => c.id);
    expect(before).toEqual(['c1', 'c2', 'c3', 'c0']);

    // Delete the middle row, exactly as the editor's × button does.
    chain.rows = chain.rows.filter((_, index) => index !== 2);

    const after = chainToSketch('outline', 'base', chain).curves.map((c) => c.id);
    // c2 is gone; c3 must still be c3, or every tag naming it would move.
    expect(after).toEqual(['c1', 'c3', 'c0']);
  });

  it('keeps point ids attached to their row across a deletion', () => {
    const chain = triangle();
    chain.rows = chain.rows.filter((_, index) => index !== 1);
    const sketch = chainToSketch('outline', 'base', chain);
    expect(Object.keys(sketch.points)).toEqual(['p0', 'p2']);
  });

  it('never reuses an id that is still taken', () => {
    // [p0, p1, p2] minus p1 has length 2, so a length-based scheme would
    // hand out p2 again and produce a duplicate.
    expect(nextId('p', ['p0', 'p2'])).toBe('p1');
    expect(nextId('p', ['p0', 'p1', 'p2'])).toBe('p3');
    expect(nextId('c', [])).toBe('c0');
  });

  it('allocates around names the user typed', () => {
    const chain = triangle();
    chain.rows[1].name = 'c2'; // a typed name that collides with the next auto id
    expect(usedCurveIds(chain)).toContain('c2');
    expect(nextId('c', usedCurveIds(chain))).not.toBe('c2');
  });

  it('uses one id convention for generated and typed names alike', () => {
    const sketch = chainToSketch('outline', 'base', triangle());
    for (const curve of sketch.curves) {
      expect(curve.id).toMatch(/^c\d+$/);
    }
    for (const point of Object.keys(sketch.points)) {
      expect(point).toMatch(/^p\d+$/);
    }
  });

  it('reports the ids currently in use', () => {
    expect(usedPointIds(triangle())).toEqual(['p0', 'p1', 'p2']);
    expect(usedCurveIds(triangle())).toEqual(['c1', 'c2', 'c0']);
  });
});

// --------------------------------------------------------------------------
// Round trip
// --------------------------------------------------------------------------

describe('sketchToChain', () => {
  it('recovers a chain without renaming anything', () => {
    const original = triangle();
    original.rows[1].name = 'bottom';
    const sketch = chainToSketch('outline', 'base', original);

    const recovered = sketchToChain(sketch);
    expect(recovered).not.toBeNull();

    // Saving what was just loaded must produce an identical document.
    const again = chainToSketch('outline', 'base', recovered!);
    expect(again.curves.map((c) => c.id)).toEqual(sketch.curves.map((c) => c.id));
    expect(again.loops).toEqual(sketch.loops);
  });

  it('survives an arc round trip', () => {
    const chain = triangle();
    chain.rows[2].join = 'arc';
    chain.rows[2].centerU = '5';
    chain.rows[2].centerV = '5';
    const sketch = chainToSketch('outline', 'base', chain);

    const again = chainToSketch('outline', 'base', sketchToChain(sketch)!);
    expect(again.curves.map((c) => [c.id, c.type])).toEqual(
      sketch.curves.map((c) => [c.id, c.type]),
    );
  });

  it('refuses a sketch with several loops', () => {
    const sketch = chainToSketch('outline', 'base', triangle());
    sketch.loops.push({ id: 'inner', curves: ['c1'] });
    expect(sketchToChain(sketch)).toBeNull();
  });

  it('refuses a circle, which is a loop on its own', () => {
    expect(
      sketchToChain({
        points: { m: [0, 0] },
        curves: [{ id: 'rim', type: 'circle', center: 'm', radius: 5 }],
        loops: [{ id: 'outer', curves: ['rim'] }],
      }),
    ).toBeNull();
  });

  it('refuses a run whose curves do not join end-to-start', () => {
    const sketch = chainToSketch('outline', 'base', triangle());
    sketch.curves[1].start = 'p0'; // break the hand-over
    expect(sketchToChain(sketch)).toBeNull();
  });

  it('refuses a sketch with no loop at all', () => {
    expect(sketchToChain({ points: {}, curves: [], loops: [] })).toBeNull();
  });
});

// --------------------------------------------------------------------------
// Undefined parameters
// --------------------------------------------------------------------------

describe('undefinedNames', () => {
  it('finds names that are not parameters', () => {
    const chain = triangle();
    chain.rows[1].u = 'wedge_w';
    expect(undefinedNames(chain, VOCABULARY)).toEqual(['wedge_w']);
  });

  it('ignores parameters that already exist', () => {
    expect(undefinedNames(triangle(), VOCABULARY)).toEqual([]);
  });

  it('does not mistake a function call for a missing parameter', () => {
    const chain = triangle();
    chain.rows[1].u = 'max(w, 10)';
    expect(undefinedNames(chain, VOCABULARY)).toEqual([]);
  });

  it('does not mistake a constant for a missing parameter', () => {
    const chain = triangle();
    chain.rows[1].u = 'pi * 2';
    expect(undefinedNames(chain, VOCABULARY)).toEqual([]);
  });

  it('only looks at an arc centre when the join is an arc', () => {
    const chain = triangle();
    chain.rows[2].centerU = 'ghost';
    expect(undefinedNames(chain, VOCABULARY)).toEqual([]);

    chain.rows[2].join = 'arc';
    expect(undefinedNames(chain, VOCABULARY)).toEqual(['ghost']);
  });

  it('reports each missing name once, sorted', () => {
    const chain = triangle();
    chain.rows[1].u = 'zeta';
    chain.rows[1].v = 'alpha';
    chain.rows[2].u = 'zeta';
    expect(undefinedNames(chain, VOCABULARY)).toEqual(['alpha', 'zeta']);
  });
});
