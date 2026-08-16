/**
 * The parameter sheet — the primary way to drive the model.
 *
 * A cell accepts either a number or an expression; typing `plate_w * 0.6` is as
 * valid as typing `72`. Editing commits on blur or Enter, which rebuilds exactly
 * the features that depend on that parameter.
 *
 * The template binds only to pre-computed values from the store; no function is
 * called during change detection.
 */

import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import type { ParameterGroup, ParameterView } from '../../core/models/cad.models';

@Component({
  selector: 'cad-parameter-sheet',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  styles: [
    `
      td.editable {
        cursor: pointer;
      }
      td.editable:hover {
        color: var(--accent);
      }
    `,
  ],
  template: `
    @if (groups().length === 0) {
      <div class="empty">No parameters yet.</div>
    } @else {
      <table class="sheet">
        <thead>
          <tr>
            <th style="width: 38%">Name</th>
            <th style="width: 36%">Value / Expression</th>
            <th style="width: 26%; text-align: right">mm / deg</th>
          </tr>
        </thead>
        <tbody>
          @for (group of groups(); track group.name) {
            @if (group.name) {
              <tr class="group-header">
                <td colspan="3">{{ group.name }}</td>
              </tr>
            }
            @for (row of group.rows; track row.name) {
              <tr>
                <td
                  class="name editable"
                  [title]="row.doc || 'Click to rename or edit'"
                  (click)="edit.emit(row.name)"
                >
                  {{ row.name }}
                  @if (row.unit !== 'mm') {
                    <span class="derived">({{ row.unit }})</span>
                  }
                </td>
                <td>
                  <input
                    [value]="row.input"
                    [class.derived]="row.isDerived"
                    [disabled]="disabled()"
                    spellcheck="false"
                    (blur)="commitFrom($event, row)"
                    (keydown.enter)="blur($event)"
                    (keydown.escape)="reset($event, row)"
                  />
                </td>
                <td class="computed">{{ row.resolved }}</td>
              </tr>
            }
          }
        </tbody>
      </table>
    }
  `,
})
export class ParameterSheetComponent {
  readonly groups = input.required<ParameterGroup[]>();
  readonly disabled = input(false);
  readonly changed = output<{ name: string; raw: string }>();
  readonly edit = output<string>();

  commitFrom(event: Event, row: ParameterView): void {
    const input = event.target as HTMLInputElement;
    const raw = input.value;
    if (raw === row.input) return;
    this.changed.emit({ name: row.name, raw });
  }

  blur(event: Event): void {
    (event.target as HTMLInputElement).blur();
  }

  reset(event: Event, row: ParameterView): void {
    const input = event.target as HTMLInputElement;
    input.value = row.input;
    input.blur();
  }
}
