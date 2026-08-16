/**
 * The geometry manager: what sketches and datums a project has.
 *
 * Datums come first because a sketch can only attach to one — that ordering is
 * the rule which keeps directions absolute, made visible in the UI.
 */

import {
  ChangeDetectionStrategy,
  Component,
  inject,
  output,
  signal,
} from '@angular/core';
import { ModalComponent } from '../../dialogs/components/dialogs.component';
import { ProjectStore } from '../../core/services/project-store';

@Component({
  selector: 'cad-geometry-manager',
  standalone: true,
  imports: [ModalComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <cad-modal title="Sketches &amp; datums" (closed)="closed.emit()">
      <div class="section-head">
        <span>Datums</span>
        <span class="spacer"></span>
        <button (click)="showNewDatum.set(!showNewDatum())">
          {{ showNewDatum() ? 'Cancel' : '+ datum' }}
        </button>
      </div>

      @if (showNewDatum()) {
        <div class="new-row">
          <input
            class="id"
            [value]="datumId()" (input)="datumId.set($any($event.target).value)"
            placeholder="mid"
            spellcheck="false"
          />
          <input
            [value]="datumZ()" (input)="datumZ.set($any($event.target).value)"
            placeholder="offset along Z — e.g. plate_t / 2"
            spellcheck="false"
          />
          <button class="primary" [disabled]="!datumId()" (click)="createDatum()">Add</button>
        </div>
        <div class="hint">
          A datum is computed from parameters, never from picked geometry. This adds one
          parallel to XY at the given height; use the Source view for arbitrary orientations.
        </div>
      }

      @for (datum of store.datumList(); track datum.id) {
        <div class="entry">
          <span class="name">{{ datum.id }}</span>
          @if (datum.builtIn) {
            <span class="type">built in</span>
          }
          <span class="spacer"></span>
          @if (!datum.builtIn) {
            <button (click)="removeDatum(datum.id)">×</button>
          }
        </div>
      }

      <div class="section-head top">
        <span>Sketches</span>
        <span class="spacer"></span>
        <button (click)="newSketch.emit()">+ sketch</button>
      </div>

      @for (sketch of store.sketchList(); track sketch.id) {
        <div class="entry clickable" (click)="editSketch.emit(sketch.id)">
          <span class="name">{{ sketch.id }}</span>
          <span class="type">on {{ sketch.plane }}</span>
          <span class="spacer"></span>
          <span class="type">
            {{ sketch.pointCount }}p · {{ sketch.curveCount }}c · {{ sketch.loopCount }}l
          </span>
        </div>
      }
      @if (store.sketchList().length === 0) {
        <div class="hint">No sketches yet. Add one, then pad its loop.</div>
      }

      <button footer (click)="closed.emit()">Close</button>
    </cad-modal>
  `,
  styles: [
    `
      .section-head {
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        color: var(--text-dim);
        margin-bottom: 6px;
      }
      .section-head.top {
        margin-top: 18px;
      }
      .spacer {
        flex: 1;
      }
      .entry {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 5px 8px;
        border-bottom: 1px solid rgba(38, 49, 64, 0.5);
        font-family: var(--mono);
        font-size: 12px;
      }
      .entry.clickable {
        cursor: pointer;
      }
      .entry.clickable:hover {
        background: var(--bg-raised);
      }
      .type {
        color: var(--text-faint);
        font-size: 10px;
      }
      .new-row {
        display: flex;
        gap: 6px;
        margin-bottom: 6px;
      }
      .new-row .id {
        width: 120px;
      }
      .hint {
        font-size: 11px;
        color: var(--text-faint);
        line-height: 1.6;
        margin-bottom: 10px;
      }
    `,
  ],
})
export class GeometryManagerComponent {
  readonly store = inject(ProjectStore);
  readonly closed = output<void>();
  readonly newSketch = output<void>();
  readonly editSketch = output<string>();

  readonly showNewDatum = signal(false);
  readonly datumId = signal('');
  readonly datumZ = signal('0');

  async createDatum(): Promise<void> {
    const raw = this.datumZ().trim();
    const asNumber = Number(raw);
    const height = raw !== '' && Number.isFinite(asNumber) ? asNumber : raw;
    const ok = await this.store.putDatum({
      id: this.datumId().trim(),
      origin: [0, 0, height],
      normal: [0, 0, 1],
    });
    if (ok) {
      this.showNewDatum.set(false);
      this.datumId.set('');
      this.datumZ.set('0');
    }
  }

  removeDatum(id: string): void {
    void this.store.deleteDatum(id);
  }
}
