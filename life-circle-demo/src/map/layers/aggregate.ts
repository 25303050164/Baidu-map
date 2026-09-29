/** 设施标记的屏幕空间聚合：同格合并、数量写在标记上，绝不静默丢弃。
 *
 * 要解决的是两个都不体面、但性质不同的选项：一次体检可能返回几百处设施，逐点铺标记
 * 既互相遮盖又拖慢地图；只画前 N 条则是在说谎 —— 被丢掉的设施在图上完全看不出来，
 * 读者会把"画出来的"当成"全部的"（§9 地图交互）。这里保留全部设施，只按屏幕像素把
 * 同一格里的设施合并成一枚标记，把成员数量写在标记上；完整数据仍然参与统计、列表和
 * 热力层，被合并只是显示方式，不是数据丢弃。
 *
 * 投影由地图提供（``pointToOverlayPixel``），因此缩放后同一批设施会重新分格：放大时
 * 拆成多个标记，缩小时并成一个更大的标记。
 */

/** 聚合格边长（CSS 像素）：小于图标直径就会互相覆盖，大于 32 又会把一条街并成一枚。 */
export const FACILITY_CLUSTER_CELL_PX = 28;

export type ProjectableFacility = { id: string; lng: number; lat: number };
export type Projector = (lng: number, lat: number) => { x: number; y: number };

/** 一枚要画的标记：``count === 1`` 时就是设施本身的位置。 */
export type AggregateCell<T> = {
  key: string;
  lng: number;
  lat: number;
  count: number;
  /** 全部成员，顺序与输入一致；点击时取第一项，图例/详情仍能列出全部。 */
  items: T[];
};

export type Aggregation<T> = {
  cells: AggregateCell<T>[];
  /** 输入的设施总数；与所有 cell 的 count 之和相等，可直接用于断言"没有丢"。 */
  total: number;
  /** 被合并进多成员格的设施数量（``count > 1`` 的成员总数）。 */
  clustered: number;
  largest: number;
};

/** 按屏幕像素分格聚合设施；无法投影的点自成一格，依旧会被画出来。 */
export function aggregateByCell<T extends ProjectableFacility>(
  facilities: readonly T[],
  options: { cellPx?: number; project: Projector },
): Aggregation<T> {
  const cellPx = Math.max(1, options.cellPx ?? FACILITY_CLUSTER_CELL_PX);
  const buckets = new Map<string, T[]>();
  for (const facility of facilities) {
    // 地图尚未就绪或坐标非法时投影可能返回非有限值：这类点自成一格，宁可多画一枚
    // 也不让它消失。
    const pixel = options.project(facility.lng, facility.lat);
    const key = Number.isFinite(pixel?.x) && Number.isFinite(pixel?.y)
      ? `${Math.floor(pixel.x / cellPx)}:${Math.floor(pixel.y / cellPx)}`
      : `unprojected:${facility.id}`;
    const bucket = buckets.get(key);
    if (bucket) bucket.push(facility); else buckets.set(key, [facility]);
  }
  const cells: AggregateCell<T>[] = [];
  let clustered = 0;
  let largest = 0;
  for (const [key, items] of buckets) {
    const count = items.length;
    if (count > 1) clustered += count;
    largest = Math.max(largest, count);
    cells.push({
      key,
      // 落在成员中间，而不是格子的左上角：合并后的标记不会"跑偏"到网格线上。
      lng: items.reduce((sum, item) => sum + item.lng, 0) / count,
      lat: items.reduce((sum, item) => sum + item.lat, 0) / count,
      count,
      items,
    });
  }
  return { cells, total: facilities.length, clustered, largest };
}
