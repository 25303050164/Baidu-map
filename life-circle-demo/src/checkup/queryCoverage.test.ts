/**
 * §5 B2 决策 1 的界面口径：达标/未达标要说清，未达标要说清**为什么**。
 *
 * 最要紧的两条：
 *
 * * 报不出来（旧修订）时**什么都不显示** —— 显示 0% 就成了"一页都没查成"这个结论。
 * * 未达标的原因**按真实停止原因分**：额度用尽要等到次日，请求超时可以立刻再试，
 *   权限不足再点多少次都一样。把它们说成同一句话，等于骗用户去重试。
 */
import { describe, expect, it } from 'vitest';
import type { FacilityGroup } from './contract';
import { queryCoverageLine, queryCoverageRetryLine, readQueryAreaCoverage } from './queryCoverage';

const group = (overrides: Partial<FacilityGroup> = {}): FacilityGroup => Object.assign({
  queryStatus: 'completed', catalogCompleteness: 'unverified', provider: 'baidu_place',
  apiVersion: '3.0', dataSource: 'baidu_place', queryDomain: {}, dataObtainedAt: null,
  initialPlan: null, countsByCategory: {}, facilities: [], nearbyFacilities: [],
  reviewCandidates: [], excludedCandidates: [], quarantine: [], queryCoverage: [],
  queryIncompleteRegions: null, queryAreaCoverage: null, statistics: {}, warnings: [],
  stopReason: null,
} as FacilityGroup, overrides);

const coverage = (fields: Record<string, unknown>): Record<string, unknown> => ({
  target: 0.8, categories: ['pharmacy', 'market'], ...fields,
});

describe('reading the published ratio', () => {
  it('shows nothing for a revision that never recorded it', () => {
    const old = group();
    expect(readQueryAreaCoverage(old)).toBeNull();
    expect(queryCoverageLine(old)).toBeNull();
    expect(queryCoverageRetryLine(old)).toBeNull();
  });

  it('treats a measured zero as measured, not as "unknown"', () => {
    const zero = group({ queryAreaCoverage: coverage({
      status: 'unmet', sharedCompletionRatio: 0, residualRatio: 1 }) });
    expect(readQueryAreaCoverage(zero)?.ratio).toBe(0);
    expect(queryCoverageLine(zero)).toContain('0.0%');
  });

  it('refuses a shape it cannot trust instead of guessing', () => {
    for (const bad of [
      { status: 'probably', sharedCompletionRatio: 0.9, residualRatio: 0.1, target: 0.8 },
      { status: 'met', sharedCompletionRatio: 1.2, residualRatio: 0, target: 0.8 },
      { status: 'met', sharedCompletionRatio: 0.9, residualRatio: 0.1, target: 0 },
      { status: 'met', sharedCompletionRatio: 0.9, residualRatio: 0.1 },
    ]) {
      expect(readQueryAreaCoverage(group({ queryAreaCoverage: bad }))).toBeNull();
    }
  });
});

describe('the line a reader sees', () => {
  it('says met, and still says the queries are not all finished', () => {
    const partial = group({ queryStatus: 'partial', queryAreaCoverage: coverage({
      status: 'met', sharedCompletionRatio: 1, residualRatio: 0 }) });
    const line = queryCoverageLine(partial)!;
    expect(line).toContain('100.0%');
    expect(line).toContain('合格线 80.0%');
    expect(line).toContain('达标');
    // 达标与"所有查询都结束了"是两件事，两个口径都要留，否则读者会把 met 读成 completed。
    expect(line).toContain('仍有查询未结束');
  });

  it('does not add the partial caveat to a run whose queries all finished', () => {
    const done = group({ queryAreaCoverage: coverage({
      status: 'met', sharedCompletionRatio: 1, residualRatio: 0 }) });
    expect(queryCoverageLine(done)).not.toContain('仍有查询未结束');
  });

  it('names the residual when the round came up short', () => {
    const short = group({ queryAreaCoverage: coverage({
      status: 'unmet', sharedCompletionRatio: 0.6434, residualRatio: 0.3566 }) });
    const line = queryCoverageLine(short)!;
    expect(line).toContain('64.3%');
    expect(line).toContain('未达标');
    expect(line).toContain('35.7%');
  });

  it('says why the ratio could not be measured rather than inventing one', () => {
    const unknown = group({ queryAreaCoverage: coverage({
      status: 'unknown', reason: 'empty_boundary_area',
      sharedCompletionRatio: null, residualRatio: null }) });
    expect(queryCoverageLine(unknown)).toContain('无法计算');
    expect(queryCoverageLine(unknown)).not.toContain('未达标');
  });
});

describe('what to do next, by real reason', () => {
  const unmet = (stopReason: string | null) => group({
    queryStatus: 'partial', stopReason,
    queryAreaCoverage: coverage({ status: 'unmet', sharedCompletionRatio: 0.6, residualRatio: 0.4 }) });

  it('tells a spent day apart from a timeout', () => {
    expect(queryCoverageRetryLine(unmet('daily_budget_exhausted'))).toContain('今日额度已用完');
    expect(queryCoverageRetryLine(unmet('daily_budget_exhausted'))).toContain('次日');
    expect(queryCoverageRetryLine(unmet('task_budget_exhausted'))).toContain('本轮请求额度已用完');
    expect(queryCoverageRetryLine(unmet('deadline_reached'))).toContain('请求超时');
  });

  it('says plainly that retrying a permission refusal does not help', () => {
    const advice = queryCoverageRetryLine(unmet('permission'))!;
    expect(advice).toContain('重试不会改善');
    expect(advice).not.toContain('可重试');
  });

  it('carries an unknown reason through instead of writing a sentence that may be wrong', () => {
    const advice = queryCoverageRetryLine(unmet('brand_new_reason'))!;
    expect(advice).toContain('brand_new_reason');
  });

  it('accepts a round that ran out of allowance only after reaching the line', () => {
    // 运营者的口径：停止派发的触发条件是"额度用尽"，而 80% 是**那一刻**的验收线。
    // 额度用尽 + 已完成 85% → 达标、干净收工、不问用户要不要重试。
    // 反过来说，额度还剩就不该提前收工（那不在这个函数里表达，由后端照常查完）。
    const exhausted = group({
      queryStatus: 'partial', stopReason: 'network_budget_exhausted',
      queryAreaCoverage: coverage({ status: 'met', sharedCompletionRatio: 0.85,
        residualRatio: 0.15 }) });
    expect(queryCoverageLine(exhausted)).toContain('达标');
    expect(queryCoverageRetryLine(exhausted)).toBeNull();
  });

  it('offers no next step when the round met the line', () => {
    const met = group({ queryAreaCoverage: coverage({
      status: 'met', sharedCompletionRatio: 0.95, residualRatio: 0.05 }) });
    expect(queryCoverageRetryLine(met)).toBeNull();
  });

  it('still says the evidence was kept when no reason was recorded', () => {
    expect(queryCoverageRetryLine(unmet(null))).toContain('已取得的证据会保留');
  });
});
