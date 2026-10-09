/**
 * 重试面板上的那几行字：把后端报的事实翻译出来，不替它下结论。
 *
 * 三条要钉住的：
 *
 * 1. **两本账分开**：`requests` 是页面处理数（含命中缓存的重放），`networkRequests` 才是真正
 *    花掉网络额度的次数。并成一个数，"额度用尽"会看起来像"查了很多"。
 * 2. **预算三个数一起报**：只说上限看不出还剩多少，只说剩余看不出这轮一共多少。
 * 3. **报不出来就整行省略**：旧记录没有这一项时写 0，就成了一句关于结果的结论。
 */
import { describe, expect, it } from 'vitest';
import { isRetryTerminal, retryActionLabel, retryBudgetLine, retryLines, retrySpendLine,
  retryStopLine } from './retry';
import { retryView } from './fixtures';

describe('retry panel lines', () => {
  it('reports the round budget as limit, spent and remaining together', () => {
    expect(retryBudgetLine(retryView())).toBe('本轮预算 240 · 已用 63 · 剩余 177');
  });

  it('reports only the budget figures the backend actually gave', () => {
    expect(retryBudgetLine(retryView({ budget: { limit: 240 } }))).toBe('本轮预算 240');
    // 一个数都没有时整行省略，而不是显示一行空的"预算"。
    expect(retryBudgetLine(retryView({ budget: {} }))).toBeNull();
  });

  it('keeps processed pages and network calls apart', () => {
    expect(retrySpendLine(retryView())).toBe('页面处理 63 · 新增网络 41');
    // 全部命中缓存的一轮：新增网络是 0 —— 那是一个观测到的 0（契约里这两个字段是必填的），
    // 报出来才说明"这一轮没花网络额度"；省略它会与"这一版没有记录"混在一起。
    expect(retrySpendLine(retryView({ requests: 20, networkRequests: 0 })))
      .toBe('页面处理 20 · 新增网络 0');
    expect(retrySpendLine(retryView({ requests: 0, networkRequests: 0 })))
      .toBe('页面处理 0 · 新增网络 0');
  });

  it('turns a stop reason into the same sentence the extension panel uses', () => {
    expect(retryStopLine('network_budget_exhausted')).toBe('停止原因：新增网络额度用尽，已保留缓存结果');
    expect(retryStopLine('daily_budget_exhausted')).toBe('停止原因：今日应用额度用尽');
    // 正常结束没有原因：不显示"停止原因：null"。
    expect(retryStopLine(null)).toBeNull();
    expect(retryStopLine(undefined)).toBeNull();
  });

  it('shows an unknown stop reason verbatim instead of leaving a blank', () => {
    // 后端新加一个原因时，宁可露出机器取值，也不要让人以为"没有原因"。
    expect(retryStopLine('some_new_reason')).toBe('停止原因：some_new_reason');
  });

  it('orders the lines and omits the ones with nothing to say', () => {
    expect(retryLines(retryView())).toEqual([
      '本轮预算 240 · 已用 63 · 剩余 177', '页面处理 63 · 新增网络 41',
    ]);
    // 预算那三个数是可缺的（旧行没记），两个计数是必填的：前者缺了就省略，后者是 0 就是 0。
    expect(retryLines(retryView({ budget: {}, requests: 0, networkRequests: 0 })))
      .toEqual(['页面处理 0 · 新增网络 0']);
  });

  it('labels the button by whether a round is running, not by the last outcome', () => {
    expect(retryActionLabel(false)).toBe('继续检索缺口');
    expect(retryActionLabel(true)).toBe('继续检索中…');
  });

  it('treats partial as terminal and running as not', () => {
    expect(isRetryTerminal('partial')).toBe(true);
    expect(isRetryTerminal('cancelled')).toBe(true);
    expect(isRetryTerminal(undefined)).toBe(false);
    expect(isRetryTerminal('running')).toBe(false);
  });
});
