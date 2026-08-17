/**
 * Modal dialogs, all reachable from the keyboard.
 *
 * The selector console is the one that matters: it answers "what would this
 * match?" against the live model before anything is written to the document —
 * the same `/resolve` endpoint an agent uses over the API.
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

import { CadApiService } from '../../core/services/cad-api.service';
import type { ExportTarget } from '../../core/services/project-store';
import { STARTER_DOCUMENT } from '../../core/models/starter-document';
import type {
  DatumHit,
  FaceDatumFound,
  FeatureRow,
  ResolvePreview,
  SketchPayload,
  SketchRow,
} from '../../core/models/cad.models';

// --------------------------------------------------------------------- shell

@Component({
  selector: 'cad-modal',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="modal-backdrop" (mousedown)="closed.emit()">
      <div class="modal" (mousedown)="$event.stopPropagation()">
        <header>{{ title() }}</header>
        <div class="content"><ng-content /></div>
        <footer><ng-content select="[footer]" /></footer>
      </div>
    </div>
  `,
})
export class ModalComponent {
  readonly title = input.required<string>();
  readonly closed = output<void>();
}

// ------------------------------------------------------------------ selector

@Component({
  selector: 'cad-selector-console',
  standalone: true,
  imports: [ModalComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <cad-modal title="Selector console" (closed)="closed.emit()">
      <div class="field">
        <label>
          Face selector — try <code>base/side[*]</code>, <code>slot/wall[*]</code> or
          <code>*/cap+</code>
        </label>
        <input
          #box
          autofocus
          spellcheck="false"
          placeholder="feature/role[source]"
          [value]="text()"
          (input)="onInput($any($event.target).value)"
        />
      </div>

      @if (preview(); as result) {
        <div class="mono" [class.ok-text]="result.ok" [class.error-text]="!result.ok">
          {{ result.error ?? summary() }}
        </div>
        <ul class="result-list">
          @for (tag of result.matched; track tag) {
            <li>{{ tag }}</li>
          }
        </ul>
      }

      <button footer (click)="closed.emit()">Close</button>
    </cad-modal>
  `,
  styles: [`.ok-text { color: var(--ok); } .error-text { color: var(--error); }`],
})
export class SelectorConsoleComponent {
  private readonly api = inject(CadApiService);

  readonly projectId = input.required<string>();
  readonly initial = input('');
  readonly closed = output<void>();

  readonly text = signal('');
  readonly preview = signal<ResolvePreview | null>(null);
  private timer?: ReturnType<typeof setTimeout>;

  constructor() {
    queueMicrotask(() => {
      if (this.initial()) this.onInput(this.initial());
    });
  }

  summary(): string {
    const count = this.preview()?.count ?? 0;
    return `${count} match${count === 1 ? '' : 'es'}`;
  }

  onInput(value: string): void {
    this.text.set(value);
    clearTimeout(this.timer);
    if (!value.trim()) {
      this.preview.set(null);
      return;
    }
    this.timer = setTimeout(async () => {
      try {
        this.preview.set(await this.api.resolve(this.projectId(), value));
      } catch {
        this.preview.set(null);
      }
    }, 180);
  }
}

// --------------------------------------------------------------- yaml source

@Component({
  selector: 'cad-document-editor',
  standalone: true,
  imports: [ModalComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <cad-modal title="Document source" (closed)="closed.emit()">
      <textarea
        class="yaml mono"
        spellcheck="false"
        [value]="text()"
        (input)="text.set($any($event.target).value)"
      ></textarea>

      <ng-container footer>
        @if (error()) {
          <span class="error-text grow">{{ error() }}</span>
        }
        <button (click)="closed.emit()">Cancel</button>
        <button class="primary" [disabled]="busy()" (click)="save()">Save &amp; rebuild</button>
      </ng-container>
    </cad-modal>
  `,
  styles: [`.error-text { color: var(--error); } .grow { flex: 1; }`],
})
export class DocumentEditorComponent {
  private readonly api = inject(CadApiService);

  readonly projectId = input.required<string>();
  readonly closed = output<void>();
  readonly saved = output<void>();

  /** A signal, not a plain field: with zoneless change detection an assignment
   * from an async callback notifies nothing, so the dialog would sit empty
   * until an unrelated event happened to flush change detection. */
  readonly text = signal('');
  readonly error = signal<string | null>(null);
  readonly busy = signal(false);

  constructor() {
    queueMicrotask(async () => {
      this.text.set(await this.api.getDocumentYaml(this.projectId()));
    });
  }

  async save(): Promise<void> {
    this.busy.set(true);
    this.error.set(null);
    try {
      await this.api.putDocumentYaml(this.projectId(), this.text());
      this.saved.emit();
      this.closed.emit();
    } catch (caught) {
      this.error.set(describe(caught));
    } finally {
      this.busy.set(false);
    }
  }
}

// -------------------------------------------------------------- add feature

@Component({
  selector: 'cad-add-feature',
  standalone: true,
  imports: [ModalComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <cad-modal [title]="editing() ? 'Edit feature' : 'Add feature'" (closed)="closed.emit()">
      <div class="row">
        <div class="field grow">
          <label>
            @if (editing()) {
              Feature id — fixed, because every tag it produced is rooted here
            } @else {
              Feature id — becomes the root of every tag it produces
            }
          </label>
          <input
            [value]="id()"
            [disabled]="editing()"
            (input)="id.set($any($event.target).value)"
            placeholder="slot_2"
          />
        </div>
        <div class="field narrow">
          <label>Type</label>
          <select
            [value]="type()"
            [disabled]="editing()"
            (change)="type.set($any($event.target).value)"
          >
            <option value="pad">pad</option>
            <option value="pocket">pocket</option>
            <option value="hole">hole</option>
            <option value="thread">thread</option>
            <option value="fillet">fillet</option>
            <option value="chamfer">chamfer</option>
          </select>
        </div>
        @if (bodies().length > 1) {
          <div class="field narrow">
            <label>Body</label>
            <select
              [value]="body()"
              [disabled]="editing()"
              (change)="body.set($any($event.target).value)"
            >
              @for (option of bodies(); track option) {
                <option [value]="option" [selected]="option === body()">{{ option }}</option>
              }
            </select>
          </div>
        }
      </div>

      @switch (kind()) {
        @case ('profile') {
          <div class="field">
            <label>Profile (sketch.loop)</label>
            <select [value]="profile()" (change)="profile.set($any($event.target).value)">
              @for (option of profiles(); track option) {
                <option [value]="option" [selected]="option === profile()">{{ option }}</option>
              }
            </select>
          </div>
          <div class="field">
            <label>{{ type() === 'pad' ? 'Length' : 'Depth' }} — number or expression</label>
            <input [value]="size()" (input)="size.set($any($event.target).value)" />
          </div>
        }

        @case ('hole') {
          <div class="field">
            <label>Position (sketch.point)</label>
            <select [value]="at()" (change)="at.set($any($event.target).value)">
              @for (option of points(); track option) {
                <option [value]="option" [selected]="option === at()">{{ option }}</option>
              }
            </select>
            @if (points().length === 0) {
              <div class="hint">
                No sketch points yet — add one in Sketches (S) to place a hole on.
              </div>
            }
          </div>
          <div class="row">
            <div class="field narrow">
              <label>Size by</label>
              <select [value]="sizing()" (change)="sizing.set($any($event.target).value)">
                <option value="standard">fastener</option>
                <option value="diameter">diameter</option>
              </select>
            </div>
            @if (sizing() === 'standard') {
              <div class="field narrow">
                <label>Thread</label>
                <select
                  [value]="standard()"
                  (change)="standard.set($any($event.target).value)"
                >
                  @for (option of threads; track option) {
                    <option [value]="option">{{ option }}</option>
                  }
                </select>
              </div>
              <div class="field narrow">
                <label>Fit</label>
                <select [value]="fit()" (change)="fit.set($any($event.target).value)">
                  <option value="close">close</option>
                  <option value="normal">normal</option>
                  <option value="loose">loose</option>
                  <option value="tapped">tapped</option>
                </select>
              </div>
            } @else {
              <div class="field grow">
                <label>Diameter</label>
                <input
                  [value]="diameter()"
                  (input)="diameter.set($any($event.target).value)"
                />
              </div>
            }
          </div>
          <div class="field">
            <label>Depth — or leave blank to drill through</label>
            <input
              [value]="size()"
              (input)="size.set($any($event.target).value)"
              placeholder="through"
            />
          </div>
        }

        @case ('thread') {
          <div class="field">
            <label>Position (sketch.point)</label>
            <select [value]="at()" (change)="at.set($any($event.target).value)">
              @for (option of points(); track option) {
                <option [value]="option" [selected]="option === at()">{{ option }}</option>
              }
            </select>
            @if (points().length === 0) {
              <div class="hint">
                No sketch points yet. Add one in Sketches, or click a face in the
                view and use "Sketch here".
              </div>
            }
          </div>
          <div class="row">
            <div class="field narrow">
              <label>Size</label>
              <select [value]="standard()" (change)="standard.set($any($event.target).value)">
                @for (option of threads; track option) {
                  <option [value]="option">{{ option }}</option>
                }
              </select>
            </div>
            <div class="field grow">
              <label>Depth — number or expression</label>
              <input [value]="size()" (input)="size.set($any($event.target).value)" />
            </div>
          </div>
          <div class="row">
            <div class="field grow">
              <label>Kind</label>
              <select [value]="internal()" (change)="internal.set($any($event.target).value)">
                <option value="true">internal — tapped hole</option>
                <option value="false">external — on a boss</option>
              </select>
            </div>
            <div class="field narrow">
              <label>Hand</label>
              <select [value]="hand()" (change)="hand.set($any($event.target).value)">
                <option value="right">right</option>
                <option value="left">left</option>
              </select>
            </div>
          </div>
          <div class="field">
            <label>Helix</label>
            <select [value]="modelled()" (change)="modelled.set($any($event.target).value)">
              <option value="export" [selected]="modelled() === 'export'">
                exported files only
              </option>
              <option value="true" [selected]="modelled() === 'true'">always</option>
              <option value="false" [selected]="modelled() === 'false'">never — a note</option>
            </select>
            <div class="consequence">{{ modelledConsequence() }}</div>
            <div class="hint">
              Cutting the helix costs a few seconds and about ninety faces, and on
              screen a threaded hole looks the same either way — so the default
              pays for it where it matters and not where it does not. A note is
              what a machinist wants; a printed part needs the geometry.
            </div>
          </div>
        }

        @case ('blend') {
          <div class="field">
            <label>Edges — a selector, re-resolved on every rebuild</label>
            <input
              [value]="edges()"
              (input)="edges.set($any($event.target).value)"
              spellcheck="false"
              placeholder="base/cap+ ^ base/side[*]"
            />
            <div class="hint">
              Two face patterns joined by <code>^</code> selects the edges between them —
              a whole perimeter, stated once. Append <code>dir=|z</code> to keep only
              upright edges.
            </div>
          </div>
          <div class="row">
            <div class="field grow">
              <label>{{ type() === 'fillet' ? 'Radius' : 'Setback' }}</label>
              <input [value]="size()" (input)="size.set($any($event.target).value)" />
            </div>
            <div class="field narrow">
              <label>If it fails</label>
              <select
                [value]="onFailure()"
                (change)="onFailure.set($any($event.target).value)"
              >
                <option value="fail">stop the build</option>
                <option value="skip">carry on</option>
              </select>
            </div>
          </div>
        }
      }

      @if (kind() !== 'blend') {
        <div class="field">
          <label>Direction — always explicit, never inferred</label>
          <select
            [value]="direction()"
            (change)="direction.set($any($event.target).value)"
          >
            <option value="-normal">-normal (into the material)</option>
            <option value="+normal">+normal</option>
          </select>
        </div>
      }

      <ng-container footer>
        @if (error()) {
          <span class="error-text grow">{{ error() }}</span>
        }
        <button (click)="closed.emit()">Cancel</button>
        <button class="primary" [disabled]="!id()" (click)="submit()">
          {{ editing() ? 'Save' : 'Add' }}
        </button>
      </ng-container>
    </cad-modal>
  `,
  styles: [
    `
      .error-text { color: var(--error); }
      .grow { flex: 1; }
      .narrow { width: 130px; }
      .row { display: flex; gap: 10px; }
      .hint {
        margin-top: 4px;
        font-size: 11px;
        color: var(--text-faint);
        line-height: 1.5;
      }
      .checkbox { display: flex; gap: 8px; align-items: center; font-size: 12px; }
      .checkbox input { width: auto; }
      .state {
        font-family: var(--mono);
        font-size: 10px;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        padding: 1px 5px;
        border: 1px solid var(--border);
        border-radius: 3px;
        color: var(--text-faint);
      }
      .state.on {
        color: var(--ok);
        border-color: var(--ok);
      }
      .consequence {
        margin-top: 6px;
        font-size: 11px;
        line-height: 1.5;
        color: var(--text-dim);
      }
    `,
  ],
})
export class AddFeatureComponent {
  private readonly api = inject(CadApiService);

  readonly projectId = input.required<string>();
  readonly profiles = input.required<string[]>();
  /**
   * Every body in the document. A feature belongs to exactly one body, and the
   * choice is made here rather than inferred, because "the body I last touched"
   * is precisely the kind of implicit context this project avoids.
   */
  readonly bodies = input<string[]>([]);
  /**
   * The body the user has made active, if any: where the dropdown starts.
   *
   * Not the same as inferring "the body I last touched" — the active body is a
   * standing, visible choice, and the Body dropdown still overrules it.
   */
  readonly activeBody = input<string | null>(null);
  /** Opened from a quick action rather than the toolbar: start on these values. */
  readonly prefill = input<FeaturePrefill | null>(null);
  /**
   * The feature being edited, or null when adding a new one.
   *
   * Editing reuses this dialog rather than a parallel one, so the two can never
   * drift apart on what a feature type needs — the fields *are* the schema.
   */
  readonly editingFeature = input<FeatureRow | null>(null);
  /** Which body owns the feature being edited. Shown, never changed here. */
  readonly editingBody = input<string>('');
  /** Every `sketch.point` a hole could be placed on. */
  readonly points = input<string[]>([]);
  readonly closed = output<void>();
  readonly added = output<void>();

  readonly threads = ['M2', 'M2.5', 'M3', 'M4', 'M5', 'M6', 'M8', 'M10', 'M12', 'M16', 'M20'];

  readonly id = signal('');
  readonly type = signal('pad');
  readonly body = signal('');
  readonly profile = signal('');
  readonly at = signal('');
  readonly sizing = signal('standard');
  readonly standard = signal('M6');
  readonly fit = signal('normal');
  readonly diameter = signal('6');
  readonly size = signal('10');
  readonly edges = signal('');
  readonly direction = signal('-normal');
  readonly onFailure = signal('fail');
  readonly internal = signal('true');
  readonly hand = signal('right');
  /**
   * Tri-state, and defaulting to 'export'.
   *
   * This was a checkbox, which could only say always or never — and unticking
   * it wrote no key at all, so the thread was absent from exported STLs too.
   * That reads as a bug rather than a setting: someone turning off a *render*
   * option does not expect it to strip geometry from their print file.
   */
  readonly modelled = signal('export');
  readonly error = signal<string | null>(null);

  readonly editing = computed(() => this.editingFeature() !== null);

  /**
   * What the current choice means, spelled out.
   *
   * Each of the three does something different to what reaches a printer, and
   * that is the part worth stating: the viewport looks identical in two of
   * them, so the screen cannot tell you which you picked.
   */
  readonly modelledConsequence = computed(() => {
    switch (this.modelled()) {
      case 'true':
        return 'Cut everywhere — in exported files and in the viewport. Slower to rebuild.';
      case 'false':
        return (
          'Never cut. The hole is drilled at the tap-drill size and the thread is a ' +
          'note only — no thread geometry in the viewport or in exported files.'
        );
      default:
        return 'Cut into exported files, skipped in the viewport — so rebuilds stay quick.';
    }
  });

  /** Which set of fields this feature type needs. */
  readonly kind = computed(() => {
    const type = this.type();
    if (type === 'hole') return 'hole';
    if (type === 'thread') return 'thread';
    if (type === 'fillet' || type === 'chamfer') return 'blend';
    return 'profile';
  });

  constructor() {
    queueMicrotask(() => {
      this.profile.set(this.profiles()[0] ?? '');
      this.at.set(this.points()[0] ?? '');
      this.body.set(this.activeBody() ?? this.bodies()[0] ?? '');
      const prefill = this.prefill();
      if (prefill) {
        this.type.set(prefill.type);
        if (prefill.edges) this.edges.set(prefill.edges);
        if (prefill.body) this.body.set(prefill.body);
        if (prefill.at) this.at.set(prefill.at);
      }
      const existing = this.editingFeature();
      if (existing) this.load(existing);
    });
  }

  async submit(): Promise<void> {
    this.error.set(null);
    const existing = this.editingFeature();
    try {
      if (existing) {
        await this.api.updateFeature(this.projectId(), existing.id, this.asSpec());
      } else {
        await this.api.addFeature(
          this.projectId(),
          this.asSpec(),
          undefined,
          this.body() || undefined,
        );
      }
      this.added.emit();
      this.closed.emit();
    } catch (caught) {
      this.error.set(describe(caught));
    }
  }

  /**
   * Fill the form from a stored feature.
   *
   * The inverse of `asSpec`, and deliberately adjacent to it: a field added to
   * one without the other shows up immediately as a value that will not load.
   */
  private load(feature: FeatureRow): void {
    this.id.set(feature.id);
    this.type.set(feature.type);
    this.body.set(this.editingBody() || this.bodies()[0] || '');

    switch (this.kind()) {
      case 'profile':
        this.profile.set(feature['profile'] ? String(feature['profile']) : '');
        this.size.set(text(feature[feature.type === 'pad' ? 'length' : 'depth']));
        this.direction.set(text(feature['direction']) || '-normal');
        break;

      case 'hole':
        this.at.set(text(feature['at']));
        if (feature['standard']) {
          this.sizing.set('standard');
          this.standard.set(text(feature['standard']));
          this.fit.set(text(feature['fit']) || 'normal');
        } else {
          this.sizing.set('diameter');
          this.diameter.set(text(feature['diameter']));
        }
        // A blank depth is what "through all" looks like in this form.
        this.size.set(feature['through_all'] ? '' : text(feature['depth']));
        this.direction.set(text(feature['direction']) || '-normal');
        break;

      case 'thread':
        this.at.set(text(feature['at']));
        this.standard.set(text(feature['standard']) || 'M6');
        this.size.set(text(feature['depth']));
        this.direction.set(text(feature['direction']) || '-normal');
        this.internal.set(feature['internal'] === false ? 'false' : 'true');
        this.hand.set(text(feature['hand']) === 'left' ? 'left' : 'right');
        // An absent key means the document predates the tri-state, where the
        // default was 'never'. Preserve that rather than silently changing an
        // existing part.
        this.modelled.set(
          feature['modelled'] === undefined ? 'false' : String(feature['modelled']),
        );
        break;

      case 'blend':
        this.edges.set(text(feature['edges']));
        this.size.set(text(feature[feature.type === 'fillet' ? 'radius' : 'distance']));
        this.onFailure.set(text(feature['on_failure']) === 'skip' ? 'skip' : 'fail');
        break;
    }
  }


  private asSpec(): Record<string, unknown> {
    const spec: Record<string, unknown> = { id: this.id().trim(), type: this.type() };

    switch (this.kind()) {
      case 'profile':
        spec['profile'] = this.profile();
        spec[this.type() === 'pad' ? 'length' : 'depth'] = numeric(this.size());
        spec['direction'] = this.type() === 'pad' ? '+normal' : this.direction();
        break;

      case 'hole':
        spec['at'] = this.at();
        if (this.sizing() === 'standard') {
          spec['standard'] = this.standard();
          spec['fit'] = this.fit();
        } else {
          spec['diameter'] = numeric(this.diameter());
        }
        if (this.size().trim()) spec['depth'] = numeric(this.size());
        else spec['through_all'] = true;
        spec['direction'] = this.direction();
        break;

      case 'thread':
        spec['at'] = this.at();
        spec['standard'] = this.standard();
        spec['depth'] = numeric(this.size());
        spec['direction'] = this.direction();
        spec['internal'] = this.internal() === 'true';
        // Written even when it is the default: the value decides whether a
        // print file has a thread in it, and that should be visible in the
        // document rather than implied by an absent key.
        spec['modelled'] = this.modelled() === 'true' ? true
          : this.modelled() === 'false' ? false
          : 'export';
        if (this.hand() === 'left') spec['hand'] = 'left';
        break;

      case 'blend':
        spec['edges'] = this.edges().trim();
        spec[this.type() === 'fillet' ? 'radius' : 'distance'] = numeric(this.size());
        if (this.onFailure() === 'skip') spec['on_failure'] = 'skip';
        break;
    }
    return spec;
  }
}

// -------------------------------------------------------------- new project

@Component({
  selector: 'cad-new-project',
  standalone: true,
  imports: [ModalComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <cad-modal title="New project" (closed)="closed.emit()">
      <div class="field">
        <label>Project id — letters, digits, hyphen, underscore</label>
        <input [value]="id()" (input)="id.set($any($event.target).value)" placeholder="bracket" />
      </div>
      <div class="field">
        <label>Display name</label>
        <input [value]="name()" (input)="name.set($any($event.target).value)" />
      </div>
      <label class="checkbox">
        <input
          type="checkbox"
          [checked]="starter()"
          (change)="starter.set($any($event.target).checked)"
        />
        Start from the example plate (a pad and a pocket)
      </label>

      <ng-container footer>
        @if (error()) {
          <span class="error-text grow">{{ error() }}</span>
        }
        <button (click)="closed.emit()">Cancel</button>
        <button class="primary" (click)="submit()">Create</button>
      </ng-container>
    </cad-modal>
  `,
  styles: [
    `
      .error-text { color: var(--error); }
      .grow { flex: 1; }
      .checkbox { display: flex; gap: 8px; align-items: center; font-size: 12px; }
      .checkbox input { width: auto; }
    `,
  ],
})
export class NewProjectComponent {
  readonly closed = output<void>();
  readonly created = output<{ id: string; name: string; document?: unknown }>();

  readonly id = signal('');
  readonly name = signal('');
  readonly starter = signal(true);
  readonly error = signal<string | null>(null);

  submit(): void {
    if (!this.id()) {
      this.error.set('a project id is required');
      return;
    }
    this.created.emit({
      id: this.id(),
      name: this.name() || this.id(),
      document: this.starter() ? STARTER_DOCUMENT : undefined,
    });
    this.closed.emit();
  }
}

// -------------------------------------------------------------------- shared

/** A field that must be a number. Unparseable text falls back to zero, which
 * the server rejects with a message rather than the browser doing it silently. */
function measure(raw: string): number {
  const value = Number(raw.trim());
  return Number.isFinite(value) ? value : 0;
}

function numeric(raw: string): number | string {
  const value = Number(raw.trim());
  return Number.isFinite(value) && raw.trim() !== '' ? value : raw.trim();
}

function describe(error: unknown): string {
  if (typeof error === 'object' && error !== null && 'error' in error) {
    const detail = (error as { error?: { detail?: { message?: string } } }).error?.detail;
    if (detail?.message) return detail.message;
  }
  return error instanceof Error ? error.message : String(error);
}

/** What a quick action hands the add-feature dialog to start from. */
export interface FeaturePrefill {
  type: string;
  edges?: string;
  body?: string;
  /** A `sketch.point` a hole or thread should be placed at. */
  at?: string;
}

/** A stored option as form text. Numbers, expressions and flags all arrive here. */
function text(value: unknown): string {
  if (value === undefined || value === null) return '';
  return String(value);
}


// ------------------------------------------------------------ sketch here

@Component({
  selector: 'cad-sketch-here',
  standalone: true,
  imports: [ModalComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <cad-modal title="Sketch here" (closed)="closed.emit()">
      <div class="hint">
        A sketch is started on the datum below. Nothing about the face you
        clicked is kept: a sketch attaches to a datum and only to a datum, which
        is what stops it flipping when a surface changes sense on a later
        rebuild.
      </div>

      @if (loading()) {
        <div class="hint">Locating…</div>
      } @else {
        @if (refusal(); as reason) {
          @if (faceTag(); as tag) {
            <!-- A refusal used to read like a hint, so the fallback plane was
                 taken for the picked face rather than a substitute for it. -->
            <div class="refused-face">
              <div class="headline">
                No datum could be derived from <code>{{ tag }}</code> — {{ reason }}
              </div>
              <div>
                The plane offered below is <strong>not</strong> the face you
                picked, and may sit at any angle to it. Choose the plane you
                mean, or cancel and pick a face this can be derived from.
              </div>
            </div>
          } @else {
            <div class="hint refused">
              The plane has to be chosen by hand here — {{ reason }}
            </div>
          }
        }

        @if (faceDatum(); as derived) {
          <div class="field">
            <label>Datum — derived from <code>{{ faceTag() }}</code></label>
            <div class="derived mono">{{ planeId() }}</div>
            <div class="hint">
              {{ derived.explanation }}
            </div>
            @if (derived.existing) {
              <div class="hint">
                Using the existing datum <code>{{ derived.existing }}</code>, which
                already describes this plane. A second one would say the same thing
                until somebody edited one of them.
              </div>
            } @else {
              <div class="hint">
                It is created from <code>{{ derivedParent() }}</code> offset by
                the feature's own expression, not by the millimetres that expression
                happens to be worth today — so the sketch, and everything drawn on
                it, follows the part when a parameter changes.
              </div>
            }
          </div>
        } @else if (options().length === 0) {
          <div class="error-text">No datums to place this on.</div>
        } @else {
          <div class="field">
            <label>Datum — nearest plane first</label>
            <select [value]="datum()" (change)="datum.set($any($event.target).value)">
              @for (option of options(); track option.datum) {
                <option [value]="option.datum" [selected]="option.datum === datum()">
                  {{ option.datum }} — {{ option.offset }} mm off plane
                </option>
              }
            </select>
          </div>

          @if (offPlane()) {
            <div class="field">
              <label>
                <input
                  type="checkbox"
                  [checked]="lift()"
                  (change)="lift.set($any($event.target).checked)"
                />
                Put the sketch on a new datum {{ offsetLabel() }}, where you clicked
              </label>
              <div class="hint">
                Otherwise the sketch lands on <code>{{ datum() }}</code>, which is
                {{ offsetLabel() }} from the surface — and a hole drilled from there
                may start outside the material.
                @if (offsetParameter(); as parameter) {
                  The new datum is <code>{{ datum() }}</code> offset by
                  <code>{{ parameter }}</code>, not by {{ offsetLabel() }}: written as
                  a literal it would stay where it is when
                  <code>{{ parameter }}</code> changes, and the hole drilled on it
                  would end up in the wrong place.
                } @else {
                  The new datum is <code>{{ datum() }}</code> offset by a number, so
                  it is still computed from parameters like every other datum.
                }
              </div>
            </div>
          }
        }

        @if (ready()) {
          <div class="choices">
            <button
              [class.active]="intent() === 'point'"
              (click)="intent.set('point')"
            >
              Start with a point
            </button>
            <button
              [class.active]="intent() === 'draw'"
              (click)="intent.set('draw')"
            >
              Open the sketch editor
            </button>
          </div>
          <div class="consequence">{{ consequence() }}</div>

          <div class="row">
            <div class="field grow">
              <label>Sketch</label>
              <input
                [value]="sketch()"
                (input)="sketch.set($any($event.target).value)"
                placeholder="holes"
              />
            </div>
            @if (intent() === 'point') {
              <div class="field narrow">
                <label>Point id</label>
                <input [value]="pointId()" (input)="pointId.set($any($event.target).value)" />
              </div>
            }
          </div>

          @if (intent() === 'point') {
            <div class="row">
              <div class="field grow">
                <label>U</label>
                <input [value]="u()" (input)="u.set($any($event.target).value)" />
              </div>
              <div class="field grow">
                <label>V</label>
                <input [value]="v()" (input)="v.set($any($event.target).value)" />
              </div>
            </div>

            @if (faceSize(); as size) {
              <div class="hint">
                This face is <strong>{{ size.uValue }}</strong> across by
                <strong>{{ size.vValue }}</strong> up, measured from the corner at
                <code>0, 0</code>.
                <button class="centre" [disabled]="centring()" (click)="centreOnFace()">
                  Centre on it
                </button>
                <br />
                Centring writes two parameters holding those dimensions and puts the
                point at half of each, so it stays centred when the part changes.
              </div>
            }

            <div class="hint">
              @if (point()) {
                The click is already filled in as two plain numbers on this
                datum — replace either with a parameter whenever you like.
              } @else {
                U and V are two plain numbers on this datum — either can be a
                parameter instead of a number.
              }
            </div>
          }

          <div class="hint">
            Anything else added to sketch <code>{{ sketch() }}</code> later — more
            points, a profile to pocket — sits on this same datum.
          </div>
        }
      }

      <ng-container footer>
        @if (error()) {
          <span class="error-text grow">{{ error() }}</span>
        }
        <button (click)="closed.emit()">Cancel</button>
        <button class="primary" [disabled]="!submittable()" (click)="submit()">
          {{ actionLabel() }}
        </button>
      </ng-container>
    </cad-modal>
  `,
  styles: [
    `
      .error-text { color: var(--error); }
      .grow { flex: 1; }
      .narrow { width: 130px; }
      .row { display: flex; gap: 10px; }
      .mono { font-family: var(--mono); font-size: 12px; }
      .derived {
        padding: 4px 8px;
        color: var(--accent);
        background: var(--bg-raised);
        border: 1px solid var(--border);
        border-radius: 3px;
      }
      .hint {
        margin-bottom: 10px;
        font-size: 11px;
        color: var(--text-faint);
        line-height: 1.5;
      }
      .refused { color: var(--text-dim); }
      .refused-face {
        margin-bottom: 10px;
        padding: 7px 9px;
        border: 1px solid var(--error);
        border-radius: 4px;
        font-size: 11px;
        line-height: 1.5;
        color: var(--text-dim);
      }
      .refused-face .headline {
        margin-bottom: 4px;
        color: var(--error);
      }
      .choices {
        display: flex;
        gap: 6px;
        margin-bottom: 2px;
      }
      .choices button.active {
        color: var(--accent);
        border-color: var(--accent-dim);
      }
      .consequence {
        margin: 6px 0 12px;
        font-size: 11px;
        line-height: 1.5;
        color: var(--text-dim);
      }
    `,
  ],
})
export class SketchHereComponent {
  private readonly api = inject(CadApiService);

  readonly projectId = input.required<string>();
  /**
   * Where the click landed, when it was a click.
   *
   * Null when the face came from the topology list instead of the viewport:
   * once the datum can be derived from the face itself, a 3D click is a
   * convenience for filling in two numbers, not a requirement.
   */
  readonly point = input<[number, number, number] | null>(null);
  /**
   * The face that was picked, when exactly one was.
   *
   * With it the datum can be derived from the document; without it the nearest
   * plane to the click is all there is to go on.
   */
  readonly faceTag = input<string | null>(null);
  /** Existing sketches, so adding a point never discards what is already there. */
  readonly sketches = input<Record<string, SketchRow>>({});
  readonly closed = output<void>();
  /** Emits the `sketch.point` that was created, ready to drill or tap. */
  readonly placed = output<string>();
  /**
   * Emits the id of a sketch created with nothing in it, to be drawn on.
   *
   * A second output rather than a flag on `placed`, because the two are
   * different destinations: one has a point to drill at, the other has an empty
   * sketch to fill in. The shell decides which editor that means, so this dialog
   * still knows nothing about the store.
   */
  readonly drawing = output<string>();

  readonly options = signal<DatumHit[]>([]);
  /** The datum the picked face implies, once the server has named one. */
  readonly faceDatum = signal<FaceDatumFound | null>(null);
  /** Why the face could not name a datum, when the server said so. */
  readonly refusal = signal<string | null>(null);
  readonly loading = signal(true);
  readonly datum = signal('');
  readonly sketch = signal('holes');
  readonly pointId = signal('p1');
  readonly u = signal('0');
  readonly v = signal('0');
  readonly centring = signal(false);

  /** The face's own extent, when the document states it. */
  readonly faceSize = computed(() => {
    const found = this.faceDatum();
    return found?.ok ? (found.size ?? null) : null;
  });
  readonly lift = signal(true);
  readonly error = signal<string | null>(null);
  /**
   * What pressing the button will do.
   *
   * A point is right for a hole and wrong for everything else: drawing a
   * profile on the face left a stray point nobody asked for, and deleting it
   * afterwards is a second trip through the sketch editor.
   */
  readonly intent = signal<'point' | 'draw'>('point');

  /** How far the click sits off the chosen datum's plane. */
  readonly offset = computed(
    () => this.options().find((o) => o.datum === this.datum())?.offset ?? 0,
  );
  readonly offPlane = computed(() => Math.abs(this.offset()) > 1e-4);
  readonly offsetLabel = computed(() => `${this.offset()} mm off`);
  /** The parameter that offset is worth, when the server found one. */
  readonly offsetParameter = computed(
    () => this.options().find((o) => o.datum === this.datum())?.offsetParameter ?? null,
  );

  /** The datum the sketch will attach to, named before anything is written. */
  readonly planeId = computed(() => {
    const derived = this.faceDatum();
    return derived ? (derived.existing ?? derived.datum.id) : this.datum();
  });

  /** The datum the derived one is built from — the world origin, if none. */
  readonly derivedParent = computed(
    () => this.faceDatum()?.datum.parent ?? 'the world origin',
  );

  /** There is a plane to sketch on, so the rest of the form means something. */
  readonly ready = computed(() => this.faceDatum() !== null || this.options().length > 0);

  readonly actionLabel = computed(() =>
    this.intent() === 'point' ? 'Start with a point' : 'Open the sketch editor',
  );

  /** One line on where the chosen action leaves you. */
  readonly consequence = computed(() =>
    this.intent() === 'point'
      ? 'The click becomes one point on this datum, then the hole dialog opens ' +
        'already pointed at it.'
      : 'No point is placed — the sketch opens in the sketch editor on this ' +
        'datum, ready to draw.',
  );

  /** A point id is only asked for, and so only required, when there is a point. */
  readonly submittable = computed(
    () =>
      this.ready() &&
      this.sketch().trim() !== '' &&
      (this.intent() === 'draw' || this.pointId().trim() !== ''),
  );

  constructor() {
    queueMicrotask(() => void this.load());
  }

  /**
   * Ask both questions at once.
   *
   * `locate` gives the click's u and v on every candidate plane, which is
   * needed whichever datum wins. `for-face` gives the datum the document would
   * build for the picked face — the better answer, because its offset is the
   * feature's expression rather than today's number. It is optional in every
   * sense: refused, unreachable or not yet deployed all mean the same thing
   * here, which is fall back to the list and let the user choose.
   */
  private async load(): Promise<void> {
    const tag = this.faceTag();
    const clicked = this.point();
    const [located, derived] = await Promise.all([
      clicked === null
        ? Promise.resolve(null)
        : this.api.locate(this.projectId(), clicked).catch((caught: unknown) => {
            this.error.set(describe(caught));
            return null;
          }),
      tag === null
        ? Promise.resolve(null)
        : this.api
            .datumForFace(this.projectId(), tag, clicked ?? undefined)
            .catch(() => null),
    ]);

    if (derived?.ok) this.faceDatum.set(derived);
    else if (derived) this.refusal.set(derived.reason);

    // The derived plane's own coordinates, when the server could give them.
    // They are the only correct answer for a face that stands on edge to its
    // sketch: the located hit below is measured on the *parent*, and those
    // numbers are coordinates on a different plane entirely.
    if (derived?.ok && derived.at) {
      this.u.set(String(derived.at.u));
      this.v.set(String(derived.at.v));
    }

    if (located) {
      this.options.set(located.datums);
      // u and v are read off the plane the derived datum is parallel to:
      // offsetting a datum along its own normal leaves in-plane coordinates
      // untouched, so the parent's numbers are the derived plane's numbers.
      const parallel = derived?.ok ? (derived.existing ?? derived.datum.parent ?? '') : '';
      const hit = located.datums.find((o) => o.datum === parallel) ?? located.datums[0];
      // Only when the server gave no coordinates of its own — `choose` fills u
      // and v from the parent, which is right only for a parallel plane.
      if (hit) this.choose(hit, !(derived?.ok && derived.at));
    }
    // Set even when `locate` gave nothing back, because a derived datum is
    // still something to sketch on and `p1` would land on whatever is already
    // called `p1`.
    this.pointId.set(this.nextPointId());
    this.loading.set(false);
  }

  /**
   * Put the point at the middle of the face, parametrically.
   *
   * The two dimensions become named parameters rather than being written into
   * the point as one long expression. Same numbers either way, but the sheet
   * then shows a row a person can read and reuse, and the point reads as
   * `<name> / 2` rather than as a wall of `hypot`.
   */
  async centreOnFace(): Promise<void> {
    const size = this.faceSize();
    const datum = this.planeId();
    if (!size || !datum) return;

    this.centring.set(true);
    this.error.set(null);
    try {
      const across = `${datum}_w`;
      const up = `${datum}_h`;
      await this.ensureParameter(across, size.u, 'width of the face this sketch is on');
      await this.ensureParameter(up, size.v, 'height of the face this sketch is on');
      this.u.set(`${across} / 2`);
      this.v.set(`${up} / 2`);
    } catch (caught) {
      this.error.set(describe(caught));
    } finally {
      this.centring.set(false);
    }
  }

  /** Add a parameter, tolerating one that is already there from a previous run. */
  private async ensureParameter(
    name: string,
    expression: string | number,
    doc: string,
  ): Promise<void> {
    try {
      await this.api.addParameter(this.projectId(), {
        name,
        expr: String(expression),
        group: 'Faces',
        doc,
      });
    } catch {
      // Already present, which is the normal case on a second visit. Its
      // expression is derived from the same face, so it is already right.
    }
  }

  private choose(hit: DatumHit, withCoordinates = true): void {
    this.datum.set(hit.datum);
    if (withCoordinates) {
      this.u.set(String(hit.u));
      this.v.set(String(hit.v));
    }
  }

  /**
   * The first free `pN` in the target sketch.
   *
   * Defaulting to `p1` every time silently overwrote the previous point, which
   * moved whatever was drilled at it — the kind of quiet damage this project
   * exists to avoid.
   */
  private nextPointId(): string {
    const taken = new Set(
      Object.keys(
        (this.sketches()[this.sketch().trim()]?.['points'] as object) ?? {},
      ),
    );
    let index = 1;
    while (taken.has(`p${index}`)) index++;
    return `p${index}`;
  }

  async submit(): Promise<void> {
    this.error.set(null);
    try {
      const plane = await this.attachTo();
      if (plane === null) return;
      const name = this.sketch().trim();
      const existing = this.sketches()[name];
      const point = this.pointId().trim();
      const kept =
        (existing?.['points'] as Record<string, (number | string)[]>) ?? {};
      await this.api.putSketch(this.projectId(), {
        id: name,
        // An existing sketch keeps its own plane: moving it because a click
        // landed elsewhere would silently move every point already on it.
        plane: existing?.plane ?? plane,
        // Drawing adds nothing: the points a named sketch already has are still
        // its own, so only the new one is conditional.
        points:
          this.intent() === 'point'
            ? { ...kept, [point]: [numeric(this.u()), numeric(this.v())] }
            : kept,
        curves: (existing?.['curves'] as SketchPayload['curves']) ?? [],
        loops: existing?.loops ?? [],
      });
      if (this.intent() === 'point') this.placed.emit(`${name}.${point}`);
      else this.drawing.emit(name);
      this.closed.emit();
    } catch (caught) {
      this.error.set(describe(caught));
    }
  }

  /**
   * The datum the sketch attaches to, created if the document lacks it.
   *
   * Null means there is nothing to attach to, which the button already
   * prevents; it is repeated here because a datum is written before the sketch
   * is, and half of that would be worse than none of it.
   */
  private async attachTo(): Promise<string | null> {
    const derived = this.faceDatum();
    if (derived) {
      // A datum already describing this plane is reused rather than copied:
      // two names for one plane drift apart the moment either is edited.
      if (derived.existing) return derived.existing;
      await this.api.putDatum(this.projectId(), derived.datum);
      return derived.datum.id;
    }

    const chosen = this.options().find((o) => o.datum === this.datum());
    if (!chosen) return null;
    if (!this.offPlane() || !this.lift()) return this.datum();

    // A datum parallel to the chosen one, offset along its normal. The parent
    // is a datum, so the rule that no datum comes from picked topology holds
    // either way — but a literal offset is frozen at the thickness it was
    // clicked at, and the hole drilled on it silently stops matching the part.
    // The parameter keeps the link.
    const along = chosen.offsetParameter ?? chosen.offset;
    const plane = `${this.datum()}_at_${String(along).replace(/[.-]/g, '_')}`;
    await this.api.putDatum(this.projectId(), {
      id: plane,
      parent: this.datum(),
      origin: [0, 0, along],
      normal: [0, 0, 1],
    });
    return plane;
  }
}


// ------------------------------------------------------------- cut settings

@Component({
  selector: 'cad-cut-settings',
  standalone: true,
  imports: [ModalComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <cad-modal title="Cut settings" (closed)="closed.emit()">
      <div class="hint">
        Finger joints cut into the edges this part's own faces share. Panels a
        joint would not survive are exported plain and listed in the response
        headers rather than mangled.
      </div>

      <div class="row">
        <div class="field grow">
          <label>Material thickness (mm)</label>
          <input
            [value]="thickness()"
            (input)="thickness.set($any($event.target).value)"
          />
        </div>
        <div class="field grow">
          <label>Kerf (mm)</label>
          <input [value]="kerf()" (input)="kerf.set($any($event.target).value)" />
        </div>
      </div>

      <div class="row">
        <div class="field narrow">
          <label>Size teeth by</label>
          <select [value]="mode()" (change)="mode.set($any($event.target).value)">
            <option value="width" [selected]="mode() === 'width'">width</option>
            <option value="count" [selected]="mode() === 'count'">count</option>
          </select>
        </div>
        <div class="field grow">
          <label>
            {{ mode() === 'width' ? 'Tooth width (mm)' : 'Teeth per edge (odd, ≥3)' }}
          </label>
          <input [value]="size()" (input)="size.set($any($event.target).value)" />
          <div class="hint">
            @if (mode() === 'width') {
              Each edge gets as many teeth as fit. A short edge falls back to
              three.
            } @else {
              Every edge gets this many however long it is — what keeps a small
              face and a large one both looking like joints.
            }
          </div>
        </div>
      </div>

      <div class="field">
        <label>Recess depth (mm) — blank to match the thickness</label>
        <input
          [value]="depth()"
          (input)="depth.set($any($event.target).value)"
          placeholder="same as thickness"
        />
      </div>

      <div class="panel-header">Per-face tooth width</div>
      <div class="hint">
        For faces the global setting does not suit. An edge is shared by two
        faces, so an override applies to both — a joint has to mate.
      </div>
      @for (entry of overrides(); track entry.tag) {
        <div class="row override">
          <span class="ellipsis mono">{{ entry.tag }}</span>
          <input
            class="width"
            [value]="entry.width"
            (input)="setWidth(entry.tag, $any($event.target).value)"
          />
          <button (click)="drop(entry.tag)">×</button>
        </div>
      }
      <button [disabled]="selection().length === 0" (click)="addSelected()">
        + override the {{ selection().length }} selected face(s)
      </button>

      <div class="panel-header pick-header">Pick faces</div>
      <div class="hint">
        Every face in the model. A picked face lights up behind this dialog, so
        an override can be aimed without closing it first.
      </div>
      <input
        spellcheck="false"
        placeholder="filter tags — slot, /side, cap"
        [value]="filter()"
        (input)="filter.set($any($event.target).value)"
      />
      <div class="face-list">
        @for (face of facePicks(); track face.tag) {
          <div class="feature" [class.selected]="face.chosen" (click)="toggle(face.tag)">
            <span class="ellipsis">{{ face.tag }}</span>
            @if (face.chosen) {
              <input
                class="width"
                [value]="face.width"
                (click)="$event.stopPropagation()"
                (input)="setWidth(face.tag, $any($event.target).value)"
              />
            }
          </div>
        } @empty {
          <div class="hint nothing">No face tag matches that.</div>
        }
      </div>

      <ng-container footer>
        <a [href]="url('svg')" download><button>SVG</button></a>
        <a [href]="url('dxf')" download><button class="primary">DXF</button></a>
        <span class="spacer"></span>
        <button (click)="closed.emit()">Close</button>
      </ng-container>
    </cad-modal>
  `,
  styles: [
    `
      .grow { flex: 1; }
      .narrow { width: 130px; }
      .row { display: flex; gap: 10px; align-items: flex-end; }
      .override { align-items: center; margin: 4px 0; }
      .override .width { width: 70px; }
      .ellipsis { flex: 1; overflow: hidden; text-overflow: ellipsis; }
      .mono { font-family: var(--mono); font-size: 11px; }
      .spacer { flex: 1; }
      .pick-header { margin-top: 14px; }
      .face-list {
        max-height: 190px;
        overflow-y: auto;
        margin-top: 6px;
        border: 1px solid var(--border);
        border-radius: 4px;
      }
      .face-list .width { width: 70px; }
      .face-list .nothing { margin: 8px 10px; }
      .hint {
        margin: 4px 0 10px;
        font-size: 11px;
        color: var(--text-faint);
        line-height: 1.5;
      }
    `,
  ],
})
export class CutSettingsComponent {
  private readonly api = inject(CadApiService);

  readonly projectId = input.required<string>();
  /** Currently selected face tags, offered as override targets. */
  readonly selection = input<readonly string[]>([]);
  /**
   * Every face tag in the model.
   *
   * Overriding used to mean selecting in the topology panel *before* opening
   * this dialog, which is fine for one face and unusable for twenty.
   */
  readonly faces = input<readonly string[]>([]);
  readonly closed = output<void>();
  /**
   * The tags the viewport should highlight.
   *
   * Emitted rather than pushed into the store: the dialogs here never inject
   * it, so the shell stays the only thing that knows what a selection means.
   */
  readonly highlighted = output<readonly string[]>();

  readonly thickness = signal('3');
  readonly kerf = signal('0.15');
  readonly mode = signal('width');
  readonly size = signal('10');
  readonly depth = signal('');
  readonly overrides = signal<{ tag: string; width: string }[]>([]);
  readonly filter = signal('');

  /** The face list narrowed by the filter, joined with the override each tag
   * already carries so a row can be picked and retuned in one place. */
  readonly facePicks = computed(() => {
    const needle = this.filter().trim().toLowerCase();
    const widths = new Map(this.overrides().map((row) => [row.tag, row.width]));
    return this.faces()
      .filter((tag) => tag.toLowerCase().includes(needle))
      .map((tag) => ({ tag, width: widths.get(tag) ?? '', chosen: widths.has(tag) }));
  });

  /** A picked face starts at the global width, which is the value it is most
   * often nudged away from. */
  toggle(tag: string): void {
    this.overrides.update((rows) =>
      rows.some((row) => row.tag === tag)
        ? rows.filter((row) => row.tag !== tag)
        : [...rows, { tag, width: this.size() }],
    );
    this.highlighted.emit(this.overrides().map((row) => row.tag));
  }

  addSelected(): void {
    const width = this.size();
    this.overrides.update((rows) => {
      const taken = new Set(rows.map((row) => row.tag));
      return [
        ...rows,
        ...this.selection().filter((tag) => !taken.has(tag)).map((tag) => ({ tag, width })),
      ];
    });
  }

  setWidth(tag: string, width: string): void {
    this.overrides.update((rows) =>
      rows.map((row) => (row.tag === tag ? { ...row, width } : row)),
    );
  }

  drop(tag: string): void {
    this.overrides.update((rows) => rows.filter((row) => row.tag !== tag));
  }

  url(fmt: string): string {
    const size = measure(this.size());
    return this.api.jointedUrl(this.projectId(), fmt, {
      thickness: measure(this.thickness()),
      kerf: measure(this.kerf()),
      finger: this.mode() === 'width' ? size : undefined,
      teeth: this.mode() === 'count' ? Math.round(size) : undefined,
      depth: this.depth().trim() ? measure(this.depth()) : undefined,
      // Semicolons, because a face tag can contain a comma once a selector
      // union is involved.
      overrides: this.overrides()
        .map((row) => `${row.tag}:${measure(row.width)}`)
        .join(';'),
    });
  }
}

// ---------------------------------------------------------------------- mesh

/**
 * Which body to export, and in what format.
 *
 * Previously the toolbar grew one download button per body, so a four-body
 * document pushed everything else off the bar. The choice belongs behind the
 * one button that provokes it.
 */
@Component({
  selector: 'cad-export',
  standalone: true,
  imports: [ModalComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <cad-modal title="Export geometry" (closed)="closed.emit()">
      @if (targets().length === 0) {
        <div class="empty">Nothing to export yet.</div>
      }
      @for (target of targets(); track target.id) {
        <div class="export-row">
          <div class="export-name">
            <strong>{{ target.label }}</strong>
            <span class="type">{{ target.note }}</span>
          </div>
          <span class="spacer"></span>
          <a [href]="target.stl" download
            ><button title="Triangle mesh, for a slicer">STL</button></a
          >
          <a [href]="target.obj" download
            ><button title="Triangle mesh with face groups">OBJ</button></a
          >
          <a [href]="target.step" download
            ><button title="Exact surfaces, for other CAD">STEP</button></a
          >
        </div>
      }
      <button footer (click)="closed.emit()">Close</button>
    </cad-modal>
  `,
  styles: [
    `
      .export-row {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 8px 0;
        border-bottom: 1px solid var(--border);
      }
      .export-row:last-of-type { border-bottom: none; }
      .export-name { display: flex; flex-direction: column; gap: 2px; }
      .export-row .spacer { flex: 1; }
    `,
  ],
})
export class ExportComponent {
  readonly targets = input.required<readonly ExportTarget[]>();
  readonly closed = output<void>();
}

// --------------------------------------------------------------------- sheet

/**
 * The parameter table, out and back in.
 *
 * Export and import were two toolbar buttons that only made sense next to each
 * other — the round trip is one workflow (edit in a spreadsheet, bring it
 * back), so it reads better as one door with both directions behind it.
 */
@Component({
  selector: 'cad-sheet',
  standalone: true,
  imports: [ModalComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <cad-modal title="Parameter sheet" (closed)="closed.emit()">
      <div class="export-row">
        <div class="export-name">
          <strong>Export CSV</strong>
          <span class="type">Every parameter, its expression and its resolved value</span>
        </div>
        <span class="spacer"></span>
        <a [href]="csvUrl()" download><button>Download</button></a>
      </div>

      <div class="export-row">
        <div class="export-name">
          <strong>Import CSV</strong>
          <span class="type">Replaces the table — edit it in Excel or Calc and bring it back</span>
        </div>
        <span class="spacer"></span>
        <button (click)="file.click()">Choose file…</button>
        <input
          #file
          type="file"
          accept=".csv,text/csv"
          hidden
          (change)="choose($event)"
        />
      </div>

      <div class="export-row">
        <div class="export-name">
          <strong>Export YAML</strong>
          <span class="type">The whole document — parameters, sketches and history</span>
        </div>
        <span class="spacer"></span>
        <a [href]="yamlUrl()" download><button>Download</button></a>
      </div>

      <button footer (click)="closed.emit()">Close</button>
    </cad-modal>
  `,
  styles: [
    `
      .export-row {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 8px 0;
        border-bottom: 1px solid var(--border);
      }
      .export-row:last-of-type { border-bottom: none; }
      .export-name { display: flex; flex-direction: column; gap: 2px; }
      .export-row .spacer { flex: 1; }
    `,
  ],
})
export class SheetComponent {
  readonly csvUrl = input.required<string>();
  readonly yamlUrl = input.required<string>();
  /** The CSV text the operator picked, ready for the store to send. */
  readonly imported = output<string>();
  readonly closed = output<void>();

  /**
   * The input is cleared afterwards so choosing the same file twice fires
   * again — someone who fixes a rejected row and re-picks it would otherwise
   * get silence.
   */
  async choose(event: Event): Promise<void> {
    const input = event.target as HTMLInputElement;
    const chosen = input.files?.[0];
    input.value = '';
    if (!chosen) return;
    this.imported.emit(await chosen.text());
  }
}

// ------------------------------------------------------------------- confirm

/**
 * A yes/no for something that cannot be undone.
 *
 * The browser's own `confirm()` would do the job, but it looks nothing like the
 * rest of the application and blocks the event loop while it is up. This is the
 * same modal shell as every other dialog, so there is one vocabulary.
 */
@Component({
  selector: 'cad-confirm',
  standalone: true,
  imports: [ModalComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <cad-modal [title]="title()" (closed)="closed.emit()">
      <p>{{ message() }}</p>
      <ng-container footer>
        <button class="danger" (click)="confirmed.emit()">{{ confirmLabel() }}</button>
        <button (click)="closed.emit()">Cancel</button>
      </ng-container>
    </cad-modal>
  `,
  styles: [
    `
      .danger {
        border-color: var(--error);
        color: var(--error);
      }
    `,
  ],
})
export class ConfirmComponent {
  readonly title = input.required<string>();
  readonly message = input.required<string>();
  readonly confirmLabel = input('Delete');
  readonly confirmed = output<void>();
  readonly closed = output<void>();
}
