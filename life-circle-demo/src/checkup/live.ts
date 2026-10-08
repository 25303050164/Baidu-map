/**
 * 运行中任务的实时读法：已用时、当前步骤、最近一次联系与服务端最近一次活动。
 *
 * 这里没有百分比、没有"预计剩余"，也没有转圈动画 —— 界面上会动的只有时钟：
 *
 * - **时间都在服务端的墙钟上。** 已用时、本阶段、本步骤、服务端最近活动都用后端给的
 *   时刻相减；两次轮询之间按本机时钟把"服务端此刻"往前推（`serverNow`），所以秒数每秒
 *   递增，而新的一次回答一到就重新对齐。本机与服务器的时钟差不进入任何读数。
 * - **计数只说已经发生的事。** `count` 是这一步真的发出的采样、请求、判定的格、核验的
 *   设施；`limit` 是它不会越过的数（预算、候选数），写成"上限""候选"，不写成分母。
 * - **三种"没有新消息"分开说。** 连接中断（问不到后端）、正在计算或等待（问得到、后端
 *   最近有动静，或者这一步本来就不报中间计数）、疑似停滞（问得到、但后端很久没有任何
 *   新进展）。三者的出路不同，混成一句"请稍候"会让人在断网时干等、在停滞时误以为断网。
 */
import type { CheckupTaskView, TaskProgress } from './contract';
import type { CheckupState } from './types';
import { STAGES, type Stage } from './validate';

/**
 * 服务端这么久没有任何新进展（计数、步骤、修订、状态都没变）才说"疑似停滞"。
 *
 * 报计数的步骤里，每发一次请求、每判定一批格、每建好或校验完两万条边都算进展，最长的
 * 正常间隔是一次请求的超时，90 秒足够。不报中间计数的步骤（读入路网文件、建立评估域、
 * 准备步行图、建立接入索引）从头到尾只有开始那一下，它们另用 15 分钟。
 */
export const STALL_SECONDS = 90;
export const QUIET_STALL_SECONDS = 900;

/** 这一步要沉默多久才算疑似停滞。 */
export function stallAfter(step: TaskProgress | null | undefined): number {
  return step && step.count === null ? QUIET_STALL_SECONDS : STALL_SECONDS;
}

/** 这些阶段里任务还没有结论，界面每秒重算一次读数；其余阶段时钟停下。 */
export const TICKING_PHASES: readonly CheckupState['phase'][] = ['submitting', 'restoring', 'queued', 'running', 'cancelling'];

/** 秒数的读法：不足一分钟写秒，再长写分秒、时分。只取整，不四舍五入成"约"。 */
export function duration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return '—';
  const total = Math.max(0, Math.floor(seconds));
  if (total < 60) return `${total} 秒`;
  if (total < 3600) return `${Math.floor(total / 60)} 分 ${total % 60} 秒`;
  return `${Math.floor(total / 3600)} 小时 ${Math.floor(total % 3600 / 60)} 分`;
}

/**
 * 最近一次成功的回答：`at` 是它到达时的本机时刻（毫秒），`serverTime` 是回答里的服务端
 * 时刻（秒）。两者一起才能把"服务端此刻"推算出来；旧版后端不给 `serverTime`。
 */
export type Contact = { at: number; serverTime?: number };

/** 连接中断的经过：从第一次失败起算，已重连几次。 */
export type Reconnect = { since: number; attempts: number };

export type LiveKind = 'submitting' | 'restoring' | 'queued' | 'working' | 'cancelling' | 'lost' | 'stalled';

export type LiveView = {
  kind: LiveKind;
  title: string;
  hint: string;
  /** 任务开始运行后的秒数（服务端时钟）；排队中、未开始为 null。 */
  elapsed: number | null;
  /** 排队已等的秒数。 */
  queued: number | null;
  stage: Stage | null;
  /** 当前阶段已持续的秒数。 */
  stageFor: number | null;
  step: TaskProgress | null;
  /** 当前步骤已持续的秒数。 */
  stepFor: number | null;
  /** "已发 37 次采样（预算上限 400）"一类的读法；这一步没有计数时为 null。 */
  stepCount: string | null;
  /** 距上一次成功连上后端的秒数（本机时钟）。 */
  contactAgo: number | null;
  /** 距服务端最近一次活动的秒数（服务端时钟）。 */
  activityAgo: number | null;
  /** 连接中断时已重连的次数。 */
  attempts: number | null;
  /** 连接中断已持续的秒数。 */
  lostFor: number | null;
};

const since = (now: number | null, then: number | null | undefined) =>
  now === null || then === null || then === undefined ? null : Math.max(0, now - then);

/** 两次回答之间按本机时钟推算的服务端此刻（秒）；没有服务端时刻就是 null。 */
export function serverNow(contact: Contact | undefined, now: number): number | null {
  if (!contact || contact.serverTime === undefined) return null;
  return contact.serverTime + Math.max(0, now - contact.at) / 1000;
}

const VERBS: Record<string, string> = {
  '次采样': '已用', '次请求': '已发', '格': '已判定', '家': '已核验', '条边': '已处理',
};
/**
 * 上限各是什么：成圈的是整档预算，检索的是本阶段的额度，核验的是候选名单 —— 都可能提前
 * 停下。只有路网的边数是确数、这一步必定走完，所以写"共"。
 */
const LIMITS: Record<string, (limit: string) => string> = {
  '次采样': limit => `预算上限 ${limit} 次`,
  '次请求': limit => `本阶段上限 ${limit} 次`,
  '家': limit => `候选 ${limit} 家`,
  '条边': limit => `共 ${limit} 条`,
};

const figure = (value: number) => value.toLocaleString('zh-CN');

/** 计数的读法。上限写成"上限""候选"，不写成"x / y"，免得被读成完成比例。 */
export function countText(step: TaskProgress | null | undefined): string | null {
  if (!step || step.count === null) return null;
  const unit = step.unit ?? '';
  const done = `${VERBS[unit] ?? '已完成'} ${figure(step.count)} ${unit}`.trim();
  if (step.limit === null) return done;
  return `${done}（${(LIMITS[unit] ?? (limit => `上限 ${limit}`))(figure(step.limit))}）`;
}

const timingOf = (task: CheckupTaskView) => task as Partial<CheckupTaskView>;

/**
 * 读出这一刻该怎么说。`now` 是本机毫秒时刻；终态、空闲、取结果中不需要实时读法，返回 null。
 */
export function liveView(state: CheckupState, now: number): LiveView | null {
  const { phase, task } = state;
  if (!TICKING_PHASES.includes(phase)) return null;
  const timing = task ? timingOf(task) : undefined;
  const server = serverNow(state.contact, now);
  const contactAgo = state.contact ? Math.max(0, (now - state.contact.at) / 1000) : null;
  const lost = state.connection === 'lost';
  const status = task?.status;
  const running = status === 'running' || status === 'cancelling';
  const started = timing?.startedAt ?? null;
  let elapsed: number | null = null;
  if (running && started !== null) elapsed = since(server, started);
  // 旧版后端没有开始时刻：用它报的已用时，加上这次回答之后本机走过的时间。
  else if (running && task && contactAgo !== null) elapsed = task.elapsedSeconds + contactAgo;
  const step = running ? timing?.progress ?? null : null;
  const view: LiveView = {
    kind: 'working', title: '', hint: '',
    elapsed, queued: status === 'queued' && task ? since(server, task.createdAt) : null,
    stage: task?.stage && (STAGES as readonly string[]).includes(task.stage) ? task.stage : null,
    stageFor: running ? since(server, timing?.stageStartedAt) : null,
    step, stepFor: step ? since(server, step.since) : null, stepCount: countText(step),
    contactAgo, activityAgo: running ? since(server, timing?.lastActivityAt) : null,
    attempts: lost ? state.reconnect?.attempts ?? null : null,
    lostFor: lost && state.reconnect ? Math.max(0, (now - state.reconnect.since) / 1000) : null,
  };
  const secs = duration;

  if (lost) {
    // 问不到后端时，不对任务下任何判断：它可能照常在跑，也可能早就结束了。
    return { ...view, kind: 'lost', title: '连接中断，正在重连',
      hint: `最近连接 ${secs(contactAgo)}前，已重试 ${view.attempts ?? 0} 次。`
        + '任务仍在服务端运行，恢复后自动同步，不会重复提交。' };
  }
  if (phase === 'submitting') {
    return { ...view, kind: 'submitting', title: '提交中',
      hint: '创建确认前刷新，会按请求标识恢复，不会重复提交。' };
  }
  if (phase === 'restoring' || !task) {
    return { ...view, kind: 'restoring', title: '恢复任务中',
      hint: '正在查询上次任务；完成前不能提交新任务。' };
  }
  if (status === 'queued') {
    return { ...view, kind: 'queued', title: '排队中',
      hint: '前序任务结束后自动开始。' };
  }
  const stepName = step ? `「${step.label}」` : '当前步骤';
  if (view.activityAgo !== null && view.activityAgo >= stallAfter(step)) {
    return { ...view, kind: 'stalled', title: '疑似停滞',
      hint: `后端仍可连接，${secs(view.activityAgo)}无进展（${stepName}）。`
        + '可继续等待，也可取消后重试。' };
  }
  if (status === 'cancelling' || phase === 'cancelling') {
    return { ...view, kind: 'cancelling', title: '正在取消',
      hint: `将在${stepName}结束时停止；已发请求仍计入额度。` };
  }
  return { ...view, kind: 'working', title: '正在计算或等待',
    hint: view.activityAgo === null ? '任务正在运行。' : `最近进展 ${secs(view.activityAgo)}前。` };
}
