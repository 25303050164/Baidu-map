/**
 * 水系证据的翻译：冲突不能被画成"已核实"，旧版本必须被认出来。
 *
 * 这里测的是两类会把错误结论送上屏的失误：把没有结论的冲突面、画错的底图水面并进
 * "已核实"一类（读者会把底图水面当成已确认的覆盖），以及把早于水系复核的旧修订当成
 * 当前结果（它的河道位置和桥梁都没核对过）。
 */
import { describe, expect, it } from 'vitest';
import { AREA, polygon, snapshot, water, waterReview } from './fixtures';
import {
  outdatedText, recomputedText, versionView, WATER_KINDS, waterReviewRefs, waterView,
} from './water';

/** 夹具评估域的包围盒是 [116.39, 39.89, 116.41, 39.91]。 */
const REVIEW = { label: 'synthetic-river@2026-09-01.1', title: '合成河段复核',
  bbox: [116.38, 39.88, 116.42, 39.92] as [number, number, number, number] };

describe('waterView', () => {
  it('没有水系证据的修订什么也不画', () => {
    const view = waterView(null);
    expect(view.available).toBe(false);
    expect(view.shapes).toEqual([]);
    expect(view.crossings).toEqual([]);
  });

  it('五类面各归各类，按绘制次序排列，冲突与底图误绘不混进已核实', () => {
    const view = waterView(water());
    expect(view.available).toBe(true);
    expect(view.shapes.map(shape => shape.kind)).toEqual(['extent', 'reach', 'supplement', 'misdrawn', 'conflict']);
    expect([...view.shapes.map(shape => shape.kind)].sort((a, b) =>
      WATER_KINDS.indexOf(a) - WATER_KINDS.indexOf(b))).toEqual(view.shapes.map(shape => shape.kind));
    const conflict = view.shapes.find(shape => shape.kind === 'conflict')!;
    expect(conflict.title).toContain('数据冲突／未知');
    expect(conflict.title).toContain('约 660 m²');
    expect(conflict.anchor).toEqual({ lng: 116.402, lat: 39.902 });
    expect(view.shapes.find(shape => shape.kind === 'misdrawn')!.title).toContain('已核实为陆地');
    expect(view.counts).toEqual({ extent: 1, reach: 1, supplement: 1, misdrawn: 1, conflict: 1, crossing: 1 });
    expect(view.conflictAreaM2).toBe(660);
  });

  it('河道说明给出计算宽度、OSM 原标宽度与沿河变化的底图偏离，不给统一平移量', () => {
    const view = waterView(water());
    const reach = view.shapes.find(shape => shape.kind === 'reach')!;
    expect(reach.title).toContain('宽 18 m');
    expect(reach.title).toContain('OSM 原标宽 10 m');
    expect(reach.title).toContain('百度底图偏离 23–193 m，沿河变化，未做统一平移');
    expect(reach.anchor).toBeNull();
    expect(view.reachWidthM).toEqual({ min: 18, max: 18 });
    expect(view.crossings).toHaveLength(1);
    expect(view.crossings[0].title).toContain('已核实桥梁');
    expect(view.crossings[0].title).toContain('桥长 32 m');
  });

  it('没有几何的条目照样计数，只是不画', () => {
    const review = waterReview({
      conflicts: [{ id: 'nogeo', status: 'unverified', areaM2: 10, note: '', geometry: null }],
      crossings: [{ osmId: 1, anchor: null }],
    });
    const view = waterView(water({ reviews: [review] }));
    expect(view.counts.conflict).toBe(1);
    expect(view.shapes.some(shape => shape.kind === 'conflict')).toBe(false);
    expect(view.counts.crossing).toBe(1);
    expect(view.crossings).toEqual([]);
  });
});

describe('waterReviewRefs', () => {
  it('只认字段完整、包围盒有效的条目', () => {
    expect(waterReviewRefs(undefined)).toEqual([]);
    expect(waterReviewRefs([REVIEW, { label: 'x', bbox: [1, 2, 0, 3] }, { bbox: [0, 0, 1, 1] },
      { label: 'y', bbox: [0, 0, 1] }, 'junk'])).toEqual([REVIEW]);
  });
});

describe('versionView', () => {
  it('评估范围落在复核里、修订却没有水系记录：旧版本，说明早于复核', () => {
    const view = versionView(snapshot(), [REVIEW]);
    expect(view.applied).toBeNull();
    expect(view.outdatedBy).toEqual([REVIEW]);
    expect(view.recomputed).toBeNull();
    expect(outdatedText(view)).toContain('早于水系复核「合成河段复核」（synthetic-river@2026-09-01.1）');
  });

  it('用了当前复核的修订不是旧版本；用了别的版本就是', () => {
    const current = snapshot({ trace: { ...snapshot().trace,
      dataVersions: { waterReviews: [REVIEW.label] } } });
    expect(versionView(current, [REVIEW]).outdatedBy).toEqual([]);
    const older = snapshot({ trace: { ...snapshot().trace,
      dataVersions: { waterReviews: ['synthetic-river@2026-08-01.1'] } } });
    const view = versionView(older, [REVIEW]);
    expect(view.outdatedBy).toEqual([REVIEW]);
    expect(outdatedText(view)).toContain('它用的是 synthetic-river@2026-08-01.1');
  });

  it('范围不相交、或者这一版根本没有评估时，不标旧版本', () => {
    const far = { ...REVIEW, bbox: [121.5, 31.3, 121.52, 31.32] as [number, number, number, number] };
    expect(versionView(snapshot(), [far]).outdatedBy).toEqual([]);
    const unassessed = snapshot({ accessibility: null, accessibilityStatus: 'failed', heatmap: null,
      serviceGaps: null, scores: null });
    expect(versionView(unassessed, [REVIEW]).outdatedBy).toEqual([]);
  });

  it('评估域缺席时退回等时圈判断范围', () => {
    const view = versionView(snapshot({ accessibility: null, accessibilityStatus: 'failed', serviceGaps: null,
      scores: null, heatmap: { metric: 'walking_route', estimated: true, stepM: 50, domain: null,
        categories: {}, notes: [] }, isochrone: { geometry: polygon() } }), [REVIEW]);
    expect(view.outdatedBy).toEqual([REVIEW]);
  });

  it('离线重算的版本写明来源、沿用了什么、花了多少', () => {
    const view = versionView(snapshot({ revision: 7, trace: { ...snapshot().trace,
      dataVersions: { waterReviews: [REVIEW.label] },
      recomputed: { fromRevision: 5, reason: `water_review ${REVIEW.label}`, networkRequests: 0,
        boundary: 'replayed_from_ledger', carriedOver: ['facilities', 'verificationRoutes'] } } }), [REVIEW]);
    expect(view.recomputed).toMatchObject({ fromRevision: 5, networkRequests: 0 });
    const text = recomputedText(view.recomputed!);
    expect(text).toContain('由第 5 版离线重算');
    expect(text).toContain('存档的采样台账');
    expect(text).toContain('设施检索与核验路线沿用原任务的结果');
    expect(text).toContain('0 次网络请求');
    expect(recomputedText({ ...view.recomputed!, boundary: 'unchanged', carriedOver: [] }))
      .not.toContain('沿用原任务');
  });
});

describe('fixture sanity', () => {
  it('水系夹具的面积与评估域自洽', () => {
    const value = water();
    expect(value.reviewedAreaM2 + (value.unreviewedAreaM2 ?? 0)).toBe(AREA);
  });
});
