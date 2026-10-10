/**
 * §5 B2 决策 1：这次体检"查完了多少" → 面板上的一行字。
 *
 * 与 `extensions.ts` 同一条规矩：**只翻译，不判断**。后端报不出这一项（旧修订、旧后端）
 * 时返回 `null`，界面就什么都不显示 —— 显示 0% 会变成一句关于**结果**的结论，而
 * "这一版没有记录"与"一页都没查成"是两件事。
 *
 * 三个口径：
 *
 * * **共同完成，不是平均**：比例由后端算 —— 它取的是各小类未完成区域的**并集**。
 *   界面不再自己算一遍：重算一遍就一定会与报告里的结论分叉，而分叉的那一天没人知道该信谁。
 * * **合格线与实测值分开**：`target` 是合格线，`ratio` 是实测。达标不等于十类全查完
 *   （`partial` 也可能是 `met`），未达标也不等于检索失败。
 * * **未达标必须说清真实原因**：额度用尽、上游限流、请求超时、本地步数上限对应完全不同的
 *   下一步动作。把它们统一写成"请求超时"，会让用户以为再点一次就能好，
 *   而额度用尽其实要等到次日 —— 这是运营者明确要求按原因区分的那一条。
 */
import type { FacilityGroup } from './contract';

const object = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value);
const ratio = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1;

export type QueryAreaCoverage = {
  status: 'met' | 'unmet' | 'unknown';
  /** 合格线，如 0.8。 */
  target: number;
  /** 共同完成比例；量不出来时为 null（`unknown`）。 */
  ratio: number | null;
  residual: number | null;
  categories: number;
};

/** 后端报的那一项，校验后才用；形状不对或旧修订 → null（界面省略，不补零）。 */
export function readQueryAreaCoverage(group: FacilityGroup | null): QueryAreaCoverage | null {
  const raw = group?.queryAreaCoverage;
  if (!object(raw)) return null;
  const status = raw.status;
  if (status !== 'met' && status !== 'unmet' && status !== 'unknown') return null;
  const target = raw.target;
  if (typeof target !== 'number' || !Number.isFinite(target) || target <= 0 || target > 1) return null;
  // Bound to locals before the guard: a predicate on a property path narrows less
  // reliably than one on a `const`, and a silently-unnarrowed value here would be a
  // `null` that looks like "not measured" when it was measured.
  const rawRatio = raw.sharedCompletionRatio;
  const rawResidual = raw.residualRatio;
  const measured = ratio(rawRatio) ? rawRatio : null;
  const residual = ratio(rawResidual) ? rawResidual : null;
  // `met`/`unmet` 都是**关于比例**的结论，所以没有可用的比例就不是一条结论：
  // 整条记录当作没记录，而不是留下一个"结论为 met、比例为空"的自相矛盾。
  // `unknown` 相反 —— 它本来就说"量不出来"，比例为空正是它的含义。
  if (status !== 'unknown' && measured === null) return null;
  const categories = Array.isArray(raw.categories) ? raw.categories.length : 0;
  return { status, target, ratio: measured, residual, categories };
}

const percent = (value: number): string => `${(value * 100).toFixed(1)}%`;

/**
 * 未达标时，"下一步到底能不能靠重试解决"。
 *
 * `permission` 是唯一一个明确"重试没用"的：它是 AK 的服务勾选问题，再点多少次都一样。
 * 把它写成"可重试"会让人反复花钱换同一个拒绝。
 */
const RETRY_ADVICE: Record<string, string> = {
  network_budget_exhausted: '本轮请求额度已用完，可重试继续',
  task_budget_exhausted: '本轮请求额度已用完，可重试继续',
  budget_exhausted: '本轮请求额度已用完，可重试继续',
  // 与"本任务额度"分开：今日额度要等到北京时间次日才恢复，说成"可重试继续"会让用户
  // 立刻再点一次，然后撞同一堵墙。
  daily_budget_exhausted: '今日额度已用完（北京时间次日恢复），届时可重试继续',
  processing_limit_reached: '本地处理步数达到上限，可重试继续',
  rate_limit: '上游限流，稍后可重试继续',
  quota: '上游接口额度不足，稍后可重试继续',
  deadline_reached: '请求超时，可重试继续',
  cancelled: '本次体检已取消，可重试继续',
  permission: '接口权限不足，重试不会改善；请先核对 AK 的服务勾选',
};

/** 面板上的那一行；没有可报的东西时返回 null。 */
export function queryCoverageLine(group: FacilityGroup | null): string | null {
  const coverage = readQueryAreaCoverage(group);
  if (coverage === null) return null;
  if (coverage.status === 'unknown' || coverage.ratio === null) {
    return '本次无法计算"共同完成面积"：圈面没有可度量的面积。';
  }
  const head = `共同完成圈面 ${percent(coverage.ratio)}（合格线 ${percent(coverage.target)}）`;
  if (coverage.status === 'met') {
    // 达标 ≠ 十类都查完：查询可能仍是 partial（例如细分未完成），两个口径都要留。
    return `${head} · 达标${group?.queryStatus === 'completed' ? '' : '（仍有查询未结束，已取得的证据保留）'}`;
  }
  const residual = coverage.residual === null ? null : percent(coverage.residual);
  return `${head} · 未达标${residual === null ? '' : `，仍有 ${residual} 的地段未查完`}`;
}

/** 未达标时的下一步：真实原因 + 能不能靠重试解决。达标或未记录时返回 null。 */
export function queryCoverageRetryLine(group: FacilityGroup | null): string | null {
  const coverage = readQueryAreaCoverage(group);
  if (coverage === null || coverage.status !== 'unmet') return null;
  const reason = typeof group?.stopReason === 'string' && group.stopReason ? group.stopReason : null;
  if (reason === null) return '可重试继续推进；已取得的证据会保留。';
  const advice = RETRY_ADVICE[reason];
  if (advice === undefined) {
    // 后端新加的原因：原样带出机器取值，也不要编一句可能说反的话。
    return `未查完（${reason}）；已取得的证据会保留。`;
  }
  return `${advice}；已取得的证据会保留。`;
}
