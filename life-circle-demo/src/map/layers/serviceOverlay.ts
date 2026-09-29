/** §8：服务覆盖热力覆盖物（后端三态网格 → 渐变面）。
 *
 * 挂载、重绘合并、裁剪与卸载在 canvasOverlay.ts 的共用骨架里；插值与配色在
 * serviceField.ts。这里只管数据的生命周期：评估格、评估域、当前模式与计算圈。
 */
import type { BaiduMapApi, BMapMap, BMapOverlayInstance } from '../baiduMapTypes';
import { createCanvasField, type CanvasFieldOptions, type Geometry } from './canvasOverlay';
import { pixelRings } from './density';
import { SERVICE_COMPOSITE, computeServiceField, type ServiceSample } from './serviceField';

export type ServiceOverlayOptions = CanvasFieldOptions & {
  /** 综合模式要求"均已知"的类别全集。 */
  categories: readonly string[];
  cell?: number;
};

export type ServiceOverlayStats = {
  drawn: boolean;
  mode: string;
  /** 本模式下参与插值的评估格数。 */
  samples: number;
  /** 着色的缓冲格数。 */
  painted: number;
};

export type ServiceOverlay = {
  readonly overlay: BMapOverlayInstance;
  attach(instance: BMapMap): void;
  detach(): void;
  /** 评估格与评估域；域内无格处画成未知，域外透明。 */
  setSamples(samples: readonly ServiceSample[], domain: Geometry | null): void;
  /** 单类（类别名）或综合。 */
  setMode(mode: string): void;
  /** 计算圈面：热力只在圈内出现。 */
  setBoundary(geometry: Geometry | null): void;
  redraw(): void;
  stats(): ServiceOverlayStats;
  destroy(): void;
};

export function createServiceOverlay(api: BaiduMapApi, options: ServiceOverlayOptions): ServiceOverlay | null {
  let samples: readonly ServiceSample[] = [];
  let domain: Geometry | null = null;
  let boundary: Geometry | null = null;
  let mode: string = SERVICE_COMPOSITE;
  const stats = { samples: 0, painted: 0 };

  const field = createCanvasField(api, options, {
    testId: 'service-heat-canvas',
    boundary() {
      stats.samples = 0; stats.painted = 0;
      return samples.length && boundary ? boundary : null;
    },
    compute(frame) {
      let domainRings: number[][][] | null = null;
      if (domain) {
        try {
          domainRings = pixelRings(domain, frame.project);
        } catch {
          // 评估域损坏时退回"只画有格的地方"，不因为掩膜不可用就整层不画。
          domainRings = null;
        }
      }
      const result = computeServiceField({
        samples, mode, categories: options.categories,
        project: frame.project, metersPerPixel: frame.metersPerPixel,
        rect: frame.cover, domain: domainRings?.length ? domainRings : null, cell: options.cell,
      });
      stats.samples = result.samples;
      stats.painted = result.painted;
      return result.painted > 0 ? result : null;
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
      samples = []; domain = null; boundary = null; stats.samples = 0; stats.painted = 0;
    },
    setSamples(next, nextDomain) { samples = next; domain = nextDomain; field.redraw(); },
    setMode(next) { if (next !== mode) { mode = next; field.redraw(); } },
    setBoundary(geometry) { boundary = geometry; field.redraw(); },
    stats: () => ({ drawn: field.drawn(), mode, ...stats }),
  };
}
