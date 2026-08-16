/**
 * Application state, as signals.
 *
 * Every value a template binds to is a `computed` signal calculated here, so
 * templates only read pre-resolved values and never invoke a function during
 * change detection.
 *
 * The store is also the only place that sequences API calls, which keeps the
 * "edit → rebuild → refresh geometry" cycle in one readable place rather than
 * scattered across components.
 */

import { Injectable, computed, inject, signal } from '@angular/core';

import { CadApiService } from './cad-api.service';
import type {
  BodyMesh,
  BuildResult,
  CadDocument,
  FeatureRow,
  DatumPayload,
  ParameterRow,
  SketchPayload,
  DiagnosticView,
  FeatureView,
  KernelInfo,
  MeshPayload,
  ParameterGroup,
  ParameterView,
  ProjectSummary,
  SketchGeometry,
  BodyTopology,
} from '../models/cad.models';

@Injectable({ providedIn: 'root' })
export class ProjectStore {
  private readonly api = inject(CadApiService);

  // -- raw state ----------------------------------------------------------

  readonly kernel = signal<KernelInfo | null>(null);
  readonly projects = signal<ProjectSummary[]>([]);
  readonly projectId = signal<string | null>(null);

  readonly document = signal<CadDocument | null>(null);
  readonly build = signal<BuildResult | null>(null);
  readonly mesh = signal<MeshPayload | null>(null);
  readonly bodyMeshes = signal<BodyMesh[]>([]);
  readonly topologies = signal<BodyTopology[]>([]);
  readonly sketchGeometry = signal<SketchGeometry | null>(null);
  /** Sketches are drawn in the viewport by default — otherwise you cannot see
   * what a profile looks like without opening the editor. */
  readonly showSketches = signal(true);

  /**
   * The selected tags, in the order they were picked.
   *
   * A list rather than a set: the order is what makes a range selection and a
   * two-face edge selector (`a ^ b`) mean something predictable, and it is what
   * a copied selector is written in.
   */
  readonly selectedTags = signal<readonly string[]>([]);
  readonly selectedFeature = signal<string | null>(null);

  /**
   * Which body is being worked on, as chosen — not as validated.
   *
   * Read `activeBody` instead; this is the raw intent and may name a body the
   * document no longer has. It outlives a rebuild on purpose: a rebuild is the
   * same document, so the body you were working on is still the one you are
   * working on.
   */
  private readonly activeBodyChoice = signal<string | null>(null);

  /**
   * Bodies the viewport is not drawing.
   *
   * A `Set` because every body heading asks "am I hidden?" on each render, and
   * it is replaced rather than mutated: a signal compares by reference, so an
   * in-place `add` would notify nobody.
   *
   * This is a *view* filter and nothing else. A hidden body is still built,
   * still exported, still in the topology and still in the document — hiding
   * must never become suppression, however tempting the shortcut looks.
   */
  readonly hiddenBodies = signal<ReadonlySet<string>>(new Set<string>());
  /** Where the last viewport click landed, in world millimetres. */
  readonly pickedPoint = signal<[number, number, number] | null>(null);
  readonly busy = signal(false);
  readonly toast = signal<{ text: string; error: boolean } | null>(null);

  // -- derived view models ------------------------------------------------

  /** Parameters bucketed by group, each row pre-formatted for display. */
  readonly parameterGroups = computed<ParameterGroup[]>(() => {
    const document = this.document();
    const resolved = this.build()?.parameters ?? {};
    if (!document) return [];

    const groups = new Map<string, ParameterView[]>();
    for (const row of document.parameters) {
      const group = row.group ?? '';
      const computedValue = resolved[row.name];
      const view: ParameterView = {
        name: row.name,
        group,
        input: row.expr ?? (row.value !== undefined ? String(row.value) : ''),
        isDerived: row.expr !== undefined && row.expr !== null,
        unit: row.unit ?? 'mm',
        doc: row.doc ?? '',
        resolved:
          computedValue === undefined ? '—' : String(Number(computedValue.toFixed(4))),
      };
      const bucket = groups.get(group);
      if (bucket) bucket.push(view);
      else groups.set(group, [view]);
    }
    return [...groups.entries()].map(([name, rows]) => ({ name, rows }));
  });

  /**
   * Every feature with the body that owns it.
   *
   * A single-body document is still written with a flat `features` list, so
   * both shapes are read here rather than forcing the server to pick one.
   */
  readonly featuresByBody = computed<{ body: string; features: FeatureRow[] }[]>(() => {
    const document = this.document();
    if (!document) return [];
    if (document.bodies?.length) {
      return document.bodies.map((body) => ({ body: body.id, features: body.features }));
    }
    return [{ body: 'main', features: document.features ?? [] }];
  });

  readonly bodyIds = computed(() => this.featuresByBody().map((group) => group.body));

  /**
   * The body being worked on, or null for "all bodies".
   *
   * Validated on read against the bodies that actually exist, so deleting the
   * active body — or opening a document that never had it — falls back to null
   * without every mutation having to remember to clean up after itself.
   */
  readonly activeBody = computed(() => {
    const chosen = this.activeBodyChoice();
    return chosen !== null && this.bodyIds().includes(chosen) ? chosen : null;
  });

  /** What the Features header shows, so the active body is never invisible. */
  readonly activeBodyLabel = computed(() => this.activeBody() ?? 'all bodies');

  /** The meshes the viewport should draw: everything that is not hidden. */
  readonly visibleBodyMeshes = computed<BodyMesh[]>(() => {
    const hidden = this.hiddenBodies();
    if (hidden.size === 0) return this.bodyMeshes();
    return this.bodyMeshes().filter((mesh) => !hidden.has(mesh.id));
  });

  /** Features joined with their build status. */
  readonly featureViews = computed<FeatureView[]>(() =>
    this.featureGroups().flatMap((group) => group.features),
  );

  /** Features grouped under the body that owns them. */
  readonly featureGroups = computed<{ body: string; features: FeatureView[] }[]>(() => {
    const outcomes = new Map((this.build()?.features ?? []).map((o) => [o.id, o]));
    return this.featuresByBody().map((group) => ({
      body: group.body,
      features: group.features.map((feature) => {
        const outcome = outcomes.get(feature.id);
        const status = feature.suppressed ? 'suppressed' : (outcome?.status ?? 'skipped');
        return {
          id: feature.id,
          type: feature.type,
          status,
          statusClass: `status-${status}`,
          faceLabel: outcome?.faceCount ? `${outcome.faceCount}f` : '',
          tooltip: outcome?.error?.message ?? '',
        };
      }),
    }));
  });

  /** Failures flattened into displayable lines. */
  readonly diagnostics = computed<DiagnosticView[]>(() => {
    const build = this.build();
    if (!build) return [];
    const views: DiagnosticView[] = [];

    if (build.error) {
      views.push({
        headline: build.error.kind,
        message: build.error.message,
        reasons: build.error.reasons ?? [],
      });
    }
    for (const outcome of build.features) {
      if (outcome.status !== 'failed' || !outcome.error) continue;
      const error = outcome.error;
      const reasons: string[] = [];
      if (error.expected !== undefined && error.expected !== null) {
        reasons.push(`expected ${error.expected}, resolved ${error.actual}`);
      }
      if (error.missing?.length) reasons.push(`missing: ${error.missing.join(', ')}`);
      reasons.push(...(error.reasons ?? []));
      views.push({
        headline: `${outcome.id} — ${error.kind}`,
        message: error.message,
        reasons,
      });
    }
    return views;
  });

  readonly buildsCleanly = computed(() => this.diagnostics().length === 0);
  readonly statusLabel = computed(() => (this.build()?.ok ? 'builds' : 'broken'));
  readonly statusClass = computed(() => (this.build()?.ok ? 'ok' : 'error'));

  /**
   * The topology of the bodies currently on screen.
   *
   * Hiding a body has to take its faces out of the panels too, not just out of
   * the picture. A tag you can select but cannot see is the one route by which
   * a view filter turns into a wrong edit — Fillet, Chamfer and the DXF export
   * all act on the selection.
   */
  readonly visibleTopologies = computed<BodyTopology[]>(() => {
    const hidden = this.hiddenBodies();
    if (hidden.size === 0) return this.topologies();
    return this.topologies().filter((body) => !hidden.has(body.id));
  });

  readonly faceTags = computed(() =>
    this.visibleTopologies().flatMap((body) => body.faces.map((f) => f.tag)),
  );

  /** Fast membership test for the panels and the viewport. */
  readonly selectedTagSet = computed(() => new Set(this.selectedTags()));

  /** The single selected tag, or null when the selection is empty or plural. */
  readonly selectedTag = computed(() => {
    const tags = this.selectedTags();
    return tags.length === 1 ? tags[0] : null;
  });

  /**
   * Every face tag a feature produced.
   *
   * The feature id is the root segment of every tag it creates — that is the
   * whole point of the tag algebra — so this needs no extra bookkeeping and no
   * round trip to the server.
   */
  tagsOfFeature(featureId: string): string[] {
    return this.faceTags().filter((tag) => rootOf(tag) === featureId);
  }

  /** Every `sketch.loop` a feature could use as a profile. */
  readonly profileOptions = computed<string[]>(() => {
    const document = this.document();
    if (!document) return [];
    return Object.entries(document.sketches).flatMap(([sketchId, sketch]) =>
      (sketch.loops ?? []).map((loop) => `${sketchId}.${loop.id}`),
    );
  });

  /** Every `sketch.point` a hole could be placed on. */
  readonly pointOptions = computed<string[]>(() =>
    Object.entries(this.document()?.sketches ?? {}).flatMap(([sketchId, sketch]) =>
      Object.keys((sketch['points'] as Record<string, unknown>) ?? {}).map(
        (pointId) => `${sketchId}.${pointId}`,
      ),
    ),
  );

  /** Sketches listed for the manager, with their loop and point counts. */
  readonly sketchList = computed(() =>
    Object.entries(this.document()?.sketches ?? {}).map(([id, sketch]) => ({
      id,
      plane: sketch.plane,
      pointCount: Object.keys((sketch['points'] as object) ?? {}).length,
      curveCount: ((sketch['curves'] as unknown[]) ?? []).length,
      loopCount: (sketch.loops ?? []).length,
    })),
  );

  /** Datums the document declares, plus the three that always exist. */
  readonly datumList = computed(() => {
    const declared = Object.keys(this.document()?.datums ?? {});
    return [
      ...declared.map((id) => ({ id, builtIn: false })),
      ...['xy', 'xz', 'yz'].map((id) => ({ id, builtIn: true })),
    ];
  });

  readonly planeOptions = computed(() => this.datumList().map((d) => d.id));

  readonly meshStats = computed(() => {
    const meshes = this.bodyMeshes();
    if (meshes.length === 0) return null;
    return {
      bodies: meshes.length,
      faces: meshes.reduce((total, m) => total + m.faceRanges.length, 0),
      edges: meshes.reduce((total, m) => total + m.edges.length, 0),
      triangles: meshes.reduce((total, m) => total + Math.floor(m.indices.length / 3), 0),
    };
  });

  readonly exportUrls = computed(() => {
    const id = this.projectId();
    if (!id) return null;
    return {
      stl: this.api.exportUrl(id, 'stl'),
      csv: this.api.exportUrl(id, 'csv'),
      yaml: this.api.exportUrl(id, 'yaml'),
    };
  });

  /**
   * One STL per body, for a document that builds more than one.
   *
   * The whole-model STL holds every body at its placement, which is right for
   * looking at an assembly and wrong for a print bed — the parts go on one at a
   * time. Empty for a single-body document, where the plain STL already is the
   * part.
   */
  readonly stlTitle = computed(() =>
    this.bodyIds().length > 1
      ? 'Every body at its placement — the assembly, not a print bed'
      : 'The whole model',
  );

  readonly bodyExportUrls = computed<{ id: string; stl: string }[]>(() => {
    const id = this.projectId();
    const bodies = this.bodyIds();
    if (!id || bodies.length < 2) return [];
    return bodies.map((body) => ({ id: body, stl: this.api.exportUrl(id, 'stl', body) }));
  });

  /**
   * A DXF of exactly what is selected, ready for a laser or a router.
   *
   * The selector travels in the URL rather than the face's index, so the same
   * link keeps producing the right file after the sheet changes.
   */
  readonly cutUrl = computed(() => {
    const id = this.projectId();
    const selector = this.selectionSelector();
    if (!id || !selector) return null;
    return this.api.cutUrl(id, selector, 'dxf');
  });

  readonly drawingUrls = computed(() => {
    const id = this.projectId();
    if (!id) return null;
    return {
      views: this.api.viewsUrl(id, 'dxf', 'top,front,right'),
      flat: this.api.flatUrl(id, 'dxf'),
      enclosure: this.api.enclosureUrl(id, 'svg'),
    };
  });

  // -- lifecycle ----------------------------------------------------------

  async initialise(): Promise<void> {
    try {
      this.kernel.set(await this.api.kernel());
    } catch {
      this.notify('Cannot reach the API', true);
      return;
    }
    const list = await this.refreshProjects();
    if (list.length > 0 && !this.projectId()) {
      await this.open(list[0].id);
    }
  }

  async refreshProjects(): Promise<ProjectSummary[]> {
    const { projects } = await this.api.listProjects();
    this.projects.set(projects);
    return projects;
  }

  async open(id: string): Promise<void> {
    this.projectId.set(id);
    this.selectedTags.set([]);
    this.selectedFeature.set(null);
    // Body ids are only unique within a document, so carrying either of these
    // across an open would point them at a different body of the same name.
    this.activeBodyChoice.set(null);
    this.hiddenBodies.set(new Set<string>());
    await this.reload();
  }

  /** Pull everything a rebuild can change, in one round trip set. */
  async reload(): Promise<void> {
    const id = this.projectId();
    if (!id) return;
    this.busy.set(true);
    try {
      const [document, payload, topology, sketches] = await Promise.all([
        this.api.getDocument(id),
        this.api.bodies(id),
        this.api.topologies(id),
        this.api.sketchGeometry(id),
      ]);
      this.document.set(document);
      this.bodyMeshes.set(payload.bodies);
      this.topologies.set(topology.bodies);
      this.sketchGeometry.set(sketches);
      this.build.set(payload.build);
    } catch (error) {
      this.notify(describe(error), true);
    } finally {
      this.busy.set(false);
    }
  }

  // -- editing ------------------------------------------------------------

  async setParameter(name: string, raw: string): Promise<void> {
    const id = this.projectId();
    if (!id) return;
    const asNumber = Number(raw);
    const value = raw.trim() !== '' && Number.isFinite(asNumber) ? asNumber : raw;

    this.busy.set(true);
    try {
      const result = await this.api.setParameters(id, { [name]: value });
      this.build.set(result);
      await this.reload();
      if (!result.ok) this.notify(`${name} applied, but the model does not build`, true);
    } catch (error) {
      this.notify(describe(error), true);
      await this.reload();
    } finally {
      this.busy.set(false);
    }
  }

  /** Run an edit, refresh everything, and surface any failure as a toast. */
  private async mutate(action: (id: string) => Promise<BuildResult>): Promise<boolean> {
    const id = this.projectId();
    if (!id) return false;
    this.busy.set(true);
    try {
      await action(id);
      await this.reload();
      return true;
    } catch (error) {
      this.notify(describe(error), true);
      await this.reload();
      return false;
    } finally {
      this.busy.set(false);
    }
  }

  /**
   * Replace the parameter table from a spreadsheet export.
   *
   * The other half of the Sheet button. A sheet that only goes out is a report;
   * one that comes back is where the editing actually happens for anyone who
   * would rather work in Excel or Calc than in a panel.
   *
   * The server rejects a bad file whole, naming the row, rather than importing
   * the part it understood — a half-applied sheet is far harder to reason about
   * than one that was refused.
   */
  async importSheet(csv: string): Promise<boolean> {
    const ok = await this.mutate((id) => this.api.importParametersCsv(id, csv));
    if (ok) this.notify('Parameters imported', false);
    return ok;
  }

  addBody(bodyId: string): Promise<boolean> {
    return this.mutate((id) => this.api.addBody(id, bodyId));
  }

  async deleteBody(bodyId: string): Promise<boolean> {
    const removed = await this.mutate((id) => this.api.deleteBody(id, bodyId));
    // A later `+ body` can reuse the freed id; a leftover entry here would make
    // the new body arrive invisible, with nothing on screen to explain it.
    if (removed) this.setBodyHidden(bodyId, false);
    return removed;
  }

  moveBody(
    bodyId: string,
    origin: (number | string)[],
    rotation: (number | string)[],
  ): Promise<boolean> {
    return this.mutate((id) => this.api.moveBody(id, bodyId, origin, rotation));
  }

  addParameter(row: ParameterRow): Promise<boolean> {
    return this.mutate((id) => this.api.addParameter(id, row));
  }

  editParameter(name: string, changes: Partial<ParameterRow>): Promise<boolean> {
    return this.mutate((id) => this.api.editParameter(id, name, changes));
  }

  deleteParameter(name: string): Promise<boolean> {
    return this.mutate((id) => this.api.deleteParameter(id, name));
  }

  async parameterUsage(name: string): Promise<string[]> {
    const id = this.projectId();
    if (!id) return [];
    try {
      return (await this.api.parameterUsage(id, name)).usedBy;
    } catch {
      return [];
    }
  }

  putSketch(sketch: SketchPayload): Promise<boolean> {
    return this.mutate((id) => this.api.putSketch(id, sketch));
  }

  deleteSketch(sketchId: string): Promise<boolean> {
    return this.mutate((id) => this.api.deleteSketch(id, sketchId));
  }

  putDatum(datum: DatumPayload): Promise<boolean> {
    return this.mutate((id) => this.api.putDatum(id, datum));
  }

  deleteDatum(datumId: string): Promise<boolean> {
    return this.mutate((id) => this.api.deleteDatum(id, datumId));
  }

  async removeFeature(featureId: string): Promise<void> {
    const id = this.projectId();
    if (!id) return;
    try {
      await this.api.deleteFeature(id, featureId);
      await this.reload();
    } catch (error) {
      this.notify(describe(error), true);
    }
  }

  async createProject(id: string, name: string, document?: unknown): Promise<void> {
    await this.api.createProject(id, name, document);
    await this.refreshProjects();
    await this.open(id);
  }

  // -- selection ----------------------------------------------------------

  toggleSketches(): void {
    this.showSketches.update((shown) => !shown);
  }

  // -- bodies (view state only) -------------------------------------------

  setActiveBody(bodyId: string | null): void {
    this.activeBodyChoice.set(bodyId);
  }

  /** Clicking the active body again goes back to working on all of them. */
  toggleActiveBody(bodyId: string): void {
    this.activeBodyChoice.set(this.activeBody() === bodyId ? null : bodyId);
  }

  toggleBodyVisibility(bodyId: string): void {
    this.setBodyHidden(bodyId, !this.hiddenBodies().has(bodyId));
  }

  private setBodyHidden(bodyId: string, hidden: boolean): void {
    const next = new Set(this.hiddenBodies());
    if (hidden) next.add(bodyId);
    else next.delete(bodyId);
    // Exactly one id moves, so an unchanged size means an unchanged set.
    if (next.size === this.hiddenBodies().size) return;
    this.hiddenBodies.set(next);
    if (hidden) this.forgetFacesOf(bodyId);
  }

  /**
   * Drop selected faces of a body that has just been hidden.
   *
   * The selection is what Fillet, Chamfer, DXF and "sketch here" act on, so
   * keeping tags for geometry that is off screen is the one way this view
   * filter could lead to a wrong edit. It would also strand them: a tag that
   * cannot be seen cannot be clicked off again.
   */
  private forgetFacesOf(bodyId: string): void {
    const owned = this.topologies().find((body) => body.id === bodyId)?.faces;
    if (!owned?.length) return;
    const gone = new Set(owned.map((face) => face.tag));
    const kept = this.selectedTags().filter((tag) => !gone.has(tag));
    if (kept.length === this.selectedTags().length) return;
    this.selectedTags.set(kept);
    // The highlight no longer shows all of what the feature made, and the
    // picked point was on a face that is now hidden.
    this.selectedFeature.set(null);
    this.pickedPoint.set(null);
  }

  /**
   * Pick a tag.
   *
   * `replace` is the plain click, `toggle` is ctrl/cmd-click, and `range`
   * (shift-click) extends from the last pick through the clicked tag in the
   * order the topology panel lists them.
   */
  /** Record a viewport pick: the tag for selection, the point for placing. */
  pick(tag: string | null, mode: SelectMode, point: [number, number, number] | null): void {
    // After `selectTag`, which clears the point: only a click on the model
    // knows where on the face it landed.
    this.selectTag(tag, mode);
    this.pickedPoint.set(point);
  }

  selectTag(tag: string | null, mode: SelectMode = 'replace'): void {
    // Picking tags by hand means the highlight no longer represents a feature.
    this.selectedFeature.set(null);
    // Nor a point on one: a tag chosen from a list says which face, never
    // where on it, and a stale coordinate from an earlier click is worse than
    // none — it looks deliberate.
    this.pickedPoint.set(null);
    if (tag === null) {
      this.selectedTags.set([]);
      return;
    }
    const current = this.selectedTags();

    if (mode === 'toggle') {
      this.selectedTags.set(
        current.includes(tag) ? current.filter((t) => t !== tag) : [...current, tag],
      );
      return;
    }

    if (mode === 'range' && current.length > 0) {
      const order = this.faceTags();
      const anchor = order.indexOf(current[current.length - 1]);
      const target = order.indexOf(tag);
      if (anchor >= 0 && target >= 0) {
        const [from, to] = anchor <= target ? [anchor, target] : [target, anchor];
        const span = order.slice(from, to + 1);
        const merged = [...current];
        for (const item of span) if (!merged.includes(item)) merged.push(item);
        this.selectedTags.set(merged);
        return;
      }
    }

    this.selectedTags.set(current.length === 1 && current[0] === tag ? [] : [tag]);
  }

  selectTags(tags: readonly string[]): void {
    this.selectedTags.set([...tags]);
  }

  clearSelection(): void {
    this.selectedTags.set([]);
    this.pickedPoint.set(null);
  }

  /**
   * Selecting a feature highlights everything it produced.
   *
   * Reading a feature's footprint off the model is far more direct than reading
   * its tag list, and it is the fastest way to see what a selector will hit
   * before writing one.
   */
  toggleFeature(id: string): void {
    const next = this.selectedFeature() === id ? null : id;
    this.selectedFeature.set(next);
    this.selectedTags.set(next ? this.tagsOfFeature(next) : []);
  }

  /**
   * The selection as a selector, ready to paste into a feature.
   *
   * Commas are the union separator the parser uses, so a multi-face selection
   * round-trips as a single valid face selector.
   */
  readonly selectionSelector = computed(() => this.selectedTags().join(', '));

  /**
   * The selection as an *edge* selector.
   *
   * Exactly two faces is the interesting case — `a ^ b` is the shared edge, or
   * the whole shared perimeter when either side is a pattern. Any other count
   * falls back to the touching form, which reads as "every edge on these
   * faces".
   */
  readonly selectionEdgeSelector = computed(() => {
    const tags = this.selectedTags();
    if (tags.length === 2) return `${tags[0]} ^ ${tags[1]}`;
    return tags.join(', ');
  });

  /**
   * The body the selection lives in, or null when it straddles two.
   *
   * A blend built from selected faces has to land in the body that owns them;
   * defaulting to the first body would fail on every part but the first.
   */
  readonly selectionBody = computed<string | null>(() => {
    const tags = this.selectedTags();
    if (tags.length === 0) return null;
    const owners = new Set<string>();
    for (const body of this.topologies()) {
      const owned = new Set(body.faces.map((f) => f.tag));
      if (tags.some((tag) => owned.has(tag))) owners.add(body.id);
    }
    return owners.size === 1 ? [...owners][0] : null;
  });

  async copySelection(): Promise<void> {
    const text = this.selectionSelector();
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      this.notify(`Copied ${this.selectedTags().length} tag(s)`, false);
    } catch {
      this.notify(text, false);
    }
  }

  // -- feedback -----------------------------------------------------------

  notify(text: string, error: boolean): void {
    this.toast.set({ text, error });
    setTimeout(() => this.toast.set(null), 2800);
  }
}

function describe(error: unknown): string {
  if (typeof error === 'object' && error !== null && 'error' in error) {
    const detail = (error as { error?: { detail?: { message?: string } } }).error?.detail;
    if (detail?.message) return detail.message;
  }
  return error instanceof Error ? error.message : String(error);
}

/** How a click combines with what is already selected. */
export type SelectMode = 'replace' | 'toggle' | 'range';

/** The feature that produced a tag: everything before the first slash. */
function rootOf(tag: string): string {
  const slash = tag.indexOf('/');
  return slash < 0 ? tag : tag.slice(0, slash);
}
