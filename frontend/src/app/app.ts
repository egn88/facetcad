/**
 * The application shell.
 *
 * Three columns: the parameter sheet and feature tree on the left, the viewport
 * in the middle, diagnostics and topology on the right. Every action has a
 * keyboard route — the mouse is for discovering face names, not for authoring.
 */

import {
  ChangeDetectionStrategy,
  Component,
  HostListener,
  inject,
  signal,
  viewChild,
} from '@angular/core';

import { ProjectStore } from './core/services/project-store';
import type { ParameterRow } from './core/models/cad.models';
import { ViewportComponent } from './viewport/components/viewport.component';
import { ParameterSheetComponent } from './parameters/components/parameter-sheet.component';
import { FeatureTreeComponent } from './features-tree/components/feature-tree.component';
import {
  DiagnosticsComponent,
  TopologyListComponent,
} from './diagnostics/components/diagnostics.component';
import { ParameterEditorComponent } from './parameters/components/parameter-editor.component';
import { GeometryManagerComponent } from './sketches/components/geometry-manager.component';
import { SketchEditorComponent } from './sketches/components/sketch-editor.component';
import {
  AddFeatureComponent,
  CutSettingsComponent,
  DocumentEditorComponent,
  NewProjectComponent,
  SketchHereComponent,
  SelectorConsoleComponent,
} from './dialogs/components/dialogs.component';
import type { FeaturePrefill } from './dialogs/components/dialogs.component';
import type { FeatureRow } from './core/models/cad.models';

type Dialog =
  | 'none'
  | 'cut'
  | 'point'
  | 'selector'
  | 'document'
  | 'feature'
  | 'project'
  | 'parameter'
  | 'geometry'
  | 'sketch';

@Component({
  selector: 'app-root',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    ViewportComponent,
    ParameterEditorComponent,
    GeometryManagerComponent,
    SketchEditorComponent,
    ParameterSheetComponent,
    FeatureTreeComponent,
    DiagnosticsComponent,
    TopologyListComponent,
    SelectorConsoleComponent,
    DocumentEditorComponent,
    AddFeatureComponent,
    NewProjectComponent,
    SketchHereComponent,
    CutSettingsComponent,
  ],
  template: `
    <div class="app">
      <div class="topbar">
        <h1>FacetCAD</h1>

        <select
          style="width: 200px"
          [value]="store.projectId() ?? ''"
          (change)="openProject($event)"
        >
          @if (store.projects().length === 0) {
            <option value="">no projects</option>
          }
          @for (project of store.projects(); track project.id) {
            <option [value]="project.id">{{ project.name }}</option>
          }
        </select>

        <button (click)="dialog.set('project')">New</button>
        <button [disabled]="!store.projectId()" (click)="dialog.set('feature')">
          Add feature <span class="badge">A</span>
        </button>
        <button [disabled]="!store.projectId()" (click)="dialog.set('geometry')">
          Sketches <span class="badge">S</span>
        </button>
        <button [disabled]="!store.projectId()" (click)="dialog.set('selector')">
          Selectors <span class="badge">⌘K</span>
        </button>
        <button [disabled]="!store.projectId()" (click)="dialog.set('document')">
          Source <span class="badge">E</span>
        </button>

        <span class="spacer"></span>

        @if (store.build()) {
          <span class="badge" [class]="store.statusClass()">{{ store.statusLabel() }}</span>
        }
        @if (store.kernel(); as kernel) {
          <span class="badge">kernel: {{ kernel.name }}</span>
        }
        @if (store.exportUrls(); as urls) {
          <a [href]="urls.stl" download
            ><button [title]="store.stlTitle()">STL</button></a
          >
          @for (body of store.bodyExportUrls(); track body.id) {
            <a [href]="body.stl" download
              ><button [title]="'Just the ' + body.id + ' body, for the print bed'">
                {{ body.id }}
              </button></a
            >
          }
          <a [href]="urls.csv" download
            ><button title="Export the parameter table as CSV">Sheet</button></a
          >
          <button
            title="Replace the parameter table from a CSV — edit it in Excel or Calc and bring it back"
            (click)="sheetInput.click()"
          >
            Import
          </button>
          <input
            #sheetInput
            type="file"
            accept=".csv,text/csv"
            hidden
            (change)="importSheet($event)"
          />
          <a [href]="urls.yaml" download><button>YAML</button></a>
        }
        @if (store.drawingUrls(); as urls) {
          <a [href]="urls.views" download
            ><button title="Top, front and right sections as DXF">Views</button></a
          >
          <a [href]="urls.flat" download
            ><button title="Every flat face of the part, laid out for cutting">
              Faces
            </button></a
          >
          <button
            title="This part's own faces, with finger joints on shared edges"
            (click)="dialog.set('cut')"
          >
            Joined
          </button>
          <a [href]="urls.enclosure" download
            ><button title="Laser-cut box sized to fit this part inside">Box</button></a
          >
        }
      </div>

      <div class="workspace">
        <div class="panel">
          <div class="panel-section grow">
            <div class="panel-header">
              Parameters<span class="spacer"></span>
              <span class="badge">{{ store.document()?.parameters?.length ?? 0 }}</span>
              <button
                title="Add a parameter"
                [disabled]="!store.projectId()"
                (click)="newParameter()"
              >
                +
              </button>
            </div>
            <div class="panel-body">
              <cad-parameter-sheet
                [groups]="store.parameterGroups()"
                [disabled]="store.busy()"
                (changed)="store.setParameter($event.name, $event.raw)"
                (edit)="editParameter($event)"
              />
            </div>
          </div>

          <div class="panel-section features">
            <div class="panel-header">
              Features<span class="spacer"></span>
              <span class="badge">{{ store.featureViews().length }}</span>
              <span class="badge" title="The body new features are added to"
                >{{ store.activeBodyLabel() }}</span
              >
              <button title="Add a body" (click)="addBody()">+ body</button>
            </div>
            <div class="panel-body">
              <cad-feature-tree
                [groups]="store.featureGroups()"
                [selected]="store.selectedFeature()"
                [activeBody]="store.activeBody()"
                [hiddenBodies]="store.hiddenBodies()"
                (picked)="store.toggleFeature($event)"
                (deleted)="store.removeFeature($event)"
                (edited)="editFeature($event)"
                (bodyDeleted)="store.deleteBody($event)"
                (bodyActivated)="store.toggleActiveBody($event)"
                (bodyVisibilityToggled)="store.toggleBodyVisibility($event)"
              />
            </div>
          </div>
        </div>

        <div class="viewport">
          <cad-viewport
            #viewport
            [bodies]="store.visibleBodyMeshes()"
            [selected]="store.selectedTagSet()"
            [resetKey]="store.projectId()"
            [sketches]="store.sketchGeometry()"
            [showSketches]="store.showSketches()"
            (tagPicked)="store.pick($event.tag, $event.mode, $event.point)"
          />

          <div class="overlay toggles">
            <button
              [class.active]="store.showSketches()"
              title="Show sketch curves and points (V)"
              (click)="store.toggleSketches()"
            >
              sketches
            </button>
          </div>

          <div class="overlay hint">
            <div>click a face to read its tag</div>
            <div>⌘K selectors · A feature · P param · S sketches · V show · E source · F fit</div>
          </div>

          @if (store.meshStats(); as stats) {
            <div class="overlay stats">
              @if (stats.bodies > 1) {
                <div>{{ stats.bodies }} bodies</div>
              }
              <div>{{ stats.faces }} faces</div>
              <div>{{ stats.edges }} edges</div>
              <div>{{ stats.triangles }} triangles</div>
            </div>
          }

          @if (store.selectedTags().length > 0) {
            <div class="overlay selection">
              @if (store.selectedTag(); as tag) {
                <span class="tag">{{ tag }}</span>
              } @else {
                <span class="tag">{{ store.selectedTags().length }} faces selected</span>
              }
              <span class="spacer"></span>
              @if (store.selectedTag() || store.pickedPoint()) {
                <button
                  title="Start a sketch on the face you clicked"
                  (click)="dialog.set('point')"
                >
                  Sketch here
                </button>
              }
              @if (store.cutUrl(); as url) {
                <a [href]="url" download
                  ><button title="Cut path of the selected faces as DXF">DXF</button></a
                >
              }
              <button (click)="store.copySelection()">Copy</button>
              <button (click)="dialog.set('selector')">Test</button>
              <button title="Fillet the edges of this selection" (click)="blend('fillet')">
                Fillet
              </button>
              <button title="Chamfer the edges of this selection" (click)="blend('chamfer')">
                Chamfer
              </button>
              <button (click)="store.clearSelection()">Clear</button>
            </div>
          }
        </div>

        <div class="panel right">
          <div class="panel-section diagnostics">
            <div class="panel-header">Diagnostics</div>
            <div class="panel-body">
              <cad-diagnostics [diagnostics]="store.diagnostics()" />
            </div>
          </div>

          <div class="panel-section grow">
            <div class="panel-header">
              Topology<span class="spacer"></span>
              <span class="badge">{{ store.faceTags().length }}</span>
            </div>
            <div class="panel-body">
              <cad-topology-list
                [bodies]="store.visibleTopologies()"
                [selected]="store.selectedTagSet()"
                (picked)="store.selectTag($event.tag, $event.mode)"
              />
            </div>
          </div>
        </div>
      </div>

      @if (store.projectId(); as id) {
        @switch (dialog()) {
          @case ('selector') {
            <cad-selector-console
              [projectId]="id"
              [initial]="store.selectionSelector()"
              (closed)="dialog.set('none')"
            />
          }
          @case ('cut') {
            <cad-cut-settings
              [projectId]="id"
              [selection]="store.selectedTags()"
              [faces]="store.faceTags()"
              (highlighted)="store.selectTags($event)"
              (closed)="dialog.set('none')"
            />
          }
          @case ('point') {
            @if (store.selectedTag() || store.pickedPoint()) {
              <cad-sketch-here
                [projectId]="id"
                [point]="store.pickedPoint()"
                [faceTag]="store.selectedTag()"
                [sketches]="store.document()?.sketches ?? {}"
                (placed)="afterPoint($event)"
                (drawing)="afterSketch($event)"
                (closed)="dialog.set('none')"
              />
            }
          }
          @case ('document') {
            <cad-document-editor
              [projectId]="id"
              (closed)="dialog.set('none')"
              (saved)="store.reload()"
            />
          }
          @case ('parameter') {
            <cad-parameter-editor
              [original]="editingParameter()"
              (closed)="dialog.set('none')"
            />
          }
          @case ('geometry') {
            <cad-geometry-manager
              (closed)="dialog.set('none')"
              (newSketch)="openSketch(null)"
              (editSketch)="openSketch($event)"
            />
          }
          @case ('sketch') {
            <cad-sketch-editor [sketchId]="editingSketch()" (closed)="dialog.set('none')" />
          }
          @case ('feature') {
            <cad-add-feature
              [projectId]="id"
              [profiles]="store.profileOptions()"
              [points]="store.pointOptions()"
              [bodies]="store.bodyIds()"
              [activeBody]="store.activeBody()"
              [prefill]="prefill()"
              [editingFeature]="editingFeature()"
              [editingBody]="editingFeatureBody()"
              (closed)="closeFeatureDialog()"
              (added)="store.reload()"
            />
          }
        }
      }
      @if (dialog() === 'project') {
        <cad-new-project (closed)="dialog.set('none')" (created)="create($event)" />
      }

      @if (store.toast(); as toast) {
        <div class="toast" [class.error]="toast.error">{{ toast.text }}</div>
      }
    </div>
  `,
})
export class App {
  readonly store = inject(ProjectStore);
  readonly dialog = signal<Dialog>('none');
  /** Fields the add-feature dialog should open with, when a quick action set them. */
  readonly prefill = signal<FeaturePrefill | null>(null);
  /** The feature the dialog is editing, or null when it is adding a new one. */
  readonly editingFeature = signal<FeatureRow | null>(null);
  readonly editingFeatureBody = signal('');
  private readonly viewport = viewChild<ViewportComponent>('viewport');
  readonly editingParameter = signal<ParameterRow | null>(null);
  readonly editingSketch = signal<string | null>(null);

  newParameter(): void {
    this.editingParameter.set(null);
    this.dialog.set('parameter');
  }

  editParameter(name: string): void {
    const row = this.store.document()?.parameters.find((p) => p.name === name) ?? null;
    this.editingParameter.set(row);
    this.dialog.set('parameter');
  }

  /**
   * Turn the current face selection into a blend.
   *
   * Two faces become `a ^ b` — the edge, or the whole shared perimeter. Any
   * other count becomes the touching form. Either way the selector is written
   * into the dialog rather than applied blind, so it can be read and narrowed
   * (with `dir=`, say) before it lands in the history.
   */
  blend(type: 'fillet' | 'chamfer'): void {
    this.prefill.set({
      type,
      edges: this.store.selectionEdgeSelector(),
      body: this.store.selectionBody() ?? undefined,
    });
    this.dialog.set('feature');
  }

  /**
   * A point was just placed: go straight on to what it is for.
   *
   * Clicking a face to put a hole there should be one gesture, not three, so
   * the feature dialog opens already pointed at the new point.
   */
  async afterPoint(reference: string): Promise<void> {
    await this.store.reload();
    this.prefill.set({ type: 'hole', at: reference });
    this.dialog.set('feature');
  }

  /**
   * A sketch was just created empty: open it for drawing.
   *
   * The reload is what makes the editor find it — the editor reads the sketch
   * out of the loaded document, not off the wire.
   */
  async afterSketch(sketchId: string): Promise<void> {
    await this.store.reload();
    this.openSketch(sketchId);
  }

  closeFeatureDialog(): void {
    this.prefill.set(null);
    this.editingFeature.set(null);
    this.editingFeatureBody.set('');
    this.dialog.set('none');
  }

  /** Open the feature dialog on an existing feature, loaded from the document. */
  editFeature(featureId: string): void {
    for (const group of this.store.featuresByBody()) {
      const found = group.features.find((feature) => feature.id === featureId);
      if (!found) continue;
      this.editingFeature.set(found);
      this.editingFeatureBody.set(group.body);
      this.dialog.set('feature');
      return;
    }
  }

  /**
   * Read a CSV off disk and hand it to the store.
   *
   * The input is cleared afterwards so choosing the same file twice fires
   * again — a person who fixes a rejected row and re-picks the same file would
   * otherwise get silence.
   */
  async importSheet(event: Event): Promise<void> {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    input.value = '';
    if (!file) return;
    await this.store.importSheet(await file.text());
  }

  async addBody(): Promise<void> {
    const existing = new Set(this.store.bodyIds());
    let index = existing.size + 1;
    while (existing.has(`body_${index}`)) index++;
    const bodyId = `body_${index}`;
    // A body is only ever added in order to build something in it.
    if (await this.store.addBody(bodyId)) this.store.setActiveBody(bodyId);
  }

  openSketch(sketchId: string | null): void {
    this.editingSketch.set(sketchId);
    this.dialog.set('sketch');
  }

  constructor() {
    void this.store.initialise();
  }

  openProject(event: Event): void {
    const id = (event.target as HTMLSelectElement).value;
    if (id) void this.store.open(id);
  }

  async create(payload: { id: string; name: string; document?: unknown }): Promise<void> {
    try {
      await this.store.createProject(payload.id, payload.name, payload.document);
    } catch {
      this.store.notify(`Could not create '${payload.id}' — is the id already taken?`, true);
    }
  }

  @HostListener('window:keydown', ['$event'])
  onKey(event: KeyboardEvent): void {
    const target = event.target as HTMLElement | null;
    const typing =
      target instanceof HTMLInputElement ||
      target instanceof HTMLTextAreaElement ||
      target instanceof HTMLSelectElement;
    if (typing) return;

    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      this.dialog.set('selector');
      return;
    }
    if (event.ctrlKey || event.metaKey || this.dialog() !== 'none') return;

    switch (event.key.toLowerCase()) {
      case 'a':
        this.dialog.set('feature');
        break;
      case 'e':
        this.dialog.set('document');
        break;
      case 's':
        this.dialog.set('geometry');
        break;
      case 'p':
        this.newParameter();
        break;
      case 'r':
        void this.store.reload();
        break;
      case 'f':
        this.viewport()?.fitView();
        break;
      case 'v':
        this.store.toggleSketches();
        break;
      case 'c':
        void this.store.copySelection();
        break;
      case 'escape':
        this.store.clearSelection();
        break;
    }
  }
}
