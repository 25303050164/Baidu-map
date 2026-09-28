/** 水系标注覆盖物：已核实河道、补录水体、复核范围、底图误绘与数据冲突。
 *
 * 与两种热力共用 canvasOverlay.ts 的骨架，但只画矢量：面按样式描边、填色，冲突与底图
 * 误绘再加斜线。画布压在热力之上 —— 标注要在覆盖色之上才读得出"这片覆盖下面的底图水面
 * 是画错的"或"这里来源冲突、没有结论"；它不接收指针事件，拖动与选点仍归地图。
 *
 * BMapGL 的多边形填不了图案，斜线只能自己画；画在自己的画布上还有一个好处：热力开关、
 * 换类别都不会把标注摘掉或压住。
 */
import type { BaiduMapApi, BMapMap, BMapOverlayInstance } from '../baiduMapTypes';
import { createCanvasField, type CanvasFieldOptions, type Geometry } from './canvasOverlay';
import { pixelRings, ringBounds, type PixelRect } from './density';

export type WaterAnnotationStyle = {
  stroke: string;
  fill: string | null;
  hatch: string | null;
  dash: number[];
};

export type WaterAnnotation = {
  key: string;
  geometry: Geometry;
  style: WaterAnnotationStyle;
  /** 面内的文字标注：锚点是后端给的面内代表点，只在面放得下字时才标。 */
  label?: { text: string; lng: number; lat: number } | null;
};

/** 斜线间距与线宽（CSS 像素）：与缩放无关，任何级别都读得出是"标注"而不是底图。 */
export const HATCH_SPACING_PX = 7;
const HATCH_WIDTH_PX = 1.4;
const STROKE_WIDTH_PX = 1.6;
/** 面在屏幕上至少这么大才标字：再小，字会盖住整块面和旁边的底图注记。 */
export const LABEL_MIN_EXTENT_PX = 56;
const LABEL_FONT = '600 12px Inter, "Microsoft YaHei", sans-serif';

export type WaterOverlayStats = { drawn: boolean; shapes: number; visible: number; labels: number };

export type WaterOverlay = {
  readonly overlay: BMapOverlayInstance;
  attach(instance: BMapMap): void;
  detach(): void;
  setAnnotations(items: readonly WaterAnnotation[]): void;
  redraw(): void;
  stats(): WaterOverlayStats;
  destroy(): void;
};

function tracePath(run: CanvasRenderingContext2D, rings: number[][][], view: PixelRect) {
  run.beginPath();
  for (const component of rings) {
    for (const ring of component) {
      if (ring.length < 6) continue;
      run.moveTo(ring[0] - view.x, ring[1] - view.y);
      for (let i = 2; i + 1 < ring.length; i += 2) run.lineTo(ring[i] - view.x, ring[i + 1] - view.y);
      run.closePath();
    }
  }
}

/** 一个面：填色、按奇偶规则裁剪后画 45° 斜线、再描边。返回是否落在视口里。 */
export function drawAnnotation(run: CanvasRenderingContext2D, rings: number[][][], view: PixelRect,
  style: WaterAnnotationStyle): boolean {
  const bounds = ringBounds(rings);
  return !!bounds && paintAnnotation(run, rings, view, style, bounds);
}

type Bounds = NonNullable<ReturnType<typeof ringBounds>>;

function paintAnnotation(run: CanvasRenderingContext2D, rings: number[][][], view: PixelRect,
  style: WaterAnnotationStyle, bounds: Bounds): boolean {
  if (bounds.minX > view.x + view.width || bounds.minY > view.y + view.height
    || bounds.maxX < view.x || bounds.maxY < view.y) return false;
  run.save();
  try {
    tracePath(run, rings, view);
    if (style.fill) {
      run.fillStyle = style.fill;
      run.fill('evenodd');
    }
    if (style.hatch) {
      run.save();
      tracePath(run, rings, view);
      run.clip('evenodd');
      run.beginPath();
      // 只在面与视口的交集里铺斜线：放大到 18 级时一个面可能比视口大很多倍。
      const left = Math.max(bounds.minX, view.x) - view.x;
      const top = Math.max(bounds.minY, view.y) - view.y;
      const right = Math.min(bounds.maxX, view.x + view.width) - view.x;
      const bottom = Math.min(bounds.maxY, view.y + view.height) - view.y;
      // 斜线 x + y = c；c 取间距的整数倍，平移时不抖动。
      const start = Math.floor((left + top) / HATCH_SPACING_PX) * HATCH_SPACING_PX;
      for (let c = start; c <= right + bottom; c += HATCH_SPACING_PX) {
        run.moveTo(c - top, top);
        run.lineTo(c - bottom, bottom);
      }
      run.strokeStyle = style.hatch;
      run.lineWidth = HATCH_WIDTH_PX;
      run.setLineDash([]);
      run.stroke();
      run.restore();
      tracePath(run, rings, view);
    }
    run.strokeStyle = style.stroke;
    run.lineWidth = STROKE_WIDTH_PX;
    run.setLineDash(style.dash);
    run.stroke();
  } finally {
    run.restore();
  }
  return true;
}

/** 白底描边的文字，底图再花也读得出来。 */
function drawLabel(run: CanvasRenderingContext2D, text: string, x: number, y: number, color: string) {
  run.save();
  run.font = LABEL_FONT;
  run.textAlign = 'center';
  run.textBaseline = 'middle';
  run.lineJoin = 'round';
  run.lineWidth = 3.5;
  run.strokeStyle = 'rgba(255, 255, 255, 0.95)';
  run.strokeText(text, x, y);
  run.fillStyle = color;
  run.fillText(text, x, y);
  run.restore();
}

export function createWaterOverlay(api: BaiduMapApi, options: CanvasFieldOptions): WaterOverlay | null {
  let items: readonly WaterAnnotation[] = [];
  const stats = { shapes: 0, visible: 0, labels: 0 };

  const field = createCanvasField(api, options, {
    testId: 'water-annotation-canvas',
    layer: 'water',
    above: true,
    boundary: () => null,
    compute: () => null,
    vector(run, frame) {
      stats.shapes = items.length;
      stats.visible = 0;
      stats.labels = 0;
      // 字最后写：先画的面不会压住后写的字。
      const labels: Array<[string, number, number, string]> = [];
      for (const item of items) {
        let rings: number[][][];
        try {
          rings = pixelRings(item.geometry, frame.project);
        } catch {
          // 一个面投影失败只丢它自己；其余标注照画，数量在 stats 里对得上。
          continue;
        }
        const bounds = ringBounds(rings);
        if (!bounds || !paintAnnotation(run, rings, frame.view, item.style, bounds)) continue;
        stats.visible++;
        const extent = Math.max(bounds.maxX - bounds.minX, bounds.maxY - bounds.minY);
        if (!item.label || extent < LABEL_MIN_EXTENT_PX) continue;
        const at = frame.project(item.label.lng, item.label.lat);
        const x = at.x - frame.view.x, y = at.y - frame.view.y;
        if (x >= 0 && y >= 0 && x <= frame.view.width && y <= frame.view.height) {
          labels.push([item.label.text, x, y, item.style.stroke]);
        }
      }
      for (const [text, x, y, color] of labels) drawLabel(run, text, x, y, color);
      stats.labels = labels.length;
      return stats.visible > 0;
    },
  });
  if (!field) return null;

  return {
    overlay: field.overlay,
    attach: field.attach,
    detach: field.detach,
    redraw: field.redraw,
    destroy() { field.destroy(); items = []; stats.shapes = 0; stats.visible = 0; stats.labels = 0; },
    setAnnotations(next) { items = next; field.redraw(); },
    stats: () => ({ drawn: field.drawn(), ...stats }),
  };
}
