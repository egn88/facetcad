/**
 * Adding and editing parameter rows.
 *
 * Inline editing in the sheet covers the common case — change a value. This
 * dialog covers the rest: creating a row, renaming one, and setting the unit,
 * group and description.
 *
 * Renaming is the interesting operation. The backend rewrites every expression
 * that reads the old name, so it is safe, but the dialog says so explicitly
 * because a rename that silently broke twenty formulas is exactly what a user
 * would fear.
 */

import {
  ChangeDetectionStrategy,
  Component,
  inject,
  input,
  output,
  signal,
} from '@angular/core';
import { ModalComponent } from '../../dialogs/components/dialogs.component';
import { ProjectStore } from '../../core/services/project-store';
import type { ParameterRow } from '../../core/models/cad.models';

const UNITS = ['mm', 'cm', 'm', 'in', 'ft', 'deg', 'rad', ''];

@Component({
  selector: 'cad-parameter-editor',
  standalone: true,
  imports: [ModalComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <cad-modal [title]="editing() ? 'Edit parameter' : 'New parameter'" (closed)="closed.emit()">
      <div class="field">
        <label>Name</label>
        <input
          [value]="name()" (input)="name.set($any($event.target).value)"
          spellcheck="false"
          placeholder="plate_w"
        />
        @if (editing() && name() !== original()?.name) {
          <div class="hint">
            Renaming rewrites every expression that reads
            <code>{{ original()?.name }}</code
            >, throughout the document.
          </div>
        }
      </div>

      <div class="row">
        <div class="field grow">
          <label>Value or expression</label>
          <input
            [value]="input()" (input)="input.set($any($event.target).value)"
            spellcheck="false"
            placeholder="120  or  plate_w * 0.6"
          />
        </div>
        <div class="field narrow">
          <label>Unit</label>
          <select [value]="unit()" (change)="unit.set($any($event.target).value)">
            @for (option of units; track option) {
              <option [value]="option">{{ option || '(none)' }}</option>
            }
          </select>
        </div>
      </div>

      <div class="row">
        <div class="field grow">
          <label>Group</label>
          <input
            [value]="group()" (input)="group.set($any($event.target).value)"
            placeholder="Plate"
          />
        </div>
        <div class="field grow">
          <label>Description</label>
          <input [value]="doc()" (input)="doc.set($any($event.target).value)" />
        </div>
      </div>

      @if (usage().length > 0) {
        <div class="hint">
          Read by {{ usage().join(', ') }}.
        </div>
      }

      <ng-container footer>
        @if (error()) {
          <span class="error-text grow">{{ error() }}</span>
        }
        @if (editing()) {
          <button
            class="danger"
            [disabled]="busy() || usage().length > 0"
            [title]="usage().length > 0 ? 'Still in use' : 'Delete this parameter'"
            (click)="remove()"
          >
            Delete
          </button>
        }
        <button (click)="closed.emit()">Cancel</button>
        <button class="primary" [disabled]="busy() || !name()" (click)="save()">
          {{ editing() ? 'Save' : 'Add' }}
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
      .grow {
        flex: 1;
      }
      .narrow {
        width: 90px;
      }
      .hint {
        margin-top: 4px;
        font-size: 11px;
        color: var(--text-faint);
        line-height: 1.5;
      }
      .error-text {
        color: var(--error);
      }
      button.danger {
        border-color: var(--error);
        color: var(--error);
      }
    `,
  ],
})
export class ParameterEditorComponent {
  private readonly store = inject(ProjectStore);

  /** Omitted when creating a new parameter. */
  readonly original = input<ParameterRow | null>(null);
  readonly closed = output<void>();

  readonly units = UNITS;
  readonly name = signal('');
  readonly input = signal('');
  readonly unit = signal('mm');
  readonly group = signal('');
  readonly doc = signal('');
  readonly usage = signal<string[]>([]);
  readonly error = signal<string | null>(null);
  readonly busy = signal(false);

  readonly editing = signal(false);

  constructor() {
    // Populate immediately rather than on first interaction: with zoneless
    // change detection a plain field assigned later would not render.
    queueMicrotask(async () => {
      const row = this.original();
      if (!row) return;
      this.editing.set(true);
      this.name.set(row.name);
      this.input.set(row.expr ?? String(row.value ?? ''));
      this.unit.set(row.unit ?? 'mm');
      this.group.set(row.group ?? '');
      this.doc.set(row.doc ?? '');
      this.usage.set(await this.store.parameterUsage(row.name));
    });
  }

  async save(): Promise<void> {
    this.error.set(null);
    this.busy.set(true);
    try {
      const row = this.asRow();
      const ok = this.editing()
        ? await this.store.editParameter(this.original()!.name, row)
        : await this.store.addParameter(row);
      if (ok) this.closed.emit();
    } finally {
      this.busy.set(false);
    }
  }

  async remove(): Promise<void> {
    this.busy.set(true);
    try {
      if (await this.store.deleteParameter(this.original()!.name)) this.closed.emit();
    } finally {
      this.busy.set(false);
    }
  }

  /** A numeric input becomes a literal; anything else is an expression. */
  private asRow(): ParameterRow {
    const raw = this.input().trim();
    const asNumber = Number(raw);
    const numeric = raw !== '' && Number.isFinite(asNumber);
    return {
      name: this.name().trim(),
      value: numeric ? asNumber : undefined,
      expr: numeric ? undefined : raw,
      unit: this.unit(),
      group: this.group().trim(),
      doc: this.doc().trim(),
    };
  }
}
