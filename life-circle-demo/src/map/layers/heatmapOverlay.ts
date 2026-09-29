/** §8：挂在真实 BMapGL 上的自定义 Canvas 热力覆盖物（设施密度模式）。
 *
 * 挂载、重绘合并、裁剪与卸载都在 canvasOverlay.ts 的共用骨架里；数值与几何在
 * density.ts。这里只把两者接起来：设施点 → 核密度 → 固定色带。
 */
import type { BaiduMapApi, BMapMap, BMapOverlayInstance } from '../baiduMapTypes';
import { createCanvasField, type CanvasFieldOptions, type Geometry } from './canvasOverlay';
import {
  DENSITY_SCALE_MAX, HEAT_KERNEL_RADIUS_M, computeDensity, densityRgba, dedupePoints, type Point2,
} from './density';

export type DensityOverlayOptions = CanvasFieldOptions & {
  radiusM?: number;
  scaleMax?: number;
  cell?: number;
};

export type DensityOverlayStats = {
  /** 是否已经画出过热力：无设施、无边界或画布不可用时为 false。 */
  drawn: boolean;
  points: number;
  max: number;
};

export type DensityOverlay = {
  /** 交给 map.addOverlay 的实例（继承 SDK 的覆盖物基类）。 */
  readonly overlay: BMapOverlayInstance;
  /** 幂等挂载；已经挂在图上时什么也不做。 */
  attach(instance: BMapMap): void;
  /** 摘除；由 SDK 因 clearOverlays 调用 remove 时同样会走到这里。 */
  detach(): void;
  setFacilities(points: ReadonlyArray<Point2 & { id?: string }>): void;
  /** 计算圈面；设施只在圈内参与着色（§8.2 步骤 6）。 */
  setBoundary(geometry: Geometry | null): void;
  redraw(): void;
  stats(): DensityOverlayStats;
  destroy(): void;
};

/**
 * 建立热力覆盖物；SDK 缺少自定义覆盖物基类时返回 null，界面据此提示降级。
 */
export function createDensityOverlay(api: BaiduMapApi, options: DensityOverlayOptions = {}): DensityOverlay | null {
  const radiusM = options.radiusM ?? HEAT_KERNEL_RADIUS_M;
  const scaleMax = options.scaleMax ?? DENSITY_SCALE_MAX;
  let facilities: Point2[] = [];
  let boundary: Geometry | null = null;
  const stats = { points: 0, max: 0 };

  const field = createCanvasField(api, options, {
    testId: 'facility-density-canvas',
    boundary() {
      stats.points = facilities.length;
      // 没有真实设施就不画：不在圈内补随机热力点（§8.2）。
      if (!facilities.length || !boundary) { stats.max = 0; return null; }
      return boundary;
    },
    compute(frame) {
      const density = computeDensity({
        points: facilities, project: frame.project, metersPerPixel: frame.metersPerPixel,
        rect: frame.cover, radiusM, cell: options.cell,
      });
      stats.max = density.max;
      if (!(density.max > 0)) return null;
      // 密度先查色带成一张颜色图，再由骨架整张贴上去。
      const rgba = new Uint8ClampedArray(density.values.length * 4);
      for (let index = 0; index < density.values.length; index++) {
        const [r, g, b, a] = densityRgba(density.values[index], scaleMax);
        const at = index * 4;
        rgba[at] = r; rgba[at + 1] = g; rgba[at + 2] = b; rgba[at + 3] = a;
      }
      return { cell: density.cell, width: density.width, height: density.height, x: density.x, y: density.y, rgba };
    },
  });
  if (!field) return null;

  return {
    overlay: field.overlay,
    attach: field.attach,
    detach: field.detach,
    redraw: field.redraw,
    destroy() {
      field.destroy();
      facilities = []; boundary = null; stats.points = 0; stats.max = 0;
    },
    setFacilities(points) { facilities = dedupePoints(points); field.redraw(); },
    setBoundary(geometry) { boundary = geometry; field.redraw(); },
    stats: () => ({ drawn: field.drawn(), ...stats }),
  };
}
