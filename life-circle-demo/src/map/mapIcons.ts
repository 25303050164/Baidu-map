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

const DIAMETER = { marker: 36, sample: 26, center: 58 };

/** 绘制并返回 BMapGL 图标；canvas 或 Icon/Size 构造器不可用（旧版脚本、测试替身）时返回 undefined，Marker 回退为默认图标。 */
export function createDotIcon(api: BaiduMapApi, spec: DotIconSpec): BMapIcon | undefined {
  if (typeof api.Icon !== 'function' || typeof api.Size !== 'function') return undefined;
  const size = spec.layered ? DIAMETER.center : spec.text ? DIAMETER.marker : DIAMETER.sample;
  const canvas = document.createElement('canvas');
  canvas.width = size * 2;
  canvas.height = size * 2;
  const ctx = canvas.getContext('2d');
  if (!ctx) return undefined;
  ctx.scale(2, 2);
  const c = size / 2;
  if (spec.layered) {
    ctx.beginPath(); ctx.arc(c, c, c - 2, 0, Math.PI * 2); ctx.globalAlpha = 0.14; ctx.fillStyle = spec.color; ctx.fill();
    ctx.globalAlpha = 1;
    ctx.beginPath(); ctx.arc(c, c, c - 9, 0, Math.PI * 2); ctx.fillStyle = '#fff'; ctx.fill();
    ctx.beginPath(); ctx.arc(c, c, c - 15, 0, Math.PI * 2); ctx.fillStyle = spec.color; ctx.fill();
    ctx.beginPath(); ctx.arc(c, c, 5, 0, Math.PI * 2); ctx.fillStyle = '#fff'; ctx.fill();
  } else if (spec.filled) {
    ctx.beginPath(); ctx.arc(c, c, c - 1.5, 0, Math.PI * 2);
    ctx.fillStyle = spec.color; ctx.strokeStyle = '#fff'; ctx.lineWidth = 2.5; ctx.fill(); ctx.stroke();
    if (spec.text) {
      ctx.fillStyle = '#fff';
      ctx.font = `700 ${Math.round(size * 0.42)}px Inter, "Microsoft YaHei", sans-serif`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(spec.text, c, c + 0.5);
    }
  } else {
    ctx.beginPath(); ctx.arc(c, c, c - 1.5, 0, Math.PI * 2);
    ctx.fillStyle = '#fff'; ctx.strokeStyle = spec.color; ctx.lineWidth = 2; ctx.fill(); ctx.stroke();
    if (spec.text) {
      ctx.fillStyle = spec.textColor ?? spec.color;
      ctx.font = `700 ${Math.round(size * 0.42)}px Inter, "Microsoft YaHei", sans-serif`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(spec.text, c, c + 0.5);
    }
  }
  return new api.Icon(canvas.toDataURL('image/png'), new api.Size(size, size), { anchor: new api.Size(c, c) });
}
