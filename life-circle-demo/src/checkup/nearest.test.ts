/**
 * 「周边设施」只做两件事：按直线距离排序、疑似重复只算一处。这里钉住的也正是这两件：
 * 排序按距离、取前 N、每组总数正确；同组记录不重复占位；缺大类不冒充三类。
 */
import { describe, expect, it } from 'vitest';
import { nearestFacilities, straightLineM } from './nearest';
import type { LayerDrawable, LayerPoint } from './layers';

const center = { lng: 116.4, lat: 39.9 };

function facility(index: number, distance: number, overrides: Record<string, unknown> = {}): LayerPoint {
  // 1 度纬度约 111.32 公里：用固定的纬度偏移表示距离，避免测试里再算一遍球面。
  return {
    key: `f-${index}`,
    lng: center.lng,
    lat: center.lat + distance / 111_320,
    color: '#168875',
    title: `设施 ${index}`,
    properties: { id: `f-${index}`, name: `设施 ${index}`, majorCategory: 'shopping',
      address: `某路 ${index} 号`, ...overrides },
  };
}

function drawable(points: LayerPoint[]): LayerDrawable {
  return { state: 'ready', shapes: [], points, note: '' };
}

describe('straight line distance', () => {
  it('measures a mostly northward offset in metres', () => {
    const distance = straightLineM(center, { lng: center.lng, lat: center.lat + 0.009 });
    expect(distance).toBeGreaterThan(1000);
    expect(distance).toBeLessThan(1002);
  });

  it('is zero for the same point', () => {
    expect(straightLineM(center, center)).toBe(0);
  });
});

describe('nearest facilities', () => {
  it('sorts by distance and keeps only the first five', () => {
    const points = [500, 100, 300, 200, 400, 50].map((distance, index) =>
      facility(index, distance));
    const groups = nearestFacilities(drawable(points), center);
    expect(groups).toHaveLength(1);
    expect(groups[0].total).toBe(6);
    expect(groups[0].facilities.map(item => Math.round(item.distanceM))).toEqual([50, 100, 200, 300, 400]);
    expect(groups[0].facilities[0]).toMatchObject({ key: 'f-5', name: '设施 5', address: '某路 5 号' });
  });

  it('counts a possible duplicate once, keeping the first record', () => {
    const points = [
      facility(0, 100, { possibleDuplicateGroup: 'possible:f0f1' }),
      facility(1, 110, { possibleDuplicateGroup: 'possible:f0f1' }),
      facility(2, 200),
    ];
    const groups = nearestFacilities(drawable(points), center);
    expect(groups[0].total).toBe(2);
    expect(groups[0].facilities.map(item => item.key)).toEqual(['f-0', 'f-2']);
  });

  it('orders the three majors first and does not pretend an unclassified point is one of them', () => {
    const points = [
      facility(0, 100, { majorCategory: 'education' }),
      facility(1, 100, { majorCategory: 'medical' }),
      facility(2, 100, { majorCategory: 'shopping' }),
      facility(3, 100, { majorCategory: undefined }),
    ];
    const groups = nearestFacilities(drawable(points), center);
    expect(groups.map(group => group.category)).toEqual(['shopping', 'medical', 'education', 'other']);
    expect(groups[3]).toMatchObject({ label: 'other', total: 1 });
  });

  it('says nothing at all without a centre or without points', () => {
    expect(nearestFacilities(drawable([facility(0, 100)]), null)).toEqual([]);
    expect(nearestFacilities(undefined, center)).toEqual([]);
  });

  it('drops points whose coordinates are not finite', () => {
    const broken = facility(0, 100);
    broken.lng = Number.NaN;
    const groups = nearestFacilities(drawable([broken, facility(1, 120)]), center);
    expect(groups[0].total).toBe(1);
  });
});
