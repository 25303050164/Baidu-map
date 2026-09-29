/**
 * 热力图真实底图验收：真实 BMapGL、真实存档任务、逐像素对照独立计算的期望值。
 *
 * 这一套回答"地图上那块颜色对不对"，而不是"画布在不在"：
 *
 * - 期望值不从页面里取。服务覆盖取后端评估格原文，按地面米重新求核权重；设施密度按
 *   半正矢距离求双权核的解析和。只借用两条色带本身（颜色的定义），不借用任何网格、投影
 *   或累加代码 —— 那些正是被测的东西。
 * - 读数用地图自己的 `pointToOverlayPixel`：同一个经纬度，平移、缩放、改窗口之后应当读出
 *   同一个颜色，这就是"不错位"。
 * - 只挑读得准的点：服务覆盖只在"核半径内全是同一种结论、离圈边与评估域边都够远"的地方
 *   判颜色；圈外与孔洞只判透明。混合地带（综合模式的过渡带、河道两岸）另按
 *   serviceField.ts 头注释的规则在地面米上独立重算混合色，并按几个屏幕像素的定位误差
 *   在邻近位置里找对应 —— 支持度处于淡入区间的点不判。
 * - 不花服务额度：两条任务都是存档里的真实体检（百度边界搜索 E8.2 与 OSM＋百度各一条），
 *   页面凭 localStorage 里的任务标识恢复；创建、取消、路线核验请求一律挡掉并记账。
 * - 高密度场景真实数据给不出来（这一处只有 4 家设施），用明确标注的合成设施补：只替换
 *   设施图层的内容，画面上贴出"验收用合成设施（非百度数据）"，汇总 JSON 里单列。
 *
 * 输出：截图与 `summary.json`（不含任何 URL 与 AK）写到 `HEAT_OUTPUT_DIR`。
 */
import { test, expect, type APIRequestContext, type Page } from '@playwright/test';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import {
  DENSITY_SCALE_MAX, HEAT_KERNEL_RADIUS_M, SINGLE_FACILITY_PEAK, densityRgba,
} from '../src/map/layers/density';
import {
  SERVICE_ALPHA, SERVICE_COMPOSITE, SERVICE_DISTANCE_MAX_M, SERVICE_GAP_RGB, SERVICE_UNKNOWN_RGB,
  distanceRgb, scoreRgb,
} from '../src/map/layers/serviceField';

const API = process.env.HEAT_API ?? 'http://127.0.0.1:8019';
const OUTPUT = resolve(process.env.HEAT_OUTPUT_DIR ?? 'output/heat-acceptance');
const redact = (value: string) => value.replace(/([?&](?:ak|key|token)=)[^&\s]+/gi, '$1[REDACTED]');

type Slug = 'e82' | 'hybrid';
/** 国定一社区，2026-09-27 两条真实体检（各 428 次百度调用），存档在后端的 CHECKUP_DIR。 */
const TASKS: Record<Slug, { engine: string; label: string; taskId: string; clientRequestId: string }> = {
  e82: { engine: 'baidu_e82', label: '百度边界搜索（E8.2）',
    taskId: 'f6128318-828e-48c0-bdc5-f164c6ece0db', clientRequestId: 'd33ffe23-a880-43f6-b6fa-725a95d49274' },
  hybrid: { engine: 'osm_hybrid', label: 'OSM＋百度',
    taskId: '5e710014-84f5-42e7-ae77-525bd429ae8a', clientRequestId: '4a1d467f-a3ba-4174-b232-9411dcaae066' },
};
const SLUGS: Slug[] = ['e82', 'hybrid'];
const CENTER = { lng: 121.513925, lat: 31.313079 };
const REVISION = 5;
/** 医疗一类三种结论都有（已覆盖、服务不足、数据未知），用它判颜色。 */
const SERVICE_CATEGORY = 'medical';
/** 综合模式要求"均已知"的三类（与后端 heatmap.categories 一致）。 */
const CATEGORIES = ['education', 'medical', 'shopping'];

/**
 * E8.2 评估域里唯一的一块水面（BD-09）：OSM 硬障碍层 data/osm/shanghai.obstacles.geojson
 * （geofabrik-shanghai-20260912）与这条任务第 5 版评估域的交集，约 330 平方米 —— 河道被
 * 等时圈东南边斜切进来的一角，东端宽约 8 米。由 backend/scripts/heat_obstacle_scan.py 从
 * 存档复算（只读、不联网）；OSM＋百度那条任务的域内水面只有 35 平方米的四条细缝，读不准，不判。
 */
const E82_WATER: Polygons = [[[
  [121.5146461, 31.3077767], [121.5146461, 31.3077644], [121.5146461, 31.3077073], [121.5145626, 31.307686],
  [121.5144791, 31.3076647], [121.5143956, 31.3076434], [121.5143122, 31.3076221], [121.5142287, 31.3076008],
  [121.5141452, 31.3075795], [121.5140617, 31.3075582], [121.5140014, 31.3075729], [121.5146461, 31.3077767],
]]];

// ---------------------------------------------------------------- 地理小工具（地面米）

type LngLat = { lng: number; lat: number };
type Polygons = number[][][][];

const EARTH_R = 6_371_008.8;
const rad = (deg: number) => (deg * Math.PI) / 180;
/** 半正矢距离：期望值用真实地面距离，与地图投影无关。 */
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

function polygonsOf(geometry: { type?: string; coordinates?: unknown } | null | undefined): Polygons {
  if (!geometry) return [];
  if (geometry.type === 'Polygon') return [geometry.coordinates as number[][][]];
  if (geometry.type === 'MultiPolygon') return geometry.coordinates as Polygons;
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

/** 奇偶规则：在某个分量的外环里、又不在它的任何孔里。 */
const inside = (p: LngLat, polygons: Polygons) =>
  polygons.some(([outer, ...holes]) => inRing(p, outer) && !holes.some(hole => inRing(p, hole)));

/** 到所有环边的最短地面距离（米），就地展开成局部平面。 */
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

function ringArea(ring: number[][]): number {
  const lat0 = ring[0][1], kx = mPerDegLng(lat0);
  let sum = 0;
  for (let i = 1; i < ring.length; i++) {
    sum += (ring[i - 1][0] * kx) * (ring[i][1] * M_PER_DEG_LAT) - (ring[i][0] * kx) * (ring[i - 1][1] * M_PER_DEG_LAT);
  }
  return Math.abs(sum) / 2;
}

// ---------------------------------------------------------------- 期望值

type Expect =
  | { kind: 'colour'; rgba: number[]; tolRgb: number; tolAlpha: number;
      /** 仅密度：探针周围 slackM 米内解析密度的 [最小, 最大]，用来吸收一个屏幕像素级的定位误差。 */
      spread?: (slackM: number) => [number, number];
      /** 仅服务覆盖的混合地带：探针周围 slackM 米内各处的期望色。 */
      around?: (slackM: number) => number[][] }
  | { kind: 'clear' }
  | { kind: 'visible'; minAlpha: number };
type Probe = { name: string; point: LngLat; expect: Expect; note?: string };

const pure = (rgb: readonly number[], alpha: number): Expect =>
  ({ kind: 'colour', rgba: [...rgb.map(Math.round), Math.round(255 * alpha)], tolRgb: 10, tolAlpha: 10 });

type ServiceSample = LngLat & { status: string; distanceM: number | null; sizeM: number };
type Layer = { layerId: string; revision: number; resultHash: string;
  geometry: { type: string; properties?: Record<string, unknown>; features?: Feature[];
    coordinates?: unknown } };
type Feature = { geometry: { type: string; coordinates: number[] }; properties: Record<string, unknown> };

/**
 * 某处服务覆盖该是什么颜色：评估格按地面米重新求双权核（核半径 = 格边长）。
 * 只有核内全是同一种结论、且离圈边与评估域边都够远时才给期望，其余返回 null。
 */
function serviceExpect(p: LngLat, samples: ServiceSample[], iso: Polygons, domain: Polygons): Expect | null {
  if (!inside(p, iso) || edgeDistance(p, iso) < 30) return null;
  if (domain.length && (!inside(p, domain) || edgeDistance(p, domain) < 30)) return null;
  let total = 0, covered = 0, distance = 0;
  const statuses = new Set<string>();
  for (const s of samples) {
    const d = meters(p, s);
    // 多留 15 米：像素量化与画布插值会把略远一点的格也带进来，那里也得是同一种结论。
    if (d >= s.sizeM + 15) continue;
    statuses.add(s.status);
    if (d >= s.sizeM) continue;
    const w = (3 / Math.PI) * (1 - (d / s.sizeM) ** 2) ** 2;
    total += w;
    if (s.status === 'covered') { covered += w; distance += w * (s.distanceM ?? 0); }
  }
  if (statuses.size !== 1 || total < 0.6) return null;
  const [status] = statuses;
  if (status === 'gap') return pure(SERVICE_GAP_RGB, SERVICE_ALPHA.gap);
  if (status === 'unknown') return pure(SERVICE_UNKNOWN_RGB, SERVICE_ALPHA.unknown);
  return pure(distanceRgb(distance / covered / SERVICE_DISTANCE_MAX_M), SERVICE_ALPHA.covered);
}

/**
 * 支持度（核权重之和）在这一段里前端按平滑台阶淡入（头注释：0.25 起显色、0.5 满色），
 * 两端各留一点余量；混合色期望只在它明确为 0 或 1 的地方给。
 */
const SUPPORT_FADE: [number, number] = [0.2, 0.6];
/** 混合色期望离圈边、评估域边至少这么远（米）：画布按圈裁剪、按缓冲格判域内外。 */
const BLEND_EDGE_M = 3;

type Tally = { covered: number; gap: number; unknown: number; distance: number; total: number };

function tally(p: LngLat, samples: ServiceSample[]): Tally {
  const sum: Tally = { covered: 0, gap: 0, unknown: 0, distance: 0, total: 0 };
  for (const s of samples) {
    // 覆盖格没有距离时前端丢弃它，这里也不算。
    if (s.status === 'covered' && s.distanceM === null) continue;
    const d = meters(p, s);
    if (d >= s.sizeM) continue;
    const w = (3 / Math.PI) * (1 - (d / s.sizeM) ** 2) ** 2;
    sum.total += w;
    if (s.status === 'covered') { sum.covered += w; sum.distance += w * (s.distanceM ?? 0); }
    else if (s.status === 'gap') sum.gap += w;
    else sum.unknown += w;
  }
  return sum;
}

const mix = (parts: [readonly number[], number][]) => {
  const weight = parts.reduce((s, [, w]) => s + w, 0);
  return [0, 1, 2].map(k => parts.reduce((s, [rgb, w]) => s + rgb[k] * w, 0) / weight);
};

type Blend = { rgba: number[]; known: number; score: number; coveredShare: number };

/**
 * 混合地带的期望色，按 serviceField.ts 头注释的规则在地面米上独立重算：三态各自按核权重
 * 累计、按不透明度预乘混合（单类）；三类都已知的份额 × 覆盖类别占比色、其余是未知（综合）；
 * 评估域内没有支持的地方是未知。支持度在淡入区间里、或离圈边与域边太近时返回 null。
 */
function blendAt(p: LngLat, plan: Plan, mode: string): Blend | null {
  if (!inside(p, plan.iso) || edgeDistance(p, plan.iso) < BLEND_EDGE_M) return null;
  if (!plan.domain.length || !inside(p, plan.domain) || edgeDistance(p, plan.domain) < BLEND_EDGE_M) return null;
  const categories = mode === SERVICE_COMPOSITE ? CATEGORIES : [mode];
  const tallies = categories.map(c => tally(p, plan.byCategory[c] ?? []));
  if (tallies.some(s => s.total > SUPPORT_FADE[0] && s.total < SUPPORT_FADE[1])) return null;
  const present = tallies.map(s => s.total >= SUPPORT_FADE[1]);
  const coveredShare = tallies.reduce((m, s) => Math.max(m, s.total > 0 ? s.covered / s.total : 0), 0);
  let rgb: number[], alpha: number, known = 0, score = 0;
  if (!present.some(Boolean)) {
    rgb = [...SERVICE_UNKNOWN_RGB];
    alpha = SERVICE_ALPHA.unknown;
  } else if (mode !== SERVICE_COMPOSITE) {
    const s = tallies[0];
    const near = s.covered > 0 ? distanceRgb(s.distance / s.covered / SERVICE_DISTANCE_MAX_M) : SERVICE_GAP_RGB;
    const parts: [readonly number[], number][] = [[near, s.covered * SERVICE_ALPHA.covered],
      [SERVICE_GAP_RGB, s.gap * SERVICE_ALPHA.gap], [SERVICE_UNKNOWN_RGB, s.unknown * SERVICE_ALPHA.unknown]];
    rgb = mix(parts);
    alpha = parts.reduce((sum, [, w]) => sum + w, 0) / s.total;
    known = (s.covered + s.gap) / s.total;
    score = s.covered + s.gap > 0 ? s.covered / (s.covered + s.gap) : 0;
  } else {
    known = 1;
    tallies.forEach((s, i) => {
      const decided = s.covered + s.gap;
      known *= present[i] ? decided / s.total : 0;
      score += decided > 0 ? s.covered / decided / categories.length : 0;
    });
    const wk = known * SERVICE_ALPHA.covered, wu = (1 - known) * SERVICE_ALPHA.unknown;
    rgb = mix([[scoreRgb(score), wk], [SERVICE_UNKNOWN_RGB, wu]]);
    alpha = wk + wu;
  }
  return { rgba: [...rgb.map(Math.round), Math.round(255 * alpha)], known, score, coveredShare };
}

/** 探针周围一圈（中心、三个半径 × 16 个方向）的期望色。 */
function blendAround(p: LngLat, plan: Plan, mode: string, radiusM: number): number[][] {
  const out: number[][] = [];
  const centre = blendAt(p, plan, mode);
  if (centre) out.push(centre.rgba);
  for (const r of [radiusM / 3, (2 * radiusM) / 3, radiusM]) {
    for (let k = 0; k < 16; k++) {
      const b = blendAt(offset(p, r * Math.cos(k * Math.PI / 8), r * Math.sin(k * Math.PI / 8)), plan, mode);
      if (b) out.push(b.rgba);
    }
  }
  return out;
}

/** 混合色期望：中心色 + 定位误差圈内的候选色。 */
function blendExpect(p: LngLat, plan: Plan, mode: string): Expect | null {
  const centre = blendAt(p, plan, mode);
  if (!centre) return null;
  return { kind: 'colour', rgba: centre.rgba, tolRgb: 10, tolAlpha: 10,
    around: slackM => blendAround(p, plan, mode, slackM) };
}

/** 15 米内期望色不变：核内结论一致，按"纯"点判，不需要定位余量。 */
function flatAt(p: LngLat, plan: Plan, mode: string): boolean {
  const centre = blendAt(p, plan, mode);
  if (!centre) return false;
  const ring = blendAround(p, plan, mode, 15);
  return ring.length === 49 && ring.every(c => c.every((v, i) => Math.abs(v - centre.rgba[i]) <= 2));
}

/** 双权核的解析和（个/公顷）：K(d) = 3/(πr²)·(1−d²/r²)²，每平方米换算到每公顷。 */
function densityAt(p: LngLat, facilities: LngLat[]): number {
  const r = HEAT_KERNEL_RADIUS_M;
  let sum = 0;
  for (const f of facilities) {
    const d = meters(p, f);
    if (d < r) sum += (3 / (Math.PI * r * r)) * (1 - (d / r) ** 2) ** 2 * 10_000;
  }
  return sum;
}

function densityExpect(p: LngLat, facilities: LngLat[], iso: Polygons): Expect | null {
  const margin = edgeDistance(p, iso);
  if (margin < 8) return null;
  if (!inside(p, iso)) return { kind: 'clear' };
  const value = densityAt(p, facilities);
  if (!(value > 0)) return { kind: 'clear' };
  const spread = (slackM: number): [number, number] => {
    let low = value, high = value;
    for (const r of [slackM / 2, slackM]) {
      for (let k = 0; k < 16; k++) {
        const v = densityAt(offset(p, r * Math.cos(k * Math.PI / 8), r * Math.sin(k * Math.PI / 8)), facilities);
        low = Math.min(low, v); high = Math.max(high, v);
      }
    }
    return [low, high];
  };
  return { kind: 'colour', rgba: densityRgba(value), tolRgb: 10, tolAlpha: 12, spread };
}

/** 设施图层 → 密度输入：按大类筛选，同一疑似重复组只留第一条（与界面约定一致）。 */
function densityInput(layer: Layer, category = 'all'): { points: (LngLat & { id: string })[]; records: number } {
  const seen = new Set<string>();
  const points: (LngLat & { id: string })[] = [];
  let records = 0;
  for (const f of layer.geometry.features ?? []) {
    if (f.geometry.type !== 'Point') continue;
    if (category !== 'all' && f.properties.majorCategory !== category) continue;
    records++;
    const id = String(f.properties.possibleDuplicateGroup ?? f.properties.id);
    if (seen.has(id)) continue;
    seen.add(id);
    points.push({ id, lng: f.geometry.coordinates[0], lat: f.geometry.coordinates[1] });
  }
  return { points, records };
}

type Plan = {
  slug: Slug; iso: Polygons; domain: Polygons; samples: ServiceSample[];
  /** 三类各自的评估格（综合模式与混合色期望用）。 */
  byCategory: Record<string, ServiceSample[]>; facilities: Layer;
  hashes: Record<string, string>; service: Probe[]; outside: Probe[];
  hole: { centre: LngLat; areaM2: number; probes: Probe[] } | null;
  counts: Record<string, number>;
};

/** 从真实图层里挑判读点：每种结论两个"纯"点、圈外两个点、最大的一个孔。 */
function planFor(slug: Slug, layers: Record<string, Layer>): Plan {
  const heat = layers.heatmap, isoLayer = layers.isochrone;
  const props = heat.geometry.properties ?? {};
  const stepM = typeof props.stepM === 'number' ? props.stepM : 50;
  const domain = polygonsOf(props.domain as { type?: string; coordinates?: unknown });
  const iso = polygonsOf(isoLayer.geometry as { type?: string; coordinates?: unknown });
  const byCategory: Record<string, ServiceSample[]> = Object.fromEntries(CATEGORIES.map(c => [c, []]));
  const counts: Record<string, number> = {};
  for (const f of heat.geometry.features ?? []) {
    const category = String(f.properties.category);
    if (f.geometry.type !== 'Point' || !byCategory[category]) continue;
    const level = Number(String(f.properties.cell).split(':')[0]);
    const status = String(f.properties.status);
    if (category === SERVICE_CATEGORY) counts[status] = (counts[status] ?? 0) + 1;
    byCategory[category].push({ lng: f.geometry.coordinates[0], lat: f.geometry.coordinates[1], status,
      distanceM: typeof f.properties.distanceM === 'number' ? f.properties.distanceM : null,
      sizeM: stepM / 2 ** level });
  }
  const samples = byCategory[SERVICE_CATEGORY];
  const byStatus: Record<string, Probe[]> = { gap: [], unknown: [], covered: [] };
  const ordered = [...samples].sort((a, b) => meters(a, CENTER) - meters(b, CENTER));
  for (const s of ordered) {
    const list = byStatus[s.status];
    if (!list || list.length >= 2 || list.some(p => meters(p.point, s) < 150)) continue;
    const expect = serviceExpect(s, samples, iso, domain);
    if (expect) list.push({ name: `${s.status}-${list.length + 1}`, point: round6(s), expect, note: s.status });
  }
  const outside: Probe[] = [];
  // 圈外点只在圈的包围盒里找：圈与圈之间、凹口里 —— 远离圈的空白不说明任何事。
  const all = iso.flatMap(polygon => polygon[0]);
  const [minLng, maxLng] = [Math.min(...all.map(v => v[0])), Math.max(...all.map(v => v[0]))];
  const [minLat, maxLat] = [Math.min(...all.map(v => v[1])), Math.max(...all.map(v => v[1]))];
  const grid: LngLat[] = [];
  for (let lng = minLng; lng <= maxLng; lng += 20 / mPerDegLng(CENTER.lat)) {
    for (let lat = minLat; lat <= maxLat; lat += 20 / M_PER_DEG_LAT) grid.push({ lng, lat });
  }
  grid.sort((a, b) => meters(a, CENTER) - meters(b, CENTER));
  for (const p of grid) {
    if (outside.length >= 2) break;
    if (inside(p, iso) || edgeDistance(p, iso) < 40 || outside.some(o => meters(o.point, p) < 200)) continue;
    outside.push({ name: `outside-${outside.length + 1}`, point: round6(p), expect: { kind: 'clear' } });
  }
  // 最大的孔：孔心判透明，孔沿（同一分量里、离所有边至少 6 米）判有色。
  let hole: Plan['hole'] = null;
  const holes = iso.flatMap(polygon => polygon.slice(1)).map(ring => ({ ring, area: ringArea(ring) }))
    .filter(h => h.area >= 40).sort((a, b) => b.area - a.area);
  if (holes.length) {
    const { ring, area } = holes[0];
    const xs = ring.map(v => v[0]), ys = ring.map(v => v[1]);
    let centre: LngLat | null = null, best = 0;
    for (let lng = Math.min(...xs); lng <= Math.max(...xs); lng += 0.5 / mPerDegLng(ys[0])) {
      for (let lat = Math.min(...ys); lat <= Math.max(...ys); lat += 0.5 / M_PER_DEG_LAT) {
        const p = { lng, lat };
        if (!inRing(p, ring)) continue;
        const margin = edgeDistance(p, [[ring]]);
        if (margin > best) { best = margin; centre = p; }
      }
    }
    if (centre && best >= 1.5) {
      const probes: Probe[] = [{ name: 'hole-centre', point: round6(centre), expect: { kind: 'clear' },
        note: `孔心离孔边 ${best.toFixed(1)} 米` }];
      rim: for (let r = 8; r <= 30; r += 2) {
        for (let k = 0; k < 16; k++) {
          const p = offset(centre, r * Math.cos((k * Math.PI) / 8), r * Math.sin((k * Math.PI) / 8));
          if (!inside(p, iso) || edgeDistance(p, iso) < 6) continue;
          if (domain.length && (!inside(p, domain) || edgeDistance(p, domain) < 6)) continue;
          probes.push({ name: 'hole-rim', point: round6(p), expect: { kind: 'visible', minAlpha: 60 },
            note: `离孔心 ${r} 米、离所有边 ≥ 6 米` });
          break rim;
        }
      }
      hole = { centre: round6(centre), areaM2: Math.round(area), probes };
    }
  }
  return { slug, iso, domain, samples, byCategory, facilities: layers.facilities,
    hashes: Object.fromEntries(Object.entries(layers).map(([id, layer]) => [id, layer.resultHash])),
    service: [...byStatus.gap, ...byStatus.unknown, ...byStatus.covered], outside, hole, counts };
}

/** 稀疏真实设施的判读点：设施中心、两两中点、中心外 60 米、离所有设施都超过核半径的一点。 */
function densityProbes(plan: Plan, category = 'all'): { probes: Probe[]; points: LngLat[] } {
  const { points } = densityInput(plan.facilities, category);
  const candidates: { name: string; point: LngLat }[] = [];
  points.forEach((f, i) => {
    candidates.push({ name: `facility-${i + 1}`, point: f });
    candidates.push({ name: `facility-${i + 1}+60m-east`, point: offset(f, 60, 0) });
    candidates.push({ name: `facility-${i + 1}+90m-north`, point: offset(f, 0, 90) });
    for (let j = i + 1; j < points.length; j++) {
      if (meters(f, points[j]) < 2 * HEAT_KERNEL_RADIUS_M) {
        candidates.push({ name: `mid-${i + 1}-${j + 1}`,
          point: { lng: (f.lng + points[j].lng) / 2, lat: (f.lat + points[j].lat) / 2 } });
      }
    }
  });
  const probes: Probe[] = [];
  for (const c of candidates) {
    const expect = densityExpect(c.point, points, plan.iso);
    if (expect) probes.push({ name: c.name, point: round6(c.point), expect });
  }
  // 圈内、离所有设施都超过核半径 + 15 米：密度为零，必须透明（不是"淡淡一层"）。
  for (let r = 150; r <= 700 && points.length; r += 25) {
    const hit = [0, 1, 2, 3, 4, 5, 6, 7].map(k => offset(CENTER, r * Math.cos(k * Math.PI / 4), r * Math.sin(k * Math.PI / 4)))
      .find(p => inside(p, plan.iso) && edgeDistance(p, plan.iso) >= 20
        && points.every(f => meters(p, f) >= HEAT_KERNEL_RADIUS_M + 15));
    if (hit) { probes.push({ name: 'zero-density', point: round6(hit), expect: { kind: 'clear' } }); break; }
  }
  return { probes, points };
}

/**
 * 综合模式的判读点：按混合色期望分类 —— 三类全覆盖、未知、部分覆盖（三类都已知但有缺口）、
 * 已知与未知之间的过渡。前两类只取 15 米内期望不变的"纯"点；后两类取混合点，按定位余量判。
 */
function compositeProbes(plan: Plan): { flat: Probe[]; blend: Probe[]; classes: Record<string, number> } {
  const scan: LngLat[] = [];
  for (let dx = -900; dx <= 900; dx += 20) for (let dy = -900; dy <= 900; dy += 20) scan.push(offset(CENTER, dx, dy));
  scan.sort((p, q) => meters(p, CENTER) - meters(q, CENTER));
  const classes: Record<string, number> = {};
  const picked: Record<string, Probe[]> = {};
  const classOf = (b: Blend) => b.known >= 0.95
    ? (b.score >= 0.999 ? 'all-covered' : b.score <= 0.001 ? 'none-covered' : 'partial')
    : b.known <= 0.05 ? 'unknown' : 'transition';
  for (const p of scan) {
    const b = blendAt(p, plan, SERVICE_COMPOSITE);
    if (!b) continue;
    const name = classOf(b);
    classes[name] = (classes[name] ?? 0) + 1;
    const list = (picked[name] ??= []);
    if (list.length >= 2 || list.some(q => meters(q.point, p) < 150)) continue;
    const flat = flatAt(p, plan, SERVICE_COMPOSITE);
    if ((name === 'all-covered' || name === 'unknown' || name === 'none-covered') && !flat) continue;
    const point = round6(p);
    const expect = flat ? pure(b.rgba.slice(0, 3), b.rgba[3] / 255) : blendExpect(point, plan, SERVICE_COMPOSITE);
    if (!expect) continue;
    list.push({ name: `composite-${name}-${list.length + 1}`, point,
      expect, note: `${name} known=${b.known.toFixed(2)} score=${b.score.toFixed(2)}` });
  }
  const all = Object.values(picked).flat();
  const mixed = (probe: Probe) => probe.expect.kind === 'colour' && !!probe.expect.around;
  return { flat: all.filter(p => !mixed(p)), blend: all.filter(mixed), classes };
}

/**
 * 水面判读：1 米扫描域内水面，统计每个像素上各类覆盖格的权重份额（覆盖色能不能过河），
 * 再挑离水边最远、彼此相距 ≥ 8 米的点，与两岸 40 米内的评估格格心一起判颜色。
 */
function waterProbes(plan: Plan, water: Polygons) {
  const ring = water.flat(2);
  const xs = ring.map(v => v[0]), ys = ring.map(v => v[1]);
  const usable: { p: LngLat; shore: number; share: number }[] = [];
  const covered = CATEGORIES.flatMap(c => plan.byCategory[c]).filter(s => s.status === 'covered');
  let pixels = 0, coveredWeightMax = 0, nearestCovered = Infinity;
  for (let lng = Math.min(...xs); lng <= Math.max(...xs); lng += 1 / mPerDegLng(ys[0])) {
    for (let lat = Math.min(...ys); lat <= Math.max(...ys); lat += 1 / M_PER_DEG_LAT) {
      const p = { lng, lat };
      if (!inside(p, water)) continue;
      pixels++;
      // 全部水面像素（包括贴着圈边、判不了颜色的那些）：覆盖格的核权重与最近的覆盖格心。
      for (const c of CATEGORIES) coveredWeightMax = Math.max(coveredWeightMax, tally(p, plan.byCategory[c]).covered);
      for (const s of covered) nearestCovered = Math.min(nearestCovered, meters(p, s));
      const b = blendAt(p, plan, SERVICE_COMPOSITE);
      if (b && blendAt(p, plan, SERVICE_CATEGORY)) usable.push({ p, shore: edgeDistance(p, water), share: b.coveredShare });
    }
  }
  const chosen: LngLat[] = [];
  for (const u of [...usable].sort((a, b) => b.shore - a.shore)) {
    if (chosen.length >= 4) break;
    if (chosen.every(q => meters(q, u.p) >= 8)) chosen.push(u.p);
  }
  const shoreCells = plan.byCategory[SERVICE_CATEGORY]
    .filter(s => (inside(s, water) || edgeDistance(s, water) < 40))
    .sort((a, b) => edgeDistance(a, water) - edgeDistance(b, water));
  const probesFor = (mode: string): Probe[] => [
    ...chosen.map((p, i) => ({ name: `water-${i + 1}`, point: round6(p), expect: blendExpect(round6(p), plan, mode) })),
    ...shoreCells.map((s, i) => ({ name: `shore-${s.status}-${i + 1}`, point: round6(s), expect: blendExpect(round6(s), plan, mode),
      note: `${s.sizeM} 米格、离水 ${edgeDistance(s, water).toFixed(1)} 米` })),
  ].filter((probe): probe is Probe => probe.expect !== null);
  return { pixels, usable: usable.length, coveredShareMax: usable.reduce((m, u) => Math.max(m, u.share), 0),
    coveredWeightMax, nearestCoveredM: Number(nearestCovered.toFixed(1)), centre: round6(chosen[0] ?? { lng: xs[0], lat: ys[0] }),
    medical: probesFor(SERVICE_CATEGORY), composite: probesFor(SERVICE_COMPOSITE) };
}

// ---------------------------------------------------------------- 页面侧

async function fetchLayer(request: APIRequestContext, taskId: string, layerId: string): Promise<Layer> {
  for (let attempt = 0; ; attempt++) {
    try {
      const response = await request.get(`${API}/api/v2/checkups/${taskId}/layers/${layerId}?revision=${REVISION}`,
        { headers: { connection: 'close' }, timeout: 60000 });
      expect(response.status()).toBe(200);
      return await response.json() as Layer;
    } catch (error) {
      if (attempt >= 3) throw error;
      await new Promise(ready => setTimeout(ready, 1500));
    }
  }
}

const plans = new Map<Slug, Plan>();
async function planOf(request: APIRequestContext, slug: Slug): Promise<Plan> {
  const cached = plans.get(slug);
  if (cached) return cached;
  const layers: Record<string, Layer> = {};
  for (const id of ['isochrone', 'facilities', 'heatmap']) layers[id] = await fetchLayer(request, TASKS[slug].taskId, id);
  const plan = planFor(slug, layers);
  plans.set(slug, plan);
  return plan;
}

type Prefs = { toggles: Record<string, boolean>; serviceMode?: string; densityCategory?: string };

/**
 * 捕获真实地图实例并提供读像素的探针。BMapGL 的 JSONP 回调触发时 `BMapGL.Map` 已就绪：
 * 在那一刻给原型上几个页面必然会调用的方法挂一个登记钩子，不替换构造函数。
 */
function installProbe() {
  const maps: unknown[] = [];
  let ready: (() => void) | undefined;
  type AnyMap = { getContainer?: () => HTMLElement; pointToOverlayPixel: (p: unknown) => { x: number; y: number };
    getCenter: () => { lng: number; lat: number } };
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
  w.__heatProbe = (testId: string, points: { lng: number; lat: number }[]) => {
    const map = (w.__heatMap as () => AnyMap | null)();
    const canvas = document.querySelector<HTMLCanvasElement>(`canvas[data-testid="${testId}"]`);
    if (!map || !canvas || !canvas.isConnected || !w.BMapGL) return null;
    const context = canvas.getContext('2d');
    const left = parseFloat(canvas.style.left), top = parseFloat(canvas.style.top);
    const cssWidth = parseFloat(canvas.style.width), cssHeight = parseFloat(canvas.style.height);
    if (!context || !(cssWidth > 0) || !(cssHeight > 0)) return null;
    // 视图中心处每个 CSS 像素多少米：由地图自己的投影量出来，与图层取米/像素的办法一致。
    const centre = map.getCenter();
    const a = map.pointToOverlayPixel(new w.BMapGL.Point(centre.lng, centre.lat));
    const b = map.pointToOverlayPixel(new w.BMapGL.Point(centre.lng + 0.001, centre.lat));
    const mpp = (111_320 * Math.cos((centre.lat * Math.PI) / 180) * 0.001) / Math.hypot(b.x - a.x, b.y - a.y);
    const values = points.map(point => {
      const pixel = map.pointToOverlayPixel(new w.BMapGL!.Point(point.lng, point.lat));
      const vx = pixel.x - left, vy = pixel.y - top;
      if (!(vx >= 4 && vy >= 4 && vx <= cssWidth - 4 && vy <= cssHeight - 4)) return null;
      const x = Math.floor((vx * canvas.width) / cssWidth), y = Math.floor((vy * canvas.height) / cssHeight);
      return [...context.getImageData(x, y, 1, 1).data];
    });
    return { mpp, values };
  };
}

const guard = { blocked: [] as string[], taskIds: new Set<string>(), problems: [] as string[] };

async function open(page: Page, slug: Slug, prefs: Record<Slug, Prefs>, route?: (page: Page) => Promise<void>) {
  await page.addInitScript(installProbe);
  // 只在这个标签页第一次打开时写入；刷新时保留页面自己改过的偏好。
  await page.addInitScript(({ seeds, prefix }) => {
    if (sessionStorage.getItem('heat-acceptance-seeded')) return;
    sessionStorage.setItem('heat-acceptance-seeded', '1');
    for (const seed of seeds) localStorage.setItem(prefix + seed.engine, JSON.stringify(seed.value));
  }, { prefix: 'life-circle:checkup:v1:', seeds: SLUGS.map(s => ({ engine: TASKS[s].engine, value: {
    handle: { input: { engine: TASKS[s].engine, clientRequestId: TASKS[s].clientRequestId, center: CENTER, budget: 400 },
      taskId: TASKS[s].taskId, savedAt: Date.now() },
    prefs: { ...prefs[s], reportOpen: false } } })) });
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
  if (route) await route(page);
  page.on('pageerror', error => guard.problems.push(redact(`page: ${error.message}`)));
  await page.goto(`/#/checkup/${slug}`);
  await expectRealBasemap(page);
  await expectTask(page, slug);
}

/** 真底图在场：没有降级提示，容器里有 BMapGL 自己的画布，地图实例已登记。 */
async function expectRealBasemap(page: Page) {
  await expect(page.getByText('地图不可用')).toHaveCount(0);
  await expect(page.getByText('正在加载百度地图')).toHaveCount(0, { timeout: 60000 });
  await expect.poll(() => page.getByTestId('checkup-map').locator('canvas:not([data-map-layer])').count(),
    { timeout: 60000 }).toBeGreaterThan(0);
  await expect.poll(() => page.evaluate(() => !!(window as unknown as { __heatMap: () => unknown }).__heatMap()),
    { timeout: 30000 }).toBe(true);
}

/** 地图区块与报告对应同一任务、同一版；图层全部来自这一版。 */
async function expectTask(page: Page, slug: Slug) {
  const section = page.getByTestId('checkup-map-section');
  await expect(section).toHaveAttribute('data-task-id', TASKS[slug].taskId, { timeout: 60000 });
  await expect(section).toHaveAttribute('data-revision', String(REVISION));
  await expect(section).toHaveAttribute('data-drawn-revisions', String(REVISION), { timeout: 60000 });
}

async function setView(page: Page, centre: LngLat, zoom: number) {
  await page.evaluate(({ centre, zoom }) => {
    const w = window as unknown as { __heatMap: () => { centerAndZoom: (p: unknown, z: number, o?: unknown) => void };
      BMapGL: { Point: new (lng: number, lat: number) => unknown } };
    w.__heatMap().centerAndZoom(new w.BMapGL.Point(centre.lng, centre.lat), zoom, { noAnimation: true });
  }, { centre, zoom });
}

async function panBy(page: Page, dx: number, dy: number) {
  await page.evaluate(({ dx, dy }) => {
    const w = window as unknown as { __heatMap: () => { panBy: (x: number, y: number, o?: unknown) => void } };
    w.__heatMap().panBy(dx, dy, { noAnimation: true });
  }, { dx, dy });
}

type Reading = { name: string; point: LngLat; expected: Expect; actual: number[] | null; ok: boolean;
  /** 密度点只在"挪一个半像素"后才对上时记下：当时的米/像素与可接受的密度区间。 */
  shifted?: { metersPerPixel: number; densityRange?: [number, number]; slackM?: number } };

/**
 * 定位容差（CSS 像素）：画布 left/top 是小数、浏览器按整像素落位（≤ 0.5 px），
 * 探针又把覆盖物像素向下取整到设备像素（≤ 1 px）。平缓处这点误差看不出来，
 * 扎堆设施的陡坡上一个像素就是好几米、零点几个/公顷，所以密度点按这一圈内的解析值判。
 */
const POSITION_SLACK_PX = 1.5;
/**
 * 服务覆盖混合地带的定位容差（CSS 像素）：上面那 1.5 px，加上缓冲格（2–3 px）的中心偏移与
 * 画布放大时的双线性插值。只用于混合点；纯点的 15 米余量已经远大于它。
 */
const BLEND_SLACK_PX = 3;

function judge(expected: Expect, actual: number[] | null, mpp: number): { ok: boolean; shifted?: Reading['shifted'] } {
  if (!actual) return { ok: false };
  if (expected.kind === 'clear') return { ok: actual[3] === 0 };
  if (expected.kind === 'visible') return { ok: actual[3] >= expected.minAlpha };
  // 画布按预乘 alpha 存 8 位颜色，读回时每个通道的量化误差约 ±128/alpha：
  // 不透明度 168 时不到 1，核尾巴上 alpha 只有十几时就有 ±8，容差随之放宽。
  const near = (rgba: ArrayLike<number>) => {
    const tolRgb = expected.tolRgb + Math.ceil(128 / Math.max(1, Math.min(actual[3], rgba[3])));
    return actual.slice(0, 3).every((v, i) => Math.abs(v - rgba[i]) <= tolRgb)
      && Math.abs(actual[3] - rgba[3]) <= expected.tolAlpha;
  };
  if (near(expected.rgba)) return { ok: true };
  if (expected.around && mpp > 0) {
    const slackM = Number((BLEND_SLACK_PX * mpp).toFixed(2));
    return { ok: expected.around(slackM).some(near), shifted: { metersPerPixel: Number(mpp.toFixed(3)), slackM } };
  }
  if (!expected.spread || !(mpp > 0)) return { ok: false };
  const [low, high] = expected.spread(POSITION_SLACK_PX * mpp);
  const shifted = { metersPerPixel: Number(mpp.toFixed(3)), densityRange: [Number(low.toFixed(3)), Number(high.toFixed(3))] as [number, number] };
  for (let k = 0; k <= 32; k++) if (near(densityRgba(low + ((high - low) * k) / 32))) return { ok: true, shifted };
  return { ok: false, shifted };
}

/**
 * 读一组点直到全部对上（重绘在 moveend/zoomend/resize 之后的下一帧）。
 * 视口外的点不算数，但至少要有 `minInView` 个在视口里被判过。
 */
async function readProbes(page: Page, testId: string, probes: Probe[], minInView = probes.length): Promise<Reading[]> {
  let readings: Reading[] = [];
  const settle = async () => {
    const probed = await page.evaluate(({ testId, points }) =>
      (window as unknown as { __heatProbe: (id: string, p: unknown) => { mpp: number; values: (number[] | null)[] } | null })
        .__heatProbe(testId, points), { testId, points: probes.map(p => p.point) });
    readings = probes.map((probe, i) => {
      const actual = probed?.values[i] ?? null;
      const verdict = judge(probe.expect, actual, probed?.mpp ?? 0);
      return { name: probe.name, point: probe.point, expected: probe.expect, actual, ...verdict };
    });
    const seen = readings.filter(r => r.actual);
    return seen.length >= minInView && seen.every(r => r.ok);
  };
  await expect.poll(settle, { timeout: 20000, intervals: [250, 500, 1000] }).toBe(true)
    .catch(error => { throw new Error(`${testId} 判读不符：\n${JSON.stringify(readings, null, 1)}\n${error}`); });
  return readings;
}

const heatCanvases = (page: Page) => page.locator('canvas[data-map-layer="heat"]');

type Mark = { point: LngLat; label: string };

/**
 * 截图。判读点按与探针相同的换算标在画面上（小圈 + 名字），读报告的人才知道
 * 数字对应图上哪里；标注是临时 DOM，截完即删，不进画布、不影响读数。
 */
async function shot(page: Page, name: string, testId?: string, marks: Mark[] = []) {
  // BMapGL 的瓦片是 WebGL 贴图，没有加载完成事件：给它时间，截出来才有路网。
  await page.waitForTimeout(6000);
  if (testId && marks.length) {
    await page.evaluate(({ testId, marks }) => {
      const w = window as unknown as { __heatMap: () => { pointToOverlayPixel: (p: unknown) => { x: number; y: number } };
        BMapGL: { Point: new (lng: number, lat: number) => unknown } };
      const canvas = document.querySelector<HTMLCanvasElement>(`canvas[data-testid="${testId}"]`);
      const map = w.__heatMap();
      if (!canvas || !map) return;
      const box = canvas.getBoundingClientRect();
      for (const mark of marks) {
        const pixel = map.pointToOverlayPixel(new w.BMapGL.Point(mark.point.lng, mark.point.lat));
        const x = box.left + pixel.x - parseFloat(canvas.style.left);
        const y = box.top + pixel.y - parseFloat(canvas.style.top);
        const el = document.createElement('div');
        el.className = 'heat-acceptance-mark';
        Object.assign(el.style, { position: 'fixed', left: `${x - 7}px`, top: `${y - 7}px`, width: '14px', height: '14px',
          border: '2px solid #111', borderRadius: '50%', boxShadow: '0 0 0 2px #fff', zIndex: '60', pointerEvents: 'none' });
        const label = document.createElement('span');
        label.textContent = mark.label;
        Object.assign(label.style, { position: 'absolute', left: '16px', top: '-4px', whiteSpace: 'nowrap',
          font: '600 12px/1.2 system-ui, sans-serif', color: '#111', background: 'rgba(255,255,255,.85)', padding: '0 3px' });
        el.appendChild(label);
        document.body.appendChild(el);
      }
    }, { testId, marks });
  }
  await page.locator('.api-map-shell').screenshot({ path: resolve(OUTPUT, name) });
  await page.evaluate(() => document.querySelectorAll('.heat-acceptance-mark').forEach(el => el.remove()));
  return name;
}

const LABELS: Record<string, string> = { gap: '缺口', unknown: '未知', covered: '覆盖', outside: '圈外',
  'hole-centre': '孔心', 'hole-rim': '孔沿', facility: '设施', mid: '中点', 'zero-density': '零密度',
  // 综合与河道的点挨得近，标签取短名；全称见验收报告。
  'composite-all-covered': '全覆', 'composite-unknown': '综未', 'composite-partial': '部分',
  'composite-none-covered': '全缺', 'composite-transition': '过渡', water: '水',
  'shore-covered': '岸覆', 'shore-unknown': '岸未', 'shore-gap': '岸缺' };
/** 判读点 → 图上标注：只标主要的点，偏移点不标，免得挤成一团。 */
const marksOf = (probes: Probe[]): Mark[] => probes
  .filter(p => !/\+\d+m-/.test(p.name))
  .map(p => {
    const [head, index] = [p.name.replace(/-\d+(-\d+)?$/, ''), p.name.match(/-(\d+(?:-\d+)?)$/)?.[1] ?? ''];
    return { point: p.point, label: `${LABELS[head] ?? p.name}${index}` };
  });

async function pick(page: Page, combo: string, item: string) {
  await page.getByRole('combobox', { name: combo }).click();
  await page.locator('.ant-select-dropdown:visible .ant-select-item-option')
    .filter({ hasText: item }).first().click();
}

const summary: Record<string, unknown> = {};
const record = (key: string, value: unknown) => { summary[key] = value; };

test.beforeAll(() => { mkdirSync(OUTPUT, { recursive: true }); });

test.afterAll(() => {
  writeFileSync(resolve(OUTPUT, 'summary.json'), `${JSON.stringify({
    generatedAt: new Date().toISOString(),
    tasks: Object.fromEntries(SLUGS.map(s => [s, { ...TASKS[s], revision: REVISION,
      // 三个图层回的是同一个版本结果哈希（按修订号），读的是同一版结果。
      resultHash: [...new Set(Object.values(plans.get(s)?.hashes ?? {}))],
      medicalCellCounts: plans.get(s)?.counts ?? null }])),
    guard: { blockedRequests: guard.blocked, taskIdsRequested: [...guard.taskIds], pageErrors: guard.problems },
    ...summary,
  }, null, 1)}\n`);
});

test.afterEach(() => {
  expect(guard.blocked, '不得出现创建、取消或路线核验请求').toEqual([]);
  expect([...guard.taskIds].every(id => SLUGS.some(s => TASKS[s].taskId === id)), '只取这两条任务').toBe(true);
  expect(guard.problems).toEqual([]);
});

const SERVICE_ONLY: Prefs = { toggles: { service: true, density: false }, serviceMode: SERVICE_CATEGORY };
const DENSITY_ONLY: Prefs = { toggles: { service: false, density: true }, densityCategory: 'all' };
const COMPOSITE_ONLY: Prefs = { toggles: { service: true, density: false }, serviceMode: SERVICE_COMPOSITE };

// ---------------------------------------------------------------- 用例

for (const slug of SLUGS) {
  test(`${TASKS[slug].label}：服务覆盖 —— 缺口灰、未知紫、覆盖按距离着色，圈外与孔洞透明，平移缩放改窗口不错位`, async ({ page, request }) => {
    const plan = await planOf(request, slug);
    for (const status of ['gap', 'unknown', 'covered']) {
      expect(plan.service.filter(p => p.note === status).length, `${status} 至少一个纯点`).toBeGreaterThan(0);
    }
    expect(plan.outside.length).toBeGreaterThan(0);
    await open(page, slug, { e82: SERVICE_ONLY, hybrid: SERVICE_ONLY });
    await expect(page.getByTestId('service-legend')).toContainText('医疗：已覆盖处最近设施步行 0–1000 米');
    await expect(page.getByTestId('service-legend')).toContainText('模型估计，不是实测');
    await page.waitForTimeout(1500);
    const probes = [...plan.service, ...plan.outside];
    const views: Record<string, Reading[]> = {};
    await setView(page, CENTER, 16);
    views.z16 = await readProbes(page, 'service-heat-canvas', probes);
    const screenshots = [await shot(page, `${slug}-service-medical-z16.png`, 'service-heat-canvas', marksOf(probes))];
    await setView(page, CENTER, 17);
    views.z17 = await readProbes(page, 'service-heat-canvas', probes, 3);
    await panBy(page, 160, -110);
    views.z17pan = await readProbes(page, 'service-heat-canvas', probes, 3);
    await page.setViewportSize({ width: 1100, height: 780 });
    views.narrow = await readProbes(page, 'service-heat-canvas', probes, 2);
    screenshots.push(await shot(page, `${slug}-service-medical-z17-pan-narrow.png`, 'service-heat-canvas', marksOf(probes)));
    await page.setViewportSize({ width: 1440, height: 1000 });
    await setView(page, CENTER, 16);
    views.restored = await readProbes(page, 'service-heat-canvas', probes);
    let hole: Record<string, unknown> | null = null;
    if (plan.hole) {
      await setView(page, plan.hole.centre, 19);
      const readings = await readProbes(page, 'service-heat-canvas', plan.hole.probes);
      screenshots.push(await shot(page, `${slug}-service-hole-z19.png`, 'service-heat-canvas', marksOf(plan.hole.probes)));
      hole = { centre: plan.hole.centre, areaM2: plan.hole.areaM2, readings };
    }
    await expect(heatCanvases(page)).toHaveCount(1);
    record(`${slug}.service`, { category: SERVICE_CATEGORY, views, hole, screenshots,
      holeNote: plan.hole ? null : '等时圈没有面积 ≥ 40 平方米的孔' });
  });

  test(`${TASKS[slug].label}：设施密度 —— 真实稀疏设施的颜色等于解析核密度，缩放不改读数，类别筛选`, async ({ page, request }) => {
    const plan = await planOf(request, slug);
    const { probes, points } = densityProbes(plan);
    expect(probes.filter(p => p.expect.kind === 'colour').length).toBeGreaterThan(2);
    await open(page, slug, { e82: DENSITY_ONLY, hybrid: DENSITY_ONLY });
    // 类别筛选在「地图图层」选项卡里。
    await page.getByTestId('checkup-tab-layers').click();
    const legend = page.getByTestId('density-legend');
    await expect(legend).toHaveAttribute('data-points', String(points.length));
    await expect(legend).toContainText(`设施密度 0–${DENSITY_SCALE_MAX} 个/公顷（竖线：单个设施中心 ${SINGLE_FACILITY_PEAK.toFixed(2)}）`);
    await expect(legend).toContainText(`核半径 ${HEAT_KERNEL_RADIUS_M} 米`);
    await expect(legend).toContainText(`${points.length} 处设施参与`);
    const legendText = (await legend.innerText()).replace(/\s+/g, ' ');
    await page.waitForTimeout(1500);
    const centroid = { lng: points.reduce((s, p) => s + p.lng, 0) / points.length,
      lat: points.reduce((s, p) => s + p.lat, 0) / points.length };
    const views: Record<string, Reading[]> = {};
    const screenshots: string[] = [];
    for (const zoom of [16, 17, 18]) {
      await setView(page, centroid, zoom);
      views[`z${zoom}`] = await readProbes(page, 'facility-density-canvas', probes, zoom === 16 ? probes.length : 3);
      if (zoom === 17) screenshots.push(await shot(page, `${slug}-density-real-z17.png`, 'facility-density-canvas', marksOf(probes)));
    }
    // 单个设施中心在浅色底图上看得出：不透明度至少 0.4。
    const peak = views.z16.find(r => r.name === 'facility-1' && r.expected.kind === 'colour');
    if (peak) expect(peak.actual![3]).toBeGreaterThanOrEqual(Math.round(0.4 * 255));
    // 类别筛选：只看教育，其余设施的颜色必须消失，而不是留在画布上。
    await pick(page, '密度类别', '教育');
    const education = densityProbes(plan, 'education');
    await expect(legend).toHaveAttribute('data-points', String(education.points.length));
    await setView(page, centroid, 16);
    views.education = await readProbes(page, 'facility-density-canvas', education.probes);
    await expect(heatCanvases(page)).toHaveCount(1);
    record(`${slug}.density`, { facilities: points.length, legend: legendText, views, screenshots,
      educationFacilities: education.points.length });
  });

  test(`${TASKS[slug].label}：综合模式 —— 三类均已知处按覆盖类别占比着色，缺一类即未知，切类别再切回不残留`, async ({ page, request }) => {
    const plan = await planOf(request, slug);
    const { flat, blend, classes } = compositeProbes(plan);
    for (const name of ['all-covered', 'unknown']) {
      expect(flat.filter(p => p.name.startsWith(`composite-${name}-`)).length, `${name} 至少一个纯点`).toBeGreaterThan(0);
    }
    // 三类都已知、但只覆盖了一两类的地方很少（扫描格上个位数），至少判一个。
    expect(blend.filter(p => p.name.startsWith('composite-partial-')).length, '部分覆盖至少一个点').toBeGreaterThan(0);
    await open(page, slug, { e82: COMPOSITE_ONLY, hybrid: COMPOSITE_ONLY });
    // 覆盖类别在「地图图层」选项卡里。
    await page.getByTestId('checkup-tab-layers').click();
    const legend = page.getByTestId('service-legend');
    await expect(legend).toContainText('三类均已知处覆盖类别占比 0–100%');
    await expect(legend).toContainText('模型估计，不是实测');
    await page.waitForTimeout(1500);
    const probes = [...flat, ...plan.outside];
    const views: Record<string, Reading[]> = {};
    await setView(page, CENTER, 16);
    views.z16 = await readProbes(page, 'service-heat-canvas', probes);
    const screenshots = [await shot(page, `${slug}-service-composite-z16.png`, 'service-heat-canvas', marksOf([...flat, ...blend]))];
    // 混合点在 z18 读：一个屏幕像素不到一米，定位余量小。
    const near = [...flat, ...blend];
    await setView(page, CENTER, 18);
    views.z18 = await readProbes(page, 'service-heat-canvas', near, Math.min(3, near.length));
    // 切到单类再切回综合：画布换内容而不叠加，读数回到综合。
    await pick(page, '覆盖类别', '医疗');
    await expect(legend).toContainText('医疗：已覆盖处最近设施步行 0–1000 米');
    await pick(page, '覆盖类别', '综合');
    await expect(legend).toContainText('三类均已知处覆盖类别占比 0–100%');
    views.z18AfterSwitch = await readProbes(page, 'service-heat-canvas', near, Math.min(3, near.length));
    // 混合点逐个居中再读：离中心远的部分覆盖点在上一个视图之外，不能靠"至少 3 个在视口里"放过去。
    views.mixedCentred = [];
    for (const probe of blend) {
      await setView(page, probe.point, 18);
      views.mixedCentred.push(...await readProbes(page, 'service-heat-canvas', [probe]));
      if (probe.name === 'composite-partial-1') {
        screenshots.push(await shot(page, `${slug}-service-composite-partial-z18.png`, 'service-heat-canvas', marksOf([probe])));
      }
    }
    await setView(page, CENTER, 17);
    views.z17 = await readProbes(page, 'service-heat-canvas', probes, 3);
    screenshots.push(await shot(page, `${slug}-service-composite-z17.png`, 'service-heat-canvas', marksOf([...flat, ...blend])));
    await expect(heatCanvases(page)).toHaveCount(1);
    record(`${slug}.composite`, { classesOnScanGrid: classes, flatProbes: flat.length, blendProbes: blend.length,
      views, screenshots });
  });
}

test('百度边界搜索（E8.2）：河道（硬障碍）—— 压在水面上的格判未知，水面画成未知紫，覆盖色不过河', async ({ page, request }) => {
  const plan = await planOf(request, 'e82');
  const water = waterProbes(plan, E82_WATER);
  expect(water.usable, '水面上有离圈边与域边都够远、能判颜色的像素').toBeGreaterThan(20);
  // 覆盖格的核（半径 = 格边长）一格也伸不到水面上：医疗、购物、教育三类都一样，
  // 整片水面（含贴着圈边、判不了颜色的像素）都如此。
  expect(water.coveredWeightMax).toBe(0);
  expect(water.coveredShareMax).toBe(0);
  expect(water.nearestCoveredM).toBeGreaterThan(25);
  expect(water.medical.filter(p => p.name.startsWith('water-')).length).toBeGreaterThan(1);
  await open(page, 'e82', { e82: SERVICE_ONLY, hybrid: SERVICE_ONLY });
  // 覆盖类别在「地图图层」选项卡里。
  await page.getByTestId('checkup-tab-layers').click();
  await page.waitForTimeout(1500);
  const views: Record<string, Reading[]> = {};
  await setView(page, water.centre, 19);
  views.medicalZ19 = await readProbes(page, 'service-heat-canvas', water.medical);
  const screenshots = [await shot(page, 'e82-water-medical-z19.png', 'service-heat-canvas', marksOf(water.medical))];
  await setView(page, water.centre, 18);
  views.medicalZ18 = await readProbes(page, 'service-heat-canvas', water.medical);
  await pick(page, '覆盖类别', '综合');
  await setView(page, water.centre, 19);
  views.compositeZ19 = await readProbes(page, 'service-heat-canvas', water.composite);
  screenshots.push(await shot(page, 'e82-water-composite-z19.png', 'service-heat-canvas', marksOf(water.composite)));
  await expect(heatCanvases(page)).toHaveCount(1);
  record('e82.water', { source: 'OSM geofabrik-shanghai-20260912 硬障碍层 ∩ 第 5 版评估域', areaM2: Math.round(ringArea(E82_WATER[0][0])),
    scannedPixels1m: water.pixels, usablePixels: water.usable, coveredWeightMaxAllPixels: water.coveredWeightMax,
    coveredShareMax: water.coveredShareMax,
    nearestCoveredCellM: water.nearestCoveredM, views, screenshots });
});

/** 百度底图的水面色（关掉热力与覆盖物后的原色，按截图取样）与判定容差（RGB 欧氏距离）。 */
const BAIDU_WATER_RGB = [117, 224, 249] as const;
const BAIDU_WATER_TOLERANCE = 24;

/**
 * 障碍判定用的是 OSM 硬障碍层，底图是百度自己的水系，百度不提供水系矢量。
 * 把前者（heat_obstacle_scan.py 导出的 nearbyWater，bd09ll）用红线叠到后者上出图；
 * 再关掉热力与所有覆盖物截纯底图，沿每个 OSM 要素每 2 像素取一点，量到最近"百度水面色"像素的距离，
 * 按地图自己的比例换成米。池塘是对照：两边都有、边界应重合，重合就说明坐标换算没问题；
 * 虬江的偏离只记录不判。没给 HEAT_OSM_WATER 时跳过。
 */
test('百度边界搜索（E8.2）：OSM 水系叠到百度底图上 —— 池塘边界重合，河道中线的偏离量出来记录', async ({ page }) => {
  const source = process.env.HEAT_OSM_WATER;
  test.skip(!source, '需要 HEAT_OSM_WATER 指向 heat_obstacle_scan.py 的输出');
  type Water = { osmId: number; name: string | null; width: string | null; inDomain: number; inDomainUnit: string;
    distanceToDomainM: number; geometry: { type: string; coordinates: unknown } };
  const scan = (JSON.parse(readFileSync(source!, 'utf8')) as Record<string, { osmDataVersion: string; nearbyWater: Water[] }>).e82;
  const nearby = scan.nearbyWater;
  expect(nearby.length).toBeGreaterThan(0);
  await open(page, 'e82', { e82: COMPOSITE_ONLY, hybrid: COMPOSITE_ONLY });
  const added = await page.evaluate(nearby => {
    type Pt = [number, number];
    const w = window as unknown as { __heatMap: () => { addOverlay: (o: unknown) => void }; __osmWater: unknown[];
      BMapGL: Record<string, new (...args: unknown[]) => unknown> & { Point: new (lng: number, lat: number) => unknown } };
    const map = w.__heatMap();
    const points = (ring: Pt[]) => ring.map(([lng, lat]) => new w.BMapGL.Point(lng, lat));
    w.__osmWater = [];
    for (const { geometry: g } of nearby) {
      const lines = g.type === 'LineString' ? [g.coordinates as Pt[]] : g.type === 'MultiLineString' ? g.coordinates as Pt[][] : [];
      const rings = g.type === 'Polygon' ? [(g.coordinates as Pt[][])[0]]
        : g.type === 'MultiPolygon' ? (g.coordinates as Pt[][][]).map(p => p[0]) : [];
      for (const line of lines)
        w.__osmWater.push(new w.BMapGL.Polyline(points(line), { strokeColor: '#e11d48', strokeWeight: 3, strokeOpacity: 0.95 }));
      // 池塘只描边不填色，百度的水面色透得出来。
      for (const ring of rings)
        w.__osmWater.push(new w.BMapGL.Polygon(points(ring), { strokeColor: '#e11d48', strokeWeight: 2, fillOpacity: 0 }));
    }
    w.__osmWater.forEach(o => map.addOverlay(o));
    return w.__osmWater.length;
  }, nearby);
  expect(added).toBeGreaterThan(0);
  const VIEWS = [['z16', { lng: 121.5165, lat: 31.3105 }, 16], ['z17', { lng: 121.518, lat: 31.3095 }, 17]] as const;
  const screenshots: string[] = [];
  for (const [label, centre, zoom] of VIEWS) {
    await setView(page, centre, zoom);
    screenshots.push(await shot(page, `e82-osm-water-vs-basemap-${label}.png`));
  }

  // 纯底图：藏起热力画布与地图上所有覆盖物（含 OSM 线），截图只在内存里量，不落盘。
  await page.evaluate(() => {
    const map = (window as unknown as { __heatMap: () => { getOverlays?: () => { hide: () => void }[] } }).__heatMap();
    map.getOverlays?.().forEach(o => o.hide());
    document.querySelectorAll<HTMLCanvasElement>('canvas[data-testid]').forEach(c => { c.style.visibility = 'hidden'; });
  });
  type Stat = { osmId: number; name: string | null; kind: 'line' | 'polygon'; samples: number; noWaterWithin200px: number;
    medianPx: number | null; medianM: number | null; p10M: number | null; p90M: number | null; maxM: number | null;
    within3pxShare: number | null;
    profile: { lng: number; lat: number; m: number | null }[] };
  const offsets: Record<string, { mpp: number; waterPixels: number; features: Stat[] }> = {};
  for (const [label, centre, zoom] of VIEWS) {
    await setView(page, centre, zoom);
    await page.waitForTimeout(6000);
    const png = (await page.locator('.api-map-shell').screenshot()).toString('base64');
    offsets[label] = await page.evaluate(async ({ png, nearby, water, tolerance }) => {
      type Pt = [number, number];
      type LngLat = { lng: number; lat: number };
      const w = window as unknown as { __heatMap: () => { getContainer: () => HTMLElement; getCenter: () => LngLat;
        pointToPixel: (p: unknown) => { x: number; y: number } }; BMapGL: { Point: new (lng: number, lat: number) => unknown } };
      const map = w.__heatMap();
      const shell = document.querySelector('.api-map-shell')!.getBoundingClientRect();
      const container = map.getContainer();
      const box = container.getBoundingClientRect();
      const image = new Image();
      image.src = `data:image/png;base64,${png}`;
      await image.decode();
      const canvas = document.createElement('canvas');
      canvas.width = image.width;
      canvas.height = image.height;
      const context = canvas.getContext('2d')!;
      context.drawImage(image, 0, 0);
      const { data } = context.getImageData(0, 0, image.width, image.height);
      const k = image.width / shell.width;
      const wx: number[] = [], wy: number[] = [];
      for (let y = 0; y < image.height; y++) for (let x = 0; x < image.width; x++) {
        const i = (y * image.width + x) * 4;
        if (Math.hypot(data[i] - water[0], data[i + 1] - water[1], data[i + 2] - water[2]) <= tolerance) {
          wx.push(x / k);
          wy.push(y / k);
        }
      }
      const toShell = (lng: number, lat: number) => {
        const p = map.pointToPixel(new w.BMapGL.Point(lng, lat));
        return [p.x + box.left - shell.left, p.y + box.top - shell.top] as const;
      };
      // 每 CSS 像素多少米：与探针同一个量法。
      const centre = map.getCenter();
      const a = toShell(centre.lng, centre.lat), b = toShell(centre.lng + 0.001, centre.lat);
      const mpp = (111_320 * Math.cos((centre.lat * Math.PI) / 180) * 0.001) / Math.hypot(b[0] - a[0], b[1] - a[1]);
      // 只量露在地图上的点：图例、选点面板压住的地方不算。
      const visible = (x: number, y: number) => x >= 4 && y >= 4 && x <= shell.width - 4 && y <= shell.height - 4
        && container.contains(document.elementFromPoint(shell.left + x, shell.top + y));
      const nearest = (x: number, y: number) => {
        let best = Infinity;
        for (let i = 0; i < wx.length; i++) {
          const dx = wx[i] - x, dy = wy[i] - y;
          if (Math.abs(dx) < best && Math.abs(dy) < best) best = Math.min(best, Math.hypot(dx, dy));
        }
        return best <= 200 ? best : null;
      };
      const quantile = (sorted: number[], q: number) =>
        sorted.length ? sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))] : null;
      const round = (v: number | null) => v === null ? null : Math.round(v * 10) / 10;
      const metres = (v: number | null) => round(v === null ? null : v * mpp);
      return { mpp: Math.round(mpp * 1000) / 1000, waterPixels: wx.length, features: nearby.map(({ osmId, name, geometry: g }) => {
        const polygon = g.type.endsWith('Polygon');
        const paths = g.type === 'LineString' ? [g.coordinates as Pt[]] : g.type === 'MultiLineString' ? g.coordinates as Pt[][]
          : g.type === 'Polygon' ? [(g.coordinates as Pt[][])[0]] : (g.coordinates as Pt[][][]).map(p => p[0]);
        const found: number[] = [];
        const profile: { lng: number; lat: number; m: number | null }[] = [];
        let samples = 0, missing = 0;
        for (const path of paths) for (let s = 0; s + 1 < path.length; s++) {
          const [p, q] = [toShell(...path[s]), toShell(...path[s + 1])];
          const steps = Math.max(1, Math.ceil(Math.hypot(q[0] - p[0], q[1] - p[1]) / 2));
          for (let t = 0; t < steps; t++) {
            const x = p[0] + ((q[0] - p[0]) * t) / steps, y = p[1] + ((q[1] - p[1]) * t) / steps;
            if (!visible(x, y)) continue;
            const d = nearest(x, y);
            // 沿线每 25 个取样点留一个，报告里画得出偏离随位置怎么变。
            if (samples % 25 === 0) profile.push({ lng: +(path[s][0] + ((path[s + 1][0] - path[s][0]) * t) / steps).toFixed(6),
              lat: +(path[s][1] + ((path[s + 1][1] - path[s][1]) * t) / steps).toFixed(6), m: metres(d) });
            samples++;
            if (d === null) missing++;
            else found.push(d);
          }
        }
        found.sort((x, y) => x - y);
        return { osmId, name, kind: polygon ? 'polygon' as const : 'line' as const, samples, noWaterWithin200px: missing,
          medianPx: round(quantile(found, 0.5)), medianM: metres(quantile(found, 0.5)), p10M: metres(quantile(found, 0.1)), p90M: metres(quantile(found, 0.9)),
          maxM: metres(found.length ? found[found.length - 1] : null),
          within3pxShare: found.length ? round(found.filter(d => d <= 3).length / found.length) : null, profile };
      }).filter(f => f.samples > 0) };
    }, { png, nearby, water: BAIDU_WATER_RGB, tolerance: BAIDU_WATER_TOLERANCE });
  }
  // 再只放出 OSM 线，截一张不带热力的对照图。
  await page.evaluate(() => (window as unknown as { __osmWater: { show: () => void }[] }).__osmWater.forEach(o => o.show()));
  await setView(page, VIEWS[1][1], VIEWS[1][2]);
  screenshots.push(await shot(page, 'e82-osm-water-vs-basemap-z17-plain.png'));

  record('e82.osmWater', { source: `OSM ${scan.osmDataVersion} 硬障碍层（地表水体），红线 / 红框`,
    baiduWater: { rgb: BAIDU_WATER_RGB, tolerance: BAIDU_WATER_TOLERANCE }, overlays: added,
    features: nearby.map(({ geometry, ...rest }) => ({ ...rest, type: geometry.type })), offsets, screenshots });
  // 对照：两个视图里都得真的量到了百度水面，且每个露出来的 OSM 池塘边界都贴着百度水面：
  // 中位偏离不到最细评估格（25 米）的一半，坐标换算的误差就不会把格判到水的另一边。
  const ponds = Object.values(offsets).flatMap(v => v.features.filter(f => f.kind === 'polygon' && f.samples >= 20));
  for (const view of Object.values(offsets)) expect(view.waterPixels).toBeGreaterThan(1000);
  expect(ponds.length).toBeGreaterThan(0);
  for (const pond of ponds) expect(pond.medianM!, `OSM 池塘 ${pond.osmId}`).toBeLessThanOrEqual(12.5);
});

test('两种算法的设施密度用同一把色标：图例文字与单个设施中心的颜色完全一致', async () => {
  const e82 = summary['e82.density'] as { legend: string; views: Record<string, Reading[]> } | undefined;
  const hybrid = summary['hybrid.density'] as { legend: string; views: Record<string, Reading[]> } | undefined;
  test.skip(!e82 || !hybrid, '需要先跑两条设施密度用例');
  const stripCount = (text: string) => text.replace(/·\s*\d+ 处设施参与.*$/, '').trim();
  expect(stripCount(e82!.legend)).toBe(stripCount(hybrid!.legend));
  record('comparableScale', { legend: stripCount(e82!.legend),
    singleFacilityPeak: Number(SINGLE_FACILITY_PEAK.toFixed(4)), scaleMax: DENSITY_SCALE_MAX,
    peakRgba: densityRgba(SINGLE_FACILITY_PEAK) });
});

test('合成密集设施（验收用，非百度数据）：高密度饱和、中密度渐变、疑似重复只算一处', async ({ page, request }) => {
  const plan = await planOf(request, 'e82');
  const real = densityInput(plan.facilities).points;
  // 在圈内找三处彼此相距 ≥ 320 米、离真实设施 ≥ 200 米、离圈边 ≥ 140 米的地方放合成设施：
  // 核半径 120 米，三簇与真实设施的核基本不重叠，画面上分得开；期望值仍按全部设施算。
  const anchors: LngLat[] = [];
  for (let r = 0; r <= 1500 && anchors.length < 3; r += 25) {
    for (let k = 0; k < 24 && anchors.length < 3; k++) {
      const p = offset(CENTER, r * Math.cos(k * Math.PI / 12), r * Math.sin(k * Math.PI / 12));
      if (!inside(p, plan.iso) || edgeDistance(p, plan.iso) < 140) continue;
      if (real.some(f => meters(p, f) < 200) || anchors.some(a => meters(a, p) < 320)) continue;
      anchors.push(p);
    }
  }
  expect(anchors.length).toBe(3);
  const [a, b, c] = anchors;
  const feature = (id: string, p: LngLat, majorCategory: string, group: string | null = null) => ({
    type: 'Feature', geometry: { type: 'Point', coordinates: [p.lng, p.lat] },
    properties: { id: `synthetic:${id}`, name: `验收合成 ${id}`, category: 'synthetic', majorCategory,
      address: '验收用合成设施（非百度数据）', classificationStatus: 'accepted', possibleDuplicateGroup: group } });
  const synthetic = [
    // A：9 家扎堆（中心一家，35 米圈上八家）——远超 4 个/公顷，应饱和到色带顶端。
    feature('A0', a, 'shopping'),
    ...[0, 1, 2, 3, 4, 5, 6, 7].map(k => feature(`A${k + 1}`, offset(a, 35 * Math.cos(k * Math.PI / 4), 35 * Math.sin(k * Math.PI / 4)), 'shopping')),
    // B：一条街上三家，间距 60 米 —— 中密度。
    ...[-60, 0, 60].map((dx, k) => feature(`B${k + 1}`, offset(b, dx, 0), 'medical')),
    // C：同一家店的两条记录（相距约 1 米，后端会标成同一个疑似重复组）—— 只算一处。
    feature('C1', c, 'education', 'possible:synthetic-c'),
    feature('C2', offset(c, 0.8, 0.3), 'education', 'possible:synthetic-c'),
  ];
  const withSynthetic: Layer = { ...plan.facilities, geometry: { ...plan.facilities.geometry,
    features: [...(plan.facilities.geometry.features ?? []), ...synthetic as Feature[]] } };
  const all = densityInput(withSynthetic).points;
  await open(page, 'e82', { e82: DENSITY_ONLY, hybrid: DENSITY_ONLY }, async p => {
    await p.route(url => url.pathname === `/api/v2/checkups/${TASKS.e82.taskId}/layers/facilities`, async route => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(withSynthetic) });
    });
  });
  // 画面上标明：这不是百度数据。
  await page.evaluate(realCount => {
    const shell = document.querySelector('.api-map-shell');
    const banner = document.createElement('div');
    banner.setAttribute('data-testid', 'synthetic-banner');
    banner.textContent = `验收用合成设施（非百度数据）：A 9 家扎堆、B 三家相距 60 米、C 同一家的两条记录；其余 ${realCount} 处为真实百度 POI`;
    Object.assign(banner.style, { position: 'absolute', top: '10px', left: '50%', transform: 'translateX(-50%)',
      zIndex: '20', padding: '6px 12px', background: '#fff4ce', border: '1px solid #d8a600', borderRadius: '6px',
      font: '600 13px/1.4 system-ui, sans-serif', color: '#5c4400', pointerEvents: 'none', maxWidth: '80%' });
    shell?.appendChild(banner);
  }, real.length);
  // 密度类别在「地图图层」选项卡里。
  await page.getByTestId('checkup-tab-layers').click();
  const legend = page.getByTestId('density-legend');
  await expect(legend).toContainText(`${all.length} 处设施参与（1 条疑似重复已合并）`);
  const probe = (name: string, point: LngLat): Probe | null => {
    const expect = densityExpect(point, all, plan.iso);
    return expect ? { name, point: round6(point), expect } : null;
  };
  const probes = [
    probe('A-centre', a), probe('A+80m-north', offset(a, 0, 80)), probe('A+150m-east', offset(a, 150, 0)),
    probe('B-middle', b), probe('B-west-end', offset(b, -60, 0)), probe('B+100m-south', offset(b, 0, -100)),
    probe('C-merged', c), probe('C+50m-west', offset(c, -50, 0)),
  ].filter((item): item is Probe => item !== null);
  expect(probes.map(item => item.name)).toEqual(expect.arrayContaining(['A-centre', 'B-middle', 'B-west-end', 'C-merged']));
  const densities = Object.fromEntries(probes.map(p => [p.name, Number(densityAt(p.point, all).toFixed(3))]));
  // 饱和与合并这两件事单独断言，不只靠颜色容差。
  expect(densities['A-centre']).toBeGreaterThan(DENSITY_SCALE_MAX);
  expect(densities['C-merged']).toBeCloseTo(SINGLE_FACILITY_PEAK, 1);
  await page.waitForTimeout(1500);
  const box = { lng: (a.lng + b.lng + c.lng) / 3, lat: (a.lat + b.lat + c.lat) / 3 };
  const syntheticMarks: Mark[] = [{ point: a, label: 'A 扎堆' }, { point: b, label: 'B 三家' },
    { point: c, label: 'C 重复两条' }];
  const views: Record<string, Reading[]> = {};
  const screenshots: string[] = [];
  for (const zoom of [16, 17]) {
    await setView(page, box, zoom);
    views[`z${zoom}`] = await readProbes(page, 'facility-density-canvas', probes, zoom === 16 ? probes.length : 3);
  }
  await setView(page, box, 16);
  await readProbes(page, 'facility-density-canvas', probes);
  screenshots.push(await shot(page, 'e82-density-synthetic-dense-z16.png', 'facility-density-canvas', syntheticMarks));
  await setView(page, a, 18);
  views.z18A = await readProbes(page, 'facility-density-canvas', probes.filter(p => p.name.startsWith('A')), 2);
  screenshots.push(await shot(page, 'e82-density-synthetic-cluster-z18.png', 'facility-density-canvas', syntheticMarks));
  // 只看购物：A 与真实菜场留下，B（医疗）处必须透明。
  await pick(page, '密度类别', '购物');
  const shopping = densityInput(withSynthetic, 'shopping').points;
  await expect(legend).toHaveAttribute('data-points', String(shopping.length));
  await setView(page, box, 16);
  const filtered: Probe[] = [{ name: 'A-centre', point: round6(a), expect: densityExpect(a, shopping, plan.iso)! },
    { name: 'B-middle(filtered)', point: round6(b), expect: { kind: 'clear' } }];
  views.shopping = await readProbes(page, 'facility-density-canvas', filtered);
  record('synthetic', { label: '验收用合成设施（非百度数据）', task: TASKS.e82.taskId, anchors: anchors.map(round6),
    syntheticRecords: synthetic.length, drawnPoints: all.length, densities, views, screenshots });
});

test('反复开关、切模式、切算法、打开报告、刷新：只剩一层热力，读数与任务一一对应', async ({ page, request }) => {
  const e82 = await planOf(request, 'e82');
  const hybrid = await planOf(request, 'hybrid');
  // 能区分两条任务的点：一边是"纯"结论、另一边要么结论不同要么在圈外 —— 串了结果就会读错。
  const differs = (a: Expect, b: Expect) => a.kind !== b.kind || (a.kind === 'colour' && b.kind === 'colour'
    && a.rgba.some((v, i) => Math.abs(v - b.rgba[i]) > 3 * Math.max(a.tolRgb, a.tolAlpha)));
  const expectAt = (plan: Plan, p: LngLat): Expect | null => {
    if (!inside(p, plan.iso)) return edgeDistance(p, plan.iso) >= 40 ? { kind: 'clear' } : null;
    return serviceExpect(p, plan.samples, plan.iso, plan.domain);
  };
  const discriminating: { point: LngLat; e82: Expect; hybrid: Expect }[] = [];
  const scan: LngLat[] = [];
  for (let dx = -900; dx <= 900; dx += 40) for (let dy = -800; dy <= 800; dy += 40) scan.push(offset(CENTER, dx, dy));
  scan.sort((p, q) => meters(p, CENTER) - meters(q, CENTER));
  for (const p of scan) {
    if (discriminating.length >= 4) break;
    if (discriminating.some(d => meters(d.point, p) < 200)) continue;
    const ee = expectAt(e82, p), he = expectAt(hybrid, p);
    if (!ee || !he || (ee.kind === 'clear' && he.kind === 'clear') || !differs(ee, he)) continue;
    discriminating.push({ point: round6(p), e82: ee, hybrid: he });
  }
  expect(discriminating.length, '至少一个能区分两条任务的点').toBeGreaterThan(0);
  const discriminatingMarks: Mark[] = discriminating.map((d, i) => ({ point: d.point, label: `区分${i + 1}` }));
  const probesFor = (slug: Slug): Probe[] => [
    ...(slug === 'e82' ? e82 : hybrid).service,
    ...discriminating.map((d, i) => ({ name: `discriminating-${i + 1}`, point: d.point, expect: d[slug] })),
  ];
  await open(page, 'e82', { e82: SERVICE_ONLY, hybrid: SERVICE_ONLY });
  await page.waitForTimeout(1500);
  await setView(page, CENTER, 16);
  const steps: Record<string, unknown> = {};
  steps.initial = await readProbes(page, 'service-heat-canvas', probesFor('e82'));

  // 图层开关与热力类别在「地图图层」选项卡里。
  await page.getByTestId('checkup-tab-layers').click();
  const service = page.getByRole('checkbox', { name: '服务覆盖热力', exact: true });
  const density = page.getByRole('checkbox', { name: '设施密度热力', exact: true });
  // 连点：不等重绘，最后停在"服务覆盖开"。
  for (let i = 0; i < 4; i++) {
    await service.uncheck();
    await density.check();
    await service.check();
  }
  await service.uncheck();
  await expect(heatCanvases(page)).toHaveCount(0);
  await service.check();
  await expect(heatCanvases(page)).toHaveCount(1);
  await expect(page.getByTestId('facility-density-canvas')).toHaveCount(0);
  steps.afterToggles = await readProbes(page, 'service-heat-canvas', probesFor('e82'));
  await pick(page, '覆盖类别', '购物');
  await pick(page, '覆盖类别', '医疗');
  steps.afterModeSwitch = await readProbes(page, 'service-heat-canvas', probesFor('e82'));

  // 切到 OSM＋百度：另一条任务、另一套读数；旧地图的热力画布不得留在文档里。
  // 算法切换已并入「引擎与预算」选项卡。
  await page.getByTestId('checkup-tab-engine').click();
  await page.getByTestId('algorithm-hybrid').click();
  await expectRealBasemap(page);
  await expectTask(page, 'hybrid');
  await page.waitForTimeout(1500);
  await setView(page, CENTER, 16);
  steps.hybrid = await readProbes(page, 'service-heat-canvas', probesFor('hybrid'));
  await expect(heatCanvases(page)).toHaveCount(1);
  const hybridShot = await shot(page, 'switch-hybrid-service-medical-z16.png', 'service-heat-canvas', discriminatingMarks);

  await page.getByTestId('checkup-tab-engine').click();
  await page.getByTestId('algorithm-e82').click();
  await expectRealBasemap(page);
  await expectTask(page, 'e82');
  await page.waitForTimeout(1500);
  await setView(page, CENTER, 16);
  steps.backToE82 = await readProbes(page, 'service-heat-canvas', probesFor('e82'));
  await expect(heatCanvases(page)).toHaveCount(1);

  // 报告与地图是同一任务、同一版。
  await page.getByRole('button', { name: '查看体检报告' }).click();
  const report = page.getByTestId('checkup-report');
  await expect(report).toHaveAttribute('data-task-id', TASKS.e82.taskId);
  await expect(report).toHaveAttribute('data-revision', String(REVISION));
  await page.locator('.ant-drawer-close').click();
  await expect(report).toBeHidden();

  // 刷新：凭保存的任务标识恢复，图层开关与类别按离开时的样子。
  await page.reload();
  await expectRealBasemap(page);
  await expectTask(page, 'e82');
  await expect(service).toBeChecked();
  await page.waitForTimeout(1500);
  await setView(page, CENTER, 16);
  steps.afterReload = await readProbes(page, 'service-heat-canvas', probesFor('e82'));
  await expect(heatCanvases(page)).toHaveCount(1);
  const e82Shot = await shot(page, 'switch-back-e82-after-reload-z16.png', 'service-heat-canvas', discriminatingMarks);
  record('lifecycle', { discriminatingPoints: discriminating.length, steps, screenshots: [hybridShot, e82Shot] });
});
