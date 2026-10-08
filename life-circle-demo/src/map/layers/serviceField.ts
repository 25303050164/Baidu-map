/** 服务覆盖层（§8.1「服务覆盖」模式）的纯计算部分：后端网格三态 → 渐变的覆盖面。
 *
 * 数据是体检的 heatmap 图层：每个评估格一个点（格中心），带类别、三态结论和模型估计的
 * 最近设施步行距离。旧界面只把这些点聚合成标记，图上能看到的"覆盖结论"只剩灰区的
 * 正方形台阶。这里把它们还原成一张连续的面，四条规矩：
 *
 * * **归一化加权平均，不是核叠加。** 每个像素的值 = Σw·v / Σw，权重只决定"谁的值说了
 *   算"，不累加亮度：同一片区域从 50 米格加密到 25 米格，颜色与透明度都不变（§8.1：
 *   服务覆盖层不采用点核叠加，否则采样越密越亮）。
 * * **核半径就是格边长。** 核恰好够到相邻格的中心，格内部仍是本格的值，只有格与格之间
 *   一个格宽的过渡带被平滑 —— 正方形台阶消失，但数值不会被推到一格以外。核半径是地理
 *   量，缩放改变的只是像素核。
 * * **三态分开累计，未知不参与插值。** 覆盖格按距离着色、缺口格为灰、未知格为淡紫；未知
 *   格即使带着模型距离也不拿来着色（结论未定的距离不是结论）。过渡带里是按权重混合
 *   三态的颜色，不是把距离外推到未知区域。
 * * **评估域内没有支持就是未知，域外一律透明。** 后端只给有距离的格出点；域内没有点的
 *   地方在报告里算未知面积，图上也必须画成未知，而不是"看起来什么都没有"。
 *
 * 色标固定、不随数据伸缩：单类是 0—1000 米步行距离（与服务标准一致），综合是三类中
 * 已覆盖的比例 0—100（§8.1 服务色标固定 0—100）。
 *
 * 与 density.ts 一样只做数值与几何，不接触 DOM、Canvas 或地图实例；像素在
 * serviceOverlay.ts。
 */
import {
  HEAT_CELL_PX, HEAT_KERNEL_CELLS, rampAt, rampCss,
  type MetersPerPixel, type PixelRect, type Point2, type Project, type RampStop, type Rgb,
} from './density';

export type ServiceStatus = 'covered' | 'gap' | 'unknown';

/** 一个评估格：格中心、边长（米）与这一类的结论。 */
export type ServiceSample = Point2 & {
  category: string;
  status: ServiceStatus;
  /** 模型最近设施步行距离；只有覆盖格拿它着色。 */
  distanceM: number | null;
  sizeM: number;
};

/** 综合模式：三类都已知的地方按"覆盖了几类"着色，其余是未知。 */
export const SERVICE_COMPOSITE = 'composite';
/** 单类色标上界（米）：服务标准的步行距离。 */
export const SERVICE_DISTANCE_MAX_M = 1000;
/** 综合色标上界：覆盖类别的百分比。 */
export const SERVICE_SCORE_MAX = 100;

/** 与服务灰区同色：服务不足。 */
export const SERVICE_GAP_RGB: Rgb = [107, 114, 128];
/** 淡紫：数据未知（§8.3 图例约定）。 */
export const SERVICE_UNKNOWN_RGB: Rgb = [167, 139, 250];

const DISTANCE_RAMP: RampStop[] = [
  { at: 0.00, rgb: [26, 152, 80] },
  { at: 0.40, rgb: [145, 207, 96] },
  { at: 0.75, rgb: [254, 224, 139] },
  { at: 1.00, rgb: [252, 141, 89] },
];

/** 0 与缺口同灰：三类全缺就是服务不足；满分与距离色带的近端同绿。 */
const SCORE_RAMP: RampStop[] = [
  { at: 0, rgb: SERVICE_GAP_RGB },
  { at: 1 / 3, rgb: [252, 141, 89] },
  { at: 2 / 3, rgb: [254, 224, 139] },
  { at: 1, rgb: [26, 152, 80] },
];

/** 各结论的不透明度：留出底图道路，未知最淡，不与结论抢眼。 */
export const SERVICE_ALPHA = { covered: 0.66, gap: 0.52, unknown: 0.4 } as const;

/** 核半径 / 格边长。 */
const KERNEL_CELLS = 1;
/** 像素核不小于这么多个缓冲格：远景下一个格不到一个缓冲格，也不能在格点之间漏掉。 */
const MIN_KERNEL_BUFFER_CELLS = 1.5;
/** 支持度（覆盖到的面积比例）从这里开始显色；满格处约 1，数据边缘外半格处已降到 0.25。 */
const SUPPORT_LOW = 0.25;
/** 支持度到这里完全显色：最外一排格的边线上约 0.5，热力恰好铺到格边为止。 */
const SUPPORT_FULL = 0.5;

const STATUSES = new Set<ServiceStatus>(['covered', 'gap', 'unknown']);

export function distanceRgb(t: number): Rgb {
  return rampAt(DISTANCE_RAMP, t);
}

export function scoreRgb(t: number): Rgb {
  return rampAt(SCORE_RAMP, t);
}

/** 图例色带：与画布用同一个插值，单类是距离、综合是覆盖类别的比例。 */
export function serviceRampCss(mode: string): string {
  return rampCss(12, mode === SERVICE_COMPOSITE ? scoreRgb : distanceRgb);
}

export type ServiceFieldInput = {
  samples: readonly ServiceSample[];
  /** 单类（类别名）或 {@link SERVICE_COMPOSITE}。 */
  mode: string;
  /** 综合模式要求"均已知"的类别全集。 */
  categories: readonly string[];
  project: Project;
  metersPerPixel: MetersPerPixel;
  /** 计算区域（覆盖物像素）。 */
  rect: PixelRect;
  /** 评估域的像素环；给出时域内无支持处画成未知、域外透明。 */
  domain?: number[][][] | null;
  /** 缓冲格下限；未给出时按核半径自动降采样。 */
  cell?: number;
};

export type ServiceField = {
  cell: number;
  width: number;
  height: number;
  x: number;
  y: number;
  /** 直通（非预乘）RGBA，逐缓冲格。 */
  rgba: Uint8ClampedArray;
  /** 着了色的缓冲格数；为 0 时不必贴图。 */
  painted: number;
  /** 本模式下参与计算的格数。 */
  samples: number;
  /** 本模式的类别里，结论或坐标不可用而被丢弃的格数。 */
  dropped: number;
};

type Grid = { x: number; y: number; cell: number; width: number; height: number };

/** 每类一组累加器：各结论的覆盖度，以及覆盖格的距离加权和。 */
type Accumulator = { covered: Float32Array; gap: Float32Array; unknown: Float32Array; distance: Float32Array };

function accumulator(size: number): Accumulator {
  return { covered: new Float32Array(size), gap: new Float32Array(size),
    unknown: new Float32Array(size), distance: new Float32Array(size) };
}

function smoothstep(low: number, high: number, x: number): number {
  const t = Math.min(1, Math.max(0, (x - low) / (high - low)));
  return t * t * (3 - 2 * t);
}

/**
 * 奇偶规则的扫描线填充：缓冲格中心落在环组内即为 1。
 * 孔洞与分量的处理与画布裁剪同一条规则，所以"域内"与"圈内"说的是同一件事。
 */
export function rasterizeRings(rings: number[][][], grid: Grid): Uint8Array {
  const { x, y, cell, width, height } = grid;
  const mask = new Uint8Array(width * height);
  const crossings: number[] = [];
  for (let j = 0; j < height; j++) {
    const cy = y + (j + 0.5) * cell;
    crossings.length = 0;
    for (const component of rings) {
      for (const ring of component) {
        const n = Math.floor(ring.length / 2);
        for (let k = 0; k < n; k++) {
          const x1 = ring[2 * k], y1 = ring[2 * k + 1];
          const next = (k + 1) % n;
          const x2 = ring[2 * next], y2 = ring[2 * next + 1];
          if ((y1 <= cy) !== (y2 <= cy)) crossings.push(x1 + ((cy - y1) * (x2 - x1)) / (y2 - y1));
        }
      }
    }
    crossings.sort((a, b) => a - b);
    for (let k = 0; k + 1 < crossings.length; k += 2) {
      const from = Math.max(0, Math.ceil((crossings[k] - x) / cell - 0.5));
      const to = Math.min(width - 1, Math.floor((crossings[k + 1] - x) / cell - 0.5));
      for (let i = from; i <= to; i++) mask[j * width + i] = 1;
    }
  }
  return mask;
}

/** 单类：覆盖按距离、缺口灰、未知淡紫，按各自覆盖度预乘混合。 */
function singleColour(acc: Accumulator, index: number): { rgb: Rgb; alpha: number; support: number } | null {
  const covered = acc.covered[index], gap = acc.gap[index], unknown = acc.unknown[index];
  const total = covered + gap + unknown;
  if (!(total > 0)) return null;
  const near = covered > 0 ? distanceRgb(acc.distance[index] / covered / SERVICE_DISTANCE_MAX_M) : SERVICE_GAP_RGB;
  const wc = covered * SERVICE_ALPHA.covered, wg = gap * SERVICE_ALPHA.gap, wu = unknown * SERVICE_ALPHA.unknown;
  const weight = wc + wg + wu;
  return {
    rgb: [
      (near[0] * wc + SERVICE_GAP_RGB[0] * wg + SERVICE_UNKNOWN_RGB[0] * wu) / weight,
      (near[1] * wc + SERVICE_GAP_RGB[1] * wg + SERVICE_UNKNOWN_RGB[1] * wu) / weight,
      (near[2] * wc + SERVICE_GAP_RGB[2] * wg + SERVICE_UNKNOWN_RGB[2] * wu) / weight,
    ],
    alpha: weight / total,
    support: smoothstep(SUPPORT_LOW, SUPPORT_FULL, total),
  };
}

/**
 * 综合：三类在此处都已知的份额 K = Π(已知比例 × 支持度)，按"覆盖了几类"着色；
 * 其余 1 − K 是未知。任何一类在这里没有格，这里就不可能"三类均已知"。
 */
function compositeColour(accs: Accumulator[], index: number): { rgb: Rgb; alpha: number; support: number } | null {
  let known = 1, share = 0, support = 0;
  for (const acc of accs) {
    const covered = acc.covered[index], gap = acc.gap[index];
    const total = covered + gap + acc.unknown[index];
    const present = total > 0 ? smoothstep(SUPPORT_LOW, SUPPORT_FULL, total) : 0;
    support = Math.max(support, present);
    const decided = covered + gap;
    known *= total > 0 ? (decided / total) * present : 0;
    share += decided > 0 ? covered / decided : 0;
  }
  if (!(support > 0) || accs.length === 0) return null;
  const score = scoreRgb(share / accs.length);
  const wk = known * SERVICE_ALPHA.covered, wu = (1 - known) * SERVICE_ALPHA.unknown;
  const weight = wk + wu;
  return {
    rgb: [
      (score[0] * wk + SERVICE_UNKNOWN_RGB[0] * wu) / weight,
      (score[1] * wk + SERVICE_UNKNOWN_RGB[1] * wu) / weight,
      (score[2] * wk + SERVICE_UNKNOWN_RGB[2] * wu) / weight,
    ],
    alpha: weight,
    support,
  };
}

/** 在覆盖物像素空间把格值铺成连续面，返回逐缓冲格的颜色。 */
export function computeServiceField(input: ServiceFieldInput): ServiceField {
  const { rect, project, metersPerPixel } = input;
  const composite = input.mode === SERVICE_COMPOSITE;
  const wanted = composite ? [...input.categories] : [input.mode];
  const slots = new Map(wanted.map((category, slot) => [category, slot]));
  const located: { x: number; y: number; sizePx: number; slot: number; status: ServiceStatus;
    distanceM: number }[] = [];
  let dropped = 0;
  for (const sample of input.samples) {
    const slot = slots.get(sample.category);
    if (slot === undefined) continue;
    const distanceM = sample.distanceM ?? NaN;
    // 覆盖格没有距离就无从着色；丢掉它，不给它编一个颜色。
    if (!STATUSES.has(sample.status) || !(sample.sizeM > 0)
      || (sample.status === 'covered' && !(Number.isFinite(distanceM) && distanceM >= 0))) {
      dropped++;
      continue;
    }
    const pixel = project(sample.lng, sample.lat);
    const mpp = metersPerPixel(sample.lat);
    if (!Number.isFinite(pixel?.x) || !Number.isFinite(pixel?.y) || !(mpp > 0)) { dropped++; continue; }
    located.push({ x: pixel.x, y: pixel.y, sizePx: sample.sizeM / mpp, slot, status: sample.status, distanceM });
  }
  const largest = located.reduce((max, item) => Math.max(max, item.sizePx * KERNEL_CELLS), 0);
  const cell = Math.max(HEAT_CELL_PX, input.cell ?? 0, largest > 0 ? Math.ceil(largest / HEAT_KERNEL_CELLS) : 0);
  const width = Math.max(1, Math.ceil(rect.width / cell));
  const height = Math.max(1, Math.ceil(rect.height / cell));
  const size = width * height;
  const accs = wanted.map(() => accumulator(size));

  for (const item of located) {
    const radius = Math.max(item.sizePx * KERNEL_CELLS, cell * MIN_KERNEL_BUFFER_CELLS);
    // 权重 = 格面积 × 归一化四次核：均匀网格内部 Σw ≈ 1，与格的大小、疏密无关。
    const scale = (item.sizePx * item.sizePx * 3) / (Math.PI * radius * radius);
    const minI = Math.max(0, Math.floor((item.x - radius - rect.x) / cell));
    const maxI = Math.min(width - 1, Math.ceil((item.x + radius - rect.x) / cell));
    const minJ = Math.max(0, Math.floor((item.y - radius - rect.y) / cell));
    const maxJ = Math.min(height - 1, Math.ceil((item.y + radius - rect.y) / cell));
    const acc = accs[item.slot];
    const target = acc[item.status];
    for (let j = minJ; j <= maxJ; j++) {
      const dy = rect.y + (j + 0.5) * cell - item.y;
      for (let i = minI; i <= maxI; i++) {
        const dx = rect.x + (i + 0.5) * cell - item.x;
        const ratio = (dx * dx + dy * dy) / (radius * radius);
        if (ratio >= 1) continue;
        const falloff = 1 - ratio;
        const weight = scale * falloff * falloff;
        const index = j * width + i;
        target[index] += weight;
        if (item.status === 'covered') acc.distance[index] += weight * item.distanceM;
      }
    }
  }

  const grid: Grid = { x: rect.x, y: rect.y, cell, width, height };
  const mask = input.domain ? rasterizeRings(input.domain, grid) : null;
  const rgba = new Uint8ClampedArray(size * 4);
  let painted = 0;
  for (let index = 0; index < size; index++) {
    if (mask && !mask[index]) continue;
    const colour = composite ? compositeColour(accs, index) : singleColour(accs[0], index);
    let rgb: Rgb | null = colour?.rgb ?? null;
    let alpha = colour ? colour.alpha * colour.support : 0;
    if (mask) {
      // 域内缺支持的那部分就是未知：与有结论的部分按不透明度预乘混合。
      const missing = 1 - (colour?.support ?? 0);
      const wu = missing * SERVICE_ALPHA.unknown;
      const total = alpha + wu;
      if (total > 0) {
        const own = rgb ?? SERVICE_UNKNOWN_RGB;
        rgb = [
          (own[0] * alpha + SERVICE_UNKNOWN_RGB[0] * wu) / total,
          (own[1] * alpha + SERVICE_UNKNOWN_RGB[1] * wu) / total,
          (own[2] * alpha + SERVICE_UNKNOWN_RGB[2] * wu) / total,
        ];
      }
      alpha = total;
    }
    if (!rgb || !(alpha > 0)) continue;
    const at = index * 4;
    rgba[at] = Math.round(rgb[0]);
    rgba[at + 1] = Math.round(rgb[1]);
    rgba[at + 2] = Math.round(rgb[2]);
    rgba[at + 3] = Math.round(255 * Math.min(1, alpha));
    if (rgba[at + 3] > 0) painted++;
  }
  return { cell, width, height, x: rect.x, y: rect.y, rgba, painted, samples: located.length, dropped };
}
