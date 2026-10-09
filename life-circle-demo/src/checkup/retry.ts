/**
 * 一次重试 → 面板上的几行字。
 *
 * 与 `extensions.ts`、`queryCoverage.ts` 同一条规矩：**只翻译，不判断**。后端报不出这一项
 * （旧记录）时整行省略 —— 写 0 就成了一句关于结果的结论："这一轮一次网络调用都没发"与
 * "这一版没有记录"是两件事。
 *
 * 停止原因复用 `EXTENSION_STOP_LABELS`：重试与补查跑的是同一个设施检索器，"额度用尽"
 * 在两个入口上必须是同一句话。各写一张表，迟早会在一个入口上更新、在另一个入口上忘记。
 */
import type { FacilityRetryView } from './contract';
import { extensionStopLabel } from './extensions';

const object = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value);
const count = (value: unknown): value is number =>
  typeof value === 'number' && Number.isInteger(value) && value >= 0;

/** 终态：轮询停下，不会再自己继续。`partial` 也是终态 —— 它带回了部分页面。 */
export function isRetryTerminal(status: FacilityRetryView['status'] | undefined): boolean {
  return status === 'completed' || status === 'partial' || status === 'failed'
    || status === 'cancelled';
}

/** 按钮上的字：正在跑就写正在跑，别的都写"继续检索缺口"。 */
export function retryActionLabel(running: boolean): string {
  return running ? '继续检索中…' : '继续检索缺口';
}

/**
 * 本轮预算：上限、已用、剩余一起报。
 *
 * 只报其中一个数都会误导：只说"预算 240"看不出还剩多少，只说"剩余 3"看不出这轮一共
 * 有多少。它是**这一轮重试自己的**上限，不是原任务的预算。
 */
export function retryBudgetLine(view: FacilityRetryView): string | null {
  const budget = object(view.budget) ? view.budget : null;
  if (budget === null) return null;
  const parts: string[] = [];
  if (count(budget.limit)) parts.push(`本轮预算 ${budget.limit}`);
  if (count(budget.spent)) parts.push(`已用 ${budget.spent}`);
  if (count(budget.remaining)) parts.push(`剩余 ${budget.remaining}`);
  return parts.length > 0 ? parts.join(' · ') : null;
}

/** 两本账分开：页面处理数（含命中缓存的重放）与真正花掉网络额度的次数。
 *
 * 这两个计数在契约里是必填的，所以 **0 就是观测到的 0**，照原样报出来 —— 它说的正是
 * "这一轮没花网络额度"。省略它，就会与"这一版没有记录"混成同一件事。
 */
export function retrySpendLine(view: FacilityRetryView): string | null {
  const parts: string[] = [];
  if (count(view.requests)) parts.push(`页面处理 ${view.requests}`);
  if (count(view.networkRequests)) parts.push(`新增网络 ${view.networkRequests}`);
  return parts.length > 0 ? parts.join(' · ') : null;
}

/** 停止原因那一行；未知取值原样带出，不显示空白。 */
export function retryStopLine(reason: unknown): string | null {
  const label = extensionStopLabel(reason);
  return label === null ? null : `停止原因：${label}`;
}

/** 面板上按顺序显示的那几行：为 null 的一律不显示。 */
export function retryLines(view: FacilityRetryView): string[] {
  return [retryBudgetLine(view), retrySpendLine(view), retryStopLine(view.stopReason)]
    .filter((line): line is string => line !== null);
}
