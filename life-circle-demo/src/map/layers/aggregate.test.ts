import { describe, expect, it } from 'vitest';
import { aggregateByCell, FACILITY_CLUSTER_CELL_PX } from './aggregate';

/** 固定投影：1 度 = 10000 像素，格边长 28 像素即 0.0028 度，便于手算分格。 */
const project = (lng: number, lat: number) => ({ x: lng * 10_000, y: lat * 10_000 });

function facility(id: string, lng: number, lat: number) {
  return { id, lng, lat };
}

describe('设施标记聚合', () => {
  it('同格合并，数量与成员保留，绝不丢弃', () => {
    // 三个点的像素坐标 1000/1003/1006 都落在同一个 28 像素格（980—1007）内。
    const facilities = [facility('a', 0.1, 0.1), facility('b', 0.1003, 0.1003), facility('c', 0.1006, 0.1)];
    const result = aggregateByCell(facilities, { project });
    expect(result.cells).toHaveLength(1);
    expect(result.cells[0].count).toBe(3);
    expect(result.cells[0].items.map(item => item.id)).toEqual(['a', 'b', 'c']);
    // 标记落在成员中间，而不是格子角上。
    expect(result.cells[0].lng).toBeCloseTo(0.1003, 9);
    expect(result.cells[0].lat).toBeCloseTo(0.1001, 9);
    expect(result.total).toBe(3);
    expect(result.clustered).toBe(3);
    expect(result.largest).toBe(3);
  });

  it('分属不同格的设施保持各自的标记，数量守恒', () => {
    // 经度相差 0.01 → 100 像素，跨过多个格。
    const facilities = [facility('a', 0.1, 0.1), facility('b', 0.11, 0.1), facility('c', 0.12, 0.1)];
    const result = aggregateByCell(facilities, { project });
    expect(result.cells).toHaveLength(3);
    expect(result.cells.every(cell => cell.count === 1)).toBe(true);
    expect(result.clustered).toBe(0);
    expect(result.largest).toBe(1);
    // 全部设施都被画出：成员数之和等于输入规模。
    expect(result.cells.reduce((sum, cell) => sum + cell.count, 0)).toBe(facilities.length);
  });

  it('缩放改变分格：放大后同一批设施拆成多个标记', () => {
    // 两点相距 5 像素：缩小视图下同格，放大十倍后分属不同格。
    const facilities = [facility('a', 0.1, 0.1), facility('b', 0.1005, 0.1)];
    const zoomed = (lng: number, lat: number) => ({ x: lng * 100_000, y: lat * 100_000 });
    expect(aggregateByCell(facilities, { project }).cells).toHaveLength(1);
    expect(aggregateByCell(facilities, { project: zoomed }).cells).toHaveLength(2);
  });

  it('大量设施不会截断：500 处设施全部有归属', () => {
    const facilities = Array.from({ length: 500 }, (_, index) =>
      facility(`f${index}`, 0.1 + (index % 25) * 0.0006, 0.1 + Math.floor(index / 25) * 0.0006));
    const result = aggregateByCell(facilities, { project });
    // 网格只减少标记数量，不减少设施数量。
    expect(result.cells.reduce((sum, cell) => sum + cell.count, 0)).toBe(500);
    expect(result.cells.length).toBeLessThan(500);
    expect(result.cells.length).toBeGreaterThan(0);
    expect(result.total).toBe(500);
  });

  it('投影失败的点自成一格，仍然会被画出来', () => {
    const facilities = [facility('ok', 0.1, 0.1), facility('bad', Number.NaN, 0.1)];
    const result = aggregateByCell(facilities, { project });
    expect(result.total).toBe(2);
    expect(result.cells.flatMap(cell => cell.items.map(item => item.id)).sort()).toEqual(['bad', 'ok']);
  });

  it('缺省格边长是常量，空输入给空结果', () => {
    expect(FACILITY_CLUSTER_CELL_PX).toBe(28);
    expect(aggregateByCell([], { project })).toEqual({ cells: [], total: 0, clustered: 0, largest: 0 });
  });
});
