/** 服务覆盖面的数值约束：§8.1 的"采样越密不越亮""未知不着距离色""不跨格外推"。
 *
 * 投影取最简单的线性替身：经纬度直接当作米，再除以米/像素。这样格的位置、核半径与
 * 缩放都能用整数米写出来，断言说的就是地面上的事。
 */
import { describe, expect, it } from 'vitest';
import {
  SERVICE_ALPHA, SERVICE_COMPOSITE, SERVICE_GAP_RGB, SERVICE_UNKNOWN_RGB,
  computeServiceField, distanceRgb, rasterizeRings, scoreRgb,
  type ServiceField, type ServiceSample, type ServiceStatus,
} from './serviceField';

const CATEGORIES = ['shopping', 'medical', 'education'];

/** 左下角在 (x0, y0) 米、nx × ny 个边长 size 米的格。 */
function block(x0: number, y0: number, nx: number, ny: number, size: number, category: string,
  status: ServiceStatus, distanceM: number | null = 300): ServiceSample[] {
  const samples: ServiceSample[] = [];
  for (let i = 0; i < nx; i++) {
    for (let j = 0; j < ny; j++) {
      samples.push({ lng: x0 + (i + 0.5) * size, lat: y0 + (j + 0.5) * size, category, status, distanceM, sizeM: size });
    }
  }
  return samples;
}

function field(samples: ServiceSample[], options: { mode?: string; mpp?: number; domain?: number[][][] } = {}) {
  const mpp = options.mpp ?? 1;
  // 计算区域固定为地面上的 −200…1400 米见方，缩放只改变它占多少像素。
  const rect = { x: -200 / mpp, y: -200 / mpp, width: 1600 / mpp, height: 1600 / mpp };
  return computeServiceField({
    samples, mode: options.mode ?? 'shopping', categories: CATEGORIES,
    project: (lng, lat) => ({ x: lng / mpp, y: lat / mpp }),
    metersPerPixel: () => mpp, rect,
    domain: options.domain ? options.domain.map(component => component.map(ring => ring.map(v => v / mpp))) : null,
  });
}

/** 地面上 (x, y) 米处的缓冲格颜色。 */
function at(result: ServiceField, x: number, y: number, mpp = 1): [number, number, number, number] {
  const i = Math.floor((x / mpp - result.x) / result.cell);
  const j = Math.floor((y / mpp - result.y) / result.cell);
  const index = (j * result.width + i) * 4;
  const d = result.rgba;
  return [d[index], d[index + 1], d[index + 2], d[index + 3]];
}

const alphaOf = (share: number) => Math.round(255 * share);

describe('服务覆盖面：归一化插值而不是核叠加', () => {
  it('同一片覆盖区从 50 米格加密到 25 米格，颜色与透明度都不变', () => {
    const coarse = field(block(0, 0, 10, 10, 50, 'shopping', 'covered', 300));
    const fine = field(block(0, 0, 20, 20, 25, 'shopping', 'covered', 300));
    const expected = [...distanceRgb(0.3), alphaOf(SERVICE_ALPHA.covered)];
    for (const [x, y] of [[250, 250], [120, 380], [260, 90]]) {
      expect(at(coarse, x, y)).toEqual(expected);
      expect(at(fine, x, y)).toEqual(expected);
    }
  });

  it('覆盖格按距离着色：近处绿、标准边缘橙', () => {
    const near = field(block(0, 0, 6, 6, 50, 'shopping', 'covered', 0));
    const far = field(block(0, 0, 6, 6, 50, 'shopping', 'covered', 1000));
    expect(at(near, 150, 150).slice(0, 3)).toEqual(distanceRgb(0));
    expect(at(far, 150, 150).slice(0, 3)).toEqual(distanceRgb(1));
    expect(distanceRgb(0)).not.toEqual(distanceRgb(1));
  });

  it('未知格即使带着模型距离也不着距离色', () => {
    const result = field(block(0, 0, 6, 6, 50, 'shopping', 'unknown', 0));
    expect(at(result, 150, 150)).toEqual([...SERVICE_UNKNOWN_RGB, alphaOf(SERVICE_ALPHA.unknown)]);
  });

  it('缺口是灰色，与服务灰区同色', () => {
    const result = field(block(0, 0, 6, 6, 50, 'shopping', 'gap', 1400));
    expect(at(result, 150, 150)).toEqual([...SERVICE_GAP_RGB, alphaOf(SERVICE_ALPHA.gap)]);
  });

  it('只在相邻格之间过渡：离交界超过一格处仍是本格的结论，不向外推', () => {
    const samples = [...block(0, 0, 10, 10, 50, 'shopping', 'covered', 200),
      ...block(500, 0, 10, 10, 50, 'shopping', 'gap', null)];
    const result = field(samples);
    expect(at(result, 440, 250).slice(0, 3)).toEqual(distanceRgb(0.2));
    expect(at(result, 560, 250).slice(0, 3)).toEqual(SERVICE_GAP_RGB);
    // 交界处是两者之间的过渡色，不是任何一边的纯色：正方形台阶就在这里被抹平。
    const edge = at(result, 500, 250);
    expect(edge.slice(0, 3)).not.toEqual(distanceRgb(0.2));
    expect(edge.slice(0, 3)).not.toEqual(SERVICE_GAP_RGB);
    // 数据以外一格多就没有颜色：没有评估域时，没有格的地方不画。
    expect(at(result, -80, 250)[3]).toBe(0);
    expect(at(result, 250, 1080)[3]).toBe(0);
  });

  it('只画选中的那一类；覆盖格缺距离时丢弃而不是编一个颜色', () => {
    const samples = [...block(0, 0, 4, 4, 50, 'medical', 'covered', 100),
      { lng: 25, lat: 25, category: 'shopping', status: 'covered' as const, distanceM: null, sizeM: 50 }];
    const result = field(samples);
    expect(result.samples).toBe(0);
    expect(result.dropped).toBe(1);
    expect(result.painted).toBe(0);
  });
});

describe('综合模式：三类均已知才给分', () => {
  const all = (x0: number, statuses: [ServiceStatus, ServiceStatus, ServiceStatus]) =>
    CATEGORIES.flatMap((category, index) => block(x0, 0, 6, 6, 50, category, statuses[index], 300));

  it('三类全覆盖为满分色，两类覆盖落在 2/3 处', () => {
    const result = field([...all(0, ['covered', 'covered', 'covered']), ...all(600, ['covered', 'covered', 'gap'])],
      { mode: SERVICE_COMPOSITE });
    expect(at(result, 150, 150)).toEqual([...scoreRgb(1), alphaOf(SERVICE_ALPHA.covered)]);
    expect(at(result, 750, 150).slice(0, 3)).toEqual(scoreRgb(2 / 3));
    // 全缺是 0 分，与缺口同灰。
    expect(scoreRgb(0)).toEqual(SERVICE_GAP_RGB);
  });

  it('任一类未知或缺格，这里就只能是未知', () => {
    const unknown = field(all(0, ['covered', 'unknown', 'covered']), { mode: SERVICE_COMPOSITE });
    expect(at(unknown, 150, 150)).toEqual([...SERVICE_UNKNOWN_RGB, alphaOf(SERVICE_ALPHA.unknown)]);
    const missing = field([...block(0, 0, 6, 6, 50, 'shopping', 'covered'), ...block(0, 0, 6, 6, 50, 'medical', 'covered')],
      { mode: SERVICE_COMPOSITE });
    expect(at(missing, 150, 150).slice(0, 3)).toEqual(SERVICE_UNKNOWN_RGB);
  });

  it('各类细分程度不同也能对齐：粗格与细格混排不改变结论', () => {
    const samples = [...block(0, 0, 6, 6, 50, 'shopping', 'covered'),
      ...block(0, 0, 12, 12, 25, 'medical', 'covered'), ...block(0, 0, 6, 6, 50, 'education', 'covered')];
    const result = field(samples, { mode: SERVICE_COMPOSITE });
    expect(at(result, 150, 150)).toEqual([...scoreRgb(1), alphaOf(SERVICE_ALPHA.covered)]);
  });
});

describe('评估域', () => {
  const domain = [[[0, 0, 1000, 0, 1000, 500, 0, 500]]];

  it('域内没有格的地方是未知，域外即使挨着格也透明', () => {
    const samples = [...block(0, 0, 10, 10, 50, 'shopping', 'covered', 300),
      ...block(1000, 0, 2, 10, 50, 'shopping', 'covered', 300)];
    const result = field(samples, { domain });
    expect(at(result, 250, 250)).toEqual([...distanceRgb(0.3), alphaOf(SERVICE_ALPHA.covered)]);
    expect(at(result, 800, 250)).toEqual([...SERVICE_UNKNOWN_RGB, alphaOf(SERVICE_ALPHA.unknown)]);
    expect(at(result, 1050, 250)[3]).toBe(0);
    expect(at(result, 250, 700)[3]).toBe(0);
  });

  it('扫描线按奇偶规则填充：孔洞不填', () => {
    const grid = { x: 0, y: 0, cell: 10, width: 10, height: 10 };
    const mask = rasterizeRings([[[0, 0, 100, 0, 100, 100, 0, 100], [30, 30, 70, 30, 70, 70, 30, 70]]], grid);
    expect(mask[5 * 10 + 5]).toBe(0);
    expect(mask[1 * 10 + 1]).toBe(1);
    expect(mask.reduce((sum, v) => sum + v, 0)).toBe(100 - 16);
  });
});

describe('缩放', () => {
  it('核半径是地面上的格边长：放大一倍后同一地点的颜色不变', () => {
    const samples = [...block(0, 0, 10, 10, 50, 'shopping', 'covered', 200),
      ...block(500, 0, 10, 10, 50, 'shopping', 'gap', null)];
    const near = field(samples, { mpp: 0.5 });
    const far = field(samples, { mpp: 1 });
    for (const x of [250, 480, 500, 520, 750]) {
      const a = at(near, x, 250, 0.5);
      const b = at(far, x, 250, 1);
      for (let k = 0; k < 4; k++) expect(Math.abs(a[k] - b[k])).toBeLessThanOrEqual(6);
    }
  });
});
