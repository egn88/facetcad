/**
 * A software renderer for the viewport.
 *
 * WebGL is not always available — a VM without a GPU, a remote desktop, a
 * hardened browser, or a GPU process that has crashed and not come back. For a
 * tool whose output goes to a slicer, "look at the part before you print it" is
 * not optional, so the viewport falls back to painting with Canvas2D rather
 * than showing an apology.
 *
 * It is a painter's-algorithm rasteriser: project every triangle, discard the
 * back-facing ones, sort what remains far-to-near, and fill. That is exact
 * enough for convex-ish mechanical parts and cheap enough for the few thousand
 * triangles a CAD model tessellates to. Intersecting triangles can sort wrongly
 * — the classic limitation — which is acceptable for review and is why WebGL is
 * still preferred when it exists.
 *
 * Crucially it paints the *same* three.js scene graph the WebGL path renders,
 * so geometry, picking and camera behaviour stay identical between the two.
 */

import * as THREE from 'three';

/** Matches the WebGL path's lighting so the two look like the same viewer. */
const KEY_LIGHT = new THREE.Vector3(1, -1, 2).normalize();
const FILL_LIGHT = new THREE.Vector3(-2, 1, 1).normalize();
const AMBIENT = 0.5;
const BACKGROUND = '#070a0f';
const EDGE_COLOR = 'rgba(11, 16, 23, 0.9)';

/** A triangle or an edge segment, sorted together so edges are occluded. */
interface Drawable {
  /** View-space depth; larger is further away. */
  depth: number;
  points: number[];
  fill?: string;
  stroke?: string;
}

/**
 * Edges are nudged toward the camera by this fraction of their depth so they
 * paint just after the faces they border. Without it they z-fight; with it,
 * an edge on the far side is painted early and then covered by nearer faces —
 * which is what gives hidden-line removal without a depth buffer.
 */
const EDGE_BIAS = 0.002;

export class CanvasPainter {
  private readonly context: CanvasRenderingContext2D;

  /** Scratch objects, reused every frame to avoid per-triangle allocation. */
  private readonly viewProjection = new THREE.Matrix4();
  private readonly modelView = new THREE.Matrix4();
  private readonly a = new THREE.Vector3();
  private readonly b = new THREE.Vector3();
  private readonly c = new THREE.Vector3();
  private readonly normal = new THREE.Vector3();
  private readonly viewA = new THREE.Vector3();
  private readonly viewB = new THREE.Vector3();
  private readonly clipA = new THREE.Vector3();
  private readonly clipB = new THREE.Vector3();
  private readonly colour = new THREE.Color();
  private readonly edgeA = new THREE.Vector3();
  private readonly edgeB = new THREE.Vector3();

  constructor(private readonly canvas: HTMLCanvasElement) {
    const context = canvas.getContext('2d');
    if (!context) throw new Error('this browser provides neither WebGL nor Canvas2D');
    this.context = context;
  }

  get element(): HTMLCanvasElement {
    return this.canvas;
  }

  setSize(width: number, height: number, pixelRatio: number): void {
    this.canvas.width = Math.floor(width * pixelRatio);
    this.canvas.height = Math.floor(height * pixelRatio);
    this.canvas.style.width = `${width}px`;
    this.canvas.style.height = `${height}px`;
  }

  render(scene: THREE.Object3D, camera: THREE.PerspectiveCamera): void {
    const { width, height } = this.canvas;
    const context = this.context;

    context.setTransform(1, 0, 0, 1, 0, 0);
    context.fillStyle = BACKGROUND;
    context.fillRect(0, 0, width, height);

    camera.updateMatrixWorld();
    // The scene's too, and not only the camera's. Every body hangs under a
    // group carrying its placement with `matrixAutoUpdate` off, so nothing
    // recomputes `matrixWorld` unless it is asked to — and the collectors below
    // read exactly that. WebGL's `render` does this internally, which is why
    // the omission was invisible there and drew every body stacked at the
    // origin here: an assembly looked like one part, and a body shown at a
    // second placement did not appear at all.
    scene.updateMatrixWorld();
    this.viewProjection.multiplyMatrices(
      camera.projectionMatrix,
      camera.matrixWorldInverse,
    );

    // Triangles and edges go into one list so the painter's algorithm occludes
    // both. Sorting them separately is what makes a CPU-drawn solid look like
    // an X-ray.
    const drawables: Drawable[] = [];
    // traverseVisible, not traverse: `traverse` walks into an invisible
    // object's children anyway, so hiding a group — which is how the sketch
    // toggle works — would hide nothing here while working under WebGL.
    scene.traverseVisible((object) => {
      if (object instanceof THREE.Mesh) {
        this.collectMesh(object, camera, width, height, drawables);
      } else if (object instanceof THREE.LineSegments) {
        this.collectLines(object, camera, width, height, drawables);
      }
    });

    // Furthest first; overlay line work carries depth -1 so it lands last.
    drawables.sort((first, second) => second.depth - first.depth);

    const edgeWidth = Math.max(1, Math.floor(width / 900));
    for (const item of drawables) {
      if (item.fill) {
        const [ax, ay, bx, by, cx, cy] = item.points;
        context.beginPath();
        context.moveTo(ax, ay);
        context.lineTo(bx, by);
        context.lineTo(cx, cy);
        context.closePath();
        context.fillStyle = item.fill;
        context.fill();
        // Stroking with the fill colour closes the hairline seams that
        // antialiasing leaves between adjacent triangles.
        context.strokeStyle = item.fill;
        context.lineWidth = 1;
        context.stroke();
      } else {
        const [ax, ay, bx, by] = item.points;
        context.beginPath();
        context.moveTo(ax, ay);
        context.lineTo(bx, by);
        context.strokeStyle = item.stroke ?? EDGE_COLOR;
        context.lineWidth = edgeWidth;
        context.stroke();
      }
    }
  }

  // -- collection ----------------------------------------------------------

  private collectMesh(
    mesh: THREE.Mesh,
    camera: THREE.PerspectiveCamera,
    width: number,
    height: number,
    out: Drawable[],
  ): void {
    const geometry = mesh.geometry as THREE.BufferGeometry;
    const position = geometry.getAttribute('position');
    const index = geometry.getIndex();
    if (!position || !index) return;

    const material = mesh.material as THREE.MeshStandardMaterial;
    const base = material.color ?? new THREE.Color(0x93a3b5);
    const emissive = material.emissive?.getHex() ?? 0;

    this.modelView.multiplyMatrices(camera.matrixWorldInverse, mesh.matrixWorld);

    for (let i = 0; i < index.count; i += 3) {
      this.a.fromBufferAttribute(position, index.getX(i)).applyMatrix4(mesh.matrixWorld);
      this.b.fromBufferAttribute(position, index.getX(i + 1)).applyMatrix4(mesh.matrixWorld);
      this.c.fromBufferAttribute(position, index.getX(i + 2)).applyMatrix4(mesh.matrixWorld);

      // Every vertex has to be in front of the camera, not just the centroid:
      // a vertex behind it projects mirrored and smears the triangle across the
      // view. Dropping such a triangle is only visible with the camera inside
      // the part, where there is nothing sensible to draw anyway.
      const za = -this.viewA.copy(this.a).applyMatrix4(camera.matrixWorldInverse).z;
      const zb = -this.viewB.copy(this.b).applyMatrix4(camera.matrixWorldInverse).z;
      const zc = -this.clipA.copy(this.c).applyMatrix4(camera.matrixWorldInverse).z;
      const limit = Math.max(camera.near, 1e-4);
      if (za < limit || zb < limit || zc < limit) continue;
      const depth = (za + zb + zc) / 3;

      // Face normal in world space, for flat shading.
      this.normal
        .subVectors(this.b, this.a)
        .cross(this.edgeA.subVectors(this.c, this.a))
        .normalize();

      const projected = this.project(this.a, this.b, this.c, width, height);
      if (!projected) continue;

      // Backface culling by screen-space winding — the part is a closed solid,
      // so anything wound away from us is hidden by definition.
      //
      // Note the sign: screen Y grows downward while NDC Y grows upward, which
      // negates the signed area. A front-facing (counter-clockwise) triangle
      // therefore has a *negative* area here. Getting this backwards keeps only
      // the back faces, which renders the solid inside-out and looks like an
      // X-ray rather than an error.
      const [ax, ay, bx, by, cx, cy] = projected;
      if ((bx - ax) * (cy - ay) - (by - ay) * (cx - ax) >= 0) continue;

      const key = Math.max(0, this.normal.dot(KEY_LIGHT));
      const fill = Math.max(0, this.normal.dot(FILL_LIGHT)) * 0.35;
      const shade = Math.min(1.25, AMBIENT + (1 - AMBIENT) * key + fill);
      out.push({ depth, points: projected, fill: shadeColor(base, shade, emissive !== 0) });
    }
  }

  private collectLines(
    lines: THREE.LineSegments,
    camera: THREE.PerspectiveCamera,
    width: number,
    height: number,
    out: Drawable[],
  ): void {
    const position = (lines.geometry as THREE.BufferGeometry).getAttribute('position');
    if (!position) return;

    const material = lines.material as THREE.LineBasicMaterial;
    const overlay = material.depthTest === false;
    // A GridHelper carries its colours per vertex and leaves the material
    // white, so reading only the material paints the whole grid white.
    const colours = material.vertexColors
      ? (lines.geometry as THREE.BufferGeometry).getAttribute('color')
      : null;
    const stroke = material.color ? `#${material.color.getHexString()}` : EDGE_COLOR;

    for (let i = 0; i < position.count; i += 2) {
      this.edgeA.fromBufferAttribute(position, i).applyMatrix4(lines.matrixWorld);
      this.edgeB.fromBufferAttribute(position, i + 1).applyMatrix4(lines.matrixWorld);

      const clipped = this.clipToNearPlane(this.edgeA, this.edgeB, camera);
      if (!clipped) continue;
      const [near, far, depth] = clipped;

      const first = this.toScreen(near, width, height);
      const second = this.toScreen(far, width, height);
      if (!first || !second) continue;

      out.push({
        // Sketch line work asks not to be depth-tested (it is annotation, not
        // solid), so it is pushed to the very front rather than depth-sorted.
        depth: overlay ? -1 : depth * (1 - EDGE_BIAS),
        points: [first[0], first[1], second[0], second[1]],
        stroke: colours ? this.vertexColour(colours, i) : stroke,
      });
    }
  }

  private vertexColour(
    colours: THREE.BufferAttribute | THREE.InterleavedBufferAttribute,
    index: number,
  ): string {
    this.colour.fromBufferAttribute(colours, index);
    return `#${this.colour.getHexString()}`;
  }

  /**
   * Trim a segment to the part in front of the camera.
   *
   * Perspective division by a negative w mirrors a point through the origin, so
   * a segment with one end behind the camera projects to a line shooting across
   * the whole screen. Testing the *average* depth misses exactly that case —
   * which is what turned the ground grid into a fan of streaks when the view
   * was tilted far enough for the far edge to pass behind the eye.
   *
   * Returns the two world points to draw and the depth to sort by, or null when
   * the whole segment is behind.
   */
  private clipToNearPlane(
    a: THREE.Vector3,
    b: THREE.Vector3,
    camera: THREE.PerspectiveCamera,
  ): [THREE.Vector3, THREE.Vector3, number] | null {
    const limit = Math.max(camera.near, 1e-4);
    const depthA = -this.viewA.copy(a).applyMatrix4(camera.matrixWorldInverse).z;
    const depthB = -this.viewB.copy(b).applyMatrix4(camera.matrixWorldInverse).z;

    if (depthA < limit && depthB < limit) return null;

    let start = a;
    let end = b;
    if (depthA < limit) {
      start = this.clipA.copy(a).lerp(b, (limit - depthA) / (depthB - depthA));
    } else if (depthB < limit) {
      end = this.clipB.copy(b).lerp(a, (limit - depthB) / (depthA - depthB));
    }
    return [start, end, (Math.max(depthA, limit) + Math.max(depthB, limit)) / 2];
  }

  // -- projection ----------------------------------------------------------

  private project(
    a: THREE.Vector3,
    b: THREE.Vector3,
    c: THREE.Vector3,
    width: number,
    height: number,
  ): number[] | null {
    const first = this.toScreen(a, width, height);
    const second = this.toScreen(b, width, height);
    const third = this.toScreen(c, width, height);
    if (!first || !second || !third) return null;
    return [first[0], first[1], second[0], second[1], third[0], third[1]];
  }

  /** World point to device pixels, or null when behind the camera. */
  private toScreen(
    point: THREE.Vector3,
    width: number,
    height: number,
  ): [number, number] | null {
    const projected = point.clone().applyMatrix4(this.viewProjection);
    if (!Number.isFinite(projected.x) || !Number.isFinite(projected.y)) return null;
    return [(projected.x * 0.5 + 0.5) * width, (-projected.y * 0.5 + 0.5) * height];
  }
}

function shadeColor(base: THREE.Color, shade: number, highlighted: boolean): string {
  const red = Math.round(base.r * 255 * shade);
  const green = Math.round(base.g * 255 * shade);
  const blue = Math.round(base.b * 255 * shade);
  if (!highlighted) return `rgb(${red},${green},${blue})`;
  // Match the WebGL path's emissive lift on the selected face.
  return `rgb(${Math.min(255, red + 18)},${Math.min(255, green + 58)},${Math.min(255, blue + 99)})`;
}

/** Whether this browser can give us a WebGL context right now. */
export function webglAvailable(): boolean {
  try {
    const canvas = document.createElement('canvas');
    return canvas.getContext('webgl2') !== null;
  } catch {
    return false;
  }
}
