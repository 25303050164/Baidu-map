/**
 * 类别选择器测三件事：**读不出来的时候有没有说实话**、**首轮页数算的是估计而不是承诺**，
 * 以及**核心口径与扩展类别的拆分**。
 *
 * 第二条的措辞是这份文件的一部分：`4 × 小类数` 是冷启动首轮页数的估计（实际分块由圈面
 * 决定，翻页、细分与重试还要更多页），把它说成"下界"会让人以为清单一交就一定能查完。
 *
 * 第三条是这个文件里最要紧的一条：把扩展大类塞进主请求，会让一次正常的三类体检变成
 * 一次预算必拒的十类检索（31 个小类 × 4 块 = 124 页首轮估计 > 默认 60）；把核心大类漏在
 * 主请求外，则会让总体区间分永远"给不出"。两者的表现都是"体检看起来能跑，但结果是错的"。
 */
import { describe, expect, it } from 'vitest';
import { estimatedFirstRoundPages, facilityCatalogView, missingCoreMajors, splitScope } from './categories';
import type { MajorCategory } from './contract';

/** 与后端 `catalog.facility_categories()` 同一形状：五个展示组、十个大类、31 个小类。 */
function catalog(overrides: Record<string, unknown> = {}) {
  return {
    facilityCategories: {
      version: 'poi-categories-v2.1',
      coreMajors: ['shopping', 'medical', 'education'],
      displayGroups: [
        { key: 'healthcare', label: '健康照护', order: 1, majors: ['medical', 'care'] },
        { key: 'education', label: '教育成长', order: 2, majors: ['education'] },
        { key: 'daily_life', label: '生活消费', order: 3, majors: ['shopping', 'dining', 'finance', 'life'] },
        { key: 'public_mobility', label: '公共出行', order: 4, majors: ['public', 'transport'] },
        { key: 'leisure', label: '文体休闲', order: 5, majors: ['leisure'] },
      ],
      majors: [
        { key: 'medical', label: '医疗健康', displayGroup: 'healthcare', core: true, minorCategories: 5 },
        { key: 'shopping', label: '购物消费', displayGroup: 'daily_life', core: true, minorCategories: 3 },
        { key: 'education', label: '教育', displayGroup: 'education', core: true, minorCategories: 6 },
        { key: 'care', label: '疗养康养', displayGroup: 'healthcare', core: false, minorCategories: 2 },
        { key: 'dining', label: '餐饮', displayGroup: 'daily_life', core: false, minorCategories: 2 },
        { key: 'finance', label: '金融', displayGroup: 'daily_life', core: false, minorCategories: 2 },
        { key: 'public', label: '政务公共服务', displayGroup: 'public_mobility', core: false, minorCategories: 3 },
        { key: 'leisure', label: '文体休闲', displayGroup: 'leisure', core: false, minorCategories: 3 },
        { key: 'transport', label: '交通出行', displayGroup: 'public_mobility', core: false, minorCategories: 3 },
        { key: 'life', label: '生活服务', displayGroup: 'daily_life', core: false, minorCategories: 2 },
      ],
    },
    budgets: { poiBlocksUpperBound: 4, poiMinorCategories: { default: 14, all: 31 } },
    ...overrides,
  };
}

const ALL: MajorCategory[] = ['medical', 'shopping', 'education', 'care', 'dining', 'finance',
  'public', 'leisure', 'transport', 'life'];

describe('facility catalog view', () => {
  it('reads the backend taxonomy without inventing one of its own', () => {
    const view = facilityCatalogView(catalog());
    expect(view).not.toBeNull();
    expect(view!.version).toBe('poi-categories-v2.1');
    expect(view!.coreMajors).toEqual(['shopping', 'medical', 'education']);
    expect(view!.majors).toHaveLength(10);
    expect(view!.groups.map(group => group.label)).toEqual(
      ['健康照护', '教育成长', '生活消费', '公共出行', '文体休闲']);
    expect(view!.blocksUpperBound).toBe(4);
  });

  it('says "this backend does not offer a selector" instead of showing an empty one', () => {
    // 旧后端没有 facilityCategories：给一个空清单会让界面显示"没有类别可选"，
    // 那是一个关于设施的结论，而真相是这一版接口不提供这一项。
    expect(facilityCatalogView({ budgets: { poiBlocksUpperBound: 4 } })).toBeNull();
    expect(facilityCatalogView({ facilityCategories: { majors: [] } })).toBeNull();
    expect(facilityCatalogView({ facilityCategories: { majors: [{ key: 'medical' }] } })).toBeNull();
  });

  it('keeps a major the backend did not place in any display group', () => {
    const value = catalog();
    value.facilityCategories.displayGroups = [
      { key: 'healthcare', label: '健康照护', order: 1, majors: ['medical', 'care'] }];
    const view = facilityCatalogView(value)!;
    const labels = view.groups.map(group => group.label);
    expect(labels).toEqual(['健康照护', '其他']);
    expect(view.groups[1].majors.map(major => major.key)).toHaveLength(8);
  });

  it('reports that the block count is unknown rather than assuming four', () => {
    const view = facilityCatalogView({ ...catalog(), budgets: {} })!;
    expect(view.blocksUpperBound).toBeNull();
    expect(estimatedFirstRoundPages(view, ['shopping'])).toBeNull();
  });
});

describe('first-round estimate', () => {
  it('counts blocks times minor categories as the cold-start first round', () => {
    const view = facilityCatalogView(catalog())!;
    // 核心三类 14 个小类 × 4 块 = 56，正好是默认 60 预算里塞得下的那一档。
    expect(estimatedFirstRoundPages(view, ['shopping', 'medical', 'education'])).toBe(56);
    // 全部十类 31 个小类 × 4 块 = 124：默认 60 装不下，日额度 80 也装不下。
    expect(estimatedFirstRoundPages(view, ALL)).toBe(124);
    expect(estimatedFirstRoundPages(view, ['dining', 'leisure'])).toBe(20);
    expect(estimatedFirstRoundPages(view, [])).toBe(0);
  });

  it('refuses to price a major the catalog does not describe', () => {
    const view = facilityCatalogView(catalog())!;
    expect(estimatedFirstRoundPages(view, ['medical', 'not_a_major' as MajorCategory])).toBeNull();
  });
});

describe('core scope versus on-demand extensions', () => {
  it('sends the core majors in the main request and the rest to a follow-up', () => {
    const view = facilityCatalogView(catalog())!;
    const scope = splitScope(view, ['dining', 'medical', 'shopping', 'education', 'leisure']);
    expect(scope.core).toEqual(['shopping', 'medical', 'education']);
    expect(scope.extended).toEqual(['dining', 'leisure']);
  });

  it('keeps the request body independent of the order the boxes were ticked', () => {
    const view = facilityCatalogView(catalog())!;
    const first = splitScope(view, ['leisure', 'education', 'dining', 'medical', 'shopping']);
    const second = splitScope(view, ['medical', 'shopping', 'dining', 'leisure', 'education']);
    expect(first).toEqual(second);
  });

  it('names the core majors a narrowed scope leaves out', () => {
    const view = facilityCatalogView(catalog())!;
    expect(missingCoreMajors(view, ['shopping', 'medical'])).toEqual(['education']);
    expect(missingCoreMajors(view, ['shopping', 'medical', 'education'])).toEqual([]);
    // 只选了扩展类别：三大类一个都没评估，总体分同样给不出。
    expect(missingCoreMajors(view, ['dining'])).toEqual(['shopping', 'medical', 'education']);
  });
});
