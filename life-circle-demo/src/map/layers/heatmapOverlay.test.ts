/** 覆盖物生命周期：挂载、视角事件合并、摘除与重挂。
 *
 * 这里用测试替身驱动 SDK 的调用约定（addOverlay → initialize/getPanes、clearOverlays →
 * remove、视角事件 → 重绘），并不代表真实地图已经验收：真实 BMapGL 上的验证是另一件事，
 * 见 tests/analysis-ui.spec.ts 与 §8 的真实验收记录。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createDensityOverlay } from './heatmapOverlay';
import type { BaiduMapApi, BMapMap, BMapOverlayInstance, BMapPanes, BMapViewEventType } from '../baiduMapTypes';

const ORIGIN = { lng: 121.513925, lat: 31.313079 };
const M_PER_DEGREE = 111_320;

// -- 测试替身 -------------------------------------------------------------

type Ctx = CanvasRenderingContext2D & { calls: string[] };

function fakeContext(calls: string[]): Ctx {
  const record = (name: string) => (...args: unknown[]) => { calls.push(`${name}:${args.length}`); };
  return {
    calls,
    setTransform: record('setTransform'),
    clearRect: record('clearRect'),
    createImageData: (width: number, height: number) => ({
      width, height, data: new Uint8ClampedArray(width * height * 4),
    }),
    putImageData: record('putImageData'),
    save: record('save'),
    restore: record('restore'),
    beginPath: record('beginPath'),
    moveTo: (...args: number[]) => { record('moveTo')(...args); recordedMoves.push(args); },
    lineTo: record('lineTo'),
    closePath: record('closePath'),
    clip: (...args: unknown[]) => { calls.push(`clip:${args[0] === 'evenodd' ? 'evenodd' : 'nonzero'}`); },
    drawImage: record('drawImage'),
    imageSmoothingEnabled: true,
  } as unknown as Ctx;
}

/** 所有画布共用一个调用记录，断言时不必关心是主画布还是离屏缓冲。 */
let recorded: string[] = [];
let recordedMoves: number[][] = [];

function fakeCanvas() {
  const context = fakeContext(recorded);
  const canvas = {
    style: {} as Record<string, string>,
    width: 0,
    height: 0,
    parentNode: null as unknown,
    attributes: {} as Record<string, string>,
    getContext: () => context,
    setAttribute: (name: string, value: string) => { canvas.attributes[name] = value; },
  };
  return canvas;
}

function fakePane(width = 800, height = 600) {
  const children: unknown[] = [];
  const pane = {
    clientWidth: width,
    clientHeight: height,
    children,
    get firstChild() { return children[0] ?? null; },
    appendChild(node: { parentNode?: unknown }) { pane.remove(node); children.push(node); node.parentNode = pane; return node; },
    insertBefore(node: { parentNode?: unknown }, before: unknown) {
      pane.remove(node);
      const at = children.indexOf(before);
      if (at < 0) children.push(node); else children.splice(at, 0, node);
      node.parentNode = pane;
      return node;
    },
    removeChild(node: { parentNode?: unknown }) { pane.remove(node); },
    remove(node: { parentNode?: unknown }) {
      const at = children.indexOf(node);
      if (at >= 0) children.splice(at, 1);
      node.parentNode = null;
    },
  };
  return pane;
}

class FakeBase { }

/** SDK 替身：与 analysis-ui.spec.ts 的假 SDK 同构，但提供自定义覆盖物与像素转换。 */
function fakeApi() {
  const mapPane = fakePane();
  const markerPane = fakePane();
  const panes: BMapPanes = { mapPane: mapPane as unknown as HTMLElement, markerPane: markerPane as unknown as HTMLElement };
  const listeners = new Map<string, Set<() => void>>();
  const augmented = new Map<BMapOverlayInstance, unknown>();
  const events: string[] = [];
  const map = {
    center: { ...ORIGIN },
    overlays: [] as BMapOverlayInstance[],
    centerAndZoom() {},
    panTo(center: { lng: number; lat: number }) { map.center = center; },
    getCenter: () => ({ ...map.center }),
    zoomIn() {}, zoomOut() {}, enableScrollWheelZoom() {},
    addOverlay(overlay: BMapOverlayInstance) {
      if (!map.overlays.includes(overlay)) map.overlays.push(overlay);
      // 与真实 BMapGL 的 Overlay.prototype._i 一致：只有覆盖物还没有 domElement 时才调
      // initialize，返回值缓存成 domElement；清空它是基类 remove 的事。
      const cached = overlay as BMapOverlayInstance & { domElement?: unknown };
      if (cached.domElement) return;
      const element = overlay.initialize?.(map as unknown as BMapMap);
      cached.domElement = element;
      if (element) augmented.set(overlay, element);
    },
    removeOverlay(overlay: BMapOverlayInstance) {
      map.overlays = map.overlays.filter(item => item !== overlay);
      overlay.remove?.();
    },
    clearOverlays() { for (const overlay of [...map.overlays]) map.removeOverlay(overlay); },
    openInfoWindow() {}, closeInfoWindow() {},
    addEventListener(type: string, handler: () => void) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type)!.add(handler);
    },
    removeEventListener(type: string, handler: () => void) {
      listeners.get(type)?.delete(handler);
      events.push(`remove:${type}`);
    },
    pointToOverlayPixel(point: { lng: number; lat: number }) {
      return {
        x: (point.lng - ORIGIN.lng) * M_PER_DEGREE * Math.cos((ORIGIN.lat * Math.PI) / 180),
        y: (ORIGIN.lat - point.lat) * M_PER_DEGREE,
      };
    },
    getPanes: () => panes,
    destroy() {},
  };
  const api = {
    Map: class { constructor() { return map as unknown as never; } },
    Point: class { constructor(public lng: number, public lat: number) {} },
    Size: class { constructor(public width: number, public height: number) {} },
    Polygon: class {}, Marker: class {}, Label: class {}, InfoWindow: class {},
    Overlay: FakeBase as unknown as new () => BMapOverlayInstance,
  } as unknown as BaiduMapApi;
  return { api, map, panes, markerPane, mapPane, listeners, events, augmented,
    fire: (type: BMapViewEventType) => { for (const handler of listeners.get(type) ?? []) handler(); } };
}

/** 4×60 米见方的圈面，中心在原点。 */
function squareGeometry(half: number) {
  const toLng = (meters: number) => ORIGIN.lng + meters / (M_PER_DEGREE * Math.cos((ORIGIN.lat * Math.PI) / 180));
  const toLat = (meters: number) => ORIGIN.lat + meters / M_PER_DEGREE;
  return { type: 'Polygon', coordinates: [[[toLng(-half), toLat(-half)], [toLng(half), toLat(-half)],
    [toLng(half), toLat(half)], [toLng(-half), toLat(half)], [toLng(-half), toLat(-half)]]] };
}

const boundary = squareGeometry(60);

let frames: (() => void)[] = [];
const requestFrame = (callback: () => void) => { frames.push(callback); return frames.length; };
const cancelFrame = () => {};
const flush = () => { const pending = frames; frames = []; for (const callback of pending) callback(); };

beforeEach(() => {
  frames = [];
  recorded = [];
  recordedMoves = [];
  (globalThis as unknown as { document: unknown }).document = {
    createElement: (tag: string) => {
      if (tag !== 'canvas') throw new Error(`unexpected element ${tag}`);
      return fakeCanvas();
    },
  };
});

function makeLayer(env = fakeApi()) {
  const layer = createDensityOverlay(env.api, { requestFrame, cancelFrame, devicePixelRatio: 2, viewport: () => ({ width: 800, height: 600 }) })!;
  layer.attach(env.map as unknown as BMapMap);
  return { layer, env };
}

describe('Canvas 热力覆盖物', () => {
  it('SDK 没有自定义覆盖物基类时不挂载', () => {
    const env = fakeApi();
    expect(createDensityOverlay({ ...env.api, Overlay: undefined } as unknown as BaiduMapApi)).toBeNull();
  });

  it('实例继承 SDK 的覆盖物基类，画布挂进覆盖物容器且不接收指针事件', () => {
    const env = fakeApi();
    const layer = createDensityOverlay(env.api, { requestFrame, cancelFrame })!;
    expect(layer.overlay).toBeInstanceOf(FakeBase);
    layer.attach(env.map as unknown as BMapMap);
    const canvas = env.augmented.get(layer.overlay) as { style: Record<string, string>; attributes: Record<string, string> };
    expect(env.mapPane.children).toContain(canvas);
    expect(canvas.style.pointerEvents).toBe('none');
    expect(canvas.attributes['aria-hidden']).toBe('true');
    // 没有 overlayPane 时退到底图容器，热力仍在设施与交互层之下。
    expect(env.markerPane.children).toHaveLength(0);
  });

  it('有设施与圈面才绘制：离屏查色带后按 evenodd 裁剪成一张图', () => {
    const env = fakeApi();
    const { layer } = makeLayer(env);
    layer.setFacilities([{ id: 'a', lng: ORIGIN.lng, lat: ORIGIN.lat }]);
    layer.setBoundary(boundary);
    flush();
    expect(layer.stats().drawn).toBe(true);
    expect(layer.stats().max).toBeGreaterThan(0);
    expect(recorded).toContain('clip:evenodd');
    // 密度先在离屏缓冲查色带，再作为一整张图贴到画布上。
    expect(recorded.filter(call => call === 'putImageData:3')).toHaveLength(1);
    expect(recorded.filter(call => call === 'drawImage:5')).toHaveLength(1);
    expect(recorded).toContain('save:0');
    expect(recorded).toContain('restore:0');
    const canvas = env.augmented.get(layer.overlay) as { style: Record<string, string> };
    // 本替身的地图中心投到 (0,0)，不是视口中心。真实 SDK 也可能如此。
    expect(canvas.style.left).toBe('-400px');
    expect(canvas.style.top).toBe('-300px');
    expect(recordedMoves[0][0]).toBeCloseTo(340, 4);
    expect(recordedMoves[0][1]).toBeCloseTo(360, 4);
  });

  it('没有真实设施就不生成热力点', () => {
    const env = fakeApi();
    const { layer } = makeLayer(env);
    layer.setBoundary(boundary);
    flush();
    expect(layer.stats()).toMatchObject({ drawn: false, points: 0, max: 0 });
    expect(recorded.filter(call => call.startsWith('drawImage'))).toHaveLength(0);
  });

  it('孔洞与分量都进同一个 evenodd 路径：一孔两环，分量各一段', () => {
    const env = fakeApi();
    const { layer } = makeLayer(env);
    const outer = squareGeometry(60).coordinates[0];
    // 孔环走向与外环相反；奇偶规则与走向无关，这里也顺带确认实现没有依赖方向。
    const hole = squareGeometry(20).coordinates[0].slice().reverse();
    layer.setFacilities([{ lng: ORIGIN.lng, lat: ORIGIN.lat }]);
    layer.setBoundary({ type: 'MultiPolygon', coordinates: [[outer, hole], squareGeometry(30).coordinates] });
    flush();
    // 三个环 = 三段子路径，孔留在一起挖掉，分量之间没有连接。
    expect(recorded.filter(call => call === 'moveTo:2')).toHaveLength(3);
    expect(recorded.filter(call => call === 'closePath:0')).toHaveLength(3);
    expect(recorded.filter(call => call === 'clip:evenodd')).toHaveLength(1);
  });

  it('画布按设备像素比设置绘图尺寸，坐标仍是 CSS 像素', () => {
    const env = fakeApi();
    const { layer } = makeLayer(env);
    layer.setFacilities([{ lng: ORIGIN.lng, lat: ORIGIN.lat }]);
    layer.setBoundary(boundary);
    flush();
    const canvas = env.augmented.get(layer.overlay) as { width: number; height: number; style: Record<string, string> };
    expect([canvas.width, canvas.height]).toEqual([1600, 1200]);
    expect([canvas.style.width, canvas.style.height]).toEqual(['800px', '600px']);
  });

  it('视角事件合并成一帧：连续移动只重绘一次', () => {
    const env = fakeApi();
    const { layer } = makeLayer(env);
    layer.setFacilities([{ lng: ORIGIN.lng, lat: ORIGIN.lat }]);
    layer.setBoundary(boundary);
    flush();
    const before = recorded.filter(call => call === 'clearRect:4').length;
    env.fire('moveend'); env.fire('moveend'); env.fire('zoomend');
    expect(frames).toHaveLength(1);
    flush();
    const after = recorded.filter(call => call === 'clearRect:4').length;
    expect(after - before).toBe(1);
  });

  it('卸载时取消待重绘、摘掉监听与画布', () => {
    const env = fakeApi();
    const { layer } = makeLayer(env);
    layer.setFacilities([{ lng: ORIGIN.lng, lat: ORIGIN.lat }]);
    layer.setBoundary(boundary);
    const canvas = env.augmented.get(layer.overlay) as { parentNode: unknown };
    const pendingFrames = frames.length;
    layer.destroy();
    expect(pendingFrames).toBeGreaterThan(0);
    expect(env.events.filter(event => event.startsWith('remove:'))).toEqual(
      ['remove:moveend', 'remove:zoomend', 'remove:resize']);
    expect(canvas.parentNode).toBeNull();
    expect(env.mapPane.children).toHaveLength(0);
    expect(env.map.overlays).toHaveLength(0);
    flush();
    expect(layer.stats().drawn).toBe(false);
  });

  it('clearOverlays 摘走覆盖物后可以重新挂载并重建画布', () => {
    const env = fakeApi();
    const { layer } = makeLayer(env);
    layer.setFacilities([{ id: 'a', lng: ORIGIN.lng, lat: ORIGIN.lat }]);
    layer.setBoundary(boundary);
    flush();
    expect(layer.stats().drawn).toBe(true);
    env.map.clearOverlays();
    expect(env.map.overlays).toHaveLength(0);
    layer.attach(env.map as unknown as BMapMap);
    flush();
    expect(env.map.overlays).toHaveLength(1);
    expect(layer.stats().drawn).toBe(true);
    expect(env.mapPane.children).toHaveLength(1);
  });

  it('关掉再打开（detach → attach）重建画布：SDK 缓存的 domElement 不会挡住 initialize', () => {
    const env = fakeApi();
    const { layer } = makeLayer(env);
    layer.setFacilities([{ id: 'a', lng: ORIGIN.lng, lat: ORIGIN.lat }]);
    layer.setBoundary(boundary);
    flush();
    const first = env.augmented.get(layer.overlay);
    for (let round = 0; round < 3; round += 1) {
      layer.detach();
      expect(env.mapPane.children).toHaveLength(0);
      expect(layer.stats().drawn).toBe(false);
      layer.attach(env.map as unknown as BMapMap);
      flush();
      expect(env.mapPane.children).toHaveLength(1);
      expect(layer.stats().drawn).toBe(true);
    }
    expect(env.augmented.get(layer.overlay)).not.toBe(first);
    // 监听不随开关次数叠加：每种视角事件仍只有一个处理器。
    expect(['moveend', 'zoomend', 'resize'].map(type => env.listeners.get(type)?.size)).toEqual([1, 1, 1]);
  });

  it('画布被移出容器时自行补回', () => {
    const env = fakeApi();
    const { layer } = makeLayer(env);
    layer.setFacilities([{ lng: ORIGIN.lng, lat: ORIGIN.lat }]);
    layer.setBoundary(boundary);
    flush();
    const canvas = env.augmented.get(layer.overlay) as { parentNode: unknown };
    env.mapPane.remove(canvas);
    expect(canvas.parentNode).toBeNull();
    layer.redraw();
    flush();
    expect(env.mapPane.children).toContain(canvas);
  });

  it('destroy 之后不再重绘', () => {
    const env = fakeApi();
    const { layer } = makeLayer(env);
    layer.destroy();
    layer.setFacilities([{ lng: ORIGIN.lng, lat: ORIGIN.lat }]);
    layer.setBoundary(boundary);
    flush();
    expect(frames).toHaveLength(0);
    expect(layer.stats().drawn).toBe(false);
  });

  it('取景框为空或几何损坏时跳过本帧，不抛给地图', () => {
    const env = fakeApi();
    const layer = createDensityOverlay(env.api, { requestFrame, cancelFrame,
      viewport: () => ({ width: 0, height: 0 }) })!;
    layer.attach(env.map as unknown as BMapMap);
    layer.setFacilities([{ lng: ORIGIN.lng, lat: ORIGIN.lat }]);
    layer.setBoundary(boundary);
    expect(() => flush()).not.toThrow();
    expect(layer.stats().drawn).toBe(false);

    const broken = fakeApi();
    const second = createDensityOverlay(broken.api, { requestFrame, cancelFrame,
      viewport: () => ({ width: 800, height: 600 }) })!;
    second.attach(broken.map as unknown as BMapMap);
    second.setFacilities([{ lng: ORIGIN.lng, lat: ORIGIN.lat }]);
    second.setBoundary({ type: 'Polygon', coordinates: 'not coordinates' });
    expect(() => flush()).not.toThrow();
    expect(second.stats().drawn).toBe(false);
  });
});

describe('真实 SDK 事件名', () => {
  it('订阅的是真实移动、缩放结束与 resize，而不是模拟事件', () => {
    const env = fakeApi();
    makeLayer(env);
    expect([...env.listeners.keys()].filter(key => key !== 'click')).toEqual(['moveend', 'zoomend', 'resize']);
  });

  it('没有 requestAnimationFrame 时同步重绘一次，不会漏帧', () => {
    const env = fakeApi();
    const layer = createDensityOverlay(env.api, { devicePixelRatio: 1,
      viewport: () => ({ width: 800, height: 600 }) })!;
    expect(typeof globalThis.requestAnimationFrame).toBe('undefined');
    layer.attach(env.map as unknown as BMapMap);
    layer.setFacilities([{ lng: ORIGIN.lng, lat: ORIGIN.lat }]);
    layer.setBoundary(boundary);
    // 没有 rAF 时同步渲染：挂上数据的那一刻就是已绘制状态，也不留悬挂的帧。
    expect(layer.stats().drawn).toBe(true);
    layer.redraw();
    expect(layer.stats().drawn).toBe(true);
    expect(frames).toHaveLength(0);
  });
});
