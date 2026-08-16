/**
 * The sketch editor.
 *
 * A sketch is points, curves and loops, and every coordinate is a number *or an
 * expression* — which is the whole point: `plate_w / 2` is as valid as `60`, so
 * a profile follows the sheet rather than being redrawn when a dimension
 * changes.
 *
 * Editing is tabular rather than graphical on purpose. There is no constraint
 * solver to drag against, and a table is what makes a sketch readable as
 * formulas and editable from the keyboard.
 *
 * The whole sketch is sent on save, which keeps the API small and means a
 * half-applied sketch cannot exist.
 */

import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  output,
  signal,
} from '@angular/core';
import { ModalComponent } from '../../dialogs/components/dialogs.component';
import { CadApiService } from '../../core/services/cad-api.service';
import { ProjectStore } from '../../core/services/project-store';
import {
  type Chain,
  type ChainRow,
  type Join,
  chainToSketch,
  emptyChain,
  emptyRow,
  nextId,
  sketchToChain,
  undefinedNames,
  usedCurveIds,
  usedPointIds,
} from '../utils/chain';
import type { SketchCurvePayload, SketchPayload } from '../../core/models/cad.models';

interface PointRow {
  id: string;
  u: string;
  v: string;
}

interface LoopRow {
  id: string;
  curves: string;
}

@Component({
  selector: 'cad-sketch-editor',
  standalone: true,
  imports: [ModalComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <cad-modal [title]="heading()" (closed)="closed.emit()">
      <div class="row">
        <div class="field narrow">
          <label>Sketch id</label>
          <input
            [value]="id()" (input)="id.set($any($event.target).value)"
            [disabled]="existing()"
            spellcheck="false"
            placeholder="outline"
          />
        </div>
        <div class="field narrow">
          <label>Datum plane</label>
          <select [value]="plane()" (change)="plane.set($any($event.target).value)">
            @for (option of store.planeOptions(); track option) {
              <option [value]="option">{{ option }}</option>
            }
          </select>
        </div>
      </div>

      <div class="modes">
        <button [class.active]="mode() === 'chain'" (click)="mode.set('chain')">Chain</button>
        <button [class.active]="mode() === 'tables'" (click)="mode.set('tables')">Tables</button>
        @if (mode() === 'chain' && !chainable()) {
          <span class="warn">
            Not a single closed run — showing the tables instead.
          </span>
        }
      </div>

      @if (mode() === 'chain' && chainable()) {
        <!-- An explicit way in, since reacting to an unknown name only helps
             once you already know that is how it works. -->
        <div class="param-bar">
          <button (click)="showNewParam.set(!showNewParam())">
            {{ showNewParam() ? 'Cancel' : '+ parameter' }}
          </button>
          @if (showNewParam()) {
            <input
              class="pname"
              placeholder="name"
              spellcheck="false"
              [value]="newParamName()"
              (input)="newParamName.set($any($event.target).value)"
            />
            <input
              class="pvalue"
              placeholder="value or expression"
              spellcheck="false"
              [value]="newParamValue()"
              (input)="newParamValue.set($any($event.target).value)"
            />
            <button
              class="primary"
              [disabled]="!newParamName()"
              (click)="createNamedParameter()"
            >
              Add
            </button>
          } @else {
            <span class="muted-hint">
              or just type a name like <code>plate_w</code> into a cell and it will offer
              to create it
            </span>
          }
        </div>

        @if (missing().length > 0) {
          <div class="missing">
            <span class="label">Not defined yet:</span>
            @for (name of missing(); track name) {
              <span class="chip">
                <code>{{ name }}</code>
                <input
                  class="tiny"
                  placeholder="value"
                  [value]="draftValues()[name] ?? ''"
                  (input)="setDraft(name, $any($event.target).value)"
                />
                <button (click)="createParameter(name)">create</button>
              </span>
            }
          </div>
        }

        <div class="section">
          <div class="section-head">
            <span>Chain — each row is where to go, and how</span>
            <span class="spacer"></span>
            <button (click)="addRow('line')">+ line</button>
            <button (click)="addRow('arc')">+ arc</button>
          </div>
          <table class="grid">
            <thead>
              <tr>
                <th style="width:9%">from</th>
                <th style="width:17%">u</th>
                <th style="width:17%">v</th>
                <th style="width:12%">join</th>
                <th style="width:16%">arc centre</th>
                <th style="width:17%">name</th>
                <th style="width:6%"></th>
              </tr>
            </thead>
            <tbody>
              @for (row of chainRows(); track $index) {
                <tr>
                  <td class="muted">
                    {{ $index === 0 ? 'start' : chainRows()[$index - 1].pointId }}
                  </td>
                  <td><input [value]="row.u" (input)="setRow($index, 'u', $any($event.target).value)" /></td>
                  <td><input [value]="row.v" (input)="setRow($index, 'v', $any($event.target).value)" /></td>
                  <td>
                    @if ($index === 0) {
                      <span class="muted">—</span>
                    } @else {
                      <select [value]="row.join" (change)="setRow($index, 'join', $any($event.target).value)">
                        <option value="line">line</option>
                        <option value="arc">arc</option>
                        <option value="none">none</option>
                      </select>
                    }
                  </td>
                  <td>
                    @if (row.join === 'arc' && $index > 0) {
                      <span class="pair">
                        <input [value]="row.centerU" (input)="setRow($index, 'centerU', $any($event.target).value)" />
                        <input [value]="row.centerV" (input)="setRow($index, 'centerV', $any($event.target).value)" />
                      </span>
                    }
                  </td>
                  <td>
                    @if ($index > 0) {
                      <input
                        [value]="row.name"
                        [placeholder]="row.autoName"
                        (input)="setRow($index, 'name', $any($event.target).value)"
                      />
                    }
                  </td>
                  <td>
                    @if ($index > 0) {
                      <button (click)="removeRow($index)">×</button>
                    }
                  </td>
                </tr>
              }
              <tr class="close-row">
                <td class="muted">close</td>
                <td class="muted" colspan="2">back to the first point</td>
                <td>
                  <select [value]="chain().close" (change)="setClose('close', $any($event.target).value)">
                    <option value="line">line</option>
                    <option value="arc">arc</option>
                    <option value="none">leave open</option>
                  </select>
                </td>
                <td>
                  @if (chain().close === 'arc') {
                    <span class="pair">
                      <input [value]="chain().closeCenterU" (input)="setClose('closeCenterU', $any($event.target).value)" />
                      <input [value]="chain().closeCenterV" (input)="setClose('closeCenterV', $any($event.target).value)" />
                    </span>
                  }
                </td>
                <td>
                  <input
                    [value]="chain().closeName"
                    [placeholder]="chain().closeAutoName"
                    (input)="setClose('closeName', $any($event.target).value)"
                  />
                </td>
                <td></td>
              </tr>
            </tbody>
          </table>
          <div class="hint">
            Coordinates may be expressions — <code>plate_w / 2</code> is as valid as
            <code>60</code>. Names are optional, but a named segment reads better in
            face tags later, so name the edges you expect to fillet.
          </div>
        </div>
      }

      @if (mode() === 'tables' || !chainable()) {
      <!-- points -->
      <div class="section">
        <div class="section-head">
          <span>Points</span>
          <span class="spacer"></span>
          <button (click)="addPoint()">+ point</button>
        </div>
        <table class="grid">
          <thead>
            <tr>
              <th style="width:26%">id</th>
              <th style="width:33%">u</th>
              <th style="width:33%">v</th>
              <th style="width:8%"></th>
            </tr>
          </thead>
          <tbody>
            @for (point of points(); track $index) {
              <tr>
                <td><input [value]="point.id" (input)="setPoint($index, 'id', $any($event.target).value)" /></td>
                <td><input [value]="point.u" (input)="setPoint($index, 'u', $any($event.target).value)" /></td>
                <td><input [value]="point.v" (input)="setPoint($index, 'v', $any($event.target).value)" /></td>
                <td><button (click)="removePoint($index)">×</button></td>
              </tr>
            }
            @if (points().length === 0) {
              <tr><td colspan="4" class="empty-cell">No points yet.</td></tr>
            }
          </tbody>
        </table>
      </div>

      <!-- curves -->
      <div class="section">
        <div class="section-head">
          <span>Curves</span>
          <span class="spacer"></span>
          <button (click)="addCurve('line')">+ line</button>
          <button (click)="addCurve('arc')">+ arc</button>
          <button (click)="addCurve('circle')">+ circle</button>
        </div>
        <table class="grid">
          <thead>
            <tr>
              <th style="width:20%">id</th>
              <th style="width:14%">type</th>
              <th style="width:16%">start</th>
              <th style="width:16%">end</th>
              <th style="width:14%">center</th>
              <th style="width:14%">radius</th>
              <th style="width:6%"></th>
            </tr>
          </thead>
          <tbody>
            @for (curve of curves(); track $index) {
              <tr>
                <td><input [value]="curve.id" (input)="setCurve($index, 'id', $any($event.target).value)" /></td>
                <td>
                  <select [value]="curve.type" (change)="setCurve($index, 'type', $any($event.target).value)">
                    <option value="line">line</option>
                    <option value="arc">arc</option>
                    <option value="circle">circle</option>
                  </select>
                </td>
                <td>
                  <input
                    [value]="curve.start ?? ''" (input)="setCurve($index, 'start', $any($event.target).value)"
                    [disabled]="curve.type === 'circle'"
                  />
                </td>
                <td>
                  <input
                    [value]="curve.end ?? ''" (input)="setCurve($index, 'end', $any($event.target).value)"
                    [disabled]="curve.type === 'circle'"
                  />
                </td>
                <td>
                  <input
                    [value]="curve.center ?? ''" (input)="setCurve($index, 'center', $any($event.target).value)"
                    [disabled]="curve.type === 'line'"
                  />
                </td>
                <td>
                  <input
                    [value]="curve.radius ?? ''" (input)="setCurve($index, 'radius', $any($event.target).value)"
                    [disabled]="curve.type !== 'circle'"
                    title="Circles only — an arc takes its radius from its centre"
                  />
                </td>
                <td><button (click)="removeCurve($index)">×</button></td>
              </tr>
            }
            @if (curves().length === 0) {
              <tr><td colspan="7" class="empty-cell">No curves yet.</td></tr>
            }
          </tbody>
        </table>
      </div>

      <!-- loops -->
      <div class="section">
        <div class="section-head">
          <span>Loops</span>
          <span class="spacer"></span>
          <button [disabled]="curves().length === 0" (click)="addLoop()">+ loop from all curves</button>
        </div>
        <table class="grid">
          <thead>
            <tr>
              <th style="width:26%">id</th>
              <th style="width:66%">curves, in order</th>
              <th style="width:8%"></th>
            </tr>
          </thead>
          <tbody>
            @for (loop of loops(); track $index) {
              <tr>
                <td><input [value]="loop.id" (input)="setLoop($index, 'id', $any($event.target).value)" /></td>
                <td>
                  <input
                    [value]="loop.curves" (input)="setLoop($index, 'curves', $any($event.target).value)"
                    placeholder="bottom, right, top, left"
                  />
                </td>
                <td><button (click)="removeLoop($index)">×</button></td>
              </tr>
            }
            @if (loops().length === 0) {
              <tr>
                <td colspan="3" class="empty-cell">
                  A loop is what a pad or pocket extrudes.
                </td>
              </tr>
            }
          </tbody>
        </table>
      </div>

      <div class="hint">
        A loop must close: each curve's end is the next one's start. A circle closes
        on its own and forms a loop by itself.
      </div>
      }

      <ng-container footer>
        @if (error()) {
          <span class="error-text grow">{{ error() }}</span>
        }
        @if (existing()) {
          <button class="danger" [disabled]="busy()" (click)="remove()">Delete sketch</button>
        }
        <button (click)="closed.emit()">Cancel</button>
        <button class="primary" [disabled]="busy() || !id()" (click)="save()">
          Save &amp; rebuild
        </button>
      </ng-container>
    </cad-modal>
  `,
  styles: [
    `
      .row {
        display: flex;
        gap: 10px;
      }
      .narrow {
        width: 180px;
      }
      .spacer {
        flex: 1;
      }
      .section {
        margin-top: 14px;
      }
      .section-head {
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        color: var(--text-dim);
        margin-bottom: 4px;
      }
      table.grid {
        width: 100%;
        border-collapse: collapse;
        font-family: var(--mono);
        font-size: 12px;
      }
      table.grid th {
        text-align: left;
        font-weight: 500;
        font-size: 10px;
        color: var(--text-faint);
        padding: 2px 4px;
        border-bottom: 1px solid var(--border);
      }
      table.grid td {
        padding: 1px 2px;
      }
      table.grid input,
      table.grid select {
        padding: 2px 5px;
      }
      .empty-cell {
        color: var(--text-faint);
        padding: 6px 4px;
        font-size: 11px;
      }
      .hint {
        margin-top: 12px;
        font-size: 11px;
        color: var(--text-faint);
        line-height: 1.6;
      }
      .error-text {
        color: var(--error);
      }
      .modes {
        display: flex;
        align-items: center;
        gap: 6px;
        margin: 12px 0 4px;
      }
      .modes button.active {
        color: var(--accent);
        border-color: var(--accent-dim);
      }
      .modes .warn {
        font-size: 11px;
        color: var(--warn);
      }
      .missing {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 8px;
        padding: 6px 8px;
        margin-top: 8px;
        border: 1px solid var(--warn);
        border-radius: 4px;
        font-size: 11px;
      }
      .param-bar {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-top: 8px;
      }
      .param-bar .pname { width: 130px; }
      .param-bar .pvalue { width: 180px; }
      .muted-hint {
        font-size: 11px;
        color: var(--text-faint);
      }
      .missing .label { color: var(--warn); }
      .missing .chip { display: inline-flex; align-items: center; gap: 4px; }
      .missing input.tiny { width: 70px; }
      .muted { color: var(--text-faint); padding-left: 4px; }
      .pair { display: flex; gap: 3px; }
      tr.close-row td { border-top: 1px solid var(--border); }
      .grow {
        flex: 1;
      }
      button.danger {
        border-color: var(--error);
        color: var(--error);
      }
    `,
  ],
})
export class SketchEditorComponent {
  readonly store = inject(ProjectStore);
  private readonly api = inject(CadApiService);

  /** The sketch id to edit; omit to create a new one. */
  readonly sketchId = input<string | null>(null);
  readonly closed = output<void>();

  readonly id = signal('');
  readonly plane = signal('xy');
  readonly points = signal<PointRow[]>([]);
  readonly curves = signal<SketchCurvePayload[]>([]);
  readonly loops = signal<LoopRow[]>([]);
  readonly existing = signal(false);
  readonly mode = signal<'chain' | 'tables'>('chain');
  readonly chain = signal<Chain>(emptyChain());
  /** Null when the stored sketch is not a single closed run. */
  readonly chainable = signal(true);
  readonly vocabulary = signal<{ functions: string[]; constants: string[] }>({
    functions: [],
    constants: [],
  });
  readonly draftValues = signal<Record<string, string>>({});
  readonly showNewParam = signal(false);
  readonly newParamName = signal('');
  readonly newParamValue = signal('');
  readonly error = signal<string | null>(null);
  readonly busy = signal(false);

  readonly heading = computed(() =>
    this.existing() ? `Sketch — ${this.id()}` : 'New sketch',
  );

  readonly chainRows = computed(() => this.chain().rows);

  /**
   * Names the chain refers to that do not exist yet.
   *
   * Knowing the shape before every dimension is named is the normal order of
   * work, so these are offered for creation in place rather than sending the
   * user away to the parameter sheet and back.
   */
  readonly missing = computed(() =>
    undefinedNames(this.chain(), {
      parameters: (this.store.document()?.parameters ?? []).map((p) => p.name),
      functions: this.vocabulary().functions,
      constants: this.vocabulary().constants,
    }),
  );

  constructor() {
    queueMicrotask(async () => {
      this.load();
      try {
        const vocabulary = await this.api.expressionVocabulary();
        this.vocabulary.set(vocabulary);
      } catch {
        // Without it every identifier looks undefined, so assume none are.
        this.vocabulary.set({ functions: [], constants: [] });
      }
    });
  }

  private load(): void {
    const identifier = this.sketchId();
    const document = this.store.document();
    if (!identifier || !document) {
      this.plane.set(this.store.planeOptions()[0] ?? 'xy');
      return;
    }
    const raw = document.sketches[identifier];
    if (!raw) return;

    this.existing.set(true);
    this.id.set(identifier);
    this.plane.set(raw.plane);
    this.points.set(
      Object.entries((raw['points'] as Record<string, unknown[]>) ?? {}).map(
        ([pointId, at]) => ({
          id: pointId,
          u: String(at?.[0] ?? 0),
          v: String(at?.[1] ?? 0),
        }),
      ),
    );
    this.curves.set(
      ((raw['curves'] as SketchCurvePayload[]) ?? []).map((curve) => ({
        ...curve,
        type: curve.type ?? 'line',
      })),
    );
    this.loops.set(
      (raw.loops ?? []).map((loop) => ({ id: loop.id, curves: loop.curves.join(', ') })),
    );

    const recovered = sketchToChain({
      points: raw['points'] as Record<string, unknown[]>,
      curves: raw['curves'] as SketchCurvePayload[],
      loops: raw.loops,
    });
    if (recovered) {
      this.chain.set(recovered);
    } else {
      // Multiple loops, a circle, or a run that does not join end-to-start:
      // the chain cannot represent it faithfully, so do not pretend it can.
      this.chainable.set(false);
      this.mode.set('tables');
    }
  }

  // -- chain editing ------------------------------------------------------

  addRow(join: Join): void {
    // Ids are allocated now and kept for the row's lifetime. Deriving them from
    // position at save time would renumber every row below a deletion, moving
    // face tags that selectors already point at.
    this.chain.update((chain) => {
      const row = emptyRow(
        join,
        nextId('p', usedPointIds(chain)),
        nextId('c', usedCurveIds(chain)),
      );
      return { ...chain, rows: [...chain.rows, row] };
    });
  }

  removeRow(index: number): void {
    this.chain.update((chain) => ({
      ...chain,
      rows: chain.rows.filter((_, i) => i !== index),
    }));
  }

  setRow(index: number, field: keyof ChainRow, value: string): void {
    this.chain.update((chain) => ({
      ...chain,
      rows: chain.rows.map((row, i) => (i === index ? { ...row, [field]: value } : row)),
    }));
  }

  setClose(field: keyof Chain, value: string): void {
    this.chain.update((chain) => ({ ...chain, [field]: value }));
  }

  setDraft(name: string, value: string): void {
    this.draftValues.update((drafts) => ({ ...drafts, [name]: value }));
  }

  /** Create a parameter the user named explicitly. */
  async createNamedParameter(): Promise<void> {
    const name = this.newParamName().trim();
    if (!name) return;
    if (await this.addParameter(name, this.newParamValue())) {
      this.newParamName.set('');
      this.newParamValue.set('0');
      this.showNewParam.set(false);
    }
  }

  /** Create a missing parameter without leaving the sketch. */
  async createParameter(name: string): Promise<void> {
    await this.addParameter(name, this.draftValues()[name] ?? '');
    this.draftValues.update((drafts) => {
      const next = { ...drafts };
      delete next[name];
      return next;
    });
  }

  private addParameter(name: string, rawValue: string): Promise<boolean> {
    const raw = rawValue.trim();
    const asNumber = Number(raw);
    const isNumber = raw !== '' && Number.isFinite(asNumber);
    return this.store.addParameter({
      name,
      value: isNumber ? asNumber : undefined,
      expr: isNumber ? undefined : raw || '0',
      unit: 'mm',
      group: 'Sketch',
      doc: `added while drawing ${this.id() || 'a sketch'}`,
    });
  }

  // -- row editing --------------------------------------------------------

  addPoint(): void {
    // `p${rows.length}` would collide after a delete: [p0, p1, p2] minus p1 has
    // length 2, so the next point would also be called p2.
    this.points.update((rows) => [
      ...rows,
      { id: nextId('p', rows.map((row) => row.id)), u: '0', v: '0' },
    ]);
  }

  setPoint(index: number, field: keyof PointRow, value: string): void {
    this.points.update((rows) =>
      rows.map((row, i) => (i === index ? { ...row, [field]: value } : row)),
    );
  }

  removePoint(index: number): void {
    this.points.update((rows) => rows.filter((_, i) => i !== index));
  }

  addCurve(type: 'line' | 'arc' | 'circle'): void {
    this.curves.update((rows) => [
      ...rows,
      { id: nextId('c', rows.map((row) => row.id)), type },
    ]);
  }

  setCurve(index: number, field: keyof SketchCurvePayload, value: string): void {
    this.curves.update((rows) =>
      rows.map((row, i) => (i === index ? { ...row, [field]: value } : row)),
    );
  }

  removeCurve(index: number): void {
    this.curves.update((rows) => rows.filter((_, i) => i !== index));
  }

  addLoop(): void {
    const all = this.curves()
      .map((c) => c.id)
      .join(', ');
    this.loops.update((rows) => [...rows, { id: rows.length ? `loop${rows.length}` : 'outer', curves: all }]);
  }

  setLoop(index: number, field: keyof LoopRow, value: string): void {
    this.loops.update((rows) =>
      rows.map((row, i) => (i === index ? { ...row, [field]: value } : row)),
    );
  }

  removeLoop(index: number): void {
    this.loops.update((rows) => rows.filter((_, i) => i !== index));
  }

  // -- persistence --------------------------------------------------------

  async save(): Promise<void> {
    this.error.set(null);
    this.busy.set(true);
    try {
      const payload =
        this.mode() === 'chain' && this.chainable()
          ? chainToSketch(this.id().trim(), this.plane(), this.chain())
          : this.asPayload();
      if (await this.store.putSketch(payload)) this.closed.emit();
    } finally {
      this.busy.set(false);
    }
  }

  async remove(): Promise<void> {
    this.busy.set(true);
    try {
      if (await this.store.deleteSketch(this.id())) this.closed.emit();
    } finally {
      this.busy.set(false);
    }
  }

  private asPayload(): SketchPayload {
    const points: Record<string, (number | string)[]> = {};
    for (const row of this.points()) {
      if (row.id.trim()) points[row.id.trim()] = [numeric(row.u), numeric(row.v)];
    }

    const curves = this.curves()
      .filter((curve) => curve.id.trim())
      .map((curve) => {
        const cleaned: SketchCurvePayload = { id: curve.id.trim(), type: curve.type };
        if (curve.type !== 'circle') {
          cleaned.start = (curve.start ?? '').trim();
          cleaned.end = (curve.end ?? '').trim();
        }
        if (curve.type !== 'line') cleaned.center = (curve.center ?? '').trim();
        if (curve.type === 'circle') cleaned.radius = numeric(String(curve.radius ?? ''));
        return cleaned;
      });

    const loops = this.loops()
      .filter((loop) => loop.id.trim())
      .map((loop) => ({
        id: loop.id.trim(),
        curves: loop.curves
          .split(',')
          .map((name) => name.trim())
          .filter(Boolean),
      }));

    return { id: this.id().trim(), plane: this.plane(), points, curves, loops };
  }
}

/** A cell that parses as a number is a literal; anything else is an expression. */
function numeric(raw: string): number | string {
  const trimmed = raw.trim();
  const value = Number(trimmed);
  return trimmed !== '' && Number.isFinite(value) ? value : trimmed;
}
