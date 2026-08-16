/**
 * Typed HTTP client for the FacetCAD API.
 *
 * One method per endpoint, no logic. Anything resembling a decision belongs in
 * the store or the backend, so this stays a boring, verifiable translation
 * layer.
 */

import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type {
  LocatePayload,
  BodiesPayload,
  BuildResult,
  CadDocument,
  DatumPayload,
  FaceDatumResult,
  ParameterRow,
  SketchPayload,
  KernelInfo,
  MeshPayload,
  ProjectSummary,
  ResolvePreview,
  SketchGeometry,
  TopologiesPayload,
  TopologyPayload,
} from '../models/cad.models';

const BASE = '/api';

@Injectable({ providedIn: 'root' })
export class CadApiService {
  private readonly http = inject(HttpClient);

  // -- meta ---------------------------------------------------------------

  kernel(): Promise<KernelInfo> {
    return firstValueFrom(this.http.get<KernelInfo>(`${BASE}/kernel`));
  }

  /** Function and constant names, so a client can spot an unknown parameter. */
  expressionVocabulary(): Promise<{ functions: string[]; constants: string[] }> {
    return firstValueFrom(
      this.http.get<{ functions: string[]; constants: string[] }>(`${BASE}/expressions`),
    );
  }

  featureTypes(): Promise<{ types: string[] }> {
    return firstValueFrom(this.http.get<{ types: string[] }>(`${BASE}/feature-types`));
  }

  // -- projects -----------------------------------------------------------

  listProjects(): Promise<{ projects: ProjectSummary[] }> {
    return firstValueFrom(this.http.get<{ projects: ProjectSummary[] }>(`${BASE}/projects`));
  }

  createProject(id: string, name: string, document?: unknown): Promise<ProjectSummary> {
    return firstValueFrom(
      this.http.post<ProjectSummary>(`${BASE}/projects`, { id, name, document }),
    );
  }

  deleteProject(id: string): Promise<void> {
    return firstValueFrom(this.http.delete<void>(`${BASE}/projects/${id}`));
  }

  // -- document -----------------------------------------------------------

  getDocument(id: string): Promise<CadDocument> {
    return firstValueFrom(this.http.get<CadDocument>(`${BASE}/projects/${id}/document`));
  }

  getDocumentYaml(id: string): Promise<string> {
    return firstValueFrom(
      this.http.get(`${BASE}/projects/${id}/document`, {
        params: { fmt: 'yaml' },
        responseType: 'text',
      }),
    );
  }

  putDocumentYaml(id: string, yaml: string): Promise<ProjectSummary> {
    return firstValueFrom(
      this.http.put<ProjectSummary>(`${BASE}/projects/${id}/document`, { yaml }),
    );
  }

  // -- editing ------------------------------------------------------------

  setParameters(id: string, changes: Record<string, number | string>): Promise<BuildResult> {
    return firstValueFrom(
      this.http.patch<BuildResult>(`${BASE}/projects/${id}/parameters`, { changes }),
    );
  }

  // -- parameters ---------------------------------------------------------

  addParameter(id: string, parameter: ParameterRow): Promise<BuildResult> {
    return firstValueFrom(
      this.http.post<BuildResult>(`${BASE}/projects/${id}/parameters`, parameter),
    );
  }

  /** Any subset of a row. Changing `name` renames it throughout the document. */
  editParameter(
    id: string,
    name: string,
    changes: Partial<ParameterRow>,
  ): Promise<BuildResult> {
    return firstValueFrom(
      this.http.patch<BuildResult>(`${BASE}/projects/${id}/parameters/${name}`, changes),
    );
  }

  deleteParameter(id: string, name: string): Promise<BuildResult> {
    return firstValueFrom(
      this.http.delete<BuildResult>(`${BASE}/projects/${id}/parameters/${name}`),
    );
  }

  /** What still reads a parameter — asked before offering to delete it. */
  parameterUsage(id: string, name: string): Promise<{ name: string; usedBy: string[] }> {
    return firstValueFrom(
      this.http.get<{ name: string; usedBy: string[] }>(
        `${BASE}/projects/${id}/parameters/${name}/usage`,
      ),
    );
  }

  // -- sketches and datums ------------------------------------------------

  putSketch(id: string, sketch: SketchPayload): Promise<BuildResult> {
    return firstValueFrom(
      this.http.put<BuildResult>(`${BASE}/projects/${id}/sketches/${sketch.id}`, sketch),
    );
  }

  deleteSketch(id: string, sketchId: string): Promise<BuildResult> {
    return firstValueFrom(
      this.http.delete<BuildResult>(`${BASE}/projects/${id}/sketches/${sketchId}`),
    );
  }

  putDatum(id: string, datum: DatumPayload): Promise<BuildResult> {
    return firstValueFrom(
      this.http.put<BuildResult>(`${BASE}/projects/${id}/datums/${datum.id}`, datum),
    );
  }

  deleteDatum(id: string, datumId: string): Promise<BuildResult> {
    return firstValueFrom(
      this.http.delete<BuildResult>(`${BASE}/projects/${id}/datums/${datumId}`),
    );
  }

  addFeature(
    id: string,
    spec: Record<string, unknown>,
    at?: number,
    body?: string,
  ): Promise<BuildResult> {
    return firstValueFrom(
      this.http.post<BuildResult>(`${BASE}/projects/${id}/features`, { spec, at, body }),
    );
  }

  updateFeature(
    id: string,
    featureId: string,
    spec: Record<string, unknown>,
  ): Promise<BuildResult> {
    return firstValueFrom(
      this.http.patch<BuildResult>(`${BASE}/projects/${id}/features/${featureId}`, spec),
    );
  }

  deleteFeature(id: string, featureId: string): Promise<BuildResult> {
    return firstValueFrom(
      this.http.delete<BuildResult>(`${BASE}/projects/${id}/features/${featureId}`),
    );
  }

  reorderFeatures(id: string, order: string[]): Promise<BuildResult> {
    return firstValueFrom(
      this.http.post<BuildResult>(`${BASE}/projects/${id}/features/reorder`, { order }),
    );
  }

  // -- rebuild and inspect ------------------------------------------------

  recompute(id: string): Promise<BuildResult> {
    return firstValueFrom(this.http.post<BuildResult>(`${BASE}/projects/${id}/recompute`, {}));
  }

  /** Every body, tessellated in its own coordinates with its placement. */
  bodies(id: string): Promise<BodiesPayload> {
    return firstValueFrom(this.http.get<BodiesPayload>(`${BASE}/projects/${id}/bodies`));
  }

  addBody(id: string, bodyId: string): Promise<BuildResult> {
    return firstValueFrom(
      this.http.post<BuildResult>(`${BASE}/projects/${id}/bodies`, { id: bodyId }),
    );
  }

  moveBody(
    id: string,
    bodyId: string,
    origin: (number | string)[],
    rotation: (number | string)[],
  ): Promise<BuildResult> {
    return firstValueFrom(
      this.http.patch<BuildResult>(`${BASE}/projects/${id}/bodies/${bodyId}`, {
        id: bodyId,
        origin,
        rotation,
      }),
    );
  }

  deleteBody(id: string, bodyId: string): Promise<BuildResult> {
    return firstValueFrom(
      this.http.delete<BuildResult>(`${BASE}/projects/${id}/bodies/${bodyId}`),
    );
  }

  mesh(id: string): Promise<MeshPayload> {
    return firstValueFrom(this.http.get<MeshPayload>(`${BASE}/projects/${id}/mesh`));
  }

  /** Sketch curves and points, drawable even when the model does not build. */
  sketchGeometry(id: string): Promise<SketchGeometry> {
    return firstValueFrom(
      this.http.get<SketchGeometry>(`${BASE}/projects/${id}/sketches/geometry`),
    );
  }

  /** Named geometry per body — each body has its own tag namespace. */
  topologies(id: string): Promise<TopologiesPayload> {
    return firstValueFrom(
      this.http.get<TopologiesPayload>(`${BASE}/projects/${id}/topologies`),
    );
  }

  topology(id: string): Promise<TopologyPayload> {
    return firstValueFrom(this.http.get<TopologyPayload>(`${BASE}/projects/${id}/topology`));
  }

  /** Preview what a selector matches, without committing it to the document. */
  resolve(id: string, selector: string, kind: 'faces' | 'edges' = 'faces'): Promise<ResolvePreview> {
    return firstValueFrom(
      this.http.post<ResolvePreview>(`${BASE}/projects/${id}/resolve`, { selector, kind }),
    );
  }

  resolveBetween(id: string, between: [string, string]): Promise<ResolvePreview> {
    return firstValueFrom(
      this.http.post<ResolvePreview>(`${BASE}/projects/${id}/resolve`, { between }),
    );
  }

  // -- import / export ----------------------------------------------------

  exportUrl(id: string, fmt: string, body?: string): string {
    const suffix = body ? `&body=${encodeURIComponent(body)}` : '';
    return `${BASE}/projects/${id}/export?fmt=${fmt}${suffix}`;
  }

  /** A cut path for whatever a selector resolves to, re-resolved on each fetch. */
  cutUrl(id: string, selector: string, fmt: string): string {
    return (
      `${BASE}/projects/${id}/export/cut` +
      `?selector=${encodeURIComponent(selector)}&fmt=${fmt}`
    );
  }

  viewsUrl(id: string, fmt: string, views: string): string {
    return `${BASE}/projects/${id}/export/views?fmt=${fmt}&views=${encodeURIComponent(views)}`;
  }

  /** Every planar face of the part, flattened — the part as a cutting list. */
  flatUrl(id: string, fmt: string, blends = false): string {
    return `${BASE}/projects/${id}/export/flat?fmt=${fmt}&blends=${blends}`;
  }

  /** The part's own faces with finger joints on the edges they share. */
  jointedUrl(id: string, fmt: string, options: JointOptions = {}): string {
    const query = new URLSearchParams({ fmt });
    query.set('thickness', String(options.thickness ?? 3));
    query.set('kerf', String(options.kerf ?? 0.15));
    if (options.finger !== undefined) query.set('finger', String(options.finger));
    if (options.teeth !== undefined) query.set('teeth', String(options.teeth));
    if (options.depth !== undefined) query.set('depth', String(options.depth));
    if (options.overrides) query.set('finger_for', options.overrides);
    return `${BASE}/projects/${id}/export/jointed?${query.toString()}`;
  }

  enclosureUrl(id: string, fmt: string, thickness = 3, finger = 10, clearance = 2): string {
    return (
      `${BASE}/projects/${id}/export/enclosure?fmt=${fmt}` +
      `&thickness=${thickness}&finger=${finger}&clearance=${clearance}`
    );
  }

  /** Where a world point falls on each datum plane. */
  locate(id: string, point: [number, number, number]): Promise<LocatePayload> {
    return firstValueFrom(
      this.http.post<LocatePayload>(`${BASE}/projects/${id}/locate`, { point }),
    );
  }

  /**
   * The datum a face implies, derived from the feature that made it.
   *
   * Better than the nearest plane to a click: the offset comes back as the
   * feature's own expression, so the datum moves with the model instead of
   * freezing at whatever the parameters resolve to today.
   */
  datumForFace(
    id: string,
    tag: string,
    point?: [number, number, number],
  ): Promise<FaceDatumResult> {
    return firstValueFrom(
      this.http.post<FaceDatumResult>(`${BASE}/projects/${id}/datums/for-face`, {
        tag,
        point,
      }),
    );
  }

  /** Round-trip the sheet through a spreadsheet application. */
  importParametersCsv(id: string, csv: string): Promise<BuildResult> {
    return firstValueFrom(
      this.http.post<BuildResult>(`${BASE}/projects/${id}/import`, { format: 'csv', body: csv }),
    );
  }
}

/** Everything the jointed export can be told. */
export interface JointOptions {
  thickness?: number;
  kerf?: number;
  finger?: number;
  teeth?: number;
  depth?: number;
  /** `tag:width` pairs separated by semicolons. */
  overrides?: string;
}
