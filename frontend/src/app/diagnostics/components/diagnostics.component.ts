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

import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
  signal,
} from '@angular/core';

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
    @if (rows().length === 0) {
      <div class="empty">No geometry yet.</div>
    } @else {
      @for (body of rows(); track body.id) {
        @if (body.showHead) {
          <div class="body-head" [title]="body.twistyTitle" (click)="toggleBody(body.id)">
            <button class="twisty">{{ body.twisty }}</button>
            {{ body.id }}
            <span class="count">{{ body.faceCount }}</span>
          </div>
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
        @if (body.retiredCount > 0) {
          <div class="panel-header retired-head" (click)="toggleRetired(body.id)">
            <button class="twisty">{{ body.retiredTwisty }}</button>
            Retired
            <span class="count">{{ body.retiredCount }}</span>
          </div>
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
        display: flex;
        align-items: center;
        gap: 4px;
        cursor: pointer;
        padding: 4px 10px;
        font-family: var(--mono);
        font-size: 10px;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        color: var(--accent);
        background: var(--bg-raised);
        border-bottom: 1px solid var(--border);
      }
      .retired-head {
        display: flex;
        align-items: center;
        gap: 4px;
        cursor: pointer;
      }
      /* A hit target rather than a control: the whole heading row toggles, and
         the arrow only has to say which way it will go. */
      .twisty {
        background: none;
        border: none;
        padding: 0 2px;
        color: inherit;
        font-size: 10px;
        line-height: 1;
        cursor: pointer;
      }
      .count { opacity: 0.55; }
    `,
  ],
})
export class TopologyListComponent {
  readonly bodies = input.required<BodyTopology[]>();
  readonly selected = input<ReadonlySet<string>>(new Set<string>());
  readonly picked = output<{ tag: string; mode: SelectMode }>();

  /** Bodies folded away. View state, so it lives here and not in the store. */
  private readonly collapsed = signal<ReadonlySet<string>>(new Set<string>());

  /**
   * Retired tags start folded.
   *
   * They are history — useful when a selector has just stopped resolving, noise
   * the rest of the time — and on a model with any churn there are more of them
   * than live faces. So this set records which bodies have been *expanded*,
   * the opposite way round to `collapsed`.
   */
  private readonly retiredOpen = signal<ReadonlySet<string>>(new Set<string>());

  /**
   * Every per-body flag resolved once, rather than per row per render.
   *
   * A collapsed body contributes no face rows at all: this list is the longest
   * thing on screen on a real model, and hiding rows with CSS would still cost
   * the render.
   */
  readonly rows = computed(() => {
    const folded = this.collapsed();
    const open = this.retiredOpen();
    const many = this.bodies().length > 1;
    return this.bodies().map((body) => {
      // With one body there is no heading, so there would be nothing to
      // expand it from again.
      const isFolded = many && folded.has(body.id);
      const showRetired = open.has(body.id);
      return {
        id: body.id,
        showHead: many,
        faces: isFolded ? [] : body.faces,
        faceCount: body.faces.length,
        retired: isFolded || !showRetired ? [] : body.retired,
        retiredCount: body.retired.length,
        twisty: isFolded ? '▸' : '▾',
        twistyTitle: isFolded ? 'Expand this body' : 'Collapse this body',
        retiredTwisty: showRetired ? '▾' : '▸',
      };
    });
  });

  toggleBody(id: string): void {
    this.collapsed.update((folded) => {
      const next = new Set(folded);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  }

  toggleRetired(id: string): void {
    this.retiredOpen.update((open) => {
      const next = new Set(open);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  }

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
