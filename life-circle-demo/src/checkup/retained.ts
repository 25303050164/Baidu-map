/**
 * 明细到期之后，面板上说什么（§5 B2 决策 2）。
 *
 * 与 `queryCoverage.ts`、`retry.ts` 同一条规矩：**只翻译，不判断**。到期时刻与原因都是后端
 * 说的，界面不自己算"大概过期了吧" —— 时钟在浏览器这边，而期限在服务端那边。
 *
 * 三条口径：
 *
 * * **按真实原因分开说**：浏览会话结束、之后又完成了三次体检、保留期开始之前的旧数据、
 *   明细已被清理，是四件不同的事。统一写成"数据已过期"，用户无法判断"再来一次会不会更好"，
 *   而其中只有第一条和第二条是他自己能影响的。
 * * **到期不是失败，也不是可重试错误**：结论与分数都还在，只是明细不再提供。所以这里
 *   给的是"还能看什么"，不是"重试一下"。
 * * **能看得到什么时候到期**：`expiresAt` 存在时显示本地时间 —— "什么时候会消失"是用户
 *   唯一能提前行动的依据。
 */
import type { RetentionView } from './contract';

const RETENTION_REASONS: Record<string, string> = {
  session_closed: '这个浏览会话已经结束（最后一个标签页关闭后超过了宽限期）',
  superseded: '这次体检之后又完成了三次新的体检',
  legacy: '它是本应用开始记录保留期之前的数据',
  cleared: '它的明细已经被清理',
};

const reasonText = (reason: unknown): string =>
  typeof reason === 'string' && reason
    ? RETENTION_REASONS[reason] ?? `保留期已过（${reason}）`
    : '保留期已过';

/** 服务端时刻 → 本地时间；只用于显示"什么时候到期"，不参与任何判定。 */
function localTime(seconds: number): string {
  const at = new Date(seconds * 1000);
  if (Number.isNaN(at.getTime())) return '未知时刻';
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())} `
    + `${pad(at.getHours())}:${pad(at.getMinutes())}`;
}

/**
 * 到期说明那一行；明细还能提供时返回 null（这一行只在到期之后出现）。
 *
 * 未知取值原样带出：后端新加一个原因时，宁可露出机器取值，也不要编一句可能说反的话。
 */
export function expiryNotice(retention: RetentionView | null | undefined): string | null {
  if (!retention || retention.detailsAvailable) return null;
  return `这次体检的明细已到期：${reasonText(retention.reason)}。设施名称、UID、地址与坐标`
    + `不再提供；下面还能看到的是到期前定稿的结论与汇总，没有重算。`;
}

/** 到期前的提示：明细保留到什么时候。没有到期时刻（无会话、后续体检不足三次）时不显示。 */
export function expiryLine(retention: RetentionView | null | undefined): string | null {
  if (!retention || !retention.detailsAvailable || retention.expiresAt === null) return null;
  return `明细保留至 ${localTime(retention.expiresAt)}（之后只保留结论与汇总）。`;
}

/** 到期之后还能看到的关键数字。取不到的项一律省略，不补 0。 */
export function retainedSummaryLines(summary: Record<string, unknown>): string[] {
  const lines: string[] = [];
  const facilities = object(summary.facilities) ? summary.facilities : null;
  const scores = object(summary.scores) ? summary.scores : null;
  const overall = scores && object(scores.overall) ? scores.overall : null;

  if (facilities) {
    const counts = object(facilities.counts) ? facilities.counts : null;
    const total = counts && typeof counts.facilities === 'number' ? counts.facilities : null;
    const status = typeof facilities.queryStatus === 'string' ? facilities.queryStatus : null;
    if (total !== null) lines.push(`检索到的设施：${total} 处`);
    if (status !== null) lines.push(`设施检索状态：${status}`);
    if (typeof facilities.stopReason === 'string' && facilities.stopReason) {
      lines.push(`停止原因：${facilities.stopReason}`);
    }
  }
  if (overall) {
    if (overall.available === true) {
      const lower = number(overall.coverageLowerPct);
      const upper = number(overall.coverageUpperPct);
      const assessable = number(overall.assessablePct);
      if (lower !== null && upper !== null) {
        lines.push(`总体覆盖区间：${fixed(lower)}–${fixed(upper)}%`);
      }
      if (assessable !== null) lines.push(`可评估比例：${fixed(assessable)}%`);
    } else if (typeof overall.reason === 'string') {
      // "给不出总体分"本身是一个结论，要说出来，而不是留一个空栏。
      lines.push(`总体分：无法给出（${overall.reason}）`);
    }
  }
  return lines;
}

const object = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value);
const number = (value: unknown): number | null =>
  typeof value === 'number' && Number.isFinite(value) ? value : null;
const fixed = (value: number): string => value.toFixed(1);
