/**
 * 到期之后面板上那几句话：按**真实原因**分开说，并且说清"还能看到什么"。
 *
 * 要钉住的是三件容易被顺手做错的事：
 *
 * 1. 四种到期原因不能被压成一句"数据过期"——用户能对第一条和第二条做点什么，对第三条不能；
 * 2. 到期不是失败：这里给的是"还能看什么"，不是"重试"；
 * 3. 到期前要能看到"保留到什么时候"，那是唯一能提前行动的依据。
 */
import { describe, expect, it } from 'vitest';
import { expiryLine, expiryNotice, retainedSummaryLines } from './retained';
import { retainedView, retentionView, task } from './fixtures';

describe('expiry notice', () => {
  it('names the reason instead of saying "expired" once', () => {
    const notice = (reason: string) => expiryNotice(retentionView({
      detailsAvailable: false, reason: reason as never, cleared: true }));
    expect(notice('session_closed')).toContain('浏览会话已经结束');
    expect(notice('superseded')).toContain('又完成了三次新的体检');
    expect(notice('legacy')).toContain('开始记录保留期之前');
    expect(notice('cleared')).toContain('明细已经被清理');
    // 三种说法互不相同：压成一句就丢掉了"下一步是什么"。
    expect(new Set([notice('session_closed'), notice('superseded'), notice('legacy')]).size).toBe(3);
  });

  it('says what is still readable and that nothing was recomputed', () => {
    const notice = expiryNotice(retentionView({ detailsAvailable: false, reason: 'legacy',
      cleared: true }));
    expect(notice).toContain('设施名称、UID、地址与坐标');
    expect(notice).toContain('没有重算');
  });

  it('shows an unknown reason verbatim rather than inventing one', () => {
    // 后端新加一个原因时，宁可露出机器取值，也不要编一句可能说反的话。
    expect(expiryNotice(retentionView({ detailsAvailable: false,
      reason: 'some_new_reason' as never }))).toContain('some_new_reason');
  });

  it('says nothing while the detail is still there', () => {
    expect(expiryNotice(retentionView())).toBeNull();
    expect(expiryNotice(null)).toBeNull();
    expect(expiryNotice(undefined)).toBeNull();
  });
});

describe('expiry line', () => {
  it('shows when the detail will stop being readable, in local time', () => {
    const line = expiryLine(retentionView({ expiresAt: Date.UTC(2026, 9, 9, 4, 30) / 1000 }));
    expect(line).toContain('明细保留至');
    // 显示的是**本地**时间，所以只断言它确实格式化出了一个时刻，而不是断言某个时区。
    expect(line).toMatch(/明细保留至 \d{4}-\d{2}-\d{2} \d{2}:\d{2}/);
  });

  it('shows nothing when no deadline could be computed', () => {
    // 没有会话、后面也还没有三次体检：没有时刻可报，就不报 —— 空的一行会被读成"马上要清"。
    expect(expiryLine(retentionView())).toBeNull();
    expect(expiryLine(retentionView({ detailsAvailable: false, reason: 'legacy' }))).toBeNull();
  });
});

describe('retained summary lines', () => {
  it('reports the numbers that survive, without inventing the ones that do not', () => {
    const lines = retainedSummaryLines(retainedView().summary);
    expect(lines).toEqual(expect.arrayContaining([
      '检索到的设施：3 处', '设施检索状态：partial',
      '停止原因：network_budget_exhausted',
      '总体覆盖区间：40.0–70.0%', '可评估比例：88.5%',
    ]));
  });

  it('says why the overall score could not be given instead of leaving a blank', () => {
    const lines = retainedSummaryLines({ scores: { overall: { available: false,
      reason: 'missing_categories' } } });
    expect(lines).toEqual(['总体分：无法给出（missing_categories）']);
  });

  it('omits what the backend did not report', () => {
    expect(retainedSummaryLines({})).toEqual([]);
    expect(retainedSummaryLines({ facilities: { counts: {} } })).toEqual([]);
  });
});

describe('task view retention', () => {
  it('carries the retention the panel reads before asking for anything', () => {
    expect(task().retention?.detailsAvailable).toBe(true);
    expect(task({ retention: retentionView({ detailsAvailable: false, reason: 'cleared',
      cleared: true }) }).retention?.detailsAvailable).toBe(false);
  });
});
