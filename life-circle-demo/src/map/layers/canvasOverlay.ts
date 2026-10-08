/** §8：挂在真实 BMapGL 上的自定义 Canvas 覆盖物骨架，设施密度与服务覆盖两层共用。
 *
 * 骨架只做四件事：把画布挂进地图公开的覆盖物容器、把视角变化合并成一次重绘、按计算圈
 * 裁剪后把一张逐缓冲格的颜色图贴上去、在卸载时把自己彻底摘掉。每一层"算什么颜色"
 * 由调用方的 compute 决定（density.ts / serviceField.ts），这里只负责像素。
 *
 * 关键取舍：
 *
 * * **画布原点用地图中心反推。** 覆盖物像素坐标只在一个视口内线性，视口中心就是
 *   地图中心，因此画布原点 = 中心像素 − 视口尺寸的一半，不需要读 DOM 矩形，也就
 *   不受覆盖物容器自身偏移的影响。
 * * **裁剪用上下文路径 + evenodd。** 外环挖内孔、分量各自独立，与 Path2D 同一条
 *   奇偶规则；用上下文路径还免去 Path2D 这个额外依赖。
 * * **拖动过程中不重绘。** 画布随容器一起移动，只在真实移动/缩放结束、resize 时
 *   合并重绘。SDK 因 clearOverlays 调走 remove 时，覆盖物可以再次挂载并重建画布，
 *   所以重绘仍然由本层自己拥有，不依赖调用方每次重新 new 一个覆盖物。
 * * **画布在 DOM 覆盖物容器里，压在 WebGL 面之上。** BMapGL 的面（等时圈、灰区）画在
 *   地图自己的 WebGL 画布上，公开的覆盖物容器（markerPane 等）都是其上的 DOM 层；
 *   自定义画布插在 markerPane 最前面，位于标记之下、所有面之上。所以面的填色不会
 *   "盖住"热力，却会透过半透明热力把颜色染成一片 —— 开热力时由调用方把圈面改成只描边。
 * * **标注画布压在热力之上。** 水系标注（冲突、底图误绘）必须在热力颜色之上才读得出，
 *   所以 `above` 的画布插在最后一张业务画布之后、标记之前；热力画布重挂时插在它们之下。
 */
import type {
  BaiduMapApi, BMapMap, BMapOverlayInstance, BMapPixel, BMapViewEventType,
} from '../baiduMapTypes';
import {
  intersectRect, pixelRings, ringBounds, type MetersPerPixel, type PixelRect, type Project,
} from './density';

const VIEW_EVENTS: BMapViewEventType[] = ['moveend', 'zoomend', 'resize'];
/** 米/像素探针的经差：约 0.001° ≈ 95 米，远大于投影像素精度。 */
const PROBE_DEGREES = 0.001;
/** requestFrame 同步执行（没有 rAF 的环境）时的句柄。 */
const SYNC_FRAME = -1;

export type Geometry = { type: string; coordinates: unknown };

export type CanvasFieldOptions = {
  /** 视口尺寸（CSS 像素）；缺省读覆盖物容器的 clientWidth/clientHeight。 */
  viewport?: () => { width: number; height: number };
  /** 设备像素比；缺省读 window.devicePixelRatio。 */
  devicePixelRatio?: number;
  requestFrame?: (callback: () => void) => number;
  cancelFrame?: (handle: number) => void;
};

/** 一帧的几何：投影、米/像素、可见视口，以及裁剪面与它的可见包围盒。 */
export type FieldFrame = {
  project: Project;
  metersPerPixel: MetersPerPixel;
  view: PixelRect;
  /** 裁剪面包围盒 ∩ 视口：只有这里需要计算。 */
  cover: PixelRect;
  /** 裁剪面的覆盖物像素环（未减视口原点）。 */
  rings: number[][][];
};

/** 矢量标注的一帧：投影与可见视口（覆盖物像素）；画的时候减去视口原点。 */
export type VectorFrame = { project: Project; view: PixelRect };

/** 逐缓冲格的直通 RGBA；(x, y) 是缓冲格 (0,0) 左上角的覆盖物像素坐标。 */
export type FieldImage = {
  cell: number;
  width: number;
  height: number;
  x: number;
  y: number;
  rgba: Uint8ClampedArray;
};

export type CanvasFieldSpec = {
  /** 画布的 data-testid；两层画布同时存在时据此区分。 */
  testId: string;
  /** 画布的 data-map-layer；缺省 'heat'。 */
  layer?: string;
  /** 压在其他业务画布之上（标注层）；缺省在最底下。 */
  above?: boolean;
  /** 每帧清空画布之后调用：本帧的裁剪面，null 表示本帧不画颜色图。 */
  boundary(): Geometry | null;
  /** 在 frame.cover 内算出颜色图；null 表示没有可画的内容。 */
  compute(frame: FieldFrame): FieldImage | null;
  /** 矢量标注：在颜色图之后画，不受裁剪面限制；返回是否画了东西。 */
  vector?(run: CanvasRenderingContext2D, frame: VectorFrame): boolean;
};

export type CanvasField = {
  /** 交给 map.addOverlay 的实例（继承 SDK 的覆盖物基类）。 */
  readonly overlay: BMapOverlayInstance;
  /** 幂等挂载；已经挂在图上时什么也不做。 */
  attach(instance: BMapMap): void;
  /** 摘除；由 SDK 因 clearOverlays 调用 remove 时同样会走到这里。 */
  detach(): void;
  redraw(): void;
  /** 上一帧是否真的贴出了图。 */
  drawn(): boolean;
  destroy(): void;
};

function frames(options: CanvasFieldOptions) {
  const request = options.requestFrame
    ?? (typeof requestAnimationFrame === 'function' ? (cb: () => void) => requestAnimationFrame(cb) : null);
  const cancel = options.cancelFrame
    ?? (typeof cancelAnimationFrame === 'function' ? (handle: number) => cancelAnimationFrame(handle) : null);
  if (!request || !cancel) return { requestFrame: (cb: () => void) => { cb(); return SYNC_FRAME; }, cancelFrame: () => {} };
  return { requestFrame: request, cancelFrame: cancel };
}

/**
 * 建立 Canvas 覆盖物；SDK 缺少自定义覆盖物基类时返回 null，界面据此提示降级。
 */
export function createCanvasField(
  api: BaiduMapApi,
  options: CanvasFieldOptions,
  spec: CanvasFieldSpec,
): CanvasField | null {
  const Base = api.Overlay;
  if (!Base) return null;
  const { requestFrame, cancelFrame } = frames(options);

  let map: BMapMap | null = null;
  let canvas: HTMLCanvasElement | null = null;
  let ctx: CanvasRenderingContext2D | null = null;
  let buffer: HTMLCanvasElement | null = null;
  let bufferCtx: CanvasRenderingContext2D | null = null;
  let pane: HTMLElement | null = null;
  let frame: number | null = null;
  let attached = false;
  let destroyed = false;
  let drawn = false;
  const viewHandlers = new Map<BMapViewEventType, () => void>();

  const pixelRatio = () => options.devicePixelRatio
    ?? (typeof window !== 'undefined' && window.devicePixelRatio ? window.devicePixelRatio : 1);

  const paneFor = (instance: BMapMap): HTMLElement | null => {
    const panes = instance.getPanes?.();
    if (!panes) return null;
    // 热力要压在设施与中心交互层之下：能用 overlayPane 就用，否则用底图容器；
    // 两者都没有时（真实 BMapGL 即如此）退回 markerPane 的首个子节点，插在已有标记之前。
    return panes.overlayPane ?? panes.mapPane ?? panes.markerPane ?? null;
  };

  const viewport = () => {
    if (options.viewport) return options.viewport();
    const el = pane ?? canvas?.parentElement ?? null;
    return { width: el?.clientWidth ?? 0, height: el?.clientHeight ?? 0 };
  };

  /** 经纬度 → 覆盖物像素；SDK 不提供时热力层无法定位，直接不绘制。 */
  const projector = () => {
    const instance = map;
    if (!instance || typeof instance.pointToOverlayPixel !== 'function') return null;
    return (lng: number, lat: number): BMapPixel => instance.pointToOverlayPixel(new api.Point(lng, lat));
  };

  /**
   * 该纬度的米/像素，由地图自身投影测得：把 0.001° 经差投到像素上反推。
   * 不假设 Web 墨卡托公式，缩放与纬度都由地图回答。
   */
  const metersPerPixelFor = (project: Project, anchorLng: number): MetersPerPixel => {
    const cache = new Map<number, number>();
    return (lat: number) => {
      if (!Number.isFinite(lat)) return 0;
      const key = Math.round(lat * 1e5) / 1e5;
      const hit = cache.get(key);
      if (hit !== undefined) return hit;
      const a = project(anchorLng, key);
      const b = project(anchorLng + PROBE_DEGREES, key);
      const pixels = Math.hypot(b.x - a.x, b.y - a.y);
      const meters = 111_320 * Math.cos((key * Math.PI) / 180) * PROBE_DEGREES;
      const value = pixels > 0 && meters > 0 ? meters / pixels : 0;
      cache.set(key, value);
      return value;
    };
  };

  const ensureCanvas = () => {
    if (canvas) return canvas;
    const element = document.createElement('canvas');
    // 画布不接收指针事件：拖动、滚轮和选点仍然归地图（§8.2 步骤 9）。
    element.style.position = 'absolute';
    element.style.left = '0';
    element.style.top = '0';
    element.style.pointerEvents = 'none';
    element.setAttribute('aria-hidden', 'true');
    element.setAttribute('data-testid', spec.testId);
    // 业务画布的共同标记：真实验收据此把它们与底图自己的画布分开。
    element.setAttribute('data-map-layer', spec.layer ?? 'heat');
    if (spec.above) element.setAttribute('data-map-stack', 'above');
    canvas = element;
    ctx = element.getContext('2d');
    return element;
  };

  /** 画布必须留在覆盖物容器里；被 SDK 或 clearOverlays 摘走时由这里补回。 */
  const ensurePlaced = () => {
    const element = canvas;
    if (!element || !pane || element.parentNode === pane) return;
    const panes = map?.getPanes?.();
    const markerOnly = panes?.markerPane === pane && !panes.overlayPane && !panes.mapPane;
    const children = Array.from((pane.children ?? []) as ArrayLike<Element>);
    const attribute = (node: Element, name: string) => node.getAttribute?.(name) ?? null;
    if (spec.above) {
      // 标注：插在最后一张业务画布之后，仍在所有标记之前。
      const business = children.filter(node => attribute(node, 'data-map-layer') !== null);
      const last = business[business.length - 1];
      if (last) pane.insertBefore(element, last.nextSibling);
      else if (markerOnly && pane.firstChild) pane.insertBefore(element, pane.firstChild);
      else pane.appendChild(element);
      return;
    }
    const annotation = children.find(node => attribute(node, 'data-map-stack') === 'above');
    if (annotation) {
      pane.insertBefore(element, annotation);
    } else if (markerOnly && pane.firstChild) {
      pane.insertBefore(element, pane.firstChild);
    } else {
      pane.appendChild(element);
    }
  };

  const paint = (field: FieldImage, rings: number[][][], rect: PixelRect, run: CanvasRenderingContext2D) => {
    if (!buffer || buffer.width !== field.width || buffer.height !== field.height) {
      buffer = document.createElement('canvas');
      buffer.width = field.width;
      buffer.height = field.height;
      bufferCtx = buffer.getContext('2d');
    }
    if (!bufferCtx) throw new Error('no-buffer-context');
    // 颜色先写进离屏缓冲，再作为一整张图贴上去；不逐格 fillRect。
    const image = bufferCtx.createImageData(field.width, field.height);
    image.data.set(field.rgba);
    bufferCtx.putImageData(image, 0, 0);
    run.save();
    try {
      // 计算圈面即 alpha mask：外环减内孔、分量独立，插值与核扩散都不得越界或填孔。
      run.beginPath();
      for (const component of rings) {
        for (const ring of component) {
          if (ring.length < 6) continue;
          run.moveTo(ring[0] - rect.x, ring[1] - rect.y);
          for (let i = 2; i + 1 < ring.length; i += 2) run.lineTo(ring[i] - rect.x, ring[i + 1] - rect.y);
          run.closePath();
        }
      }
      run.clip('evenodd');
      run.imageSmoothingEnabled = true;
      run.drawImage(buffer, field.x - rect.x, field.y - rect.y,
        field.width * field.cell, field.height * field.cell);
    } finally {
      run.restore();
    }
  };

  const render = () => {
    const instance = map;
    const run = ctx;
    const element = canvas;
    drawn = false;
    if (destroyed || !instance || !run || !element) return;
    const project = projector();
    const size = viewport();
    if (!project || !(size.width > 0) || !(size.height > 0)) return;
    const dpr = pixelRatio();
    element.width = Math.max(1, Math.round(size.width * dpr));
    element.height = Math.max(1, Math.round(size.height * dpr));
    element.style.width = `${size.width}px`;
    element.style.height = `${size.height}px`;
    ensurePlaced();
    run.setTransform(dpr, 0, 0, dpr, 0, 0);
    run.clearRect(0, 0, size.width, size.height);
    const boundary = spec.boundary();
    if (!boundary && !spec.vector) return;
    const centrePoint = instance.getCenter();
    const centre = project(centrePoint.lng, centrePoint.lat);
    if (!Number.isFinite(centre?.x) || !Number.isFinite(centre?.y)) return;
    const rect: PixelRect = { x: centre.x - size.width / 2, y: centre.y - size.height / 2,
      width: size.width, height: size.height };
    // SDK 的覆盖物坐标原点不一定是视口左上角；画布位置与裁剪路径需使用同一原点。
    element.style.left = `${rect.x}px`;
    element.style.top = `${rect.y}px`;
    const painted = boundary ? paintField(boundary, project, centrePoint.lng, rect, run) : false;
    let annotated = false;
    if (spec.vector) {
      try {
        annotated = spec.vector(run, { project, view: rect });
      } catch {
        annotated = false;
      }
    }
    drawn = painted || annotated;
  };

  /** 颜色图：按裁剪面算、按裁剪面贴。返回是否贴出了图。 */
  const paintField = (boundary: Geometry, project: Project, anchorLng: number, rect: PixelRect,
    run: CanvasRenderingContext2D): boolean => {
    let rings: number[][][];
    try {
      rings = pixelRings(boundary, project);
    } catch {
      return false;
    }
    const bounds = ringBounds(rings);
    const cover = bounds ? intersectRect(rect, bounds) : null;
    if (!cover) return false;
    try {
      const field = spec.compute({
        project, metersPerPixel: metersPerPixelFor(project, anchorLng), view: rect, cover, rings,
      });
      if (!field) return false;
      paint(field, rings, rect, run);
      return true;
    } catch {
      return false;
    }
  };

  const schedule = () => {
    if (destroyed || frame !== null) return;
    const handle = requestFrame(() => { frame = null; render(); });
    frame = handle === SYNC_FRAME ? null : handle;
  };

  const unlisten = () => {
    for (const [type, handler] of viewHandlers) map?.removeEventListener?.(type, handler);
    viewHandlers.clear();
  };

  const listen = (instance: BMapMap) => {
    // 重复 initialize（假 SDK 每次 addOverlay 都调）不叠加监听。
    unlisten();
    for (const type of VIEW_EVENTS) {
      const handler = () => schedule();
      viewHandlers.set(type, handler);
      instance.addEventListener(type, handler);
    }
  };

  const teardown = () => {
    if (frame !== null) { cancelFrame(frame); frame = null; }
    unlisten();
    canvas?.parentNode?.removeChild(canvas);
    if (canvas) { canvas.width = 0; canvas.height = 0; }
    canvas = null; ctx = null; buffer = null; bufferCtx = null; pane = null;
    drawn = false;
    // 真实 BMapGL 的 addOverlay 只在覆盖物没有 domElement 时才调 initialize，并把返回值
    // 缓存成 domElement；清空它的是基类 remove，而这里覆盖了 remove。不清掉的话，
    // 关掉热力再打开时 SDK 以为画布还在，不再调 initialize，图层就再也画不出来。
    (overlay as { domElement?: unknown }).domElement = null;
  };

  const attach = (instance: BMapMap) => {
    if (destroyed || attached) return;
    instance.addOverlay(overlay);
    attached = true;
  };

  const detach = () => {
    if (!attached) return;
    attached = false;
    map?.removeOverlay?.(overlay);
    teardown();
  };

  // 覆盖物基类由 SDK 提供，只能在运行时继承：用 Object.create 保持原型链，
  // addOverlay 的 instanceof 判断与官方 Overlay 子类一致。
  const overlay = Object.assign(
    Object.create((Base as { prototype?: object }).prototype ?? Object.prototype) as BMapOverlayInstance,
    {
      initialize(instance: BMapMap): HTMLElement | undefined {
        if (destroyed) return undefined;
        map = instance;
        pane = paneFor(instance);
        const element = ensureCanvas();
        // 画布立即进入覆盖物容器，不必等第一帧；render 里再补一次兜底。
        ensurePlaced();
        listen(instance);
        schedule();
        return element;
      },
      draw(): void { schedule(); },
      remove(): void {
        if (destroyed) return;
        attached = false;
        teardown();
      },
      addEventListener(): void { /* 覆盖物自身不派发事件 */ },
      removeEventListener(): void { /* 同上 */ },
    },
  ) as BMapOverlayInstance;

  const destroy = () => {
    if (destroyed) return;
    destroyed = true;
    detach();
  };

  return { overlay, attach, detach, destroy, redraw: schedule, drawn: () => drawn };
}
