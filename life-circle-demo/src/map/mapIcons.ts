/** 地图标注图标工厂：用 canvas 绘制彩色圆点图标（dataURL），风格对齐示意地图的 SVG 标记。 */

import type { BaiduMapApi, BMapIcon } from './baiduMapTypes';

export type DotIconSpec = {
  /** 主色：边框/文字/中心点。 */
  color: string;
  /** 圆内文字（设施符号或 A/B/C 字母）；缺省画实心点。 */
  text?: string;
  textColor?: string;
  /** 分析中心点样式：多层同心圆 + 光晕。 */
  layered?: boolean;
  /** 实心样式：主色填充 + 白色文字，用于选中态等需要突出的标记。 */
  filled?: boolean;
};

/** 图标外框边长（含阴影留白）：带字的合并点、选中的单点、普通单点、分析中心。 */
const DIAMETER = { marker: 30, selected: 26, sample: 18, center: 28 };
const FONT = 'Bahnschrift, "Segoe UI", "Microsoft YaHei", sans-serif';

/** 白边实心圆，带一圈很淡的投影，让点在浅色底图和热力上都立得住。 */
function disc(ctx: CanvasRenderingContext2D, c: number, r: number, color: string, ring = 2) {
  ctx.save();
  ctx.shadowColor = 'rgba(20, 28, 32, 0.35)'; ctx.shadowBlur = 2.5; ctx.shadowOffsetY = 0.5;
  ctx.beginPath(); ctx.arc(c, c, r, 0, Math.PI * 2); ctx.fillStyle = '#fff'; ctx.fill();
  ctx.restore();
  ctx.beginPath(); ctx.arc(c, c, r - ring, 0, Math.PI * 2); ctx.fillStyle = color; ctx.fill();
}

/** 绘制并返回 BMapGL 图标；canvas 或 Icon/Size 构造器不可用（旧版脚本、测试替身）时返回 undefined，Marker 回退为默认图标。 */
export function createDotIcon(api: BaiduMapApi, spec: DotIconSpec): BMapIcon | undefined {
  if (typeof api.Icon !== 'function' || typeof api.Size !== 'function') return undefined;
  const size = spec.layered ? DIAMETER.center : spec.text ? DIAMETER.marker
    : spec.filled ? DIAMETER.selected : DIAMETER.sample;
  const canvas = document.createElement('canvas');
  canvas.width = size * 2;
  canvas.height = size * 2;
  const ctx = canvas.getContext('2d');
  if (!ctx) return undefined;
  ctx.scale(2, 2);
  const c = size / 2;
  if (spec.layered) {
    // 中心点：淡色光晕 + 白边实心圆 + 白色圆心，像一枚图钉的俯视。
    ctx.beginPath(); ctx.arc(c, c, c - 1, 0, Math.PI * 2); ctx.globalAlpha = 0.16; ctx.fillStyle = spec.color; ctx.fill();
    ctx.globalAlpha = 1;
    disc(ctx, c, 8, spec.color, 2.5);
    ctx.beginPath(); ctx.arc(c, c, 2, 0, Math.PI * 2); ctx.fillStyle = '#fff'; ctx.fill();
  } else if (spec.text) {
    // 合并点与带符号的标记：实心、白字；未选中的只是描边更细，不再画成空心圈。
    disc(ctx, c, c - 2.5, spec.color, spec.filled ? 2 : 1.5);
    ctx.fillStyle = spec.filled ? '#fff' : spec.textColor ?? '#fff';
    ctx.font = `600 ${Math.round(size * 0.44)}px ${FONT}`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(spec.text, c, c + 0.5);
  } else if (spec.filled) {
    // 选中的单点：外加一圈同色光晕。
    ctx.beginPath(); ctx.arc(c, c, c - 1, 0, Math.PI * 2);
    ctx.globalAlpha = 0.22; ctx.fillStyle = spec.color; ctx.fill(); ctx.globalAlpha = 1;
    disc(ctx, c, 7.5, spec.color);
  } else {
    disc(ctx, c, c - 3, spec.color);
  }
  return new api.Icon(canvas.toDataURL('image/png'), new api.Size(size, size), { anchor: new api.Size(c, c) });
}
