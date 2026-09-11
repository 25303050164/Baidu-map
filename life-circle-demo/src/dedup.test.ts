import { describe, it, expect } from 'vitest';
import { classifyPoi, type RawPoi } from './classify';
import { dedupe, fallbackKey, stableUid, type ClassifiedPoi } from './dedup';

const classify = (poi: RawPoi): ClassifiedPoi => ({ ...poi, ...classifyPoi(poi) });

describe('stable UID deduplication', () => {
  it('prefers uid, then source:id, then poiId', () => {
    expect(stableUid({ name: 'x', uid: 'A', poiId: 'B', source: 'S', sourceId: 'C' })).toBe('A');
    expect(stableUid({ name: 'x', source: 'S', sourceId: 'C', poiId: 'B' })).toBe('S:C');
    expect(stableUid({ name: 'x', poiId: 'B' })).toBe('B');
    expect(stableUid({ name: 'x' })).toBeUndefined();
  });

  it('merges records that share a stable UID and keeps the most complete one', () => {
    const result = dedupe([
      classify({ uid: 'U1', name: '青禾菜市场', x: 1, y: 1 }),
      classify({ uid: 'U1', name: '青禾菜市场（二店）', x: 1, y: 1, tags: ['农贸市场'], address: '青禾路 1 号' })
    ]);
    expect(result.unique).toHaveLength(1);
    expect(result.removedCount).toBe(1);
    expect(result.duplicates[0]).toMatchObject({ key: 'uid:U1', kind: 'uid', count: 2 });
    expect(result.unique[0].address).toBe('青禾路 1 号');
  });

  it('falls back to name plus coordinates when no stable UID exists', () => {
    const result = dedupe([
      classify({ name: '无名生鲜', x: 2, y: 2 }),
      classify({ name: '无名生鲜', x: 2, y: 2, tags: ['生鲜'] })
    ]);
    expect(result.unique).toHaveLength(1);
    expect(result.removedCount).toBe(1);
    expect(result.duplicates[0].kind).toBe('fallback');
    expect(fallbackKey({ name: '无名生鲜', x: 2, y: 2 })).toBe('无名生鲜|2,2');
  });

  it('keeps the same name at different coordinates separate', () => {
    const { unique } = dedupe([
      classify({ name: '无名生鲜', x: 2, y: 2 }),
      classify({ name: '无名生鲜', x: 9, y: 9 })
    ]);
    expect(unique).toHaveLength(2);
  });

  it('is order-independent and deterministic', () => {
    const a = classify({ uid: 'U2', name: '甲', x: 1, y: 1 });
    const b = classify({ uid: 'U3', name: '乙', x: 2, y: 2 });
    expect(dedupe([a, b]).unique.map(item => item.uid)).toEqual(dedupe([b, a]).unique.map(item => item.uid));
  });
});
