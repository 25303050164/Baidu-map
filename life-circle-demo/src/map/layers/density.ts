/** 设施密度层（§8.1「设施密度」模式）的纯计算部分。
 *
 * 三条不变量决定了这里的函数形态：
 *
 * * **核半径是地理量，不是屏幕量。** 每个设施按 120 米折算成像素核，折算用的是
 *   该设施所在纬度的米/像素。缩放因此改变的是像素核，不改变地面上的 120 米。
 * * **色标固定。** 上下界不随数据变化，否则两个算法、两幅截图之间颜色无法比较。
 * * **等权、去重、不造点。** 同一个 uid 只算一次；没有真实设施时场为空，而不是
 *   在圈内补随机热力点。
 *
 * 本模块只做数值与几何，不接触 DOM、Canvas 或地图实例，因此可以在 node 下直接
 * 验证；绘制与生命周期在 heatmapOverlay.ts。
 */
import type { BMapPixel } from '../baiduMapTypes';

/** 核半径（米）：密度模式的固定参数，不随缩放变化。 */
export const HEAT_KERNEL_RADIUS_M = 120;
/** 色带上下界（个/公顷）：固定色标，与实际数据无关。 */
export const DENSITY_SCALE_MAX = 4;
/** 密度单位；与设施密度这一层一起显示，不与其他图层共用。 */
export const DENSITY_UNIT = '个/公顷';
/** 颜色缓冲的降采样基准：每个缓冲格不超过 2 个 CSS 像素（§8.2 步骤 3）。 */
export const HEAT_CELL_PX = 2;
/** 核覆盖的最大缓冲格数（半径方向）；超过则加大缓冲格，保证重绘代价有界。 */
export const HEAT_KERNEL_CELLS = 48;

export type Point2 = { lng: number; lat: number };
export type PixelRect = { x: number; y: number; width: number; height: number };
export type Project = (lng: number, lat: number) => BMapPixel;
/** 该纬度的米/像素；由地图自身的投影测得，不假设 Web 墨卡托公式。 */
export type MetersPerPixel = (lat: number) => number;

export type DensityField = {
  /** 缓冲格边长（CSS 像素）。 */
  cell: number;
  width: number;
  height: number;
  /** 缓冲格 (0,0) 左上角在覆盖物像素坐标中的位置。 */
  x: number;
  y: number;
  /** 每格密度（个/公顷）。 */
  values: Float32Array;
  /** 场内最大值；无设施时为 0。 */
  max: number;
  /** 实际参与计算的去重设施数。 */
  points: number;
  /** 坐标不可用而被丢弃的设施数；丢弃不改变其余点的权重。 */
  dropped: number;
};

export type DensityInput = {
  points: ReadonlyArray<Point2 & { id?: string }>;
  project: Project;
  metersPerPixel: MetersPerPixel;
  /** 计算区域（覆盖物像素）；一般是可见视口。 */
  rect: PixelRect;
  radiusM?: number;
  /** 缓冲格上限；未给出时按核半径与视口自动降采样。 */
  cell?: number;
};

/** 二维四次核（biweight），在半径处归零，积分为 1；返回值单位是 1/平方米。 */
export function kernelWeight(distanceM: number, radiusM: number = HEAT_KERNEL_RADIUS_M): number {
  if (!Number.isFinite(distanceM) || !(radiusM > 0)) return 0;
  const ratio = distanceM / radiusM;
  if (ratio >= 1) return 0;
  const falloff = 1 - ratio * ratio;
  return (3 / (Math.PI * radiusM * radiusM)) * falloff * falloff;
}

/** 1/平方米 → 个/公顷。 */
export function perHectare(weightPerM2: number): number {
  return weightPerM2 * 10_000;
}

export type Rgb = [number, number, number];
export type RampStop = { at: number; rgb: Rgb };

const RAMP: RampStop[] = [
  { at: 0.00, rgb: [44, 127, 184] },
  { at: 0.33, rgb: [65, 171, 93] },
  { at: 0.66, rgb: [254, 178, 76] },
  { at: 1.00, rgb: [227, 26, 28] },
];

/** 任一固定色带取值：t 为 0—1，超出即截断。密度与服务覆盖两层共用这一个插值。 */
export function rampAt(stops: readonly RampStop[], t: number): Rgb {
  const value = Number.isFinite(t) ? Math.min(1, Math.max(0, t)) : 0;
  for (let i = 1; i < stops.length; i++) {
    const right = stops[i];
    if (value <= right.at) {
      const left = stops[i - 1];
      const span = right.at - left.at;
      const k = span <= 0 ? 0 : (value - left.at) / span;
      return [
        Math.round(left.rgb[0] + (right.rgb[0] - left.rgb[0]) * k),
        Math.round(left.rgb[1] + (right.rgb[1] - left.rgb[1]) * k),
        Math.round(left.rgb[2] + (right.rgb[2] - left.rgb[2]) * k),
      ];
    }
  }
  return stops[stops.length - 1].rgb;
}

/** 固定色带取值：t 为 0—1 的归一化密度，超出即截断。 */
export function rampRgb(t: number): Rgb {
  return rampAt(RAMP, t);
}

/** 单个设施在自己位置上的密度（个/公顷）：核常数，图例上标出来，读者才知道"一个店"是什么颜色。 */
export const SINGLE_FACILITY_PEAK = perHectare(kernelWeight(0));
/** 有贡献处的不透明度下限：单个设施（约 0.66 个/公顷）在浅色底图上也要看得出。 */
export const DENSITY_ALPHA_FLOOR = 0.34;
/** 色标顶端的不透明度：再高就压住道路与路名。 */
export const DENSITY_ALPHA_MAX = 0.62;
/** 核支撑最外缘的羽化宽度（个/公顷）：约是单个设施在 0.92 倍核半径处的密度，只柔化边线。 */
export const DENSITY_FEATHER = 0.03;

/**
 * 密度（个/公顷）→ RGBA。
 *
 * **颜色（色相）是读数，按固定色标线性取值**；不透明度只负责"看得见"：
 *
 * * **零密度完全透明。** 核支撑之外没有任何设施贡献，必须不着色：只要给零密度留一点底色，
 *   整片计算区域都会被涂上色带低端，被读成"处处都有一点设施"。
 * * **有贡献处有下限。** 线性不透明度下单个设施只有两成，在百度浅色底图上几乎看不出；
 *   这里按 √t 抬高低端，下限 0.34。只在支撑最外缘（< 0.03 个/公顷）羽化到 0，
 *   避免 120 米处出现一圈硬边。
 * * **顶端有上限。** 0.62 以上会盖住道路，底图就不能读了。
 */
export function densityRgba(density: number, scaleMax: number = DENSITY_SCALE_MAX): [number, number, number, number] {
  if (!(density > 0)) return [RAMP[0].rgb[0], RAMP[0].rgb[1], RAMP[0].rgb[2], 0];
  const t = scaleMax > 0 ? Math.min(1, Math.max(0, density / scaleMax)) : 0;
  const [r, g, b] = rampRgb(t);
  const alpha = (DENSITY_ALPHA_FLOOR + (DENSITY_ALPHA_MAX - DENSITY_ALPHA_FLOOR) * Math.sqrt(t))
    * Math.min(1, density / DENSITY_FEATHER);
  return [r, g, b, Math.round(255 * alpha)];
}

/** 图例渐变：与地图同一个 densityRgba，连不透明度一起，浅色底上看到的就是图上的颜色。 */
export function densityLegendCss(stops = 12, scaleMax: number = DENSITY_SCALE_MAX): string {
  const parts: string[] = [];
  for (let i = 0; i <= stops; i++) {
    const t = i / stops;
    // 0 处取羽化后的第一档，而不是透明：图例左端要看得出色带从哪一色开始。
    const [r, g, b, a] = densityRgba(Math.max(DENSITY_FEATHER, t * scaleMax), scaleMax);
    parts.push(`rgba(${r}, ${g}, ${b}, ${(a / 255).toFixed(3)}) ${Math.round(t * 100)}%`);
  }
  return `linear-gradient(90deg, ${parts.join(', ')})`;
}

/** CSS 渐变串，图例与色带用同一个函数，避免两处色标不一致。 */
export function rampCss(stops = 12, color: (t: number) => Rgb = rampRgb): string {
  const parts: string[] = [];
  for (let i = 0; i <= stops; i++) {
    const t = i / stops;
    const [r, g, b] = color(t);
    parts.push(`rgb(${r}, ${g}, ${b}) ${Math.round(t * 100)}%`);
  }
  return `linear-gradient(90deg, ${parts.join(', ')})`;
}

/** 去重：同 id 的设施只保留第一个；无 id 的按坐标去重。 */
export function dedupePoints<T extends Point2 & { id?: string }>(points: readonly T[]): T[] {
  const seen = new Set<string>();
  const kept: T[] = [];
  for (const point of points) {
    const key = point.id ?? `${point.lng},${point.lat}`;
    if (seen.has(key)) continue;
    seen.add(key);
    kept.push(point);
  }
  return kept;
}

function chooseCell(radiiPx: number[], requested?: number): number {
  const largest = radiiPx.length ? Math.max(...radiiPx) : 0;
  // 降采样不会低于 HEAT_CELL_PX；核过大时按 HEAT_KERNEL_CELLS 反推，重绘代价有界。
  const bounded = largest > 0 ? Math.ceil(largest / HEAT_KERNEL_CELLS) : 0;
  return Math.max(HEAT_CELL_PX, requested ?? 0, bounded);
}

/** 在覆盖物像素空间累积核密度，返回按格存储的密度场。 */
export function computeDensity(input: DensityInput): DensityField {
  const radiusM = input.radiusM ?? HEAT_KERNEL_RADIUS_M;
  const { rect, project, metersPerPixel } = input;
  const kept = dedupePoints(input.points);
  // 每个点记下自己的米/像素：核半径按点所在纬度折算，缩放与纬度都不改变地面半径。
  const located: { x: number; y: number; mpp: number; radiusPx: number }[] = [];
  let dropped = 0;
  for (const point of kept) {
    const pixel = project(point.lng, point.lat);
    const mpp = metersPerPixel(point.lat);
    if (!Number.isFinite(pixel?.x) || !Number.isFinite(pixel?.y) || !(mpp > 0)) { dropped++; continue; }
    located.push({ x: pixel.x, y: pixel.y, mpp, radiusPx: radiusM / mpp });
  }
  const cell = chooseCell(located.map(item => item.radiusPx), input.cell);
  const width = Math.max(1, Math.ceil(rect.width / cell));
  const height = Math.max(1, Math.ceil(rect.height / cell));
  const values = new Float32Array(width * height);
  let max = 0;
  for (const item of located) {
    // 只遍历核覆盖到的缓冲格：半径外权重为 0，遍历也没有意义。
    const min = Math.max(0, Math.floor((item.x - item.radiusPx - rect.x) / cell));
    const maxI = Math.min(width - 1, Math.ceil((item.x + item.radiusPx - rect.x) / cell));
    const minJ = Math.max(0, Math.floor((item.y - item.radiusPx - rect.y) / cell));
    const maxJ = Math.min(height - 1, Math.ceil((item.y + item.radiusPx - rect.y) / cell));
    for (let j = minJ; j <= maxJ; j++) {
      const cy = rect.y + (j + 0.5) * cell;
      for (let i = min; i <= maxI; i++) {
        const cx = rect.x + (i + 0.5) * cell;
        const distanceM = Math.hypot(cx - item.x, cy - item.y) * item.mpp;
        const weight = perHectare(kernelWeight(distanceM, radiusM));
        if (weight <= 0) continue;
        const index = j * width + i;
        values[index] += weight;
        if (values[index] > max) max = values[index];
      }
    }
  }
  return { cell, width, height, x: rect.x, y: rect.y, values, max, points: located.length, dropped };
}

/**
 * 几何 → 覆盖物像素环。
 * 结构校验与 polygonPaths 一致；分量之间不连接，孔洞留在本分量的环序列里。
 */
export function pixelRings(
  geometry: { type: string; coordinates: unknown } | null | undefined,
  project: Project,
): number[][][] {
  if (!geometry) return [];
  const polygons = geometry.type === 'Polygon'
    ? [geometry.coordinates]
    : geometry.type === 'MultiPolygon'
      ? (geometry.coordinates as unknown[])
      : [];
  return polygons.map(polygon => {
    if (!Array.isArray(polygon)) throw new Error('Invalid polygon');
    return polygon.map(ring => {
      if (!Array.isArray(ring)) throw new Error('Invalid ring');
      const flat: number[] = [];
      for (const point of ring) {
        if (!Array.isArray(point) || point.length < 2 || !Number.isFinite(point[0]) || !Number.isFinite(point[1])) {
          throw new Error('Invalid point');
        }
        const pixel = project(point[0] as number, point[1] as number);
        if (!Number.isFinite(pixel?.x) || !Number.isFinite(pixel?.y)) throw new Error('Invalid projection');
        flat.push(pixel.x, pixel.y);
      }
      return flat;
    });
  });
}

/** 环组在覆盖物像素空间的包围盒；无可用环时返回 null。 */
export function ringBounds(rings: number[][][]): { minX: number; minY: number; maxX: number; maxY: number } | null {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const component of rings) {
    for (const ring of component) {
      for (let i = 0; i + 1 < ring.length; i += 2) {
        if (ring[i] < minX) minX = ring[i];
        if (ring[i] > maxX) maxX = ring[i];
        if (ring[i + 1] < minY) minY = ring[i + 1];
        if (ring[i + 1] > maxY) maxY = ring[i + 1];
      }
    }
  }
  return Number.isFinite(minX) ? { minX, minY, maxX, maxY } : null;
}

export function intersectRect(a: PixelRect, b: { minX: number; minY: number; maxX: number; maxY: number }): PixelRect | null {
  const x = Math.max(a.x, b.minX);
  const y = Math.max(a.y, b.minY);
  const right = Math.min(a.x + a.width, b.maxX);
  const bottom = Math.min(a.y + a.height, b.maxY);
  if (!(right > x) || !(bottom > y)) return null;
  return { x, y, width: right - x, height: bottom - y };
}
