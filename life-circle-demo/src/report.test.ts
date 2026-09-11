import { describe, it, expect } from 'vitest';
import { analysisResultToReportView, reportBarSeries, reportWarnings } from './report';
import { createResult, customSample, samples } from './data';
import { pointToCenter } from './domain';
import { categories, type AnalysisResult } from './types';

describe('analysisResultToReportView — 完整正常数据', () => {
  const result = createResult(samples[0], 'normal');
  const view = analysisResultToReportView(result);

  it('carries basic analysis info from the result', () => {
    expect(view.centerName).toBe(samples[0].name);
    expect(view.center).toEqual(samples[0].center);
    expect(view.generatedAt).toBe(result.generatedAt);
    expect(view.scenario).toBe('normal');
    expect(view.scenarioLabel).toBe('正常结果');
    expect(view.dataSource).toBeTruthy();
    expect(view.dataLimitation).toBeTruthy();
  });

  it('counts in-circle facilities per category and matches a raw recount', () => {
    expect(view.facilityStats.map(s => s.category)).toEqual(categories);
    const recount = (c: (typeof categories)[number]) => result.facilities.filter(f => f.inCircle && f.category === c).length;
    for (const stat of view.facilityStats) {
      expect(stat.inCircleCount).toBe(recount(stat.category));
      expect(stat.inCircleCount).not.toBeNull();
      expect(stat.dataQuality).toBe('complete');
    }
    expect(view.totalInCircle).toBe(result.facilities.filter(f => f.inCircle).length);
    expect(view.hasUnknownCategory).toBe(false);
  });

  it('reports zero zone problems for the normal scenario', () => {
    expect(view.zones).toEqual([]);
    expect(view.blindZoneTotal).toBe(0);
    expect(view.unknownZoneTotal).toBe(0);
    for (const stat of view.zoneStats) {
      expect(stat.blindCount).toBe(0);
      expect(stat.unknownCount).toBe(0);
    }
  });
});

describe('analysisResultToReportView — 存在 unknown / null 的数据', () => {
  const result = createResult(samples[0], 'insufficient');
  const view = analysisResultToReportView(result);
  const pharmacy = view.facilityStats.find(s => s.category === 'pharmacy')!;
  const market = view.facilityStats.find(s => s.category === 'market')!;

  it('keeps unknown as null, never as 0', () => {
    expect(result.quality.pharmacy).toBe('unknown');
    expect(pharmacy.inCircleCount).toBeNull();
    expect(pharmacy.inCircleCount).not.toBe(0);
    expect(pharmacy.dataQuality).toBe('unknown');
    expect(view.hasUnknownCategory).toBe(true);
  });

  it('still counts categories whose data is complete', () => {
    expect(market.inCircleCount).toEqual(expect.any(Number));
    expect(market.dataQuality).toBe('complete');
  });

  it('records recorded facilities in the total without inventing unknowns', () => {
    // 数据不足类别的已记录点位计入总数，但该类别数量仍为 null
    expect(view.totalInCircle).toBe(result.facilities.filter(f => f.inCircle).length);
  });

  it('counts unknown zones separately from blind zones', () => {
    expect(view.unknownZoneTotal).toBe(1);
    expect(view.blindZoneTotal).toBe(0);
    expect(view.zoneStats.find(s => s.category === 'pharmacy')!.unknownCount).toBe(1);
    expect(view.zoneStats.find(s => s.category === 'pharmacy')!.blindCount).toBe(0);
    expect(view.zones.every(z => z.status === 'unknown')).toBe(true);
  });
});

describe('analysisResultToReportView — 场景兼容', () => {
  it('counts a blind zone per category in the missing scenario without degrading data quality', () => {
    const result = createResult(samples[0], 'missing');
    const view = analysisResultToReportView(result);
    expect(view.zoneStats.find(s => s.category === 'market')!.blindCount).toBe(1);
    expect(view.blindZoneTotal).toBe(1);
    expect(view.zones[0].status).toBe('blind');
    // missing 场景只是移除了设施点位，不意味着数据质量下降
    expect(view.facilityStats.find(s => s.category === 'market')!.dataQuality).toBe('complete');
    expect(view.facilityStats.find(s => s.category === 'market')!.inCircleCount).not.toBeNull();
  });

  it('labels a failure-scenario result as retry-succeeded', () => {
    const view = analysisResultToReportView(createResult(samples[0], 'failure'));
    expect(view.scenarioLabel).toContain('重试成功');
  });

  it('reports no zones when the center is far from preset areas', () => {
    const far = customSample(pointToCenter({ x: 900, y: 700 }));
    const view = analysisResultToReportView(createResult(far, 'missing'));
    expect(view.zones).toEqual([]);
    for (const stat of view.zoneStats) {
      expect(stat.blindCount).toBe(0);
      expect(stat.unknownCount).toBe(0);
    }
  });

  it('is deterministic for the same input', () => {
    const result = createResult(samples[1], 'normal');
    expect(analysisResultToReportView(result)).toEqual(analysisResultToReportView(result));
  });
});

describe('reportBarSeries — 报告柱状图数据', () => {
  it('maps complete stats to numeric bars matching a raw recount', () => {
    const result = createResult(samples[0], 'normal');
    const series = reportBarSeries(analysisResultToReportView(result));
    expect(series.map(s => s.category)).toEqual(categories);
    for (const item of series) {
      expect(item.value).toBe(result.facilities.filter(f => f.inCircle && f.category === item.category).length);
      expect(item.value).not.toBeNull();
      expect(item.label).toBeTruthy();
      expect(item.color).toBeTruthy();
    }
  });
  it('keeps an unknown category as null in the chart instead of a zero bar', () => {
    const view = analysisResultToReportView(createResult(samples[0], 'insufficient'));
    const pharmacy = reportBarSeries(view).find(s => s.category === 'pharmacy')!;
    expect(pharmacy.value).toBeNull();
    expect(pharmacy.value).not.toBe(0);
    expect(reportBarSeries(view).find(s => s.category === 'market')!.value).toEqual(expect.any(Number));
  });
  it('marks an unknown category on the chart axis so an empty slot is not misread as zero', () => {
    const view = analysisResultToReportView(createResult(samples[0], 'insufficient'));
    const series = reportBarSeries(view);
    expect(series.find(s => s.category === 'pharmacy')!.axisLabel).toBe('药店（数据不足）');
    expect(series.find(s => s.category === 'pharmacy')!.axisLabel).not.toBe('药店');
    for (const item of series.filter(s => s.value !== null)) expect(item.axisLabel).toBe(item.label);
  });
  it('uses plain axis labels when every category is complete', () => {
    const series = reportBarSeries(analysisResultToReportView(createResult(samples[0], 'normal')));
    expect(series.every(s => s.axisLabel === s.label)).toBe(true);
    expect(series.some(s => s.axisLabel.includes('数据不足'))).toBe(false);
  });
});

describe('reportWarnings — 报告顶部提示', () => {
  it('returns no warning for a fresh successful report', () => {
    expect(reportWarnings({ stale: false, lastAttemptFailed: false })).toEqual([]);
  });
  it('warns when conditions changed after the report was generated', () => {
    const warnings = reportWarnings({ stale: true, lastAttemptFailed: false });
    expect(warnings).toHaveLength(1);
    expect(warnings[0].kind).toBe('stale');
    expect(warnings[0].message).toContain('原分析条件');
  });
  it('warns when the latest analysis failed so the report is from the last success', () => {
    const warnings = reportWarnings({ stale: false, lastAttemptFailed: true });
    expect(warnings).toHaveLength(1);
    expect(warnings[0].kind).toBe('failed');
    expect(warnings[0].message).toContain('未成功');
  });
  it('can show both warnings at once, stale first', () => {
    expect(reportWarnings({ stale: true, lastAttemptFailed: true }).map(w => w.kind)).toEqual(['stale', 'failed']);
  });
});

describe('analysisResultToReportView — 空结果处理', () => {
  const emptyResult: AnalysisResult = {
    sample: { id: 'empty', name: '空结果位置', subtitle: '测试夹具', position: { x: 500, y: 380 }, center: pointToCenter({ x: 500, y: 380 }) },
    scenario: 'normal',
    facilities: [],
    circle: [],
    zones: [],
    quality: { market: 'complete', pharmacy: 'complete', school: 'complete' },
    generatedAt: '2026-09-10T12:00:00.000Z'
  };
  const view = analysisResultToReportView(emptyResult);

  it('treats a scan with no facilities and no zones as real zeros, not unknowns', () => {
    expect(view.totalInCircle).toBe(0);
    expect(view.facilityStats.every(s => s.inCircleCount === 0 && s.dataQuality === 'complete')).toBe(true);
    expect(view.hasUnknownCategory).toBe(false);
    expect(view.zones).toEqual([]);
    expect(view.blindZoneTotal).toBe(0);
    expect(view.unknownZoneTotal).toBe(0);
    expect(view.zoneStats.every(s => s.blindCount === 0 && s.unknownCount === 0)).toBe(true);
  });
  it('still produces chart series with explicit zeros for a truly empty scan', () => {
    const series = reportBarSeries(view);
    expect(series).toHaveLength(categories.length);
    expect(series.every(s => s.value === 0)).toBe(true);
  });
  it('carries basic info even for an empty scan', () => {
    expect(view.centerName).toBe('空结果位置');
    expect(view.generatedAt).toBe(emptyResult.generatedAt);
    expect(view.dataSource).toBeTruthy();
    expect(view.dataLimitation).toBeTruthy();
  });
});
