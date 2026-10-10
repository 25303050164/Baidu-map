/**
 * 一次按需补查 → 面板上的几行字。
 *
 * 与 `capabilities.ts` 同一条规矩：**只翻译，不判断**。后端报了哪几项就说哪几项，报不出来
 * 的（旧后端、旧记录）一律省略 —— 补查面板上写 0 就成了一句关于结果的结论："这次一次网络
 * 调用都没发"与"这一版没有记录"是两件事。
 *
 * 三条口径：
 *
 * * **两本账**：`requests` 是本地处理的页面数（含命中缓存的重放），`networkRequests` 才是
 *   真正花掉网络额度的次数。把它们并成一个数，会让"额度用尽"看起来像"查了很多"。
 * * **首轮是估算**：`initialPlan` 是**执行前**（派发第一页之前）算出的估计，随这次补查的
 *   结果一起定稿，其中已经算进了缓存可复用的页数，所以"首轮 20 页"不等于"要发 20 次调用"；
 *   它是估算、不是额度预留。
 * * **停止原因分开读**：`network_budget_exhausted` 说的是新增网络额度用尽（缓存结果仍然
 *   保留下来了），`processing_limit_reached` 说的是本地调度上限到了 —— 后者完全可能是在
 *   把缓存里能答的页面都取完之后才发生的。未知取值原样带出，绝不显示空白。
 */
import type { FacilityExtensionView } from './contract';

type RecordValue = Record<string, unknown>;
const object = (value: unknown): value is RecordValue =>
  value !== null && typeof value === 'object' && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === 'string' && value.length > 0;
/** 页数、调用数、预算都是非负整数；小数或负数说明这不是后端报的那个数。 */
const count = (value: unknown): value is number =>
  typeof value === 'number' && Number.isInteger(value) && value >= 0;

/**
 * 停止原因 → 中文说法。表里没有的取值**原样显示**：后端新加一个原因时，界面宁可露出机器
 * 取值，也不要显示一行空白让人以为"没有原因"。
 */
export const EXTENSION_STOP_LABELS: Record<string, string> = {
  // 新增的两种：网络额度与本地处理上限是两本账，不能都读成"额度没了"。
  network_budget_exhausted: '新增网络额度用尽，已保留缓存结果',
  processing_limit_reached: '达到本地处理步数上限',
  // 原有的两类预算。
  daily_budget_exhausted: '今日应用额度用尽',
  task_budget_exhausted: '本任务预算用尽',
  budget_exhausted: '预算用尽',
  // 上游拒绝。
  rate_limit: '触发限流，已保留已取到的结果',
  permission: '接口权限不足',
  quota: '接口额度不足',
  deadline_reached: '达到时间上限',
  cancelled: '已取消',
  completed: '检索完成',
  partial: '仅取到部分结果',
  // 结果密度的信号：块被截断了，与额度无关。
  possible_truncation: '结果可能被截断',
  page_limit: '达到分页上限',
  pagination_anomaly: '分页异常',
  pagination_uncertain: '分页结果不确定',
};

/** 停止原因的中文标签；`null`/缺失 → null（不显示），未知取值原样返回。 */
export function extensionStopLabel(reason: unknown): string | null {
  if (!text(reason)) return null;
  return EXTENSION_STOP_LABELS[reason] ?? reason;
}

/**
 * 这次补查花了多少：页面处理、新增网络调用与它自己的预算上限。
 *
 * "预算"取 `budget.limit`：它是这一次补查自己的上限，不是原任务的预算。旧行没记这一项时
 * 只报前两个数，不补一个 0。
 */
export function extensionBudgetLine(view: FacilityExtensionView): string | null {
  const parts: string[] = [];
  if (count(view.requests)) parts.push(`页面处理 ${view.requests}`);
  if (count(view.networkRequests)) parts.push(`新增网络 ${view.networkRequests}`);
  const limit = object(view.budget) ? view.budget.limit : undefined;
  if (count(limit)) parts.push(`预算 ${limit}`);
  return parts.length > 0 ? parts.join(' · ') : null;
}

/**
 * 执行前算出的首轮估算：首轮页数、其中缓存可复用的页数、预计新增的调用数。
 *
 * `initialPlan` 为 null（旧后端/旧记录）时返回 null —— 界面据此整行省略；对象在但个别键
 * 缺失时只报有的那几项。乘积是估算而不是预留，所以这里从不把它写成"至少"或"保证"。
 */
export function extensionPlanLine(view: FacilityExtensionView): string | null {
  const plan = object(view.initialPlan) ? view.initialPlan : null;
  if (plan === null) return null;
  const parts: string[] = [];
  if (count(plan.initialPageCount)) parts.push(`首轮 ${plan.initialPageCount} 页`);
  if (count(plan.reusableInitialPageCount)) {
    parts.push(`缓存可复用 ${plan.reusableInitialPageCount} 页`);
  }
  if (count(plan.estimatedNewInitialCalls)) {
    parts.push(`预计新增 ${plan.estimatedNewInitialCalls} 次调用`);
  }
  return parts.length > 0 ? parts.join('，') : null;
}

/** 停止原因那一行；未知取值原样带出，不显示空白。 */
export function extensionStopLine(reason: unknown): string | null {
  const label = extensionStopLabel(reason);
  return label === null ? null : `停止原因：${label}`;
}

/** 面板上按顺序显示的那几行：为 null 的一律不显示。 */
export function extensionLines(view: FacilityExtensionView): string[] {
  return [extensionBudgetLine(view), extensionPlanLine(view), extensionStopLine(view.stopReason)]
    .filter((line): line is string => line !== null);
}
