/**
 * The feature history, in build order, with each entry's build status.
 *
 * The status dot is the fastest way to see where a rebuild stopped: green built,
 * grey served from cache, red failed, faded skipped because something earlier
 * failed.
 */

import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type { FeatureView } from '../../core/models/cad.models';

@Component({
  selector: 'cad-feature-tree',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (rows().length === 0) {
      <div class="empty">No features. Add a pad to begin.</div>
    } @else {
      @for (group of rows(); track group.body) {
        @if (showHeads()) {
          <div
            class="body-head"
            [class.selected]="group.active"
            [class.dim]="group.hidden"
            title="Work on this body"
            (click)="bodyActivated.emit(group.body)"
          >
            {{ group.body }}
            <span class="spacer"></span>
            <button [title]="group.eyeTitle" (click)="toggleVisibility(group.body, $event)">
              {{ group.eye }}
            </button>
            <button title="Delete this body" (click)="removeBody(group.body, $event)">×</button>
          </div>
        }
        @for (feature of group.features; track feature.id) {
        <div
          class="feature"
          [class.selected]="feature.id === selected()"
          [class.dim]="group.hidden"
          [title]="feature.tooltip"
          (click)="picked.emit(feature.id)"
        >
          <span class="status-dot" [class]="feature.statusClass"></span>
          <span>{{ feature.id }}</span>
          <span class="type">{{ feature.type }}</span>
          <span class="spacer"></span>
          @if (feature.faceLabel) {
            <span class="type">{{ feature.faceLabel }}</span>
          }
          <button title="Edit feature" (click)="edit(feature.id, $event)">✎</button>
          <button title="Delete feature" (click)="remove(feature.id, $event)">×</button>
        </div>
        }
      }
    }
  `,
  styles: [
    `
      .body-head {
        display: flex;
        align-items: center;
        gap: 4px;
        padding: 4px 10px;
        font-family: var(--mono);
        font-size: 10px;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        color: var(--accent);
        background: var(--bg-raised);
        border-bottom: 1px solid var(--border);
        cursor: pointer;
      }
      .body-head .spacer { flex: 1; }
      /* The same marker the topology list and the feature rows use for
         "this is the one", so there is one vocabulary to learn, not two. */
      .body-head.selected { box-shadow: inset 2px 0 0 var(--accent); }
      .dim { opacity: 0.45; }
    `,
  ],
})
export class FeatureTreeComponent {
  readonly groups = input.required<{ body: string; features: FeatureView[] }[]>();
  readonly selected = input<string | null>(null);
  /** The body being worked on, or null for "all bodies". */
  readonly activeBody = input<string | null>(null);
  /** Bodies the viewport is not drawing. Shown dimmed, never removed. */
  readonly hiddenBodies = input<ReadonlySet<string>>(new Set<string>());
  readonly picked = output<string>();
  readonly deleted = output<string>();
  readonly edited = output<string>();
  readonly bodyDeleted = output<string>();
  readonly bodyActivated = output<string>();
  readonly bodyVisibilityToggled = output<string>();

  /**
   * The groups with every per-body flag already resolved.
   *
   * The heading needs four answers about each body; asking for them from the
   * template would mean four calls per row per render.
   */
  readonly rows = computed(() => {
    const active = this.activeBody();
    const hidden = this.hiddenBodies();
    return this.groups().map((group) => {
      const isHidden = hidden.has(group.body);
      return {
        body: group.body,
        features: group.features,
        active: group.body === active,
        hidden: isHidden,
        eye: isHidden ? '◌' : '◉',
        eyeTitle: isHidden ? 'Show this body' : 'Hide this body',
      };
    });
  });

  /** One body needs no headings: there is nothing to tell it apart from. */
  readonly showHeads = computed(() => this.groups().length > 1);

  toggleVisibility(body: string, event: Event): void {
    event.stopPropagation();
    this.bodyVisibilityToggled.emit(body);
  }

  removeBody(body: string, event: Event): void {
    event.stopPropagation();
    this.bodyDeleted.emit(body);
  }

  remove(id: string, event: Event): void {
    event.stopPropagation();
    this.deleted.emit(id);
  }

  edit(id: string, event: Event): void {
    event.stopPropagation();
    this.edited.emit(id);
  }
}
