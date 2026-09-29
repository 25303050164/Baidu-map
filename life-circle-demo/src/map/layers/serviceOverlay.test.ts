/** 服务覆盖热力覆盖物：与设施密度共用骨架，这里只验它自己的数据生命周期。
 *
 * 骨架（挂载、合并重绘、摘除重挂）已由 heatmapOverlay.test.ts 覆盖；这里确认服务层
 * 画的是评估格而不是设施、换类别会重绘、没有圈面或没有格就不画，以及两层画布可区分。
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { createServiceOverlay } from './serviceOverlay';
import { SERVICE_COMPOSITE, type ServiceSample } from './serviceField';
import type { BaiduMapApi, BMapMap, BMapOverlayInstance, BMapPanes } from '../baiduMapTypes';

/** 替身投影：与 heatmapOverlay.test.ts 同一个等距近似，1 米 ≈ 1 像素，原点是地图中心。 */
const ORIGIN = { lng: 121.513925, lat: 31.313079 };
const M_PER_DEGREE = 111_320;
const COS = Math.cos((ORIGIN.lat * Math.PI) / 180);
const project = (point: { lng: number; lat: number }) => ({
  x: (point.lng - ORIGIN.lng) * M_PER_DEGREE * COS, y: (ORIGIN.lat - point.lat) * M_PER_DEGREE });
const toLng = (meters: number) => ORIGIN.lng + meters / (M_PER_DEGREE * COS);
const toLat = (meters: number) => ORIGIN.lat + meters / M_PER_DEGREE;

let calls: string[] = [];
let painted: Uint8ClampedArray[] = [];

function fakeCanvas() {
  const context = {
    setTransform() {}, clearRect() { calls.push('clear'); }, save() {}, restore() {}, beginPath() {},
    moveTo() {}, lineTo() {}, closePath() {}, clip() {},
    drawImage() { calls.push('draw'); },
    createImageData: (width: number, height: number) => ({ width, height, data: new Uint8ClampedArray(width * height * 4) }),
    putImageData(image: { data: Uint8ClampedArray }) { painted.push(image.data); },
    imageSmoothingEnabled: true,
  };
  const canvas = {
    style: {} as Record<string, string>, width: 0, height: 0, parentNode: null as unknown,
    attributes: {} as Record<string, string>,
    getContext: () => context,
    setAttribute(name: string, value: string) { canvas.attributes[name] = value; },
  };
  return canvas;
}

function fakeEnv() {
  const children: { parentNode?: unknown }[] = [];
  const pane = {
    clientWidth: 800, clientHeight: 600, children,
    get firstChild() { return children[0] ?? null; },
    appendChild(node: { parentNode?: unknown }) { children.push(node); node.parentNode = pane; return node; },
    insertBefore(node: { parentNode?: unknown }) { children.unshift(node); node.parentNode = pane; return node; },
    removeChild(node: { parentNode?: unknown }) { children.splice(children.indexOf(node), 1); node.parentNode = null; },
  };
  const panes: BMapPanes = { markerPane: pane as unknown as HTMLElement };
  const map = {
    overlays: [] as BMapOverlayInstance[],
    getCenter: () => ({ ...ORIGIN }),
    // 与真实 BMapGL 的 _i 一致：已有 domElement 就不再 initialize。
    addOverlay(overlay: BMapOverlayInstance) {
      map.overlays.push(overlay);
      const cached = overlay as BMapOverlayInstance & { domElement?: unknown };
      if (!cached.domElement) cached.domElement = overlay.initialize?.(map as unknown as BMapMap);
    },
    removeOverlay(overlay: BMapOverlayInstance) { map.overlays = map.overlays.filter(o => o !== overlay); overlay.remove?.(); },
    addEventListener() {}, removeEventListener() {},
    pointToOverlayPixel: project,
    getPanes: () => panes,
  };
  const api = {
    Point: class { constructor(public lng: number, public lat: number) {} },
    Overlay: class {},
  } as unknown as BaiduMapApi;
  return { api, map, pane };
}

const square = (half: number) => ({ type: 'Polygon', coordinates: [[[toLng(-half), toLat(-half)],
  [toLng(half), toLat(-half)], [toLng(half), toLat(half)], [toLng(-half), toLat(half)], [toLng(-half), toLat(-half)]]] });

function cells(category: string, status: ServiceSample['status']): ServiceSample[] {
  const out: ServiceSample[] = [];
  for (let x = -100; x < 100; x += 50) for (let y = -100; y < 100; y += 50) {
    out.push({ lng: toLng(x + 25), lat: toLat(y + 25), category, status, distanceM: 200, sizeM: 50 });
  }
  return out;
}

beforeEach(() => {
  calls = [];
  painted = [];
  (globalThis as unknown as { document: unknown }).document = { createElement: () => fakeCanvas() };
});

function makeLayer() {
  const env = fakeEnv();
  const layer = createServiceOverlay(env.api, { categories: ['shopping', 'medical', 'education'],
    devicePixelRatio: 1, viewport: () => ({ width: 800, height: 600 }) })!;
  layer.attach(env.map as unknown as BMapMap);
  return { env, layer };
}

describe('服务覆盖热力覆盖物', () => {
  it('SDK 没有自定义覆盖物基类时不挂载', () => {
    const env = fakeEnv();
    expect(createServiceOverlay({ ...env.api, Overlay: undefined } as unknown as BaiduMapApi,
      { categories: [] })).toBeNull();
  });

  it('画布与设施密度画布可区分，并带业务热力的共同标记', () => {
    const { env } = makeLayer();
    const canvas = env.pane.children[0] as unknown as { attributes: Record<string, string>; style: Record<string, string> };
    expect(canvas.attributes['data-testid']).toBe('service-heat-canvas');
    expect(canvas.attributes['data-map-layer']).toBe('heat');
    expect(canvas.style.pointerEvents).toBe('none');
  });

  it('关掉再打开后画布重建并重画，不因 SDK 缓存的 domElement 而空白', () => {
    const { env, layer } = makeLayer();
    layer.setMode('shopping');
    layer.setBoundary(square(300));
    layer.setSamples(cells('shopping', 'covered'), null);
    expect(layer.stats().drawn).toBe(true);
    layer.detach();
    expect(env.pane.children).toHaveLength(0);
    layer.attach(env.map as unknown as BMapMap);
    expect(env.pane.children).toHaveLength(1);
    expect(layer.stats()).toMatchObject({ drawn: true, mode: 'shopping', samples: 16 });
  });

  it('有评估格与圈面才绘制；没有圈面或没有格时什么也不画', () => {
    const { layer } = makeLayer();
    expect(layer.stats().mode).toBe(SERVICE_COMPOSITE);
    layer.setMode('shopping');
    layer.setSamples(cells('shopping', 'covered'), null);
    expect(layer.stats()).toMatchObject({ drawn: false, samples: 0 });
    layer.setBoundary(square(300));
    expect(layer.stats().drawn).toBe(true);
    expect(layer.stats().samples).toBe(16);
    expect(layer.stats().painted).toBeGreaterThan(0);
    layer.setSamples([], null);
    expect(layer.stats().drawn).toBe(false);
  });

  it('换类别会重绘；这一类没有格时不画，综合模式缺类即未知', () => {
    const { layer } = makeLayer();
    layer.setMode('shopping');
    layer.setBoundary(square(300));
    layer.setSamples(cells('medical', 'covered'), null);
    expect(layer.stats().drawn).toBe(false);
    layer.setMode('medical');
    expect(layer.stats()).toMatchObject({ drawn: true, mode: 'medical', samples: 16 });
    layer.setMode(SERVICE_COMPOSITE);
    // 只有医疗一类：综合模式下这里只能是未知（淡紫），不是"医疗覆盖了就算覆盖"。
    expect(layer.stats().drawn).toBe(true);
    const last = painted[painted.length - 1];
    const lit = [...Array(last.length / 4).keys()].find(index => last[index * 4 + 3] > 0)!;
    expect([last[lit * 4], last[lit * 4 + 1], last[lit * 4 + 2]]).toEqual([167, 139, 250]);
  });

  it('评估域内没有格的地方画成未知：只有域、没有这一类的格时也会着色', () => {
    const { layer } = makeLayer();
    layer.setMode('shopping');
    layer.setBoundary(square(300));
    layer.setSamples(cells('medical', 'covered'), square(200));
    expect(layer.stats().drawn).toBe(true);
    const last = painted[painted.length - 1];
    const lit = [...Array(last.length / 4).keys()].filter(index => last[index * 4 + 3] > 0);
    expect(lit.length).toBeGreaterThan(0);
    for (const index of lit.slice(0, 20)) {
      expect([last[index * 4], last[index * 4 + 1], last[index * 4 + 2]]).toEqual([167, 139, 250]);
    }
  });

  it('评估域损坏时退回只画有格的地方，不抛给地图', () => {
    const { layer } = makeLayer();
    layer.setMode('shopping');
    layer.setBoundary(square(300));
    expect(() => layer.setSamples(cells('shopping', 'gap'), { type: 'Polygon', coordinates: 'broken' })).not.toThrow();
    expect(layer.stats().drawn).toBe(true);
  });

  it('destroy 之后清空数据、摘掉画布，不再绘制', () => {
    const { env, layer } = makeLayer();
    layer.setBoundary(square(300));
    layer.setSamples(cells('shopping', 'covered'), null);
    layer.destroy();
    expect(env.pane.children).toHaveLength(0);
    expect(env.map.overlays).toHaveLength(0);
    const before = calls.length;
    layer.setSamples(cells('shopping', 'covered'), null);
    expect(calls.length).toBe(before);
    expect(layer.stats().drawn).toBe(false);
  });
});
