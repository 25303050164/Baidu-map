/**
 * 水系证据 → 地图标注、数据来源说明与版本标识。
 *
 * 热力与灰区把 OSM 水体当障碍用；百度底图上的水面只是底图，不参与计算。两者对不上时
 * （国定一社区的虬江：底图把河道画在实际河道以北 20–190 米，偏离沿河变化），读者看到
 * 的是"底图水面上铺着覆盖色"或者"陆地上一条未知带"，两种都会被读成算错了。这里把后端
 * 在修订里写明的东西翻译成图上能看见的标注，三条规矩：
 *
 * - **只画后端给的几何**，不平移、不裁剪、不按底图"对齐"：偏离沿河变化，任何统一平移
 *   都是在另一段制造新的错位。
 * - **冲突就是冲突**：来源互相矛盾且未裁决的地方标"数据冲突／未知"，不借用"已核实"
 *   的样式；底图画错的水面标"底图水面有误（已核实为陆地）"，不画成水。
 * - **版本按数据说话**：一版修订用了哪些复核写在 `trace.dataVersions.waterReviews`；能力表
 *   列出当前部署采用的复核及其范围。评估范围落在某份复核里、却没有用它的修订，就是
 *   旧版本 —— 不论它是在复核之前算的，还是复核后来又出了新版。
 */
import type { CheckupSnapshot, WaterDataEvidence } from './contract';
import type { DrawableGeometry } from '../analysis/geometry';

type RecordValue = Record<string, unknown>;
const object = (value: unknown): value is RecordValue =>
  value !== null && typeof value === 'object' && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === 'string' && value.length > 0;
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);

export type WaterKind = 'extent' | 'reach' | 'supplement' | 'conflict' | 'misdrawn';

export type WaterStyle = {
  label: string;
  stroke: string;
  /** 面的底色；null 为不填。 */
  fill: string | null;
  /** 斜线颜色；null 为不画斜线。 */
  hatch: string | null;
  /** 描边虚线（像素）；空数组为实线。 */
  dash: number[];
  note: string;
};

/** 绘制顺序即数组顺序：范围在最下，冲突与底图误绘压在河道之上。 */
export const WATER_KINDS: WaterKind[] = ['extent', 'reach', 'supplement', 'misdrawn', 'conflict'];

export const WATER_STYLES: Record<WaterKind, WaterStyle> = {
  extent: { label: '水系复核范围', stroke: '#0f766e', fill: null, hatch: null, dash: [8, 5],
    note: '范围内的河道位置、河宽、补录水体与桥梁经独立影像和第二家地图核对；范围外按 OSM 原样计算。' },
  reach: { label: '已核实河道', stroke: '#1d4ed8', fill: 'rgba(37, 99, 235, 0.28)', hatch: null, dash: [],
    note: '计算用的河道：OSM 位置经影像核对，按实测河宽成面。底图上的河道画在别处时，以这里为准。' },
  supplement: { label: '补录水体（已核实）', stroke: '#1d4ed8', fill: 'rgba(37, 99, 235, 0.28)', hatch: null,
    dash: [], note: 'OSM 未收录、经影像与第二家地图确认的水面，已计入障碍。' },
  misdrawn: { label: '底图水面有误（已核实为陆地）', stroke: '#334155', fill: 'rgba(255, 255, 255, 0.30)',
    hatch: '#334155', dash: [3, 3],
    note: '百度底图在这里画了水面，影像与第二家地图均为陆地；按陆地计算，这里的覆盖色不是"水面上有服务"。' },
  conflict: { label: '数据冲突／未知', stroke: '#c2410c', fill: 'rgba(194, 65, 12, 0.14)', hatch: '#c2410c',
    dash: [5, 3], note: '水系来源互相矛盾且未能裁决：这里既不计覆盖，也不计灰区。' },
};

/** 已核实桥梁的标记色。 */
export const CROSSING_COLOR = '#1d4ed8';

export type WaterShape = {
  key: string;
  kind: WaterKind;
  geometry: DrawableGeometry;
  title: string;
  /** 后端给的面内代表点（放文字标注用）；没有就不标字。 */
  anchor: { lng: number; lat: number } | null;
};
export type WaterMark = { key: string; lng: number; lat: number; title: string };

export type WaterView = {
  /** 这一版带水系证据；false 表示修订早于水系证据（或这一版没有评估）。 */
  available: boolean;
  obstacleLayerAvailable: boolean;
  osmDataVersion: string | null;
  reviews: { label: string; title: string }[];
  shapes: WaterShape[];
  crossings: WaterMark[];
  /** 已核实河道的计算宽度范围（米）；没有河道时为 null。 */
  reachWidthM: { min: number; max: number } | null;
  /** 各类条目数（含没有几何、只在范围外被裁掉的不计）。 */
  counts: Record<WaterKind | 'crossing', number>;
  conflictAreaM2: number;
  reviewedAreaM2: number;
  unreviewedAreaM2: number | null;
  rejected: string[];
  statements: string[];
};

const EMPTY_COUNTS = (): WaterView['counts'] =>
  ({ extent: 0, reach: 0, supplement: 0, misdrawn: 0, conflict: 0, crossing: 0 });

function polygonal(value: unknown): value is DrawableGeometry {
  return object(value) && (value.type === 'Polygon' || value.type === 'MultiPolygon')
    && Array.isArray(value.coordinates) && value.coordinates.length > 0;
}

function anchorOf(value: unknown): { lng: number; lat: number } | null {
  return object(value) && finite(value.lng) && finite(value.lat) ? { lng: value.lng, lat: value.lat } : null;
}

const meters = (value: unknown) => finite(value) ? `${value.toFixed(value < 10 ? 1 : 0)} m` : null;

function reachTitle(item: RecordValue): string {
  const offset = object(item.baiduOffset) ? item.baiduOffset : null;
  return [
    `${WATER_STYLES.reach.label}（OSM${finite(item.widthM) ? `，宽 ${meters(item.widthM)}` : ''}）`,
    text(item.name) ? item.name : null,
    text(item.osmWidthTag) ? `OSM 原标宽 ${item.osmWidthTag} m` : null,
    offset && finite(offset.minM) && finite(offset.maxM)
      ? `百度底图偏离 ${Math.round(offset.minM)}–${Math.round(offset.maxM)} m，沿河变化，未做统一平移` : null,
  ].filter(Boolean).join(' · ');
}

function itemTitle(kind: WaterKind, item: RecordValue): string {
  return [WATER_STYLES[kind].label, text(item.id) ? item.id : null,
    finite(item.areaM2) ? `约 ${Math.round(item.areaM2)} m²` : null,
    text(item.note) ? item.note : null].filter(Boolean).join(' · ');
}

function crossingTitle(item: RecordValue): string {
  return ['已核实桥梁', text(item.highway) ? `${item.highway} 道路` : null,
    finite(item.lengthM) ? `桥长 ${meters(item.lengthM)}` : null,
    finite(item.osmId) ? `OSM ${item.osmId}` : null,
    text(item.note) ? item.note : null].filter(Boolean).join(' · ');
}

/**
 * 一版修订的水系标注。`water` 为 null（早于水系证据的修订）时什么也不画 ——
 * 这时该说的是"旧版本"，由 {@link versionView} 说。
 */
export function waterView(water: WaterDataEvidence | null | undefined): WaterView {
  const view: WaterView = {
    available: !!water, obstacleLayerAvailable: water?.obstacleLayerAvailable ?? false,
    osmDataVersion: water?.osmDataVersion ?? null, reviews: [], shapes: [], crossings: [], reachWidthM: null,
    counts: EMPTY_COUNTS(), conflictAreaM2: water?.conflictAreaM2 ?? 0,
    reviewedAreaM2: water?.reviewedAreaM2 ?? 0, unreviewedAreaM2: water?.unreviewedAreaM2 ?? null,
    rejected: [...(water?.rejectedReviews ?? [])], statements: [...(water?.statements ?? [])],
  };
  if (!water) return view;
  for (const review of water.reviews) {
    const label = text(review.label) ? review.label : '未命名复核';
    view.reviews.push({ label, title: text(review.title) ? review.title : label });
    const add = (kind: WaterKind, key: string, geometry: unknown, title: string, anchor: unknown = null) => {
      view.counts[kind]++;
      if (polygonal(geometry)) view.shapes.push({ key: `${label}:${key}`, kind, geometry, title,
        anchor: anchorOf(anchor) });
    };
    add('extent', 'extent', review.extent, `${WATER_STYLES.extent.label}：${text(review.title) ? review.title : label}`
      + `（${label}）${text(review.reviewedAt) ? `，${review.reviewedAt} 复核` : ''}`);
    const rows = (key: string): RecordValue[] => {
      const list = review[key];
      return Array.isArray(list) ? list.filter(object) : [];
    };
    rows('reaches').forEach((item, index) => {
      add('reach', `reach:${item.osmId ?? index}`, item.geometry, reachTitle(item));
      if (finite(item.widthM)) view.reachWidthM = {
        min: Math.min(view.reachWidthM?.min ?? item.widthM, item.widthM),
        max: Math.max(view.reachWidthM?.max ?? item.widthM, item.widthM) };
    });
    rows('supplements').forEach((item, index) => add('supplement', `supplement:${item.id ?? index}`,
      item.geometry, itemTitle('supplement', item), item.anchor));
    rows('basemapMisdrawn').forEach((item, index) => add('misdrawn', `misdrawn:${item.id ?? index}`,
      item.geometry, itemTitle('misdrawn', item), item.anchor));
    rows('conflicts').forEach((item, index) => add('conflict', `conflict:${item.id ?? index}`,
      item.geometry, itemTitle('conflict', item), item.anchor));
    rows('crossings').forEach((item, index) => {
      view.counts.crossing++;
      const anchor = anchorOf(item.anchor);
      if (anchor) view.crossings.push({ key: `${label}:crossing:${item.osmId ?? index}`, ...anchor,
        title: crossingTitle(item) });
    });
  }
  view.shapes.sort((a, b) => WATER_KINDS.indexOf(a.kind) - WATER_KINDS.indexOf(b.kind));
  return view;
}

/** 能力表里的一份复核：当前部署采用它，范围是 bd09ll 包围盒。 */
export type WaterReviewRef = { label: string; title: string; bbox: [number, number, number, number] };

/** 能力表的 `waterReviews` → 复核清单；字段不全的条目不认（认错范围会把新结果标成旧的）。 */
export function waterReviewRefs(value: unknown): WaterReviewRef[] {
  if (!Array.isArray(value)) return [];
  return value.filter(object).flatMap(item => {
    const bbox = item.bbox;
    if (!text(item.label) || !Array.isArray(bbox) || bbox.length !== 4 || !bbox.every(finite)
      || bbox[0] > bbox[2] || bbox[1] > bbox[3]) return [];
    return [{ label: item.label, title: text(item.title) ? item.title : item.label,
      bbox: bbox as [number, number, number, number] }];
  });
}

export type RecomputedView = {
  fromRevision: number;
  reason: string;
  networkRequests: number;
  /** 边界怎么来的：存档台账回放重建，或者沿用原边界。 */
  boundary: string;
  carriedOver: string[];
};

export type VersionView = {
  /** 这一版是数据修订后离线重算出来的。 */
  recomputed: RecomputedView | null;
  /** 这一版用到的水系复核（`id@version`）；null 表示早于水系复核支持的修订。 */
  applied: string[] | null;
  /** 评估范围落在其中、这一版却没有用到的复核：非空即旧版本。 */
  outdatedBy: WaterReviewRef[];
};

type Bounds = [number, number, number, number];

function boundsOf(geometry: unknown): Bounds | null {
  if (!object(geometry) || !Array.isArray(geometry.coordinates)) return null;
  let bounds: Bounds | null = null;
  const visit = (node: unknown) => {
    if (!Array.isArray(node)) return;
    if (node.length >= 2 && finite(node[0]) && finite(node[1])) {
      const [x, y] = node as number[];
      bounds = bounds ? [Math.min(bounds[0], x), Math.min(bounds[1], y), Math.max(bounds[2], x),
        Math.max(bounds[3], y)] : [x, y, x, y];
      return;
    }
    node.forEach(visit);
  };
  visit(geometry.coordinates);
  return bounds;
}

const overlaps = (a: Bounds, b: Bounds) => a[0] <= b[2] && b[0] <= a[2] && a[1] <= b[3] && b[1] <= a[3];

/** 修订的评估范围：评估域优先，没有时退回等时圈，再没有就只看中心点。 */
function extentOf(snapshot: CheckupSnapshot): Bounds {
  const isochrone = object(snapshot.isochrone) ? snapshot.isochrone.displayGeometry
    ?? snapshot.isochrone.geometry : null;
  return boundsOf(snapshot.accessibility?.domain) ?? boundsOf(snapshot.heatmap?.domain)
    ?? boundsOf(isochrone)
    ?? [snapshot.center.lng, snapshot.center.lat, snapshot.center.lng, snapshot.center.lat];
}

function recomputedOf(value: unknown): RecomputedView | null {
  if (!object(value) || !finite(value.fromRevision)) return null;
  return { fromRevision: value.fromRevision, reason: text(value.reason) ? value.reason : '未说明',
    networkRequests: finite(value.networkRequests) ? value.networkRequests : 0,
    boundary: text(value.boundary) ? value.boundary : 'unchanged',
    carriedOver: Array.isArray(value.carriedOver) ? value.carriedOver.filter(text) : [] };
}

export function versionView(snapshot: CheckupSnapshot, reviews: readonly WaterReviewRef[]): VersionView {
  const listed = snapshot.trace.dataVersions?.waterReviews;
  const applied = Array.isArray(listed) ? listed.filter(text) : null;
  // 没有评估（等时圈失败、评估没跑）的修订谈不上用了哪份水系，不标旧版本。
  const assessed = snapshot.accessibility !== null || snapshot.heatmap !== null;
  const extent = extentOf(snapshot);
  const outdatedBy = assessed ? reviews.filter(review => overlaps(review.bbox, extent)
    && !(applied ?? []).includes(review.label)) : [];
  return { recomputed: recomputedOf(snapshot.trace.recomputed), applied, outdatedBy };
}

const BOUNDARY_LABELS: Record<string, string> = {
  replayed_from_ledger: '边界由存档的采样台账按修订后的水系回放重建',
  unchanged: '边界沿用原版（该算法成圈不读水系数据）',
};

const CARRIED_LABELS: Record<string, string> = { facilities: '设施检索', verificationRoutes: '核验路线' };

/** 重算版本的一句话说明：从哪一版来、为什么、沿用了什么、花了多少。 */
export function recomputedText(view: RecomputedView): string {
  const carried = view.carriedOver.map(item => CARRIED_LABELS[item] ?? item);
  return `由第 ${view.fromRevision} 版离线重算（${view.reason}）；`
    + `${BOUNDARY_LABELS[view.boundary] ?? view.boundary}；`
    + (carried.length > 0 ? `${carried.join('与')}沿用原任务的结果，` : '')
    + `重算发出 ${view.networkRequests} 次网络请求。`;
}

/** 旧版本的一句话说明。 */
export function outdatedText(view: VersionView): string {
  const names = view.outdatedBy.map(review => `「${review.title}」（${review.label}）`).join('、');
  return view.applied === null
    ? `这一版早于水系复核${names}：热力与灰区里的水体按复核前的 OSM 原样计算，河道位置、河宽与桥梁未经核对，`
      + '底图与障碍层不一致的地方可能被读错。请以重算后的版本或新的体检为准。'
    : `这一版没有采用当前的水系复核${names}（它用的是 ${view.applied.join('、') || '无复核'}）。`
      + '请以重算后的版本或新的体检为准。';
}
