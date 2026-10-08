/**
 * 补查面板的诊断读法测三件事：**两本账分开报**、**首轮估算只在后端报过时才说**、
 * **停止原因给得出说法**（未知取值原样带出，而不是一行空白）。
 *
 * 第一条是这份文件里最要紧的：`requests` 含命中缓存的重放，`networkRequests` 才是花掉网络
 * 额度的部分。并成一个数时，"缓存把能答的页面都答完了"会被读成"又花了很多额度"。
 */
import { describe, expect, it } from 'vitest';
import { EXTENSION_STOP_LABELS, extensionBudgetLine, extensionLines, extensionPlanLine,
  extensionStopLabel, extensionStopLine } from './extensions';
import { extensionView } from './fixtures';

describe('extension budget readout', () => {
  it('keeps local page processing and new network calls apart', () => {
    expect(extensionBudgetLine(extensionView())).toBe('页面处理 20 · 新增网络 4 · 预算 60');
  });

  it('omits the ceiling an old record did not keep, but keeps a real zero', () => {
    // 预算上限缺席（旧记录）时只报前两项，不补一个 0 当成"没预算"。
    expect(extensionBudgetLine(extensionView({ budget: {} }))).toBe('页面处理 20 · 新增网络 4');
    // 用量上的 0 是量出来的：一次都没处理、也没发新调用。
    expect(extensionBudgetLine(extensionView({ requests: 0, networkRequests: 0, budget: {} })))
      .toBe('页面处理 0 · 新增网络 0');
  });
});

describe('first-round plan readout', () => {
  it('reports the frozen estimate, including how much the cache can answer', () => {
    expect(extensionPlanLine(extensionView()))
      .toBe('首轮 20 页，缓存可复用 16 页，预计新增 4 次调用');
  });

  it('says nothing when an old record has no plan, and never invents a zero', () => {
    const lines = extensionLines(extensionView({ initialPlan: null }));
    expect(lines.some(line => line.includes('首轮'))).toBe(false);
    expect(lines.some(line => line.includes('缓存可复用'))).toBe(false);
    // 计划在但缺键：只报有的那几项；一项都没有就整行省略。
    expect(extensionPlanLine(extensionView({ initialPlan: { initialPageCount: 6 } }))).toBe('首轮 6 页');
    expect(extensionPlanLine(extensionView({ initialPlan: {} }))).toBeNull();
  });
});

describe('stop reason readout', () => {
  it('names the two new ceilings apart from each other', () => {
    // 网络额度用尽与本地处理上限是两件事：前者说新调用发不出去了，后者说调度步数到顶了。
    expect(extensionStopLabel('network_budget_exhausted')).toBe('新增网络额度用尽，已保留缓存结果');
    expect(extensionStopLabel('processing_limit_reached')).toBe('达到本地处理步数上限');
    expect(extensionStopLabel('daily_budget_exhausted')).toBe('今日应用额度用尽');
    expect(extensionStopLabel('task_budget_exhausted')).toBe('本任务预算用尽');
  });

  it('shows an unknown reason verbatim instead of a blank line', () => {
    expect(extensionStopLabel('a_reason_from_a_newer_backend')).toBe('a_reason_from_a_newer_backend');
    expect(extensionStopLine('a_reason_from_a_newer_backend'))
      .toBe('停止原因：a_reason_from_a_newer_backend');
    // 上游拒绝也各有各的说法，不能都落成"额度用尽"。
    expect(EXTENSION_STOP_LABELS.rate_limit).toContain('限流');
    expect(EXTENSION_STOP_LABELS.possible_truncation).toContain('截断');
  });

  it('omits the line when no reason was recorded', () => {
    // 正常结束与"旧记录没记"都读作 null：不写"未知"，也不留一行空白。
    expect(extensionStopLabel(null)).toBeNull();
    expect(extensionStopLabel(undefined)).toBeNull();
    expect(extensionStopLabel('')).toBeNull();
    expect(extensionLines(extensionView({ stopReason: null }))
      .some(line => line.includes('停止原因'))).toBe(false);
  });

  it('lists budget, plan and reason in the order the panel shows them', () => {
    expect(extensionLines(extensionView({ stopReason: 'network_budget_exhausted' }))).toEqual([
      '页面处理 20 · 新增网络 4 · 预算 60',
      '首轮 20 页，缓存可复用 16 页，预计新增 4 次调用',
      '停止原因：新增网络额度用尽，已保留缓存结果',
    ]);
  });
});
