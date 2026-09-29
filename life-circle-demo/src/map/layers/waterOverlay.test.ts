/** 水系标注覆盖物：画布叠放次序、斜线只给冲突与底图误绘、标字的门槛。
 *
 * 骨架的挂载与重挂已由 heatmapOverlay.test.ts 覆盖；这里要守住的是"读得出来"：
 * 标注画布必须压在热力之上（否则冲突区被覆盖色盖住，读者看到的还是"已覆盖"），
 * 且必须在标记之下（桥梁、设施点仍能点）。
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { createWaterOverlay, LABEL_MIN_EXTENT_PX, type WaterAnnotation } from './waterOverlay';
import { createServiceOverlay } from './serviceOverlay';
import type { BaiduMapApi, BMapMap, BMapOverlayInstance, BMapPanes } from '../baiduMapTypes';

/** 替身投影：1 米 ≈ 1 像素，原点是地图中心（与 serviceOverlay.test.ts 相同）。 */
const ORIGIN = { lng: 121.513925, lat: 31.313079 };
const M_PER_DEGREE = 111_320;
const COS = Math.cos((ORIGIN.lat * Math.PI) / 180);
const project = (point: { lng: number; lat: number }) => ({
  x: (point.lng - ORIGIN.lng) * M_PER_DEGREE * COS, y: (ORIGIN.lat - point.lat) * M_PER_DEGREE });
const toLng = (meters: number) => ORIGIN.lng + meters / (M_PER_DEGREE * COS);
const toLat = (meters: number) => ORIGIN.lat + meters / M_PER_DEGREE;

let calls: string[] = [];

type FakeNode = {
  parentNode?: FakePane | null;
  attributes: Record<string, string>;
  getAttribute(name: string): string | null;
  readonly nextSibling: FakeNode | null;
};
type FakePane = ReturnType<typeof fakePane>;

function node(attributes: Record<string, string> = {}): FakeNode {
  const self: FakeNode = {
    parentNode: null, attributes,
    getAttribute: name => self.attributes[name] ?? null,
    get nextSibling() {
      const siblings = self.parentNode?.children ?? [];
      return siblings[siblings.indexOf(self) + 1] ?? null;
    },
  };
  return self;
}

function fakeCanvas() {
  const context: Record<string, unknown> = {
    strokeStyle: '', fillStyle: '', lineWidth: 1, font: '', textAlign: '', textBaseline: '', lineJoin: '',
    setTransform() {}, clearRect() { calls.push('clear'); }, save() {}, restore() {}, beginPath() {},
    moveTo() {}, lineTo() {}, closePath() {}, setLineDash() {}, drawImage() { calls.push('draw'); },
    clip() { calls.push('clip'); },
    fill() { calls.push(`fill:${context.fillStyle}`); },
    stroke() { calls.push(`stroke:${context.strokeStyle}`); },
    strokeText() {},
    fillText(text: string) { calls.push(`text:${text}`); },
    createImageData: (width: number, height: number) => ({ width, height, data: new Uint8ClampedArray(width * height * 4) }),
    putImageData() {}, imageSmoothingEnabled: true,
  };
  const canvas = Object.assign(node(), {
    style: {} as Record<string, string>, width: 0, height: 0,
    getContext: () => context,
    setAttribute(name: string, value: string) { canvas.attributes[name] = value; },
  });
  return canvas;
}

function fakePane() {
  const children: FakeNode[] = [];
  const take = (item: FakeNode) => {
    const index = children.indexOf(item);
    if (index >= 0) children.splice(index, 1);
  };
  const pane = {
    clientWidth: 800, clientHeight: 600, children,
    get firstChild() { return children[0] ?? null; },
    appendChild(item: FakeNode) { take(item); children.push(item); item.parentNode = pane; return item; },
    insertBefore(item: FakeNode, before: FakeNode | null) {
      take(item);
      const index = before ? children.indexOf(before) : -1;
      if (index < 0) children.push(item); else children.splice(index, 0, item);
      item.parentNode = pane;
      return item;
    },
    removeChild(item: FakeNode) { take(item); item.parentNode = null; },
  };
  return pane;
}

function fakeEnv() {
  const pane = fakePane();
  const panes: BMapPanes = { markerPane: pane as unknown as HTMLElement };
  const map = {
    overlays: [] as BMapOverlayInstance[],
    getCenter: () => ({ ...ORIGIN }),
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

/** 以 (cx, cy) 米为中心、半边长 half 米的方块。 */
const square = (half: number, cx = 0, cy = 0) => ({ type: 'Polygon', coordinates: [[
  [toLng(cx - half), toLat(cy - half)], [toLng(cx + half), toLat(cy - half)],
  [toLng(cx + half), toLat(cy + half)], [toLng(cx - half), toLat(cy + half)],
  [toLng(cx - half), toLat(cy - half)]]] });

const STYLES = {
  reach: { stroke: '#1d4ed8', fill: 'rgba(37, 99, 235, 0.28)', hatch: null, dash: [] },
  conflict: { stroke: '#c2410c', fill: 'rgba(194, 65, 12, 0.14)', hatch: '#c2410c', dash: [5, 3] },
  misdrawn: { stroke: '#334155', fill: 'rgba(255, 255, 255, 0.30)', hatch: '#334155', dash: [3, 3] },
};

beforeEach(() => {
  calls = [];
  (globalThis as unknown as { document: unknown }).document = { createElement: () => fakeCanvas() };
});

function makeLayer() {
  const env = fakeEnv();
  const layer = createWaterOverlay(env.api, { devicePixelRatio: 1, viewport: () => ({ width: 800, height: 600 }) })!;
  return { env, layer };
}

const layerOf = (item: FakeNode) => item.getAttribute('data-map-layer') ?? 'marker';

describe('水系标注覆盖物', () => {
  it('SDK 没有自定义覆盖物基类时不挂载', () => {
    const env = fakeEnv();
    expect(createWaterOverlay({ ...env.api, Overlay: undefined } as unknown as BaiduMapApi, {})).toBeNull();
  });

  it('画布压在热力之上、标记之下；热力后开也插在标注之下', () => {
    const { env, layer } = makeLayer();
    env.pane.appendChild(node());
    layer.setAnnotations([{ key: 'c', geometry: square(100), style: STYLES.conflict }]);
    layer.attach(env.map as unknown as BMapMap);
    const canvas = env.pane.children.find(item => item.getAttribute('data-testid') === 'water-annotation-canvas')!;
    expect(canvas.getAttribute('data-map-layer')).toBe('water');
    expect(canvas.getAttribute('data-map-stack')).toBe('above');
    expect(env.pane.children.map(layerOf)).toEqual(['water', 'marker']);

    const heat = createServiceOverlay(env.api, { categories: ['shopping'], devicePixelRatio: 1,
      viewport: () => ({ width: 800, height: 600 }) })!;
    heat.attach(env.map as unknown as BMapMap);
    expect(env.pane.children.map(layerOf)).toEqual(['heat', 'water', 'marker']);

    // 标注关掉再打开：仍回到热力之上。
    layer.detach();
    expect(env.pane.children.map(layerOf)).toEqual(['heat', 'marker']);
    layer.attach(env.map as unknown as BMapMap);
    expect(env.pane.children.map(layerOf)).toEqual(['heat', 'water', 'marker']);
  });

  it('冲突与底图误绘加斜线（裁剪后描线），已核实河道只填色描边', () => {
    const { env, layer } = makeLayer();
    layer.attach(env.map as unknown as BMapMap);
    layer.setAnnotations([{ key: 'r', geometry: square(40), style: STYLES.reach }]);
    expect(layer.stats()).toMatchObject({ drawn: true, shapes: 1, visible: 1 });
    expect(calls).not.toContain('clip');
    expect(calls).toContain('fill:rgba(37, 99, 235, 0.28)');
    calls = [];
    layer.setAnnotations([
      { key: 'c', geometry: square(40), style: STYLES.conflict },
      { key: 'm', geometry: square(40, 150), style: STYLES.misdrawn },
    ]);
    expect(calls.filter(call => call === 'clip')).toHaveLength(2);
    // 每个带斜线的面：一次斜线描线 + 一次边线描线，颜色都是它自己的。
    expect(calls.filter(call => call === 'stroke:#c2410c')).toHaveLength(2);
    expect(calls.filter(call => call === 'stroke:#334155')).toHaveLength(2);
  });

  it('只在面放得下时标字，锚点在视口外不标', () => {
    const { env, layer } = makeLayer();
    layer.attach(env.map as unknown as BMapMap);
    const labelled = (half: number, at = { lng: ORIGIN.lng, lat: ORIGIN.lat }): WaterAnnotation => ({
      key: `c${half}`, geometry: square(half), style: STYLES.conflict,
      label: { text: '数据冲突／未知', ...at } });
    layer.setAnnotations([labelled(LABEL_MIN_EXTENT_PX)]);
    expect(layer.stats().labels).toBe(1);
    expect(calls).toContain('text:数据冲突／未知');
    layer.setAnnotations([labelled(LABEL_MIN_EXTENT_PX / 2 - 5)]);
    expect(layer.stats()).toMatchObject({ visible: 1, labels: 0 });
    layer.setAnnotations([labelled(2000, { lng: toLng(900), lat: ORIGIN.lat })]);
    expect(layer.stats()).toMatchObject({ visible: 1, labels: 0 });
  });

  it('视口外的面不计可见；坏几何只丢它自己', () => {
    const { env, layer } = makeLayer();
    layer.attach(env.map as unknown as BMapMap);
    layer.setAnnotations([
      { key: 'far', geometry: square(20, 5000), style: STYLES.reach },
      { key: 'bad', geometry: { type: 'Polygon', coordinates: [[['x', 1]]] }, style: STYLES.conflict },
      { key: 'ok', geometry: square(20), style: STYLES.misdrawn },
    ]);
    expect(layer.stats()).toMatchObject({ drawn: true, shapes: 3, visible: 1 });
    layer.setAnnotations([{ key: 'far', geometry: square(20, 5000), style: STYLES.reach }]);
    expect(layer.stats()).toMatchObject({ drawn: false, visible: 0 });
  });

  it('destroy 之后摘掉画布、清空标注，不再绘制', () => {
    const { env, layer } = makeLayer();
    layer.attach(env.map as unknown as BMapMap);
    layer.setAnnotations([{ key: 'c', geometry: square(40), style: STYLES.conflict }]);
    layer.destroy();
    expect(env.pane.children).toHaveLength(0);
    expect(env.map.overlays).toHaveLength(0);
    const before = calls.length;
    layer.setAnnotations([{ key: 'c', geometry: square(40), style: STYLES.conflict }]);
    expect(calls.length).toBe(before);
    expect(layer.stats()).toMatchObject({ drawn: false, shapes: 0 });
  });
});
