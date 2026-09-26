import { describe, expect, it } from 'vitest';
import { heatNoticeFor, heatPointsOf, undeterminedInCircleOf } from './heat';
import type { Facility, FacilityAnalysis } from '../api-contract';

function facility(overrides: Partial<Facility> = {}): Facility {
  return { id: 'f1', name: '测试设施', category: 'pharmacy', major_category: 'medical',
    minor_category: 'pharmacy', location: { lng: 121.5, lat: 31.3 }, in_circle: true, ...overrides };
}

function analysis(overrides: Partial<FacilityAnalysis> = {}): FacilityAnalysis {
  return { status: 'complete', queries: [], assessments: [], candidate_points: 0, assessed_points: 0,
    unassessed_points: 0, network_requests: 0, elapsed_seconds: 0, search_radius_m: 1000,
    routes: {}, serviceBlindRegions: {}, warnings: [], ...overrides };
}

describe('参与热力的设施', () => {
  it('只有已确认在圈内的设施参与：未判定的不当作圈内，就单独计数', () => {
    const facilities = [
      facility({ id: 'in', in_circle: true }),
      facility({ id: 'out', in_circle: false }),
      facility({ id: 'unknown', in_circle: null }),
    ];
    expect(heatPointsOf(facilities).map(item => item.id)).toEqual(['in']);
    expect(undeterminedInCircleOf(facilities)).toBe(1);
    // 缺省值缺失（旧接口）也不能当作圈内：没有 in_circle 字段的设施同样不参与。
    expect(heatPointsOf([{ ...facility({ id: 'x' }), in_circle: undefined as never }])).toEqual([]);
  });

  it('按大类筛选，缺数据时是空集而不是整圈热力', () => {
    const facilities = [facility({ id: 'a', major_category: 'medical' }),
      facility({ id: 'b', major_category: 'shopping' })];
    expect(heatPointsOf(facilities, 'education')).toEqual([]);
    expect(heatPointsOf(facilities, 'all')).toHaveLength(2);
    expect(heatPointsOf(undefined)).toEqual([]);
    expect(undeterminedInCircleOf(undefined)).toBe(0);
    expect(undeterminedInCircleOf([
      facility({ id: 'm', major_category: 'medical', in_circle: null }),
      facility({ id: 's', major_category: 'shopping', in_circle: null }),
    ], 'medical')).toBe(1);
  });
});

describe('数据范围提示', () => {
  it('完整查询不制造警告', () => {
    expect(heatNoticeFor(analysis({ queries: [{ category: 'pharmacy', query: '药店', status: 'complete',
      pages: 1, returned: 3, excluded: 0, invalid: 0, total: 3, reason: null }] }))).toBeNull();
  });

  it('部分与截断的查询必须写明只覆盖已检索到的设施', () => {
    const notice = heatNoticeFor(analysis({ status: 'partial', queries: [
      { category: 'pharmacy', query: '药店', status: 'truncated', pages: 2, returned: 1, excluded: 0,
        invalid: 0, total: 150, reason: 'page_limit' },
      { category: 'market', query: '菜市场', status: 'complete', pages: 1, returned: 2, excluded: 0,
        invalid: 0, total: 2, reason: null },
    ] }));
    expect(notice).toContain('被截断');
    expect(notice).toContain('只覆盖已检索到的设施');
    expect(notice).toContain('不代表圈内全部分布');
  });

  it('设施检索未接入时说明本层没有真实设施，而不是画一层空热力', () => {
    expect(heatNoticeFor(null)).toContain('尚未接入');
    expect(heatNoticeFor(undefined)).toContain('尚未接入');
  });
});
