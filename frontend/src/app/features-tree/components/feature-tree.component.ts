/**
 * The feature history, in build order, with each entry's build status.
 *
 * The status dot is the fastest way to see where a rebuild stopped: green built,
 * grey served from cache, red failed, faded skipped because something earlier
 * failed.
 */

import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
  signal,
} from '@angular/core';

import type { BodyGroup } from '../../core/models/cad.models';

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
            <button
              class="twisty"
              [title]="group.twistyTitle"
              (click)="toggleCollapsed(group.body, $event)"
            >
              {{ group.twisty }}
            </button>
            {{ group.body }}
            @if (group.copyOf) {
              <span class="copy" [title]="group.copyTitle">copy of {{ group.copyOf }}</span>
            } @else {
              <span class="count">{{ group.features.length }}</span>
            }
            @if (group.quantityLabel) {
              <span class="quantity" [title]="group.quantityTitle">{{ group.quantityLabel }}</span>
            }
            <span class="spacer"></span>
            <button
              title="Show this body again at another placement — one build, edited once"
              (click)="duplicate(group.body, $event)"
            >
              ⧉
            </button>
            <button [title]="group.eyeTitle" (click)="toggleVisibility(group.body, $event)">
              {{ group.eye }}
            </button>
            <button [title]="group.deleteTitle" (click)="removeBody(group.body, $event)">×</button>
          </div>
        }
        @if (group.copyOf && !group.folded) {
          <div class="copy-note">
            Built by <button class="link" (click)="bodyActivated.emit(group.copyOf)">
              {{ group.copyOf }}</button>. Edit it there and every copy follows.
          </div>
        }
        @for (feature of group.visibleFeatures; track feature.id) {
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
      /* Flat and narrow: the twisty is a hit target, not a button in its own
         right, and must not compete with the eye and delete controls. */
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
      /* The piece count reads as a fact about the part, so it sits with the
         name rather than among the buttons that act on it. */
      .quantity {
        font-weight: 600;
        color: var(--accent);
        opacity: 0.9;
      }
      .copy {
        opacity: 0.55;
        text-transform: none;
        letter-spacing: 0;
      }
      .copy-note {
        padding: 4px 10px 4px 26px;
        font-size: 11px;
        color: var(--fg-muted, inherit);
        opacity: 0.7;
      }
      .link {
        background: none;
        border: none;
        padding: 0;
        color: var(--accent);
        font: inherit;
        cursor: pointer;
        text-decoration: underline;
      }
      /* The same marker the topology list and the feature rows use for
         "this is the one", so there is one vocabulary to learn, not two. */
      .body-head.selected { box-shadow: inset 2px 0 0 var(--accent); }
      .dim { opacity: 0.45; }
    `,
  ],
})
export class FeatureTreeComponent {
  readonly groups = input.required<BodyGroup[]>();
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
  readonly bodyDuplicated = output<string>();

  /**
   * Bodies whose features are folded away.
   *
   * Collapsing is a view convenience, not document state, so it lives here
   * rather than in the store — and it is keyed on collapse rather than expand
   * so a body that appears later starts open, which is what someone who just
   * created it expects.
   */
  private readonly collapsed = signal<ReadonlySet<string>>(new Set<string>());

  /**
   * The groups with every per-body flag already resolved.
   *
   * The heading needs half a dozen answers about each body; asking for them
   * from the template would mean that many calls per row per render.
   */
  readonly rows = computed(() => {
    const active = this.activeBody();
    const hidden = this.hiddenBodies();
    const folded = this.collapsed();
    // With a single body there is no heading, and so nothing to expand from.
    const collapsible = this.groups().length > 1;
    return this.groups().map((group) => {
      const isHidden = hidden.has(group.body);
      const isFolded = collapsible && folded.has(group.body);
      // Only worth saying once it is more than one. A model of one-offs
      // labelled "x1" everywhere is a row of noise to read past.
      const repeats = group.quantity > 1;
      return {
        body: group.body,
        features: group.features,
        visibleFeatures: isFolded ? [] : group.features,
        folded: isFolded,
        active: group.body === active,
        hidden: isHidden,
        copyOf: group.of,
        copyTitle: group.of
          ? `The same solid as ${group.of}, placed here. Built once, and edited there.`
          : '',
        quantityLabel: repeats ? `x${group.quantity}` : '',
        quantityTitle: repeats
          ? `${group.quantity} of this part in the model — how many to produce`
          : '',
        deleteTitle: repeats
          ? `Delete this body — refused while ${group.quantity - 1} ` +
            `cop${group.quantity === 2 ? 'y' : 'ies'} of it remain`
          : 'Delete this body',
        eye: isHidden ? '◌' : '◉',
        eyeTitle: isHidden ? 'Show this body' : 'Hide this body',
        twisty: isFolded ? '▸' : '▾',
        twistyTitle: isFolded ? 'Expand this body' : 'Collapse this body',
      };
    });
  });

  /** One body needs no headings: there is nothing to tell it apart from. */
  readonly showHeads = computed(() => this.groups().length > 1);

  toggleCollapsed(body: string, event: Event): void {
    event.stopPropagation();
    this.collapsed.update((folded) => {
      const next = new Set(folded);
      if (!next.delete(body)) next.add(body);
      return next;
    });
  }

  toggleVisibility(body: string, event: Event): void {
    event.stopPropagation();
    this.bodyVisibilityToggled.emit(body);
  }

  duplicate(body: string, event: Event): void {
    event.stopPropagation();
    this.bodyDuplicated.emit(body);
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
