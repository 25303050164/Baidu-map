/**
 * 右栏「周边设施」：按中心点直线距离，每类取最近若干处。
 *
 * 这里**只做排序与格式化**：设施点来自设施图层（审核候选、隔离记录本来就不在图层里），
 * 距离是中心点到设施点的直线距离 —— 它**不是**报告里判定服务标准的步行距离，所以界面
 * 必须写明"直线距离"，点开设施后的步行路线仍由后端计算。
 *
 * 疑似重复只算一处：与密度图层同一规则（同名同址、相距 20 米内标成同一组），否则同一家
 * 店会在"最近五处"里占两个名额，看着像两处设施。
 */
import type { Center } from '../types';
import type { LayerDrawable } from './layers';
import { CATEGORY_COLORS, CATEGORY_ORDER, categoryLabel } from './report';

const EARTH_RADIUS_M = 6_371_008.8;

/** 两点的直线距离（米）。只用于排序与显示，不参与任何服务标准判定。 */
export function straightLineM(a: Center, b: Center): number {
  const rad = (deg: number) => (deg * Math.PI) / 180;
  const dLat = rad(b.lat - a.lat);
  const dLng = rad(b.lng - a.lng);
  const h = Math.sin(dLat / 2) ** 2
    + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dLng / 2) ** 2;
  return 2 * EARTH_RADIUS_M * Math.asin(Math.min(1, Math.sqrt(h)));
}

export type NearestFacility = {
  key: string;
  name: string;
  address: string | null;
  distanceM: number;
};

export type NearestGroup = {
  category: string;
  label: string;
  color: string;
  /** 本类别参与排序的设施总数（疑似重复合并后）。 */
  total: number;
  facilities: NearestFacility[];
};

export const NEAREST_LIMIT = 5;

const pointText = (value: unknown): string | null =>
  typeof value === 'string' && value.length > 0 ? value : null;

/**
 * 设施图层 → 每类最近的 N 处。没有中心点或没有设施时返回空数组，由界面说明原因。
 * 类别缺大类的记录归入 `other`，不在三类里冒充。
 */
export function nearestFacilities(drawable: LayerDrawable | undefined, center: Center | null,
  limit: number = NEAREST_LIMIT): NearestGroup[] {
  if (!center) return [];
  const groups = new Map<string, { total: number; facilities: NearestFacility[];
    seen: Set<string> }>();
  for (const point of drawable?.points ?? []) {
    if (!Number.isFinite(point.lng) || !Number.isFinite(point.lat)) continue;
    const category = pointText(point.properties.majorCategory) ?? 'other';
    const group = groups.get(category) ?? { total: 0, facilities: [], seen: new Set<string>() };
    groups.set(category, group);
    const duplicate = pointText(point.properties.possibleDuplicateGroup);
    const identity = duplicate ?? point.key;
    if (group.seen.has(identity)) continue;
    group.seen.add(identity);
    group.total += 1;
    group.facilities.push({
      key: point.key,
      name: pointText(point.properties.name) ?? point.key,
      address: pointText(point.properties.address),
      distanceM: straightLineM(center, { lng: point.lng, lat: point.lat }),
    });
  }
  const known: readonly string[] = CATEGORY_ORDER;
  const order = [...known.filter(category => groups.has(category)),
    ...[...groups.keys()].filter(category => !known.includes(category))];
  return order.map(category => {
    const group = groups.get(category)!;
    return {
      category, label: categoryLabel(category),
      color: CATEGORY_COLORS[category] ?? '#7c8b95',
      total: group.total,
      facilities: [...group.facilities].sort((a, b) => a.distanceM - b.distanceM).slice(0, limit),
    };
  });
}
