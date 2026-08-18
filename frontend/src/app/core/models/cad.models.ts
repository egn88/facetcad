/**
 * Wire types for the FacetCAD API.
 *
 * These mirror the backend's response shapes exactly. The UI has no privileged
 * access — every type here corresponds to a public endpoint, which is what
 * keeps the API honest and lets an MCP server drive the same surface.
 */

export interface ProjectSummary {
  id: string;
  name: string;
  updatedAt: string;
  featureCount: number;
  parameterCount: number;
}

export interface ParameterRow {
  name: string;
  value?: number;
  expr?: string;
  unit?: string;
  group?: string;
  doc?: string;
}

export interface FeatureRow {
  id: string;
  type: string;
  profile?: string;
  suppressed?: boolean;
  [key: string]: unknown;
}

export interface SketchRow {
  plane: string;
  loops?: { id: string; curves: string[] }[];
  [key: string]: unknown;
}

export interface BodyRow {
  id: string;
  /** Absent on a copy, which can never have a history of its own. */
  features?: FeatureRow[];
  placement?: { origin: (number | string)[]; rotation: (number | string)[] };
  /**
   * The body this one copies, when it is a copy.
   *
   * A copy holds no features: it shows the named body's solid at its own
   * placement. Editing the source therefore edits every copy, and the model
   * records how many of the part it calls for.
   */
  of?: string | null;
}

export interface CadDocument {
  schema: string;
  project: string;
  parameters: ParameterRow[];
  datums: Record<string, unknown>;
  sketches: Record<string, SketchRow>;
  /** Present on single-body documents, which stay written flat. */
  features?: FeatureRow[];
  /** Present once a document has more than one body. */
  bodies?: BodyRow[];
}

export type FeatureStatus = 'built' | 'cached' | 'suppressed' | 'failed' | 'skipped';

export interface DomainError {
  kind: string;
  message: string;
  /** Present on selector failures: what the document expected to resolve. */
  expected?: number | null;
  actual?: number;
  feature?: string | null;
  missing?: string[];
  reasons?: string[];
  [key: string]: unknown;
}

export interface FeatureOutcome {
  id: string;
  type: string;
  status: FeatureStatus;
  faceCount: number;
  error: DomainError | null;
  /** Said about a feature that still built — an ignored option, say. */
  warnings?: string[];
}

export interface BuildResult {
  ok: boolean;
  bodies: BodyOutcome[];
  features: FeatureOutcome[];
  parameters: Record<string, number>;
  /** Each distinct part and how many of it the model calls for. */
  parts: PartCount[];
  lastGoodFeature: string | null;
  error: DomainError | null;
}

/** How many of a part to produce — what a print run needs to know. */
export interface PartCount {
  body: string;
  quantity: number;
}

export interface FaceRange {
  ref: string;
  /** The stable tag. This is what the user copies — never the kernel ref. */
  tag: string;
  start: number;
  count: number;
}

/**
 * One body's geometry, in its own coordinates, plus where it sits.
 *
 * Coordinates are typed arrays rather than `number[]` because that is what they
 * become anyway — the renderer's first move is `new Float32Array(...)`. `/state`
 * sends them base64-packed and they are decoded once, at the API boundary, so
 * nothing downstream knows the difference.
 */
export interface BodyMesh {
  id: string;
  /** Set when this body is a copy of another, which drew the same solid. */
  of: string | null;
  /** How many pieces of this part the model calls for; 0 on a copy. */
  quantity: number;
  /** Column-major 4x4. Kept separate from the points so moving a body cannot
   * perturb the geometry the naming engine fingerprints. */
  placement: number[];
  positions: Float32Array;
  normals: Float32Array;
  indices: Uint32Array;
  faceRanges: FaceRange[];
  edges: { ref: string; points: Float32Array }[];
}

/** A body as `/state` puts it on the wire. */
export interface PackedBodyMesh {
  id: string;
  /** Set when this body is a copy of another, which drew the same solid. */
  of: string | null;
  /** How many pieces of this part the model calls for; 0 on a copy. */
  quantity: number;
  placement: number[];
  encoding: string;
  positions: string;
  normals: string;
  indices: string;
  faceRanges: FaceRange[];
  edges: { ref: string; points: string }[];
}

/** `/bodies` still sends plain numbers, for clients that already read it. */
export interface BodiesPayload {
  bodies: PlainBodyMesh[];
  build: BuildResult;
}

export interface PlainBodyMesh {
  id: string;
  /** Set when this body is a copy of another, which drew the same solid. */
  of: string | null;
  /** How many pieces of this part the model calls for; 0 on a copy. */
  quantity: number;
  placement: number[];
  positions: number[];
  normals: number[];
  indices: number[];
  faceRanges: FaceRange[];
  edges: { ref: string; points: number[] }[];
}

/**
 * Everything needed to draw a project, from one rebuild.
 *
 * The four separate reads this replaces each re-parsed the document and three
 * of them each entered the recompute engine, so most of the work was answering
 * a question that had already been answered.
 */
export interface ViewState {
  document: CadDocument;
  bodies: PackedBodyMesh[];
  topologies: TopologiesPayload;
  sketches: SketchGeometry;
  build: BuildResult;
}

export interface BodyOutcome {
  id: string;
  ok: boolean;
  features: FeatureOutcome[];
  placement: number[];
  faceCount: number;
  error: DomainError | null;
  /** The body this one copies, or null when it builds itself. */
  of: string | null;
  /** Ids of the bodies copying this one. Empty on a copy. */
  copies: string[];
  /** How many pieces of this body the model calls for; 0 on a copy, which its
   * source counts, so the quantities sum to the piece count. */
  quantity: number;
}

export interface MeshPayload {
  positions: number[];
  normals: number[];
  indices: number[];
  faceRanges: FaceRange[];
  edges: { ref: string; points: number[] }[];
  build: BuildResult;
}

export interface TopologyPayload {
  faces: { tag: string; fingerprint: Record<string, unknown> }[];
  edges: { tag: string; fingerprint: Record<string, unknown> }[];
  retired: { tag: string; reason: string; retired_by: string | null }[];
}

export interface BodyTopology extends TopologyPayload {
  id: string;
}

export interface TopologiesPayload {
  bodies: BodyTopology[];
}

export interface BodyMatches {
  id: string;
  matched: string[];
  count: number;
}

export interface ResolvePreview {
  selector: string;
  matched: string[];
  count: number;
  ok: boolean;
  error: string | null;
  /** Which body each match came from — a feature can only use its own body's. */
  bodies: BodyMatches[];
  /** The body the query was narrowed to, when it was. */
  body: string | null;
  /** True and worth knowing, without being an error: matches spanning bodies. */
  note: string | null;
}

export interface KernelInfo {
  name: string;
  version: string;
  capabilities: string[];
}

/** Drawable sketch geometry, in world coordinates. */
export interface SketchGeometry {
  sketches: {
    id: string;
    plane: string;
    curves: { id: string; type: string; points: number[] }[];
    points: { id: string; at: number[] }[];
    error: string | null;
  }[];
  error: string | null;
}

/** A sketch as the editor sends it back. */
export interface SketchPayload {
  id: string;
  plane: string;
  points: Record<string, (number | string)[]>;
  curves: SketchCurvePayload[];
  loops: { id: string; curves: string[] }[];
}

export interface SketchCurvePayload {
  id: string;
  type: 'line' | 'arc' | 'circle';
  start?: string;
  end?: string;
  center?: string;
  radius?: number | string;
  clockwise?: boolean;
}

export interface DatumPayload {
  id: string;
  origin: (number | string)[];
  normal: (number | string)[];
  x_axis?: (number | string)[] | null;
  parent?: string | null;
}

/** A parameter row joined with its computed value, ready for the template. */
export interface ParameterView {
  name: string;
  group: string;
  /** What the user types: either the literal or the expression. */
  input: string;
  isDerived: boolean;
  unit: string;
  doc: string;
  /** Canonical mm/deg, pre-formatted so the template calls no functions. */
  resolved: string;
}

/** A group of parameters, pre-bucketed so the template only iterates. */
export interface ParameterGroup {
  name: string;
  rows: ParameterView[];
}

/** A feature joined with its build outcome, ready for the template. */
export interface FeatureView {
  id: string;
  type: string;
  status: FeatureStatus;
  statusClass: string;
  faceLabel: string;
  tooltip: string;
}

/**
 * One body in the feature tree: its history, and how many of it to make.
 *
 * `of` is set on a copy, which has no history of its own — the tree shows it as
 * a placement of the body named there rather than as an empty part.
 */
export interface BodyGroup {
  body: string;
  of: string | null;
  /** Pieces the model calls for; 0 on a copy, which its source counts. */
  quantity: number;
  features: FeatureView[];
}

/** A diagnostic flattened into displayable lines. */
export interface DiagnosticView {
  headline: string;
  message: string;
  reasons: string[];
}

/** Where a clicked point falls on one datum plane. */
export interface DatumHit {
  datum: string;
  u: number;
  v: number;
  offset: number;
  /**
   * A parameter whose value equals `offset`, or null when no parameter does.
   *
   * A datum built on the name follows the parameter; one built on the number
   * does not. Older servers omit the field entirely, so read it as
   * `?? null` rather than trusting it to be present.
   */
  offsetParameter: string | null;
}

export interface LocatePayload {
  datums: DatumHit[];
}

/**
 * The datum a picked face implies, read off the document rather than the mesh.
 *
 * The offset arrives as the expression or parameter the feature was built from,
 * never the number it currently resolves to, so a sketch attached here follows
 * the model when that parameter changes.
 */
export interface FaceDatumFound {
  ok: true;
  datum: DatumPayload;
  /** A datum already describing this plane, which is used instead of a copy. */
  existing: string | null;
  /** How the datum was derived, in words, so the user can check it. */
  explanation: string;
  /**
   * The clicked point, in the derived plane's own coordinates.
   *
   * Not the same as its coordinates on the parent unless the two happen to be
   * parallel — a side or a chamfer stands on edge to its sketch, and the
   * parent's numbers are then coordinates on a different plane. Null when no
   * point was sent.
   */
  at: { u: number; v: number } | null;
  /**
   * The face's own width and height.
   *
   * `u`/`v` are expressions, so centring with them survives a change of
   * dimensions; `uValue`/`vValue` are today's millimetres, so a person can see
   * the expressions are the right ones. Null for a face whose extent the
   * document does not state — a cap's is a whole profile, not one number.
   */
  size: { u: string | number; v: string | number; uValue?: number; vValue?: number } | null;
}

/** Why a face cannot name a datum on its own — a side face, say. */
export interface FaceDatumRefused {
  ok: false;
  reason: string;
}

export type FaceDatumResult = FaceDatumFound | FaceDatumRefused;
