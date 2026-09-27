import { describe, expect, it } from 'vitest';
import {
  DENSITY_ALPHA_FLOOR, DENSITY_ALPHA_MAX, DENSITY_FEATHER, DENSITY_SCALE_MAX, DENSITY_UNIT, HEAT_CELL_PX,
  HEAT_KERNEL_RADIUS_M, SINGLE_FACILITY_PEAK, computeDensity, dedupePoints, densityLegendCss, densityRgba,
  intersectRect, kernelWeight, perHectare, pixelRings, rampCss, rampRgb, ringBounds,
} from './density';

const ORIGIN = { lng: 121.513925, lat: 31.313079 };
/** 每度经差的地面米数（按原点纬度）；测试用的解析投影与它一致。 */
const M_PER_DEGREE_LNG = 111_320 * Math.cos((ORIGIN.lat * Math.PI) / 180);
const M_PER_DEGREE_LAT = 111_320;

/** 解析投影：1 米 = 1 像素，屏幕 y 向南为正（与覆盖物像素一致）。 */
const project = (lng: number, lat: number) => ({
  x: (lng - ORIGIN.lng) * M_PER_DEGREE_LNG,
  y: (ORIGIN.lat - lat) * M_PER_DEGREE_LAT,
});
const metersPerPixel = () => 1;

/** 以原点为心、半宽 half 米的正方形环（经纬度），向东/向北为正。 */
function square(half: number, centreLng = ORIGIN.lng, centreLat = ORIGIN.lat): number[][] {
  const toLng = (meters: number) => centreLng + meters / M_PER_DEGREE_LNG;
  const toLat = (meters: number) => centreLat + meters / M_PER_DEGREE_LAT;
  return [
    [toLng(-half), toLat(-half)], [toLng(half), toLat(-half)],
    [toLng(half), toLat(half)], [toLng(-half), toLat(half)], [toLng(-half), toLat(-half)],
  ];
}

function ring(halfLng: number, halfLat: number, holeHalf?: number): number[][][] {
  const outer = square(halfLng);
  if (holeHalf === undefined) return [outer];
  const hole = square(holeHalf).slice().reverse();
  return [outer, hole];
}

const rect = { x: -100, y: -100, width: 200, height: 200 };

function field(points: { lng: number; lat: number; id?: string }[], overrides: Partial<Parameters<typeof computeDensity>[0]> = {}) {
  return computeDensity({ points, project, metersPerPixel, rect, ...overrides });
}

/** 过原点那一行上，最后一个非零缓冲格距原点的地面米数。 */
function supportEdgeMeters(result: ReturnType<typeof computeDensity>, mpp: number): number {
  const row = Math.round((0 - result.y) / result.cell - 0.5);
  let edge = 0;
  for (let i = 0; i < result.width; i++) {
    if (result.values[row * result.width + i] <= 0) continue;
    const offset = Math.abs(result.x + (i + 0.5) * result.cell) * mpp;
    if (offset > edge) edge = offset;
  }
  return edge;
}

/** 离散核的总质量：Σ(个/公顷)×格面积(公顷) 应当接近设施个数。 */
function totalFacilities(result: ReturnType<typeof computeDensity>, mpp: number): number {
  const hectares = (result.cell * mpp) ** 2 / 10_000;
  let total = 0;
  for (const value of result.values) total += value;
  return total * hectares;
}

describe('核权重', () => {
  it('半径处归零，半径外恒为 0，中心处最大', () => {
    expect(kernelWeight(0)).toBeGreaterThan(kernelWeight(60));
    expect(kernelWeight(HEAT_KERNEL_RADIUS_M)).toBe(0);
    expect(kernelWeight(HEAT_KERNEL_RADIUS_M + 1)).toBe(0);
    expect(kernelWeight(Number.NaN)).toBe(0);
  });

  it('积分为 1：单点在其中心的密度估计等于核常数（个/公顷）', () => {
    // 二维四次核在中心为 3/(πr²)，换算成公顷即 3/(π·120²)·10⁴ ≈ 0.663 个/公顷。
    expect(perHectare(kernelWeight(0))).toBeCloseTo(0.6631, 3);
    expect(DENSITY_UNIT).toBe('个/公顷');
  });
});

describe('密度场', () => {
  it('核半径固定 120 米：缩放只改变像素核，不改变地面核', () => {
    const point = [{ lng: ORIGIN.lng, lat: ORIGIN.lat }];
    // 视口比核大，支撑边界才落在视口内部；两种像素尺度：米/像素 1 与 4。
    const wide = { x: -200, y: -200, width: 400, height: 400 };
    const near = field(point, { metersPerPixel: () => 1, rect: wide });
    const far = field(point, { metersPerPixel: () => 4, rect: wide });
    // 支撑边界都落在 120 米处：两次计算的支撑半径相同，误差不超过一格。
    const edgeNear = supportEdgeMeters(near, 1);
    const edgeFar = supportEdgeMeters(far, 4);
    expect(edgeNear).toBeLessThanOrEqual(120);
    expect(edgeFar).toBeLessThanOrEqual(120);
    expect(120 - edgeNear).toBeLessThanOrEqual(near.cell * 1);
    expect(120 - edgeFar).toBeLessThanOrEqual(far.cell * 4);
    expect(Math.abs(edgeNear - edgeFar)).toBeLessThanOrEqual(far.cell * 4);
    // 总质量都等于一个设施：核是同一个归一化核，不是被放大或缩小的核。
    expect(totalFacilities(near, 1)).toBeCloseTo(1, 1);
    expect(totalFacilities(far, 4)).toBeCloseTo(1, 1);
  });

  it('圈外的格子不参与计算', () => {
    const point = [{ lng: ORIGIN.lng, lat: ORIGIN.lat }];
    // 视口整体落在核之外，场必须为空，而不是把核平移到视口里。
    const outside = computeDensity({ points: point, project, metersPerPixel: () => 4,
      rect: { x: 200, y: 200, width: 40, height: 40 } });
    expect(outside.max).toBe(0);
    expect(outside.points).toBe(1);
  });

  it('同 id 的设施等权参与一次，不因重复记录叠加变亮', () => {
    const once = field([{ id: 'a', lng: ORIGIN.lng, lat: ORIGIN.lat }]);
    const twice = field([{ id: 'a', lng: ORIGIN.lng, lat: ORIGIN.lat },
      { id: 'a', lng: ORIGIN.lng, lat: ORIGIN.lat }]);
    expect(twice.max).toBeCloseTo(once.max, 8);
    expect(twice.points).toBe(1);
    expect(dedupePoints([{ id: 'a', lng: 1, lat: 1 }, { id: 'a', lng: 2, lat: 2 }])).toHaveLength(1);
    // 没有 id 的按坐标去重。
    expect(dedupePoints([{ lng: 1, lat: 1 }, { lng: 1, lat: 1 }])).toHaveLength(1);
  });

  it('聚集的点比分散的点更亮，且不加权单个设施的权重', () => {
    const dense = field([{ lng: ORIGIN.lng, lat: ORIGIN.lat }, { lng: ORIGIN.lng + 0.0002, lat: ORIGIN.lat }]);
    const sparse = field([{ lng: ORIGIN.lng, lat: ORIGIN.lat }, { lng: ORIGIN.lng + 0.01, lat: ORIGIN.lat }]);
    expect(dense.max).toBeGreaterThan(sparse.max);
    expect(sparse.max).toBeGreaterThan(0);
  });

  it('没有 POI 时场为空，不生成任何热力', () => {
    const empty = field([]);
    expect(empty.max).toBe(0);
    expect(empty.points).toBe(0);
    expect([...empty.values].every(value => value === 0)).toBe(true);
  });

  it('坐标不可用的点被丢弃并计数，其余点不受影响', () => {
    const result = field([{ lng: ORIGIN.lng, lat: ORIGIN.lat }, { lng: Number.NaN, lat: ORIGIN.lat }]);
    expect(result.points).toBe(1);
    expect(result.dropped).toBe(1);
    expect(result.max).toBeGreaterThan(0);
  });

  it('缓冲格随核半径降采样，重绘代价有界', () => {
    const point = [{ lng: ORIGIN.lng, lat: ORIGIN.lat }];
    // 大比例尺下像素核很大（120 米 ÷ 0.25 米/像素 = 480 像素），必须降采样。
    const coarse = field(point, { metersPerPixel: () => 0.25 });
    expect(coarse.cell).toBeGreaterThan(HEAT_CELL_PX);
    // 降采样后核在半径方向仍然覆盖不超过 48 格，单帧代价有界。
    expect(120 / 0.25 / coarse.cell).toBeLessThanOrEqual(48);
    // 常规比例尺不降采样：分子像素级的核用 2 像素的格子画。
    expect(field(point, { metersPerPixel: () => 4 }).cell).toBe(HEAT_CELL_PX);
    expect(field(point, { metersPerPixel: () => 2 }).cell).toBe(HEAT_CELL_PX);
  });
});

describe('色标', () => {
  it('固定上下界：数据变化不移动色标，超出即截断', () => {
    expect(rampRgb(-1)).toEqual(rampRgb(0));
    expect(rampRgb(2)).toEqual(rampRgb(1));
    expect(densityRgba(DENSITY_SCALE_MAX * 10)).toEqual(densityRgba(DENSITY_SCALE_MAX));
  });

  it('零密度完全透明，有贡献的最低密度仍然可见，最高密度不遮挡底图', () => {
    expect(densityRgba(0)[3]).toBe(0);
    expect(densityRgba(-1)[3]).toBe(0);
    expect(densityRgba(Number.NaN)[3]).toBe(0);
    // 单个设施的峰值（≈0.66 个/公顷）在浅色底图上要看得出：不透明度不低于 0.4。
    expect(SINGLE_FACILITY_PEAK).toBeCloseTo(0.663, 3);
    expect(densityRgba(SINGLE_FACILITY_PEAK)[3]).toBeGreaterThanOrEqual(Math.round(255 * 0.4));
    // 离单个设施 0.9 倍核半径处（≈0.024 个/公顷）仍可见，只是在羽化带里变淡。
    const nearEdge = SINGLE_FACILITY_PEAK * (1 - 0.9 ** 2) ** 2;
    expect(densityRgba(nearEdge)[3]).toBeGreaterThan(0);
    expect(densityRgba(nearEdge)[3]).toBeLessThan(densityRgba(DENSITY_FEATHER)[3]);
    // 羽化带之外一律不低于下限，且随密度单调增加。
    expect(densityRgba(DENSITY_FEATHER)[3]).toBe(Math.round(255 * (DENSITY_ALPHA_FLOOR
      + (DENSITY_ALPHA_MAX - DENSITY_ALPHA_FLOOR) * Math.sqrt(DENSITY_FEATHER / DENSITY_SCALE_MAX))));
    const alphas = [0.05, 0.3, SINGLE_FACILITY_PEAK, 1, 2, 3, DENSITY_SCALE_MAX].map(d => densityRgba(d)[3]);
    expect(alphas).toEqual([...alphas].sort((a, b) => a - b));
    // 顶端不遮挡底图道路。
    expect(densityRgba(DENSITY_SCALE_MAX)[3]).toBe(Math.round(255 * DENSITY_ALPHA_MAX));
  });

  it('颜色只由密度决定：不透明度的调整不改变色相读数', () => {
    for (const d of [0.01, SINGLE_FACILITY_PEAK, 2, DENSITY_SCALE_MAX]) {
      expect(densityRgba(d).slice(0, 3)).toEqual(rampRgb(d / DENSITY_SCALE_MAX));
    }
  });

  it('图例渐变连不透明度一起取自 densityRgba，左端从羽化后的第一档开始', () => {
    const css = densityLegendCss(4);
    const [r, g, b, a] = densityRgba(DENSITY_SCALE_MAX);
    expect(css).toContain(`rgba(${r}, ${g}, ${b}, ${(a / 255).toFixed(3)}) 100%`);
    const low = densityRgba(DENSITY_FEATHER);
    expect(css).toContain(`rgba(${low[0]}, ${low[1]}, ${low[2]}, ${(low[3] / 255).toFixed(3)}) 0%`);
  });

  it('图例渐变与色带取值同源', () => {
    expect(rampCss(2)).toContain('rgb(44, 127, 184) 0%');
    expect(rampCss(2)).toContain('rgb(227, 26, 28) 100%');
  });
});

describe('几何：环、孔洞与分量', () => {
  it('分量之间不连接，孔洞留在本分量的环序列里', () => {
    const geometry = {
      type: 'MultiPolygon',
      coordinates: [
        ring(40, 40, 20),
        ring(10, 10),
      ],
    };
    const rings = pixelRings(geometry, project);
    expect(rings).toHaveLength(2);
    expect(rings[0]).toHaveLength(2);  // 外环 + 内孔
    expect(rings[1]).toHaveLength(1);
    // 每个环都必须是闭合的像素点串，分量之间没有任何共享点。
    for (const component of rings) {
      for (const item of component) {
        expect(item).toHaveLength(10);
        expect([item[0], item[1]]).toEqual([item[8], item[9]]);
        expect(item.every(Number.isFinite)).toBe(true);
      }
    }
  });

  it('孔洞在像素空间仍然比外环小，可以整块挖掉', () => {
    const [outer, hole] = pixelRings({ type: 'Polygon', coordinates: ring(40, 40, 20) }, project)[0];
    const width = (item: number[]) => Math.max(...item.filter((_, index) => index % 2 === 0))
      - Math.min(...item.filter((_, index) => index % 2 === 0));
    expect(width(hole)).toBeLessThan(width(outer));
  });

  it('形状不可读时抛错，调用方据此跳过本帧', () => {
    expect(() => pixelRings({ type: 'Polygon', coordinates: [[[1, 2], [1, 'x']]] }, project)).toThrow();
    expect(pixelRings(null, project)).toEqual([]);
    expect(pixelRings({ type: 'Point', coordinates: [1, 2] }, project)).toEqual([]);
  });

  it('包围盒与视口求交，圈外一半不参与计算', () => {
    const bounds = ringBounds(pixelRings({ type: 'Polygon', coordinates: ring(50, 50) }, project))!;
    expect(bounds.minX).toBeCloseTo(-50, 6);
    expect(bounds.minY).toBeCloseTo(-50, 6);
    expect(bounds.maxX).toBeCloseTo(50, 6);
    expect(bounds.maxY).toBeCloseTo(50, 6);
    const cover = intersectRect(rect, bounds)!;
    expect(cover.width).toBeCloseTo(100, 6);
    expect(cover.height).toBeCloseTo(100, 6);
    expect(cover.x).toBeCloseTo(-50, 6);
    expect(intersectRect({ x: 500, y: 500, width: 10, height: 10 }, bounds)).toBeNull();
    expect(ringBounds([])).toBeNull();
  });
});
