/**
 * The diagnostics panel and the topology browser.
 *
 * Diagnostics are the visible half of the project's central promise: when a
 * rebuild refuses to guess which face a selector meant, this is where it says
 * so, and names the feature responsible.
 *
 * The topology list is the discovery surface — every tag that currently exists,
 * clickable to highlight in the viewport, plus the tags that have been retired
 * and by which feature.
 */

import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import type { BodyTopology, DiagnosticView } from '../../core/models/cad.models';
import type { SelectMode } from '../../core/services/project-store';

@Component({
  selector: 'cad-diagnostics',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (diagnostics().length === 0) {
      <div class="empty">Model builds cleanly.</div>
    } @else {
      @for (item of diagnostics(); track item.headline) {
        <div class="diagnostic">
          <div class="headline">{{ item.headline }}</div>
          <div>{{ item.message }}</div>
          @for (reason of item.reasons; track reason) {
            <div class="reason">{{ reason }}</div>
          }
        </div>
      }
    }
  `,
})
export class DiagnosticsComponent {
  readonly diagnostics = input.required<DiagnosticView[]>();
}

@Component({
  selector: 'cad-topology-list',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (bodies().length === 0) {
      <div class="empty">No geometry yet.</div>
    } @else {
      @for (body of bodies(); track body.id) {
        @if (bodies().length > 1) {
          <div class="body-head">{{ body.id }}</div>
        }
        @for (face of body.faces; track face.tag) {
          <div
            class="feature"
            [class.selected]="selected().has(face.tag)"
            (click)="pick(face.tag, $event)"
          >
            <span class="ellipsis">{{ face.tag }}</span>
          </div>
        }
        @if (body.retired.length > 0) {
          <div class="panel-header">Retired</div>
          @for (retired of body.retired; track retired.tag) {
            <div class="feature" [title]="retired.reason">
              <span class="type struck">{{ retired.tag }}</span>
              <span class="spacer"></span>
              <span class="type">by {{ retired.retired_by ?? '?' }}</span>
            </div>
          }
        }
      }
    }
  `,
  styles: [
    `
      .ellipsis {
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .struck {
        text-decoration: line-through;
      }
      .body-head {
        padding: 4px 10px;
        font-family: var(--mono);
        font-size: 10px;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        color: var(--accent);
        background: var(--bg-raised);
        border-bottom: 1px solid var(--border);
      }
    `,
  ],
})
export class TopologyListComponent {
  readonly bodies = input.required<BodyTopology[]>();
  readonly selected = input<ReadonlySet<string>>(new Set<string>());
  readonly picked = output<{ tag: string; mode: SelectMode }>();

  /**
   * Ctrl/cmd adds one, shift extends a run, a plain click replaces.
   *
   * The same convention as every file list, chosen so nobody has to learn a
   * selection idiom that only exists here.
   */
  pick(tag: string, event: MouseEvent): void {
    const mode: SelectMode = event.shiftKey
      ? 'range'
      : event.ctrlKey || event.metaKey
        ? 'toggle'
        : 'replace';
    this.picked.emit({ tag, mode });
  }
}
