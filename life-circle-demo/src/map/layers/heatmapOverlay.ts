/** §8：挂在真实 BMapGL 上的自定义 Canvas 热力覆盖物（设施密度模式）。
 *
 * 覆盖物只做三件事：把画布挂进地图公开的覆盖物容器、把视角变化合并成一次重绘、
 * 在卸载时把自己彻底摘掉。数值与几何全部在 density.ts，这里只负责像素。
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
 */
import type {
  BaiduMapApi, BMapMap, BMapOverlayInstance, BMapPixel, BMapViewEventType,
} from '../baiduMapTypes';
import {
  DENSITY_SCALE_MAX, HEAT_KERNEL_RADIUS_M, computeDensity, densityRgba, dedupePoints,
  intersectRect, pixelRings, ringBounds, type DensityField, type PixelRect, type Point2,
} from './density';

const VIEW_EVENTS: BMapViewEventType[] = ['moveend', 'zoomend', 'resize'];
/** 米/像素探针的经差：约 0.001° ≈ 95 米，远大于投影像素精度。 */
const PROBE_DEGREES = 0.001;
/** requestFrame 同步执行（没有 rAF 的环境）时的句柄。 */
const SYNC_FRAME = -1;

export type DensityOverlayOptions = {
  radiusM?: number;
  scaleMax?: number;
  cell?: number;
  /** 视口尺寸（CSS 像素）；缺省读覆盖物容器的 clientWidth/clientHeight。 */
  viewport?: () => { width: number; height: number };
  /** 设备像素比；缺省读 window.devicePixelRatio。 */
  devicePixelRatio?: number;
  requestFrame?: (callback: () => void) => number;
  cancelFrame?: (handle: number) => void;
};

export type DensityOverlayStats = {
  /** 是否已经画出过热力：无设施、无边界或画布不可用时为 false。 */
  drawn: boolean;
  points: number;
  max: number;
};

export type DensityOverlay = {
  /** 交给 map.addOverlay 的实例（继承 SDK 的覆盖物基类）。 */
  readonly overlay: BMapOverlayInstance;
  /** 幂等挂载；已经挂在图上时什么也不做。 */
  attach(instance: BMapMap): void;
  /** 摘除；由 SDK 因 clearOverlays 调用 remove 时同样会走到这里。 */
  detach(): void;
  setFacilities(points: ReadonlyArray<Point2 & { id?: string }>): void;
  /** 计算圈面；设施只在圈内参与着色（§8.2 步骤 6）。 */
  setBoundary(geometry: { type: string; coordinates: unknown } | null): void;
  redraw(): void;
  stats(): DensityOverlayStats;
  destroy(): void;
};

function frames(options: DensityOverlayOptions) {
  const request = options.requestFrame
    ?? (typeof requestAnimationFrame === 'function' ? (cb: () => void) => requestAnimationFrame(cb) : null);
  const cancel = options.cancelFrame
    ?? (typeof cancelAnimationFrame === 'function' ? (handle: number) => cancelAnimationFrame(handle) : null);
  if (!request || !cancel) return { requestFrame: (cb: () => void) => { cb(); return SYNC_FRAME; }, cancelFrame: () => {} };
  return { requestFrame: request, cancelFrame: cancel };
}

/**
 * 建立热力覆盖物；SDK 缺少自定义覆盖物基类时返回 null，界面据此提示降级。
 */
export function createDensityOverlay(api: BaiduMapApi, options: DensityOverlayOptions = {}): DensityOverlay | null {
  const Base = api.Overlay;
  if (!Base) return null;
  const radiusM = options.radiusM ?? HEAT_KERNEL_RADIUS_M;
  const scaleMax = options.scaleMax ?? DENSITY_SCALE_MAX;
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
  let facilities: Point2[] = [];
  let boundary: { type: string; coordinates: unknown } | null = null;
  const stats: DensityOverlayStats = { drawn: false, points: 0, max: 0 };
  const viewHandlers = new Map<BMapViewEventType, () => void>();

  const pixelRatio = () => options.devicePixelRatio
    ?? (typeof window !== 'undefined' && window.devicePixelRatio ? window.devicePixelRatio : 1);

  const paneFor = (instance: BMapMap): HTMLElement | null => {
    const panes = instance.getPanes?.();
    if (!panes) return null;
    // 热力要压在设施与中心交互层之下：能用 overlayPane 就用，否则用底图容器；
    // 两者都没有时退回 markerPane 的首个子节点，插在已有标记之前。
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
  const metersPerPixelFor = (project: (lng: number, lat: number) => BMapPixel, anchorLng: number) => {
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
    canvas = element;
    ctx = element.getContext('2d');
    return element;
  };

  /** 画布必须留在覆盖物容器里；被 SDK 或 clearOverlays 摘走时由这里补回。 */
  const ensurePlaced = () => {
    const element = canvas;
    if (!element || !pane || element.parentNode === pane) return;
    const panes = map?.getPanes?.();
    if (panes?.markerPane === pane && !panes.overlayPane && !panes.mapPane && pane.firstChild) {
      pane.insertBefore(element, pane.firstChild);
    } else {
      pane.appendChild(element);
    }
  };

  const paint = (field: DensityField, rings: number[][][], rect: PixelRect, run: CanvasRenderingContext2D) => {
    if (!buffer || buffer.width !== field.width || buffer.height !== field.height) {
      buffer = document.createElement('canvas');
      buffer.width = field.width;
      buffer.height = field.height;
      bufferCtx = buffer.getContext('2d');
    }
    if (!bufferCtx) throw new Error('no-buffer-context');
    // 密度先在离屏缓冲查色带，再作为一整张图贴上去；不逐格 fillRect。
    const image = bufferCtx.createImageData(field.width, field.height);
    for (let index = 0; index < field.values.length; index++) {
      const [r, g, b, a] = densityRgba(field.values[index], scaleMax);
      const at = index * 4;
      image.data[at] = r; image.data[at + 1] = g; image.data[at + 2] = b; image.data[at + 3] = a;
    }
    bufferCtx.putImageData(image, 0, 0);
    run.save();
    try {
      // 计算圈面即 alpha mask：外环减内孔、分量独立，核扩散不得越界或填孔。
      run.beginPath();
      for (const component of rings) {
        for (const ring of component) {
          if (ring.length < 6) continue;
          run.moveTo(ring[0], ring[1]);
          for (let i = 2; i + 1 < ring.length; i += 2) run.lineTo(ring[i], ring[i + 1]);
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
    stats.drawn = false;
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
    stats.points = facilities.length;
    // 没有真实设施就不画：不在圈内补随机热力点（§8.2）。
    if (!facilities.length || !boundary) { stats.max = 0; return; }
    const centrePoint = instance.getCenter();
    const centre = project(centrePoint.lng, centrePoint.lat);
    if (!Number.isFinite(centre?.x) || !Number.isFinite(centre?.y)) return;
    const rect: PixelRect = { x: centre.x - size.width / 2, y: centre.y - size.height / 2,
      width: size.width, height: size.height };
    let rings: number[][][];
    try {
      rings = pixelRings(boundary, project);
    } catch {
      return;
    }
    const bounds = ringBounds(rings);
    const cover = bounds ? intersectRect(rect, bounds) : null;
    if (!cover) return;
    const field = computeDensity({
      points: facilities, project,
      metersPerPixel: metersPerPixelFor(project, centrePoint.lng),
      rect: cover, radiusM, cell: options.cell,
    });
    stats.max = field.max;
    if (!(field.max > 0)) return;
    try {
      paint(field, rings, rect, run);
      stats.drawn = true;
    } catch {
      stats.drawn = false;
    }
  };

  const schedule = () => {
    if (destroyed || frame !== null) return;
    const handle = requestFrame(() => { frame = null; render(); });
    frame = handle === SYNC_FRAME ? null : handle;
  };

  const listen = (instance: BMapMap) => {
    for (const type of VIEW_EVENTS) {
      const handler = () => schedule();
      viewHandlers.set(type, handler);
      instance.addEventListener(type, handler);
    }
  };

  const teardown = () => {
    if (frame !== null) { cancelFrame(frame); frame = null; }
    for (const [type, handler] of viewHandlers) map?.removeEventListener?.(type, handler);
    viewHandlers.clear();
    canvas?.parentNode?.removeChild(canvas);
    if (canvas) { canvas.width = 0; canvas.height = 0; }
    canvas = null; ctx = null; buffer = null; bufferCtx = null; pane = null;
    stats.drawn = false;
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
    facilities = []; boundary = null; stats.points = 0; stats.max = 0;
  };

  return { overlay, attach, detach, destroy,
    setFacilities(points) { facilities = dedupePoints(points); schedule(); },
    setBoundary(geometry) { boundary = geometry; schedule(); },
    redraw: schedule,
    stats: () => ({ ...stats }) };
}
