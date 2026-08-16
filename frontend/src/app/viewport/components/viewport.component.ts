/**
 * The 3D viewport.
 *
 * Its job is *discovery*, not authoring: clicking a face reports that face's
 * stable tag, so the mouse finds names and the keyboard does the work.
 *
 * three.js is driven directly rather than through a wrapper library. The render
 * loop runs at display rate and must never trigger Angular change detection —
 * with zoneless change detection that is automatic, since nothing here touches
 * a signal per frame. Selection is pushed out through an `output()` so the
 * canvas stays a leaf that owns its own imperative state.
 */

import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  OnDestroy,
  effect,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';

import type { BodyMesh, SketchGeometry } from '../../core/models/cad.models';
import type { SelectMode } from '../../core/services/project-store';
import { CanvasPainter, webglAvailable } from '../utils/canvas-painter';

const FACE_COLOR = 0x93a3b5;
const FACE_SELECTED = 0x4da3ff;
const EMISSIVE_OFF = 0x000000;
const EMISSIVE_ON = 0x123a63;
/** Pointer travel beyond this is an orbit drag, not a click. */
const CLICK_SLOP_PX = 4;
/** Sketch line work, in the amber CAD convention rather than model grey. */
const SKETCH_COLOR = 0xd9a441;
const SKETCH_POINT_COLOR = 0xf0c674;
/** Half-length of a point marker's arms, in millimetres. */
const MARKER_SIZE = 1.2;

@Component({
  selector: 'cad-viewport',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div #host class="host"></div>
    @if (software()) {
      <div class="software-note" title="WebGL is unavailable, so the view is drawn on the CPU">
        software rendering
      </div>
    }
  `,
  styles: [
    `
      :host {
        position: absolute;
        inset: 0;
        display: block;
      }
      .host {
        width: 100%;
        height: 100%;
      }
      .software-note {
        position: absolute;
        left: 10px;
        bottom: 10px;
        padding: 2px 7px;
        border: 1px solid var(--border-strong);
        border-radius: 3px;
        font-family: var(--mono);
        font-size: 10px;
        color: var(--warn);
        pointer-events: none;
      }
    `,
  ],
})
export class ViewportComponent implements OnDestroy {
  /** Every body, each drawn in its own coordinates under its placement. */
  readonly bodies = input<BodyMesh[]>([]);
  readonly selected = input<ReadonlySet<string>>(new Set<string>());
  /** Changing this reframes the camera — set it to the project id, so opening a
   * different model does not leave the view pointed at where the last one was. */
  readonly resetKey = input<string | null>(null);
  readonly sketches = input<SketchGeometry | null>(null);
  readonly showSketches = input(true);
  readonly tagPicked = output<{
    tag: string | null;
    mode: SelectMode;
    /** Where on the model the click landed, in world millimetres. */
    point: [number, number, number] | null;
  }>();

  private readonly host = viewChild.required<ElementRef<HTMLDivElement>>('host');
  /** True when painting on the CPU because WebGL is unavailable. */
  readonly software = signal(false);

  private renderer?: THREE.WebGLRenderer;
  private painter?: CanvasPainter;
  private canvas?: HTMLCanvasElement;
  /** The CPU path repaints only when something changed, rather than at 60fps. */
  private dirty = true;
  private started = false;
  private scene?: THREE.Scene;
  private camera?: THREE.PerspectiveCamera;
  private controls?: OrbitControls;
  private modelGroup?: THREE.Group;
  /** Sketches live in their own group so they can be hidden without a rebuild. */
  private sketchGroup?: THREE.Group;
  private faces: THREE.Mesh[] = [];
  private readonly raycaster = new THREE.Raycaster();
  private frameHandle = 0;
  private disposed = false;
  private framed = false;
  private pointerDownAt: { x: number; y: number } | null = null;
  private resizeObserver?: ResizeObserver;

  constructor() {
    // Build the scene once the host element exists.
    effect(() => {
      const host = this.host().nativeElement;
      // Guard on `started`, not on `renderer`: in software mode there is no
      // renderer, and testing for one would re-run setup on every flush.
      if (!this.started) {
        this.started = true;
        this.setup(host);
      }
    });

    effect(() => {
      // Reading the key registers the dependency; a change means a new model.
      this.resetKey();
      this.framed = false;
    });

    effect(() => {
      const bodies = this.bodies();
      if (this.modelGroup) this.rebuildModel(bodies);
    });

    effect(() => {
      const selected = this.selected();
      this.applySelection(selected);
    });

    effect(() => {
      const sketches = this.sketches();
      if (this.sketchGroup) this.rebuildSketches(sketches);
    });

    effect(() => {
      const shown = this.showSketches();
      if (this.sketchGroup) {
        this.sketchGroup.visible = shown;
        this.dirty = true;
      }
    });
  }

  ngOnDestroy(): void {
    this.disposed = true;
    cancelAnimationFrame(this.frameHandle);
    this.resizeObserver?.disconnect();
    window.removeEventListener('resize', this.onResize);
    this.controls?.dispose();
    this.disposeModel();
    // dispose() releases three's own resources but leaves the GL context alive;
    // without forcing the loss, repeated mounts exhaust the browser's context
    // budget and every later viewport fails to start.
    this.renderer?.forceContextLoss();
    this.renderer?.dispose();
    this.canvas?.remove();
  }

  // -- setup ---------------------------------------------------------------

  private setup(host: HTMLElement): void {
    // WebGL is not always available — no GPU, a hardened browser, a remote
    // desktop, or a GPU process that has crashed. Rather than apologise, fall
    // back to painting the same scene on the CPU: for a tool whose output goes
    // to a slicer, looking at the part is not optional.
    // `?render=software` forces the CPU path, so the fallback can be exercised
    // on a machine where WebGL works — otherwise it is only ever tested where
    // it is hardest to debug.
    const forced = new URLSearchParams(location.search).get('render') === 'software';
    if (!forced && webglAvailable()) {
      try {
        const renderer = new THREE.WebGLRenderer({ antialias: true });
        renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
        renderer.setClearColor(0x070a0f);
        host.appendChild(renderer.domElement);
        this.renderer = renderer;
        this.canvas = renderer.domElement;
        // A context can also be lost while running; recover rather than freeze.
        renderer.domElement.addEventListener('webglcontextlost', this.onContextLost);
      } catch {
        this.renderer = undefined;
      }
    }

    if (!this.renderer) {
      const canvas = document.createElement('canvas');
      host.appendChild(canvas);
      this.canvas = canvas;
      this.painter = new CanvasPainter(canvas);
      this.software.set(true);
    }

    const scene = new THREE.Scene();
    this.scene = scene;

    const camera = new THREE.PerspectiveCamera(40, 1, 0.1, 500_000);
    camera.up.set(0, 0, 1); // Z-up, as CAD expects
    camera.position.set(200, -200, 160);
    this.camera = camera;

    const controls = new OrbitControls(camera, this.canvas!);
    controls.enableDamping = true;
    controls.dampingFactor = 0.12;
    this.controls = controls;

    scene.add(new THREE.HemisphereLight(0xffffff, 0x0a1018, 1.6));
    const key = new THREE.DirectionalLight(0xffffff, 2);
    key.position.set(1, -1, 2);
    scene.add(key);
    const fill = new THREE.DirectionalLight(0xffffff, 0.7);
    fill.position.set(-2, 1, 1);
    scene.add(fill);

    const grid = new THREE.GridHelper(2000, 100, 0x26374a, 0x1b2735);
    grid.rotation.x = Math.PI / 2; // GridHelper is XZ by default; we want XY
    const gridMaterial = grid.material as THREE.Material;
    gridMaterial.transparent = true;
    gridMaterial.opacity = 0.5;
    scene.add(grid);
    scene.add(new THREE.AxesHelper(40));

    this.modelGroup = new THREE.Group();
    scene.add(this.modelGroup);
    this.sketchGroup = new THREE.Group();
    scene.add(this.sketchGroup);

    // Size from the parent's rect. ResizeObserver is used when it works, but
    // is never depended upon: where it is silent the window listener and the
    // initial measurement still produce a correctly sized canvas.
    this.onResize();
    window.addEventListener('resize', this.onResize);
    if (typeof ResizeObserver !== 'undefined') {
      this.resizeObserver = new ResizeObserver(() => this.onResize());
      this.resizeObserver.observe(host);
    }

    this.canvas?.addEventListener('pointerdown', this.onPointerDown);
    this.canvas?.addEventListener('pointerup', this.onPointerUp);

    this.rebuildModel(this.bodies());
    this.rebuildSketches(this.sketches());
    this.loop();
  }

  private readonly onResize = (): void => {
    const host = this.host().nativeElement;
    if (!this.camera) return;
    const width = Math.max(host.clientWidth, 1);
    const height = Math.max(host.clientHeight, 1);

    this.renderer?.setSize(width, height, false);
    this.painter?.setSize(width, height, Math.min(window.devicePixelRatio, 2));
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    this.dirty = true;
  };

  /** A lost context is recoverable; drop to the CPU painter and carry on. */
  private readonly onContextLost = (event: Event): void => {
    event.preventDefault();
    if (this.software()) return;
    this.renderer?.dispose();
    this.renderer = undefined;
    const host = this.host().nativeElement;
    host.replaceChildren();
    const canvas = document.createElement('canvas');
    host.appendChild(canvas);
    this.canvas = canvas;
    this.painter = new CanvasPainter(canvas);
    this.software.set(true);
    this.onResize();
    this.dirty = true;
  };

  private loop = (): void => {
    if (this.disposed || !this.scene || !this.camera) return;
    const moved = this.controls?.update() ?? false;

    if (this.renderer) {
      this.renderer.render(this.scene, this.camera);
    } else if (this.painter && (moved || this.dirty)) {
      // Painting costs real CPU, so only repaint when something actually
      // changed rather than burning a core at display rate.
      this.painter.render(this.scene, this.camera);
      this.dirty = false;
    }
    this.frameHandle = requestAnimationFrame(this.loop);
  };

  // -- picking -------------------------------------------------------------

  private readonly onPointerDown = (event: PointerEvent): void => {
    this.pointerDownAt = { x: event.clientX, y: event.clientY };
  };

  private readonly onPointerUp = (event: PointerEvent): void => {
    const down = this.pointerDownAt;
    this.pointerDownAt = null;
    if (!down || !this.canvas || !this.camera) return;

    // Orbiting must never change the selection.
    if (Math.hypot(event.clientX - down.x, event.clientY - down.y) > CLICK_SLOP_PX) return;

    const rect = this.canvas.getBoundingClientRect();
    const pointer = new THREE.Vector2(
      ((event.clientX - rect.left) / rect.width) * 2 - 1,
      -((event.clientY - rect.top) / rect.height) * 2 + 1,
    );
    this.raycaster.setFromCamera(pointer, this.camera);
    const hits = this.raycaster.intersectObjects(this.faces, false);
    // Ctrl/cmd-click adds to the selection here exactly as it does in the
    // topology list, so picking faces off the model builds the same set.
    const mode: SelectMode = event.shiftKey
      ? 'range'
      : event.ctrlKey || event.metaKey
        ? 'toggle'
        : 'replace';
    const hit = hits[0];
    const tag = hit ? (hit.object.userData['tag'] as string) : null;
    // three.js reports the intersection already in world coordinates, so a
    // body's placement is accounted for and nothing more is applied here.
    this.tagPicked.emit({
      tag,
      mode,
      point: hit ? [hit.point.x, hit.point.y, hit.point.z] : null,
    });
  };

  // -- model ---------------------------------------------------------------

  private rebuildModel(bodies: BodyMesh[]): void {
    this.disposeModel();
    if (bodies.length === 0) return;

    for (const body of bodies) {
      if (body.positions.length === 0) continue;
      // Each body is its own group carrying the placement matrix, so moving a
      // body is a transform rather than a geometry rebuild.
      const group = new THREE.Group();
      group.matrixAutoUpdate = false;
      group.matrix.fromArray(body.placement);
      group.userData['body'] = body.id;
      this.modelGroup?.add(group);
      this.addBodyGeometry(group, body);
    }

    this.applySelection(this.selected());
    this.dirty = true;

    // Frame once. Later rebuilds leave the camera where the user put it.
    if (!this.framed) {
      this.framed = true;
      this.frameModel(bodies);
    }
  }

  private addBodyGeometry(group: THREE.Group, mesh: BodyMesh): void {
    // Faces share one position/normal buffer and each owns an index range, so
    // a raycast hit resolves to exactly one tag.
    const positions = new THREE.BufferAttribute(new Float32Array(mesh.positions), 3);
    const normals = new THREE.BufferAttribute(new Float32Array(mesh.normals), 3);

    for (const range of mesh.faceRanges) {
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute('position', positions);
      geometry.setAttribute('normal', normals);
      geometry.setIndex(mesh.indices.slice(range.start, range.start + range.count));
      geometry.computeBoundingSphere();

      const face = new THREE.Mesh(
        geometry,
        new THREE.MeshStandardMaterial({
          color: FACE_COLOR,
          roughness: 0.55,
          metalness: 0.05,
          side: THREE.DoubleSide,
        }),
      );
      face.userData['tag'] = range.tag;
      face.userData['body'] = mesh.id;
      group.add(face);
      this.faces.push(face);
    }

    // The kernel's exact edge curves, not mesh wireframe — this is what makes
    // the viewport read as CAD rather than as a triangle soup.
    const edgePoints: number[] = [];
    for (const edge of mesh.edges) {
      for (let i = 0; i + 5 < edge.points.length; i += 3) {
        edgePoints.push(
          edge.points[i], edge.points[i + 1], edge.points[i + 2],
          edge.points[i + 3], edge.points[i + 4], edge.points[i + 5],
        );
      }
    }
    if (edgePoints.length > 0) {
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute('position', new THREE.Float32BufferAttribute(edgePoints, 3));
      group.add(
        new THREE.LineSegments(geometry, new THREE.LineBasicMaterial({ color: 0x0b1017 })),
      );
    }
  }

  /**
   * Draw sketches as line work on their datum planes.
   *
   * Points are drawn as small three-axis crosses rather than as THREE.Points,
   * so both the WebGL and the CPU path render them with no extra code — a
   * marker is just more line segments.
   */
  private rebuildSketches(geometry: SketchGeometry | null): void {
    const group = this.sketchGroup;
    if (!group) return;

    for (const child of [...group.children]) {
      group.remove(child);
      const line = child as THREE.LineSegments;
      line.geometry?.dispose();
      (line.material as THREE.Material)?.dispose();
    }
    if (!geometry) return;

    const curveSegments: number[] = [];
    const markerSegments: number[] = [];

    for (const sketch of geometry.sketches) {
      for (const curve of sketch.curves) {
        // A polyline of n points becomes n-1 segments.
        for (let i = 0; i + 5 < curve.points.length; i += 3) {
          curveSegments.push(
            curve.points[i], curve.points[i + 1], curve.points[i + 2],
            curve.points[i + 3], curve.points[i + 4], curve.points[i + 5],
          );
        }
      }
      for (const point of sketch.points) {
        const [x, y, z] = point.at;
        const size = MARKER_SIZE;
        markerSegments.push(
          x - size, y, z, x + size, y, z,
          x, y - size, z, x, y + size, z,
          x, y, z - size, x, y, z + size,
        );
      }
    }

    if (curveSegments.length > 0) {
      group.add(this.lineSegments(curveSegments, SKETCH_COLOR));
    }
    if (markerSegments.length > 0) {
      group.add(this.lineSegments(markerSegments, SKETCH_POINT_COLOR));
    }
    group.visible = this.showSketches();
    this.dirty = true;
  }

  private lineSegments(points: number[], color: number): THREE.LineSegments {
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.Float32BufferAttribute(points, 3));
    return new THREE.LineSegments(
      geometry,
      new THREE.LineBasicMaterial({ color, depthTest: false, transparent: true, opacity: 0.9 }),
    );
  }

  private disposeModel(): void {
    if (!this.modelGroup) return;
    for (const group of [...this.modelGroup.children]) {
      this.modelGroup.remove(group);
      group.traverse((child) => {
        const drawable = child as THREE.Mesh | THREE.LineSegments;
        drawable.geometry?.dispose();
        const material = (drawable as THREE.Mesh).material;
        if (Array.isArray(material)) material.forEach((m) => m.dispose());
        else material?.dispose();
      });
    }
    this.faces = [];
  }

  private applySelection(selected: ReadonlySet<string>): void {
    for (const face of this.faces) {
      const material = face.material as THREE.MeshStandardMaterial;
      const active = selected.has(face.userData['tag'] as string);
      material.color.setHex(active ? FACE_SELECTED : FACE_COLOR);
      material.emissive.setHex(active ? EMISSIVE_ON : EMISSIVE_OFF);
    }
    this.dirty = true;
  }

  /** Frame the current model on demand, for the "fit view" shortcut. */
  fitView(): void {
    this.frameModel(this.bodies());
  }

  /** Point the camera at the model from a standard CAD three-quarter angle. */
  private frameModel(bodies: BodyMesh[]): void {
    if (!this.camera || !this.controls) return;
    const box = new THREE.Box3();
    const point = new THREE.Vector3();
    const placement = new THREE.Matrix4();

    // Bodies are modelled at their own origin, so the bounds have to be taken
    // after placement or an assembly frames onto only one of them.
    for (const body of bodies) {
      placement.fromArray(body.placement);
      for (let i = 0; i < body.positions.length; i += 3) {
        point
          .set(body.positions[i], body.positions[i + 1], body.positions[i + 2])
          .applyMatrix4(placement);
        box.expandByPoint(point);
      }
    }
    if (box.isEmpty()) return;

    const centre = box.getCenter(new THREE.Vector3());
    const radius = Math.max(box.getSize(new THREE.Vector3()).length() / 2, 1);
    const distance = radius / Math.sin((this.camera.fov * Math.PI) / 360);

    this.controls.target.copy(centre);
    this.camera.position.copy(
      centre
        .clone()
        .add(new THREE.Vector3(0.75, -1, 0.7).normalize().multiplyScalar(distance * 1.25)),
    );
    this.camera.near = Math.max(distance / 1000, 0.01);
    this.camera.far = distance * 100;
    this.camera.updateProjectionMatrix();
    this.controls.update();
    this.dirty = true;
  }
}
