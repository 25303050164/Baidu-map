/**
 * 水系复核的真实底图验收：国定一社区虬江（guoding1-qiujiang@2026-09-28.1）。
 *
 * 这一套回答"读者会不会把底图的水面当成计算结果"：
 *
 * - 计算用的水系要画出来、并且和底图的水面分得开：已核实河道与补录水体（蓝）按 OSM 位置与
 *   实测宽度成面；底图画错的水面（灰斜线，实为陆地）与来源冲突未裁决处（橙斜线，数据冲突／未知）
 *   单独标出。标注画布压在热力之上、标记之下 —— 冲突处读者看到的是"未知"，不是"已覆盖"。
 * - 期望值不从页面里取：面、锚点与桥梁取后端这一版水系证据的原文，判读窗口在地面米上挑
 *   （离各面边界够远、避开标字），颜色取 water.ts 的样式定义（颜色的定义本身，不借绘制代码）；
 *   读数用地图自己的 `pointToOverlayPixel` 换算。窗口里的每个像素都得归到期望的那一类，
 *   冲突与底图误绘还得读到斜线（不透明像素），已核实水体不得有斜线。
 * - 多个河段、池塘、桥梁与缩放级别：沿河五处、两处冲突面、底图误绘的河段与场地、九座桥、
 *   15–19 级。
 * - 版本：离线重算的第 7 版写明来源与复核版本；早于复核的第 5 版被认成旧版本、不画水系标注，
 *   与第 7 版来回切换不串。
 * - 不花服务额度：页面凭 localStorage 里的任务标识恢复；创建、取消、路线核验请求一律挡掉并记账。
 *
 * 输出：截图与 `summary.json`（不含任何 URL 与 AK）写到 `WATER_OUTPUT_DIR`。
 */
import { test, expect, type APIRequestContext, type Page } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { WATER_STYLES } from '../src/checkup/water';
import { SERVICE_ALPHA, SERVICE_UNKNOWN_RGB } from '../src/map/layers/serviceField';
import { LABEL_MIN_EXTENT_PX } from '../src/map/layers/waterOverlay';

const API = process.env.WATER_API ?? 'http://127.0.0.1:8019';
const OUTPUT = resolve(process.env.WATER_OUTPUT_DIR ?? 'output/water-acceptance');
const redact = (value: string) => value.replace(/([?&](?:ak|key|token)=)[^&\s]+/gi, '$1[REDACTED]');

const REVIEW = 'guoding1-qiujiang@2026-09-28.1';
const REVIEW_TITLE = '国定一社区 · 虬江水系复核';
const OSM_VERSION = 'geofabrik-shanghai-20260912';

type Slot = 'e82' | 'hybrid';
type Task = { engine: string; label: string; taskId: string; clientRequestId: string; revision: number };
/**
 * 国定一社区，2026-09-27 两条真实体检（各 428 次百度调用）按水系复核离线重算出的第 7 版，
 * 与一条没有重算、仍停在第 5 版的同址体检（旧版本的样本）。存档在后端的 CHECKUP_DIR。
 */
const TASKS: Record<Slot | 'old', Task> = {
  e82: { engine: 'baidu_e82', label: '百度边界搜索（E8.2）', revision: 7,
    taskId: 'f6128318-828e-48c0-bdc5-f164c6ece0db', clientRequestId: 'd33ffe23-a880-43f6-b6fa-725a95d49274' },
  hybrid: { engine: 'osm_hybrid', label: 'OSM＋百度', revision: 7,
    taskId: '5e710014-84f5-42e7-ae77-525bd429ae8a', clientRequestId: '4a1d467f-a3ba-4174-b232-9411dcaae066' },
  old: { engine: 'baidu_e82', label: '百度边界搜索（E8.2）早于复核', revision: 5,
    taskId: '090ecd78-d58c-45cc-aea6-d292ba789a41', clientRequestId: '63fc3173-163b-4360-a96f-5adcb1cef3e4' },
};
const SLOTS: Slot[] = ['e82', 'hybrid'];
const CENTER = { lng: 121.513925, lat: 31.313079 };

// ---------------------------------------------------------------- 地理小工具（地面米）

type LngLat = { lng: number; lat: number };
type Polygons = number[][][][];

const EARTH_R = 6_371_008.8;
const rad = (deg: number) => (deg * Math.PI) / 180;
function meters(a: LngLat, b: LngLat): number {
  const dLat = rad(b.lat - a.lat), dLng = rad(b.lng - a.lng);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dLng / 2) ** 2;
  return 2 * EARTH_R * Math.asin(Math.min(1, Math.sqrt(h)));
}
const M_PER_DEG_LAT = (Math.PI * EARTH_R) / 180;
const mPerDegLng = (lat: number) => M_PER_DEG_LAT * Math.cos(rad(lat));
const offset = (p: LngLat, east: number, north: number): LngLat =>
  ({ lng: p.lng + east / mPerDegLng(p.lat), lat: p.lat + north / M_PER_DEG_LAT });
const round6 = (p: LngLat) => ({ lng: Number(p.lng.toFixed(6)), lat: Number(p.lat.toFixed(6)) });

function polygonsOf(geometry: unknown): Polygons {
  const value = geometry as { type?: string; coordinates?: unknown } | null | undefined;
  if (!value) return [];
  if (value.type === 'Polygon') return [value.coordinates as number[][][]];
  if (value.type === 'MultiPolygon') return value.coordinates as Polygons;
  return [];
}

function inRing(p: LngLat, ring: number[][]): boolean {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i], [xj, yj] = ring[j];
    if ((yi > p.lat) !== (yj > p.lat) && p.lng < ((xj - xi) * (p.lat - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

const inside = (p: LngLat, polygons: Polygons) =>
  polygons.some(([outer, ...holes]) => inRing(p, outer) && !holes.some(hole => inRing(p, hole)));

function edgeDistance(p: LngLat, polygons: Polygons): number {
  const kx = mPerDegLng(p.lat), ky = M_PER_DEG_LAT;
  let best = Infinity;
  for (const polygon of polygons) for (const ring of polygon) {
    for (let i = 1; i < ring.length; i++) {
      const ax = (ring[i - 1][0] - p.lng) * kx, ay = (ring[i - 1][1] - p.lat) * ky;
      const bx = (ring[i][0] - p.lng) * kx, by = (ring[i][1] - p.lat) * ky;
      const dx = bx - ax, dy = by - ay, len = dx * dx + dy * dy;
      const t = len > 0 ? Math.max(0, Math.min(1, -(ax * dx + ay * dy) / len)) : 0;
      best = Math.min(best, Math.hypot(ax + t * dx, ay + t * dy));
    }
  }
  return best;
}

function bboxOf(polygons: Polygons): [number, number, number, number] {
  const all = polygons.flatMap(polygon => polygon[0]);
  return [Math.min(...all.map(v => v[0])), Math.min(...all.map(v => v[1])),
    Math.max(...all.map(v => v[0])), Math.max(...all.map(v => v[1]))];
}

// ---------------------------------------------------------------- 后端原文

type Kind = 'reach' | 'supplement' | 'misdrawn' | 'conflict';
type Shape = { kind: Kind; id: string; polygons: Polygons; anchor: LngLat | null; areaM2: number | null;
  note: string };
type Crossing = { osmId: number; river: number; highway: string; lengthM: number; anchor: LngLat };
type Cell = LngLat & { category: string; status: string; reason: string | null; sizeM: number };
type Evidence = { revision: number; snapshot: Record<string, unknown>; water: Record<string, unknown>;
  extent: Polygons; shapes: Shape[]; crossings: Crossing[]; reachOsmId: number; cells: Cell[];
  iso: Polygons; domain: Polygons };

const KIND_OF: Record<string, Kind> = { reaches: 'reach', supplements: 'supplement',
  basemapMisdrawn: 'misdrawn', conflicts: 'conflict' };

async function getJson(request: APIRequestContext, path: string): Promise<Record<string, unknown>> {
  for (let attempt = 0; ; attempt++) {
    try {
      const response = await request.get(`${API}${path}`, { headers: { connection: 'close' }, timeout: 60000 });
      expect(response.status()).toBe(200);
      return await response.json() as Record<string, unknown>;
    } catch (error) {
      if (attempt >= 3) throw error;
      await new Promise(ready => setTimeout(ready, 1500));
    }
  }
}

const evidences = new Map<Slot, Evidence>();
async function evidenceOf(request: APIRequestContext, slot: Slot): Promise<Evidence> {
  const cached = evidences.get(slot);
  if (cached) return cached;
  const task = TASKS[slot];
  const snapshot = await getJson(request, `/api/v2/checkups/${task.taskId}/result`);
  expect(snapshot.revision, `${task.label} 的最新修订`).toBe(task.revision);
  const water = snapshot.water as Record<string, unknown>;
  expect(water, '第 7 版带水系证据').toBeTruthy();
  const reviews = water.reviews as Record<string, unknown>[];
  expect(reviews.map(review => review.label)).toEqual([REVIEW]);
  const review = reviews[0];
  const shapes: Shape[] = [];
  for (const [key, kind] of Object.entries(KIND_OF)) {
    for (const item of (review[key] as Record<string, unknown>[] | undefined) ?? []) {
      const polygons = polygonsOf(item.geometry);
      if (!polygons.length) continue;
      shapes.push({ kind, id: String(item.id ?? item.osmId), polygons,
        anchor: (item.anchor as LngLat | null | undefined) ?? null,
        areaM2: typeof item.areaM2 === 'number' ? item.areaM2 : null, note: String(item.note ?? '') });
    }
  }
  const crossings = ((review.crossings as Record<string, unknown>[] | undefined) ?? [])
    .filter(item => item.anchor)
    .map(item => ({ osmId: Number(item.osmId), river: Number(item.river), highway: String(item.highway),
      lengthM: Number(item.lengthM), anchor: item.anchor as LngLat }));
  const reaches = (review.reaches as Record<string, unknown>[] | undefined) ?? [];
  const heat = await getJson(request, `/api/v2/checkups/${task.taskId}/layers/heatmap?revision=${task.revision}`);
  const iso = await getJson(request, `/api/v2/checkups/${task.taskId}/layers/isochrone?revision=${task.revision}`);
  const geometry = heat.geometry as { properties?: Record<string, unknown>;
    features?: { geometry: { type: string; coordinates: number[] }; properties: Record<string, unknown> }[] };
  const stepM = typeof geometry.properties?.stepM === 'number' ? geometry.properties.stepM : 50;
  const cells: Cell[] = (geometry.features ?? []).filter(f => f.geometry.type === 'Point').map(f => ({
    lng: f.geometry.coordinates[0], lat: f.geometry.coordinates[1], category: String(f.properties.category),
    status: String(f.properties.status), reason: typeof f.properties.reason === 'string' ? f.properties.reason : null,
    sizeM: stepM / 2 ** Number(String(f.properties.cell).split(':')[0]) }));
  const evidence: Evidence = { revision: Number(snapshot.revision), snapshot, water,
    extent: polygonsOf(review.extent), shapes, crossings, reachOsmId: Number(reaches[0]?.osmId), cells,
    iso: polygonsOf(iso.geometry), domain: polygonsOf(geometry.properties?.domain) };
  evidences.set(slot, evidence);
  return evidence;
}

const shapeOf = (evidence: Evidence, id: string) => {
  const shape = evidence.shapes.find(item => item.id === id);
  expect(shape, `水系证据里有 ${id}`).toBeTruthy();
  return shape!;
};

/** 评估格（正方形）是否盖住某点：格心在 bd09 上按地面米展开。 */
const cellCovers = (cell: Cell, p: LngLat) => {
  const dx = (p.lng - cell.lng) * mPerDegLng(cell.lat), dy = (p.lat - cell.lat) * M_PER_DEG_LAT;
  return Math.abs(dx) <= cell.sizeM / 2 && Math.abs(dy) <= cell.sizeM / 2;
};

// ---------------------------------------------------------------- 颜色的定义

const hex = (value: string) => [1, 3, 5].map(i => parseInt(value.slice(i, i + 2), 16));
const rgbOf = (value: string | null) => (value?.match(/[\d.]+/g) ?? []).slice(0, 3).map(Number);
type PixelClass = 'clear' | 'conflict' | 'blue' | 'misdrawn' | 'extent' | 'other';
/**
 * 每一类是"描边／斜线色"与"填充色"之间的一段：两者叠在一起、抗锯齿混出来的颜色都在这段上
 * （还原预乘 alpha 后）。已核实河道与补录水体同一套蓝；冲突的填充与斜线同色。
 */
const PALETTE: { name: PixelClass; a: number[]; b: number[] }[] = [
  { name: 'conflict', a: hex(WATER_STYLES.conflict.stroke), b: rgbOf(WATER_STYLES.conflict.fill) },
  { name: 'blue', a: hex(WATER_STYLES.reach.stroke), b: rgbOf(WATER_STYLES.reach.fill) },
  { name: 'misdrawn', a: hex(WATER_STYLES.misdrawn.stroke), b: rgbOf(WATER_STYLES.misdrawn.fill) },
  { name: 'extent', a: hex(WATER_STYLES.extent.stroke), b: hex(WATER_STYLES.extent.stroke) },
];
const EXPECTED_CLASS: Record<Kind, PixelClass> = { reach: 'blue', supplement: 'blue', misdrawn: 'misdrawn',
  conflict: 'conflict' };
/** 斜线像素的不透明度下限；填充最浓的是蓝（0.28 → 71），差得很开。 */
const HATCH_ALPHA = 160;
/** 判读窗口的半边长（CSS 像素）：7×7 的窗口里一定压着一条斜线（间距 7 px）。 */
const WINDOW_PX = 3;
/** 标字的遮挡范围（CSS 像素，以锚点为中心）：最长的一条"底图水面有误·实为陆地"约 130 px 宽。 */
const LABEL_BOX = { dx: 80, dy: 14 };

// ---------------------------------------------------------------- 页面侧探针

type WindowRead = { x: number; y: number; total: number; counts: Record<string, number>; maxAlpha: number;
  whiteOpaque: number; pixel: number[] | null } | { x: number; y: number; total: 0 };
type ReadResult = { mpp: number; width: number; height: number; values: WindowRead[] } | null;

/** 与热力验收同一个钩子（登记地图实例）；读像素时按调色板归类，只把计数带回来。 */
function installProbe() {
  const maps: unknown[] = [];
  let ready: (() => void) | undefined;
  type AnyMap = { getContainer?: () => HTMLElement; pointToOverlayPixel: (p: unknown) => { x: number; y: number };
    getCenter: () => { lng: number; lat: number } };
  type Palette = { name: string; a: number[]; b: number[] }[];
  const w = window as unknown as Record<string, unknown> & { BMapGL?: { Map?: { prototype: Record<string, unknown> };
    Point: new (lng: number, lat: number) => unknown } };
  const hook = () => {
    const proto = w.BMapGL?.Map?.prototype;
    if (!proto || proto.__heatHooked) return;
    Object.defineProperty(proto, '__heatHooked', { value: true });
    for (const name of ['centerAndZoom', 'addOverlay', 'addEventListener', 'getCenter', 'panTo']) {
      const original = proto[name];
      if (typeof original !== 'function') continue;
      proto[name] = function (this: unknown, ...args: unknown[]) {
        if (!maps.includes(this)) maps.push(this);
        return (original as (...a: unknown[]) => unknown).apply(this, args);
      };
    }
  };
  Object.defineProperty(window, '__baiduMapReady__', { configurable: true,
    get: () => (ready ? () => { hook(); ready?.(); } : undefined),
    set: (fn: () => void) => { ready = fn; } });
  w.__heatMap = () => {
    for (let i = maps.length - 1; i >= 0; i--) {
      const container = (maps[i] as AnyMap).getContainer?.();
      if (container && container.isConnected) return maps[i];
    }
    return null;
  };
  const classify = (palette: Palette, r: number, g: number, b: number, a: number) => {
    if (a === 0) return 'clear';
    const tol = 8 + Math.ceil(128 / a);
    for (const c of palette) {
      const d = [c.b[0] - c.a[0], c.b[1] - c.a[1], c.b[2] - c.a[2]];
      const len = d[0] * d[0] + d[1] * d[1] + d[2] * d[2];
      const t = len > 0 ? Math.max(0, Math.min(1, ((r - c.a[0]) * d[0] + (g - c.a[1]) * d[1] + (b - c.a[2]) * d[2]) / len)) : 0;
      if (Math.max(Math.abs(r - c.a[0] - t * d[0]), Math.abs(g - c.a[1] - t * d[1]), Math.abs(b - c.a[2] - t * d[2])) <= tol) {
        return c.name;
      }
    }
    return 'other';
  };
  const tally = (palette: Palette, data: Uint8ClampedArray) => {
    const counts: Record<string, number> = {};
    let maxAlpha = 0, whiteOpaque = 0;
    for (let i = 0; i < data.length; i += 4) {
      const kind = classify(palette, data[i], data[i + 1], data[i + 2], data[i + 3]);
      counts[kind] = (counts[kind] ?? 0) + 1;
      maxAlpha = Math.max(maxAlpha, data[i + 3]);
      // 标字的白色描边（0.95 不透明）：底图误绘的白色填充只有 0.30，分得开。
      if (data[i + 3] >= 200 && data[i] >= 235 && data[i + 1] >= 235 && data[i + 2] >= 235) whiteOpaque++;
    }
    return { total: data.length / 4, counts, maxAlpha, whiteOpaque };
  };
  const canvasOf = (testId: string) => {
    const map = (w.__heatMap as () => AnyMap | null)();
    const canvas = document.querySelector<HTMLCanvasElement>(`canvas[data-testid="${testId}"]`);
    if (!map || !canvas || !canvas.isConnected || !w.BMapGL) return null;
    const context = canvas.getContext('2d');
    const left = parseFloat(canvas.style.left), top = parseFloat(canvas.style.top);
    const width = parseFloat(canvas.style.width), height = parseFloat(canvas.style.height);
    if (!context || !(width > 0) || !(height > 0)) return null;
    const centre = map.getCenter();
    const a = map.pointToOverlayPixel(new w.BMapGL.Point(centre.lng, centre.lat));
    const b = map.pointToOverlayPixel(new w.BMapGL.Point(centre.lng + 0.001, centre.lat));
    const mpp = (111_320 * Math.cos((centre.lat * Math.PI) / 180) * 0.001) / Math.hypot(b.x - a.x, b.y - a.y);
    return { map, canvas, context, left, top, width, height, mpp };
  };
  /** 以每个点为中心读 (2r+1)² CSS 像素的窗口并归类；r < 0 只换算位置。 */
  w.__waterRead = (testId: string, points: { lng: number; lat: number }[], radius: number, palette: Palette) => {
    const found = canvasOf(testId);
    if (!found) return null;
    const { map, canvas, context, left, top, width, height, mpp } = found;
    const sx = canvas.width / width, sy = canvas.height / height;
    const values = points.map(point => {
      const pixel = map.pointToOverlayPixel(new w.BMapGL!.Point(point.lng, point.lat));
      const x = pixel.x - left, y = pixel.y - top, margin = Math.max(radius, 0) + 3;
      if (radius < 0 || !(x >= margin && y >= margin && x <= width - margin && y <= height - margin)) {
        return { x, y, total: 0 };
      }
      const x0 = Math.floor((x - radius) * sx), y0 = Math.floor((y - radius) * sy);
      const x1 = Math.floor((x + radius) * sx), y1 = Math.floor((y + radius) * sy);
      const data = context.getImageData(x0, y0, x1 - x0 + 1, y1 - y0 + 1).data;
      const centre = context.getImageData(Math.floor(x * sx), Math.floor(y * sy), 1, 1).data;
      return { x, y, ...tally(palette, data), pixel: [...centre] };
    });
    return { mpp, width, height, values };
  };
  /** 整张画布的归类直方图，外加某些锚点周围标字框里白色描边的像素数。 */
  w.__waterHistogram = (testId: string, palette: Palette, anchors: { lng: number; lat: number }[],
    box: { dx: number; dy: number }) => {
    const found = canvasOf(testId);
    if (!found) return null;
    const { map, canvas, context, left, top, width, mpp } = found;
    const s = canvas.width / width;
    const whole = tally(palette, context.getImageData(0, 0, canvas.width, canvas.height).data);
    const labels = anchors.map(anchor => {
      const pixel = map.pointToOverlayPixel(new w.BMapGL!.Point(anchor.lng, anchor.lat));
      const x0 = Math.max(0, Math.floor((pixel.x - left - box.dx) * s));
      const y0 = Math.max(0, Math.floor((pixel.y - top - box.dy) * s));
      const x1 = Math.min(canvas.width - 1, Math.floor((pixel.x - left + box.dx) * s));
      const y1 = Math.min(canvas.height - 1, Math.floor((pixel.y - top + box.dy) * s));
      if (x1 <= x0 || y1 <= y0) return null;
      return tally(palette, context.getImageData(x0, y0, x1 - x0 + 1, y1 - y0 + 1).data).whiteOpaque;
    });
    return { mpp, counts: whole.counts, total: whole.total, labels };
  };
}

const guard = { blocked: [] as string[], taskIds: new Set<string>(), problems: [] as string[] };
const allowed = new Set(Object.values(TASKS).map(task => task.taskId));

type Prefs = { toggles: Record<string, boolean>; serviceMode?: string };
const SERVICE_MEDICAL: Prefs = { toggles: { service: true, density: false, water: true }, serviceMode: 'medical' };

async function open(page: Page, seeds: Record<Slot, Task>, slot: Slot, prefs: Prefs = SERVICE_MEDICAL) {
  await page.addInitScript(installProbe);
  await page.addInitScript(({ seeds, prefix }) => {
    if (sessionStorage.getItem('water-acceptance-seeded')) return;
    sessionStorage.setItem('water-acceptance-seeded', '1');
    for (const seed of seeds) localStorage.setItem(prefix + seed.engine, JSON.stringify(seed.value));
  }, { prefix: 'life-circle:checkup:v1:', seeds: SLOTS.map(s => ({ engine: seeds[s].engine, value: {
    handle: { input: { engine: seeds[s].engine, clientRequestId: seeds[s].clientRequestId, center: CENTER, budget: 400 },
      taskId: seeds[s].taskId, savedAt: Date.now() },
    prefs: { ...prefs, reportOpen: false } } })) });
  // 花额度或改状态的请求一律挡掉：这一套只读存档。
  await page.route(url => url.pathname.startsWith('/api/v2/checkups'), async route => {
    const request = route.request();
    const { pathname } = new URL(request.url());
    const id = /^\/api\/v2\/checkups\/([0-9a-f-]{36})/.exec(pathname)?.[1];
    if (id) guard.taskIds.add(id);
    if (request.method() !== 'GET' || /\/(routes|cancel)(\/|$)/.test(pathname)) {
      guard.blocked.push(`${request.method()} ${pathname}`);
      await route.abort('blockedbyclient');
      return;
    }
    await route.fallback();
  });
  page.on('pageerror', error => guard.problems.push(redact(`page: ${error.message}`)));
  await page.goto(`/#/checkup/${slot}`);
  await expectRealBasemap(page);
  await expectTask(page, seeds[slot]);
}

async function expectRealBasemap(page: Page) {
  await expect(page.getByText('地图不可用')).toHaveCount(0);
  await expect(page.getByText('正在加载百度地图')).toHaveCount(0, { timeout: 60000 });
  await expect.poll(() => page.getByTestId('checkup-map').locator('canvas:not([data-map-layer])').count(),
    { timeout: 60000 }).toBeGreaterThan(0);
  await expect.poll(() => page.evaluate(() => !!(window as unknown as { __heatMap: () => unknown }).__heatMap()),
    { timeout: 30000 }).toBe(true);
}

async function expectTask(page: Page, task: Task) {
  const section = page.getByTestId('checkup-map-section');
  await expect(section).toHaveAttribute('data-task-id', task.taskId, { timeout: 60000 });
  await expect(section).toHaveAttribute('data-revision', String(task.revision));
  await expect(section).toHaveAttribute('data-drawn-revisions', String(task.revision), { timeout: 60000 });
}

async function setView(page: Page, centre: LngLat, zoom: number) {
  await page.evaluate(({ centre, zoom }) => {
    const w = window as unknown as { __heatMap: () => { centerAndZoom: (p: unknown, z: number, o?: unknown) => void };
      BMapGL: { Point: new (lng: number, lat: number) => unknown } };
    w.__heatMap().centerAndZoom(new w.BMapGL.Point(centre.lng, centre.lat), zoom, { noAnimation: true });
  }, { centre, zoom });
}

const WATER_CANVAS = 'water-annotation-canvas';
const HEAT_CANVAS = 'service-heat-canvas';

async function read(page: Page, testId: string, points: LngLat[], radius: number): Promise<ReadResult> {
  return page.evaluate(({ testId, points, radius, palette }) =>
    (window as unknown as { __waterRead: (...a: unknown[]) => ReadResult }).__waterRead(testId, points, radius, palette),
  { testId, points, radius, palette: PALETTE });
}

async function metersPerPixel(page: Page): Promise<number> {
  let mpp = 0;
  await expect.poll(async () => { mpp = (await read(page, WATER_CANVAS, [], 0))?.mpp ?? 0; return mpp > 0; },
    { timeout: 20000 }).toBe(true);
  return mpp;
}

/**
 * 在一个面里挑判读点：离它自己的边、离其他标注面的边、离复核范围边都至少 marginM 米，
 * 且不落在其他标注面里、不被 `avoid` 排除；按离边远近排、彼此至少隔 spacingM 米。`near`
 * 给出时只在那一圈里找。
 */
function interior(shape: Shape, all: Shape[], extent: Polygons, marginM: number, spacingM: number, count: number,
  near?: { centre: LngLat; radiusM: number }, avoid: (p: LngLat) => boolean = () => false): LngLat[] {
  const [minLng, minLat, maxLng, maxLat] = near
    ? [offset(near.centre, -near.radiusM, 0).lng, offset(near.centre, 0, -near.radiusM).lat,
      offset(near.centre, near.radiusM, 0).lng, offset(near.centre, 0, near.radiusM).lat]
    : bboxOf(shape.polygons);
  const lat0 = (minLat + maxLat) / 2;
  const step = Math.max(0.8, marginM / 2);
  const others = all.filter(other => other !== shape);
  const found: { p: LngLat; edge: number }[] = [];
  for (let lng = minLng; lng <= maxLng; lng += step / mPerDegLng(lat0)) {
    for (let lat = minLat; lat <= maxLat; lat += step / M_PER_DEG_LAT) {
      const p = { lng, lat };
      if (!inside(p, shape.polygons) || avoid(p)) continue;
      const edge = edgeDistance(p, shape.polygons);
      if (edge < marginM || edgeDistance(p, extent) < marginM) continue;
      if (others.some(other => inside(p, other.polygons) || edgeDistance(p, other.polygons) < marginM)) continue;
      found.push({ p, edge });
    }
  }
  const chosen: LngLat[] = [];
  for (const { p } of found.sort((a, b) => b.edge - a.edge)) {
    if (chosen.length >= count) break;
    if (chosen.every(q => meters(p, q) >= spacingM)) chosen.push(round6(p));
  }
  return chosen;
}

type Group = { name: string; expect: PixelClass; hatch: boolean | null; points: LngLat[]; min: number; max?: number };
type Reading = { group: string; point: LngLat; expect: PixelClass; hatch: boolean | null; counts?: Record<string, number>;
  total?: number; maxAlpha?: number; ok: boolean };

/**
 * 读几组判读窗口直到全部对上（重绘在 moveend/zoomend 之后的下一帧）。落在视口外、
 * 或落在任何一处标字框里的窗口不算；每组至少 `min` 个窗口被判过。
 */
async function readGroups(page: Page, groups: Group[], evidence: Evidence, radius = WINDOW_PX): Promise<Reading[]> {
  const anchored = anchoredShapes(evidence);
  const anchors = anchored.map(shape => shape.anchor!);
  let readings: Reading[] = [];
  let problem = '';
  const points = groups.flatMap(group => group.points);
  const settle = async () => {
    const result = await read(page, WATER_CANVAS, [...anchors, ...points], radius);
    if (!result) { problem = '没有水系标注画布'; return false; }
    const anchorAt = result.values.slice(0, anchors.length).filter((_, i) => labelled(anchored[i], result.mpp));
    const values = result.values.slice(anchors.length);
    readings = [];
    let index = 0;
    problem = '';
    for (const group of groups) {
      const mine: Reading[] = [];
      for (const point of group.points) {
        const value = values[index++];
        if (!('counts' in value)) continue;
        const labelled = anchorAt.some(a => Math.abs(a.x - value.x) <= LABEL_BOX.dx + radius
          && Math.abs(a.y - value.y) <= LABEL_BOX.dy + radius);
        if (labelled || mine.length >= (group.max ?? 4)) continue;
        const counts = value.counts;
        const ok = (counts[group.expect] ?? 0) === value.total
          && (group.hatch === null || (group.hatch ? value.maxAlpha >= HATCH_ALPHA : value.maxAlpha < HATCH_ALPHA - 20));
        mine.push({ group: group.name, point, expect: group.expect, hatch: group.hatch, counts, total: value.total,
          maxAlpha: value.maxAlpha, ok });
      }
      if (mine.length < group.min) problem += `${group.name} 只有 ${mine.length} 个可判窗口；`;
      readings.push(...mine);
    }
    return !problem && readings.every(reading => reading.ok);
  };
  await expect.poll(settle, { timeout: 20000, intervals: [250, 500, 1000] }).toBe(true)
    .catch(error => { throw new Error(`水系标注判读不符：${problem}\n${JSON.stringify(readings, null, 1)}\n${error}`); });
  return readings;
}

/** 某个面的判读组：窗口要整个落在面里，余量随比例尺走。 */
function groupOf(shape: Shape, evidence: Evidence, mpp: number, radius = WINDOW_PX, min = 2,
  near?: { centre: LngLat; radiusM: number }, name?: string): Group {
  const marginM = (radius + 2.5) * mpp;
  // 标字框里的点反正不判，挑点时就绕开，免得离边最远的候选全落在字底下（面刚够标字时最常见）。
  const boxes = anchoredShapes(evidence).filter(other => labelled(other, mpp)).map(other => other.anchor!);
  const avoid = (p: LngLat) => boxes.some(a => {
    const dx = Math.abs(p.lng - a.lng) * mPerDegLng(a.lat) / mpp;
    const dy = Math.abs(p.lat - a.lat) * M_PER_DEG_LAT / mpp;
    return dx <= LABEL_BOX.dx + radius + 2 && dy <= LABEL_BOX.dy + radius + 2;
  });
  const points = interior(shape, evidence.shapes, evidence.extent, marginM, Math.max(6, 16 * mpp), 14, near, avoid);
  return { name: name ?? `${shape.kind}:${shape.id}`, expect: EXPECTED_CLASS[shape.kind],
    hatch: radius >= 3 ? shape.kind === 'misdrawn' || shape.kind === 'conflict' : null, points, min };
}

/**
 * 这个比例尺下会标字的锚点：面的外包框放得下字才标（waterOverlay 的门槛）。按地面米
 * 估的外包框与屏幕上的差一点投影变形，门槛打八折，宁可多避开几处。
 */
const labelled = (shape: Shape, mpp: number) => {
  const [minLng, minLat, maxLng, maxLat] = bboxOf(shape.polygons);
  const extentM = Math.max((maxLng - minLng) * mPerDegLng(minLat), (maxLat - minLat) * M_PER_DEG_LAT);
  return extentM / mpp >= LABEL_MIN_EXTENT_PX * 0.8;
};
const anchoredShapes = (evidence: Evidence) => evidence.shapes.filter(shape => shape.kind !== 'reach' && shape.anchor);

/**
 * 冲突面上热力该是纯未知紫的点：核半径（= 格边长）再放 slackM 米内只有未知格，核权重之和
 * 到了满色（与热力验收同一条双权核），离等时圈与评估域边界至少 10 米（画布按它们裁剪）。
 */
function pureUnknown(shape: Shape, evidence: Evidence, slackM: number): LngLat[] {
  const medical = evidence.cells.filter(cell => cell.category === 'medical');
  const [minLng, minLat, maxLng, maxLat] = bboxOf(shape.polygons);
  const found: LngLat[] = [];
  const clear = (p: LngLat, polygons: Polygons) => !polygons.length || (inside(p, polygons) && edgeDistance(p, polygons) >= 10);
  for (let lng = minLng; lng <= maxLng; lng += 1 / mPerDegLng(minLat)) {
    for (let lat = minLat; lat <= maxLat; lat += 1 / M_PER_DEG_LAT) {
      const p = { lng, lat };
      if (!inside(p, shape.polygons) || !clear(p, evidence.iso) || !clear(p, evidence.domain)) continue;
      let total = 0, pure = true;
      for (const cell of medical) {
        const d = meters(p, cell);
        if (d >= cell.sizeM + slackM) continue;
        if (cell.status !== 'unknown') { pure = false; break; }
        if (d < cell.sizeM) total += (3 / Math.PI) * (1 - (d / cell.sizeM) ** 2) ** 2;
      }
      if (pure && total >= 0.6) found.push(round6(p));
    }
  }
  const chosen: LngLat[] = [];
  for (const p of found) if (chosen.length < 3 && chosen.every(q => meters(p, q) >= 3)) chosen.push(p);
  return chosen;
}

/** 标注画布在热力之上、SDK 标记之下（markerPane 里的次序）。 */
async function stacking(page: Page) {
  return page.evaluate(() => {
    const water = document.querySelector('canvas[data-testid="water-annotation-canvas"]');
    if (!water?.parentElement) return null;
    return [...water.parentElement.children].map(child => child.getAttribute('data-map-layer') ?? 'sdk');
  });
}

function expectStacked(order: string[] | null) {
  expect(order, '水系标注画布在文档里').not.toBeNull();
  const water = order!.indexOf('water');
  expect(order!.filter(layer => layer === 'water')).toHaveLength(1);
  order!.forEach((layer, index) => {
    if (layer === 'heat') expect(index, '热力画布在标注之下').toBeLessThan(water);
    if (layer === 'sdk') expect(index, 'SDK 标记在标注之上').toBeGreaterThan(water);
  });
}

type Bridge = { lng: number; lat: number; title: string };
async function bridges(page: Page): Promise<Bridge[]> {
  return page.evaluate(() => {
    type Overlay = { getTitle?: () => string; getPosition?: () => { lng: number; lat: number } };
    const w = window as unknown as { __heatMap: () => { getOverlays?: () => Overlay[] } };
    return (w.__heatMap().getOverlays?.() ?? [])
      .filter(overlay => typeof overlay.getTitle === 'function' && String(overlay.getTitle()).includes('已核实桥梁'))
      .map(overlay => ({ ...overlay.getPosition!(), title: String(overlay.getTitle!()) }))
      .map(({ lng, lat, title }) => ({ lng, lat, title }));
  });
}

type Mark = { point: LngLat; label: string };
/** 截图；判读点按与探针相同的换算标在画面上（临时 DOM，截完即删）。 */
async function shot(page: Page, name: string, marks: Mark[] = []) {
  await page.waitForTimeout(6000);
  if (marks.length) {
    await page.evaluate(({ marks }) => {
      const w = window as unknown as { __heatMap: () => { pointToOverlayPixel: (p: unknown) => { x: number; y: number } };
        BMapGL: { Point: new (lng: number, lat: number) => unknown } };
      const canvas = document.querySelector<HTMLCanvasElement>('canvas[data-testid="water-annotation-canvas"]');
      const map = w.__heatMap();
      if (!canvas || !map) return;
      const box = canvas.getBoundingClientRect();
      for (const mark of marks) {
        const pixel = map.pointToOverlayPixel(new w.BMapGL.Point(mark.point.lng, mark.point.lat));
        const el = document.createElement('div');
        el.className = 'water-acceptance-mark';
        Object.assign(el.style, { position: 'fixed', left: `${box.left + pixel.x - parseFloat(canvas.style.left) - 5}px`,
          top: `${box.top + pixel.y - parseFloat(canvas.style.top) - 5}px`, width: '10px', height: '10px',
          border: '2px solid #111', borderRadius: '50%', boxShadow: '0 0 0 2px #fff', zIndex: '60', pointerEvents: 'none' });
        const label = document.createElement('span');
        label.textContent = mark.label;
        Object.assign(label.style, { position: 'absolute', left: '13px', top: '-5px', whiteSpace: 'nowrap',
          font: '600 11px/1.2 system-ui, sans-serif', color: '#111', background: 'rgba(255,255,255,.85)', padding: '0 3px' });
        el.appendChild(label);
        document.body.appendChild(el);
      }
    }, { marks });
  }
  await page.locator('.api-map-shell').screenshot({ path: resolve(OUTPUT, name) });
  await page.evaluate(() => document.querySelectorAll('.water-acceptance-mark').forEach(el => el.remove()));
  return name;
}

const marksOf = (readings: Reading[], short: string) => readings.map((reading, i) => ({ point: reading.point,
  label: `${short}${i + 1}` }));

const summary: Record<string, unknown> = {};
const record = (key: string, value: unknown) => { summary[key] = value; };

test.beforeAll(() => { mkdirSync(OUTPUT, { recursive: true }); });

test.afterAll(() => {
  writeFileSync(resolve(OUTPUT, 'summary.json'), `${JSON.stringify({
    generatedAt: new Date().toISOString(), review: REVIEW, osmDataVersion: OSM_VERSION,
    tasks: TASKS,
    guard: { blockedRequests: guard.blocked, taskIdsRequested: [...guard.taskIds], pageErrors: guard.problems },
    ...summary,
  }, null, 1)}\n`);
});

test.afterEach(() => {
  expect(guard.blocked, '不得出现创建、取消或路线核验请求').toEqual([]);
  expect([...guard.taskIds].every(id => allowed.has(id)), '只取这三条任务').toBe(true);
  expect(guard.problems).toEqual([]);
});

const CURRENT: Record<Slot, Task> = { e82: TASKS.e82, hybrid: TASKS.hybrid };

// ---------------------------------------------------------------- 用例

for (const slot of SLOTS) {
  test(`${TASKS[slot].label}：第 7 版写明水系来源与复核版本 —— 离线重算、0 次网络请求、不是旧版本，报告第 06 节给出来源与冲突面积`, async ({ page, request }) => {
    const evidence = await evidenceOf(request, slot);
    const trace = evidence.snapshot.trace as { recomputed?: Record<string, unknown>; dataVersions?: Record<string, unknown> };
    expect(trace.dataVersions?.waterReviews).toEqual([REVIEW]);
    expect(trace.recomputed).toMatchObject({ fromRevision: 5, networkRequests: 0 });
    expect(evidence.water.osmDataVersion).toBe(OSM_VERSION);

    await open(page, CURRENT, slot);
    const section = page.getByTestId('checkup-map-section');
    await expect(section).toHaveAttribute('data-water-reviews', REVIEW);
    await expect(section).toHaveAttribute('data-outdated', 'no');
    await expect(page.getByTestId('checkup-outdated')).toHaveCount(0);
    const version = page.getByTestId('checkup-version');
    await expect(version).toHaveAttribute('data-recomputed', 'yes');
    await expect(version).toContainText(`第 ${TASKS[slot].revision} 版`);
    await expect(version).toContainText(REVIEW);
    await expect(version).toContainText('由第 5 版离线重算');
    await expect(version).toContainText('0 次网络请求');
    const legend = page.getByTestId('water-legend');
    await expect(legend).toHaveAttribute('data-shapes', String(evidence.shapes.length + 1));
    await expect(legend).toHaveAttribute('data-crossings', String(evidence.crossings.length));
    for (const kind of ['extent', 'reach', 'supplement', 'misdrawn', 'conflict']) {
      await expect(legend.locator(`[data-water-kind="${kind}"]`)).toHaveCount(1);
    }
    await expect(legend).toContainText('底图水面仅作参照');
    const texts = { version: await version.innerText(), legend: await legend.innerText(),
      note: await page.getByTestId('water-layer-note').innerText() };

    await page.getByRole('button', { name: '查看体检报告' }).click();
    const report = page.getByTestId('checkup-report');
    await expect(report).toHaveAttribute('data-task-id', TASKS[slot].taskId);
    await expect(report).toHaveAttribute('data-revision', String(TASKS[slot].revision));
    await expect(page.getByTestId('report-outdated')).toHaveCount(0);
    await expect(page.getByTestId('report-water-version')).toContainText(REVIEW);
    await expect(page.getByTestId('report-recomputed')).toContainText('由第 5 版离线重算');
    const sources = page.getByTestId('report-data-sources');
    await expect(sources).toContainText(OSM_VERSION);
    await expect(sources).toContainText(String(evidence.water.sourcePbfSha256).slice(0, 12));
    await expect(sources).toContainText('百度底图上的水面只作显示，不参与计算');
    const card = page.getByTestId(`water-review-${REVIEW}`);
    await expect(card).toContainText(REVIEW_TITLE);
    await expect(card).toContainText('sufe-garden-pond');
    await expect(card).toContainText('未裁决');
    await expect(sources).toContainText('数据冲突／未知');
    await sources.scrollIntoViewIfNeeded();
    await sources.screenshot({ path: resolve(OUTPUT, `${slot}-report-data-sources.png`) });
    record(`${slot}.version`, { revision: TASKS[slot].revision, recomputed: trace.recomputed,
      dataVersions: trace.dataVersions, texts, reportSources: await sources.innerText(),
      screenshot: `${slot}-report-data-sources.png` });
  });

  test(`${TASKS[slot].label}：数据冲突／未知 —— 财大校园池塘与林下水道画成橙色斜线并标字，压在热力之上；对应评估格判未知、不计覆盖也不计灰区`, async ({ page, request }) => {
    const evidence = await evidenceOf(request, slot);
    const pond = shapeOf(evidence, 'sufe-garden-pond');
    const channel = shapeOf(evidence, 'sufe-channel');
    const conflicts = [pond, channel];
    // 数据：冲突格只出在冲突面上，冲突面上没有"已覆盖"或"服务不足"的格。
    const conflictCells = evidence.cells.filter(cell => cell.reason === 'water_data_conflict');
    for (const category of ['education', 'medical', 'shopping']) {
      expect(conflictCells.filter(cell => cell.category === category).length, `${category} 有冲突格`).toBeGreaterThan(0);
    }
    for (const cell of conflictCells) {
      expect(cell.status).toBe('unknown');
      const near = conflicts.some(shape => inside(cell, shape.polygons)
        || edgeDistance(cell, shape.polygons) <= (cell.sizeM / 2) * Math.SQRT2);
      expect(near, `冲突格 ${cell.lng},${cell.lat} 压在冲突面上`).toBe(true);
    }
    const decidedOnConflict = evidence.cells.filter(cell => cell.status !== 'unknown'
      && conflicts.some(shape => inside(cell, shape.polygons)));
    expect(decidedOnConflict, '冲突面上没有已覆盖或服务不足的格').toEqual([]);

    await open(page, CURRENT, slot);
    await page.waitForTimeout(1500);
      const views: Record<string, unknown> = {};
    const screenshots: string[] = [];

    // 池塘，19 级：窗口全是冲突色、读得到斜线；锚点上标了字；热力是未知紫。
    await setView(page, pond.anchor!, 19);
    let mpp = await metersPerPixel(page);
    const pondGroup = groupOf(pond, evidence, mpp);
    const pondWindows = await readGroups(page, [pondGroup], evidence);
    views.pondZ19 = { metersPerPixel: Number(mpp.toFixed(3)), windows: pondWindows };
    // 热力：池塘上一点，核半径内只有冲突格（全是未知）时，颜色就是纯的未知紫。
    const purePond = pureUnknown(pond, evidence, Math.max(3, 3 * mpp));
    expect(purePond.length, '池塘上有核内全是未知格的点').toBeGreaterThan(0);
    const unknownRgba = [...SERVICE_UNKNOWN_RGB, Math.round(255 * SERVICE_ALPHA.unknown)];
    let heatPixels: (number[] | null)[] = [];
    await expect.poll(async () => {
      const result = await read(page, HEAT_CANVAS, purePond, 0);
      heatPixels = result?.values.map(value => ('pixel' in value ? value.pixel : null)) ?? [];
      return heatPixels.length > 0 && heatPixels.every(pixel => pixel !== null
        && Math.abs(pixel[3] - unknownRgba[3]) <= 10
        && pixel.slice(0, 3).every((v, i) => Math.abs(v - unknownRgba[i]) <= 10 + Math.ceil(128 / Math.max(1, pixel[3]))));
    }, { timeout: 20000 }).toBe(true);
    views.pondHeat = { expected: unknownRgba, points: purePond, pixels: heatPixels };
    let histogram = await page.evaluate(({ palette, anchors, box }) =>
      (window as unknown as { __waterHistogram: (...a: unknown[]) => { labels: (number | null)[] } })
        .__waterHistogram('water-annotation-canvas', palette, anchors, box),
    { palette: PALETTE, anchors: [pond.anchor], box: { dx: 40, dy: 8 } });
    expect(histogram.labels[0], '池塘在 19 级放得下"数据冲突／未知"几个字').toBeGreaterThan(20);
    views.pondLabelWhitePixelsZ19 = histogram.labels[0];
    expectStacked(await stacking(page));
    views.stacking = await stacking(page);
    screenshots.push(await shot(page, `${slot}-conflict-pond-z19.png`,
      [...marksOf(pondWindows, '冲'),
        ...purePond.slice(0, 2).map((point, i) => ({ point, label: `热${i + 1}` }))]));

    // 17 级：池塘只有十几像素宽，仍是冲突色（单像素判读），但不标字。
    await setView(page, pond.anchor!, 17);
    mpp = await metersPerPixel(page);
    views.pondZ17 = await readGroups(page, [groupOf(pond, evidence, mpp, 0, 1)], evidence, 0);
    histogram = await page.evaluate(({ palette, anchors, box }) =>
      (window as unknown as { __waterHistogram: (...a: unknown[]) => { labels: (number | null)[] } })
        .__waterHistogram('water-annotation-canvas', palette, anchors, box),
    { palette: PALETTE, anchors: [pond.anchor], box: { dx: 40, dy: 8 } });
    expect(histogram.labels[0], '17 级池塘放不下字，不标').toBe(0);
    views.pondLabelWhitePixelsZ17 = histogram.labels[0];

    // 林下水道，19 级。
    await setView(page, channel.anchor!, 19);
    mpp = await metersPerPixel(page);
    views.channelZ19 = await readGroups(page, [groupOf(channel, evidence, mpp)], evidence);
    screenshots.push(await shot(page, `${slot}-conflict-channel-z19.png`, marksOf(views.channelZ19 as Reading[], '冲')));

    // 关掉水系标注：画布与桥梁标记一起摘掉，热力照旧；再打开回到热力之上。
    const toggle = page.getByRole('checkbox', { name: '水系标注', exact: true });
    await toggle.uncheck();
    await expect(page.locator(`canvas[data-testid="${WATER_CANVAS}"]`)).toHaveCount(0);
    await expect(page.getByTestId('water-legend')).toHaveCount(0);
    await expect.poll(async () => (await bridges(page)).length).toBe(0);
    await expect(page.locator('canvas[data-map-layer="heat"]')).toHaveCount(1);
    await toggle.check();
    await expect(page.locator(`canvas[data-testid="${WATER_CANVAS}"]`)).toHaveCount(1);
    await expect.poll(async () => (await bridges(page)).length).toBe(evidence.crossings.length);
    expectStacked(await stacking(page));
    views.channelAfterToggle = await readGroups(page, [groupOf(channel, evidence, mpp)], evidence);

    record(`${slot}.conflict`, { conflictAreaM2InDomain: evidence.water.conflictAreaM2,
      conflictCells: conflictCells.map(cell => ({ category: cell.category, lng: cell.lng, lat: cell.lat, sizeM: cell.sizeM })),
      views, screenshots });
  });
}

test('百度边界搜索（E8.2）：底图水面画错处 —— 沿河错位的四段与体育场、校内球场画成灰斜线（实为陆地），已核实河道沿河五处都是蓝色、无斜线，错位量沿河变化', async ({ page, request }) => {
  const evidence = await evidenceOf(request, 'e82');
  const reach = evidence.shapes.find(shape => shape.kind === 'reach')!;
  const misdrawn = ['baidu-misdrawn-01', 'baidu-misdrawn-02', 'baidu-misdrawn-03', 'baidu-misdrawn-04']
    .map(id => shapeOf(evidence, id));
  // 数据：底图画错的河段离真实河道有远有近 —— 统一平移修不好。
  const gaps = misdrawn.map(shape => ({ id: shape.id, anchorToReachM: Number(edgeDistance(shape.anchor!, reach.polygons).toFixed(1)),
    insideReach: inside(shape.anchor!, reach.polygons) }));
  expect(gaps.every(gap => !gap.insideReach)).toBe(true);
  const spread = Math.max(...gaps.map(gap => gap.anchorToReachM)) - Math.min(...gaps.map(gap => gap.anchorToReachM));
  expect(spread, '各段底图河道离真实河道的距离不同').toBeGreaterThan(30);
  // 数据：底图误绘处按陆地算 —— 那里的格可以是已覆盖，而且没有一格是因为水系判未知的。
  const onMisdrawn = evidence.cells.filter(cell => cell.category === 'medical'
    && evidence.shapes.some(shape => shape.kind === 'misdrawn' && inside(cell, shape.polygons)));
  expect(onMisdrawn.filter(cell => cell.reason === 'water_data_conflict')).toEqual([]);
  const misdrawnStatuses = onMisdrawn.reduce<Record<string, number>>((out, cell) =>
    ({ ...out, [cell.status]: (out[cell.status] ?? 0) + 1 }), {});

  await open(page, CURRENT, 'e82');
  await page.waitForTimeout(1500);
  const views: Record<string, unknown> = {};
  const screenshots: string[] = [];

  // 河道五处：沿复核河道的经度等分，每处取河心附近。
  const [minLng, minLat, maxLng, maxLat] = bboxOf(reach.polygons);
  const stations = [0.06, 0.3, 0.52, 0.74, 0.94].map(f => {
    const lng = minLng + (maxLng - minLng) * f;
    const hits: number[] = [];
    for (let lat = minLat; lat <= maxLat; lat += 0.5 / M_PER_DEG_LAT) if (inside({ lng, lat }, reach.polygons)) hits.push(lat);
    expect(hits.length, `河道在经度 ${lng.toFixed(5)} 处有水面`).toBeGreaterThan(0);
    return round6({ lng, lat: hits[Math.floor(hits.length / 2)] });
  });
  const stationViews: unknown[] = [];
  for (const [i, station] of stations.entries()) {
    await setView(page, station, 18);
    const mpp = await metersPerPixel(page);
    const readings = await readGroups(page,
      [groupOf(reach, evidence, mpp, 2, 1, { centre: station, radiusM: 60 }, `reach@${i + 1}`)], evidence, 2);
    stationViews.push({ station, metersPerPixel: Number(mpp.toFixed(3)), readings });
  }
  views.reachStations = stationViews;

  // 底图误绘：河段四处（18 级）、体育场田径场与校内球场（18 级）。
  for (const shape of [...misdrawn, shapeOf(evidence, 'baidu-misdrawn-08'), shapeOf(evidence, 'baidu-misdrawn-11')]) {
    await setView(page, shape.anchor!, 18);
    const mpp = await metersPerPixel(page);
    const groups = [groupOf(shape, evidence, mpp, WINDOW_PX, 1)];
    const readings = await readGroups(page, groups, evidence);
    views[shape.id] = { metersPerPixel: Number(mpp.toFixed(3)), areaM2: shape.areaM2, note: shape.note, readings };
    if (shape.id === 'baidu-misdrawn-01' || shape.id === 'baidu-misdrawn-08' || shape.id === 'baidu-misdrawn-11') {
      screenshots.push(await shot(page, `e82-${shape.id}-z18.png`, marksOf(readings, '误')));
    }
  }

  // 17 级沿河一张全景：误绘河段在北、已核实河道在南，两者间距沿河变化。
  const middle = { lng: (minLng + maxLng) / 2, lat: (minLat + maxLat) / 2 + 60 / M_PER_DEG_LAT };
  await setView(page, middle, 17);
  const mpp17 = await metersPerPixel(page);
  // 细长的误绘河段在 17 级可能整段压在自己的字下面：逐段不强求，合起来至少两个窗口。
  const panorama = await readGroups(page, [
    groupOf(reach, evidence, mpp17, 0, 3, undefined, 'reach'),
    ...misdrawn.map(shape => groupOf(shape, evidence, mpp17, 0, 0)),
  ], evidence, 0);
  expect(panorama.filter(reading => reading.expect === 'misdrawn').length).toBeGreaterThanOrEqual(2);
  screenshots.push(await shot(page, 'e82-river-z17.png', [
    ...stations.map((point, i) => ({ point, label: `河${i + 1}` })),
    ...misdrawn.map(shape => ({ point: shape.anchor!, label: shape.id.replace('baidu-misdrawn-', '误') })),
  ]));
  views.panoramaZ17 = panorama;
  record('e82.misdrawn', { baiduToReachGapsM: gaps, gapSpreadM: Number(spread.toFixed(1)),
    medicalCellsOnMisdrawn: misdrawnStatuses, views, screenshots });
});

test('百度边界搜索（E8.2）：已核实桥梁 —— 九座桥的标记落在证据给的位置上，复核河道上的桥都压在蓝色河面上', async ({ page, request }) => {
  const evidence = await evidenceOf(request, 'e82');
  const reach = evidence.shapes.find(shape => shape.kind === 'reach')!;
  const onReach = evidence.crossings.filter(crossing => crossing.river === evidence.reachOsmId);
  expect(onReach.length, '复核河道上的桥').toBeGreaterThan(3);
  for (const crossing of onReach) {
    expect(inside(crossing.anchor, reach.polygons), `桥 ${crossing.osmId} 在河面上`).toBe(true);
  }
  await open(page, CURRENT, 'e82');
  await page.waitForTimeout(1500);
  await expect.poll(async () => (await bridges(page)).length).toBe(evidence.crossings.length);
  const marks = await bridges(page);
  const deviations: Record<string, number> = {};
  for (const crossing of evidence.crossings) {
    const found = marks.find(mark => mark.title.includes(`OSM ${crossing.osmId}`));
    expect(found, `桥 ${crossing.osmId} 的标记`).toBeTruthy();
    deviations[crossing.osmId] = Number(meters(found!, crossing.anchor).toFixed(3));
    // SDK 存点时会把经纬度截到有限位数：差出的只有几厘米，19 级也不到十分之一像素。
    expect(deviations[crossing.osmId], `桥 ${crossing.osmId} 的标记位置`).toBeLessThan(0.2);
  }
  const views: Record<string, unknown> = {};
  const screenshots: string[] = [];
  // 淞沪路一带的桥群与西头的桥，18 级；桥位像素是河面蓝（标记在画布之上，读的是画布本身）。
  const clusters = [onReach[0].anchor, onReach.find(c => c.highway === 'primary')?.anchor ?? onReach[2].anchor];
  for (const [i, centre] of clusters.entries()) {
    await setView(page, centre, 18);
    await metersPerPixel(page);
    const inView = onReach.filter(crossing => meters(crossing.anchor, centre) < 300);
    const readings = await readGroups(page, [{ name: `bridges@${i + 1}`, expect: 'blue', hatch: null,
      points: inView.map(crossing => crossing.anchor), min: 1, max: 8 }], evidence, 1);
    views[`cluster${i + 1}`] = { centre, readings };
    screenshots.push(await shot(page, `e82-bridges-${i + 1}-z18.png`));
  }
  const domTitles = await page.locator('[title*="已核实桥梁"]').count();
  record('e82.bridges', { crossings: evidence.crossings.map(c => ({ osmId: c.osmId, river: c.river, highway: c.highway,
    lengthM: c.lengthM, onReviewedReach: c.river === evidence.reachOsmId })), markers: marks.length,
    markerDomNodesWithTitle: domTitles, markerDeviationM: deviations, views, screenshots });
});

test('百度边界搜索（E8.2）：缩放 15–18 级与补录水塘 —— 各级都画出复核范围、河道、误绘与冲突，河心像素始终是河道蓝，北侧补录水塘是已核实蓝', async ({ page, request }) => {
  const evidence = await evidenceOf(request, 'e82');
  const reach = evidence.shapes.find(shape => shape.kind === 'reach')!;
  const pond = shapeOf(evidence, 'north-pond');
  expect(pond.kind).toBe('supplement');
  await open(page, CURRENT, 'e82');
  await page.waitForTimeout(1500);
  const [minLng, minLat, maxLng, maxLat] = bboxOf(reach.polygons);
  const middle = round6({ lng: (minLng + maxLng) / 2, lat: (minLat + maxLat) / 2 });
  const levels: Record<string, unknown> = {};
  const screenshots: string[] = [];
  for (const zoom of [15, 16, 17, 18]) {
    await setView(page, zoom <= 16 ? CENTER : middle, zoom);
    const mpp = await metersPerPixel(page);
    // 单像素判读：河宽 18 m，15 级也有两个多像素；河心离两岸都要多于一个半像素。
    const marginM = 1.2 * mpp;
    const reachPoints = interior(reach, evidence.shapes, evidence.extent, Math.min(marginM, 8.5), Math.max(40, 30 * mpp), 12);
    const readings = await readGroups(page, [{ name: `reach@z${zoom}`, expect: 'blue', hatch: null, points: reachPoints,
      min: 2, max: 6 }], evidence, 0);
    const histogram = await page.evaluate(({ palette }) =>
      (window as unknown as { __waterHistogram: (...a: unknown[]) => { counts: Record<string, number>; total: number } })
        .__waterHistogram('water-annotation-canvas', palette, [], { dx: 0, dy: 0 }), { palette: PALETTE });
    for (const kind of zoom <= 16 ? ['blue', 'misdrawn', 'conflict', 'extent'] : ['blue', 'misdrawn']) {
      expect(histogram.counts[kind] ?? 0, `${zoom} 级画出了 ${kind}`).toBeGreaterThan(0);
    }
    levels[`z${zoom}`] = { metersPerPixel: Number(mpp.toFixed(3)), readings, histogram: histogram.counts };
    screenshots.push(await shot(page, `e82-zoom-${zoom}.png`, marksOf(readings, '河')));
  }
  // 补录水塘是一条约 75 m × 25 m 的斜带，18 级时它自己的标字框就盖住了全部离边够远的点；19 级读字框之外。
  await setView(page, pond.anchor!, 19);
  const mpp = await metersPerPixel(page);
  levels.northPondZ19 = await readGroups(page, [groupOf(pond, evidence, mpp)], evidence);
  screenshots.push(await shot(page, 'e82-north-pond-z19.png', marksOf(levels.northPondZ19 as Reading[], '塘')));
  record('e82.zoom', { levels, northPond: { areaM2: pond.areaM2, note: pond.note }, screenshots });
});

test('旧版本（第 5 版，早于水系复核）：标明旧版本与所缺的复核，不画水系标注；与第 7 版来回切换、刷新都不串', async ({ page }) => {
  const seeds: Record<Slot, Task> = { e82: TASKS.old, hybrid: TASKS.hybrid };
  await open(page, seeds, 'e82');
  const section = page.getByTestId('checkup-map-section');
  const expectOld = async () => {
    await expect(section).toHaveAttribute('data-outdated', 'yes');
    await expect(section).toHaveAttribute('data-water-reviews', '');
    const alert = page.getByTestId('checkup-outdated');
    await expect(alert).toContainText('旧版本（第 5 版）：水系数据已修订');
    await expect(alert).toContainText(REVIEW);
    await expect(alert).toContainText(REVIEW_TITLE);
    await expect(page.getByTestId('checkup-version')).toContainText('早于水系复核');
    await expect(page.getByTestId('checkup-version')).toHaveAttribute('data-recomputed', 'no');
    await expect(page.getByTestId('water-layer-note')).toContainText('这一版没有水系证据');
    await expect(page.locator(`canvas[data-testid="${WATER_CANVAS}"]`)).toHaveCount(0);
    await expect(page.getByTestId('water-legend')).toHaveCount(0);
    await expect.poll(async () => (await bridges(page)).length).toBe(0);
  };
  await expectOld();
  const oldAlert = await page.getByTestId('checkup-outdated').innerText();
  await page.getByRole('button', { name: '查看体检报告' }).click();
  const report = page.getByTestId('checkup-report');
  await expect(report).toHaveAttribute('data-task-id', TASKS.old.taskId);
  await expect(report).toHaveAttribute('data-revision', '5');
  await expect(page.getByTestId('report-outdated')).toContainText(REVIEW);
  await expect(page.getByTestId('report-water-version')).toContainText('早于水系复核');
  await expect(page.getByTestId('report-data-sources')).toContainText('这一版早于水系证据');
  const oldReport = await page.getByTestId('report-data-sources').innerText();
  await page.getByTestId('report-outdated').screenshot({ path: resolve(OUTPUT, 'old-report-outdated.png') });
  await page.locator('.ant-drawer-close').click();
  await expect(report).toBeHidden();
  await setView(page, CENTER, 16);
  const screenshots = [await shot(page, 'old-e82-rev5-z16.png')];

  // 切到第 7 版：不再是旧版本，标注回来。
  await page.getByTestId('algorithm-hybrid').click();
  await expectRealBasemap(page);
  await expectTask(page, TASKS.hybrid);
  await expect(section).toHaveAttribute('data-outdated', 'no');
  await expect(section).toHaveAttribute('data-water-reviews', REVIEW);
  await expect(page.getByTestId('checkup-outdated')).toHaveCount(0);
  await expect(page.locator(`canvas[data-testid="${WATER_CANVAS}"]`)).toHaveCount(1);
  await expect(page.getByTestId('water-legend')).toBeVisible();
  await setView(page, CENTER, 16);
  screenshots.push(await shot(page, 'switch-hybrid-rev7-z16.png'));

  // 切回、刷新：仍是旧版本，第 7 版的标注不残留。
  await page.getByTestId('algorithm-e82').click();
  await expectRealBasemap(page);
  await expectTask(page, TASKS.old);
  await expectOld();
  await page.reload();
  await expectRealBasemap(page);
  await expectTask(page, TASKS.old);
  await expectOld();
  await setView(page, CENTER, 16);
  screenshots.push(await shot(page, 'old-e82-after-reload-z16.png'));
  record('old', { task: TASKS.old, alert: oldAlert, reportSources: oldReport,
    screenshots: [...screenshots, 'old-report-outdated.png'] });
});
