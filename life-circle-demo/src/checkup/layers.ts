/**
 * v2 图层 → 可绘制的形状与点。
 *
 * 纯函数，不认识地图：一份图层响应进来，一组"画什么、用什么样式、带什么属性"出去。
 * 地图组件只负责把它们交给 SDK，因此这里的每一条规则都能在没有浏览器的情况下测。
 *
 * 三条规矩：
 *
 * - **没取到的图层和取到但为空的图层不是一回事**，用 `state` 分开表达：前者界面要说
 *   "尚未加载"，后者要说"这次没有内容"。把它们都画成"什么都没有"，读者会以为已经查过了。
 * - **画的是后端给的几何，不做任何裁剪或简化**：灰区面积是按同一份几何算的，图上少一块
 *   就与报告里的数字对不上。
 * - **点按属性着色**，颜色表只有这一处；图例、地图、面板共用它，才不会出现"图上是医疗蓝、
 *   图例写的购物绿"。
 */
import type { CheckupLayer } from './contract';
import { readLayerGeometry, type LayerId } from './validate';
import type { DrawableGeometry } from '../analysis/geometry';

export type LayerStyle = {
  label: string;
  strokeColor: string;
  fillColor: string;
  fillOpacity: number;
  strokeWeight: number;
  strokeStyle?: 'solid' | 'dashed';
  /** 只有边界、没有面积含义的图层（评估域、等时圈轮廓）在这里说明怎么读。 */
  note: string;
};

export const LAYER_STYLES: Record<LayerId, LayerStyle> = {
  isochrone: { label: '步行等时圈', strokeColor: '#147d70', fillColor: '#2da990',
    fillOpacity: 0.26, strokeWeight: 2, note: '按相同步行预算圈出的边界，不是服务覆盖结论。' },
  accessibility: { label: '评估域', strokeColor: '#64748b', fillColor: '#64748b',
    fillOpacity: 0, strokeWeight: 1, strokeStyle: 'dashed',
    note: '所有面积比例的分母。域外一律不评估，也不计入覆盖率。' },
  service_gaps: { label: '服务灰区', strokeColor: '#4b5563', fillColor: '#6b7280',
    fillOpacity: 0.38, strokeWeight: 1, note: '步行超出服务标准的连片区域，按面积降序编号。' },
  facilities: { label: '设施点位', strokeColor: '#ffffff', fillColor: '#64748b',
    fillOpacity: 1, strokeWeight: 2, note: '检索并被接受的设施；审核候选与隔离记录不上图。' },
  heatmap: { label: '热力采样点', strokeColor: '#ffffff', fillColor: '#c78b36',
    fillOpacity: 1, strokeWeight: 2, note: '每个点是一次实测步行距离，不是覆盖判定。' },
  verification: { label: '核验设施', strokeColor: '#ffffff', fillColor: '#147d70',
    fillOpacity: 1, strokeWeight: 2, note: '问过路的那几家设施及其结论。' },
  report: { label: '体检报告', strokeColor: '#147d70', fillColor: '#147d70',
    fillOpacity: 0, strokeWeight: 0, note: '报告是文档，没有形状。' },
};

/** 点色：设施与热力按大类，核验按结论。缺的属性给中性灰，不猜。 */
export const POINT_COLORS: Record<string, string> = {
  shopping: '#168875', medical: '#397ac6', education: '#c78b36',
  covered: '#147d70', gap: '#b54708', unknown: '#64748b',
  verified_reachable: '#147d70', verified_unreachable: '#b54708', pending: '#64748b',
};

export type LayerShape = { key: string; geometry: DrawableGeometry;
  properties: Record<string, unknown>; style: LayerStyle };
export type LayerPoint = { key: string; lng: number; lat: number; color: string;
  title: string; properties: Record<string, unknown> };

export type LayerDrawable = {
  state: 'absent' | 'empty' | 'ready';
  shapes: LayerShape[];
  points: LayerPoint[];
  /** 这一层的说明（读法与限制），由样式表给。 */
  note: string;
};

function pointOf(feature: Record<string, unknown>) {
  const geometry = feature.geometry as { coordinates: number[] };
  return { lng: geometry.coordinates[0], lat: geometry.coordinates[1] };
}

function propertiesOf(feature: Record<string, unknown>): Record<string, unknown> {
  return (feature.properties ?? {}) as Record<string, unknown>;
}

/** 点的颜色与提示：三类点各有自己的关键字段，缺字段时的说法必须能看出是缺字段。 */
function pointView(layerId: LayerId, index: number, feature: Record<string, unknown>): LayerPoint {
  const properties = propertiesOf(feature);
  const { lng, lat } = pointOf(feature);
  const pick = (...keys: string[]) => {
    for (const key of keys) {
      const value = properties[key];
      if (typeof value === 'string' && value) return value;
    }
    return null;
  };
  const classification = pick('majorCategory', 'status');
  const name = pick('name', 'facilityId', 'cell');
  const detail = pick('category', 'distanceM');
  const distance = properties.distanceM;
  const title = [name ?? `第 ${index + 1} 个点`, classification ?? '未分类',
    typeof distance === 'number' ? `${distance.toFixed(0)} 米` : detail].filter(Boolean).join(' · ');
  return {
    key: String(properties.id ?? properties.facilityId ?? properties.cell ?? `${layerId}-${index}`),
    lng, lat,
    color: (classification && POINT_COLORS[classification]) ?? LAYER_STYLES[layerId].fillColor,
    title, properties,
  };
}

/**
 * 一层图层 → 可绘制内容。
 *
 * `layer` 为 `undefined` 表示**这次还没取**，与"取回来是空的"分开表达：地图上都画不出
 * 东西，但面板上必须说得不一样。
 */
export function drawableLayer(layerId: LayerId, layer: CheckupLayer | undefined): LayerDrawable {
  const style = LAYER_STYLES[layerId];
  if (!layer) return { state: 'absent', shapes: [], points: [], note: style.note };
  const geometry = readLayerGeometry(layer);
  if (geometry === null) throw new Error(`图层的几何无法解析：${layerId}`);
  if (geometry.kind === 'document') return { state: 'ready', shapes: [], points: [], note: style.note };
  if (geometry.kind === 'none') return { state: 'empty', shapes: [], points: [], note: style.note };
  if (geometry.kind === 'polygon') {
    return { state: 'ready', note: style.note,
      shapes: [{ key: layerId, geometry: geometry.geometry as DrawableGeometry,
        properties: {}, style }], points: [] };
  }
  const shapes: LayerShape[] = [];
  const points: LayerPoint[] = [];
  geometry.features.forEach((feature, index) => {
    const shape = feature.geometry as { type: string };
    if (shape.type === 'Point') {
      points.push(pointView(layerId, index, feature));
      return;
    }
    const properties = propertiesOf(feature);
    shapes.push({
      key: String(properties.id ?? `${layerId}-${index}`),
      geometry: shape as DrawableGeometry,
      // 灰区的颜色按"有没有完成设施检索"分：检索没跑完的灰区不是同一种结论。
      properties,
      style: properties.queryStatus === 'partial'
        ? { ...style, strokeColor: '#b54708', fillColor: '#b54708', fillOpacity: 0.28 } : style,
    });
  });
  return { state: shapes.length + points.length === 0 ? 'empty' : 'ready', shapes, points,
    note: style.note };
}
