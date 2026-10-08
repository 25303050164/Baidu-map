/**
 * 实时读法测的是"这一刻该怎么说"：三种没有新消息的情形分得开，秒数按服务端时钟走，
 * 计数只说已经发生的事。每一条都用固定的本机时刻调用，不依赖真实时钟。
 */
import { describe, expect, it } from 'vitest';
import { countText, duration, liveView, QUIET_STALL_SECONDS, serverNow, STALL_SECONDS, type LiveView } from './live';
import { CREATED_AT, SERVER_TIME, STARTED_AT, task } from './fixtures';
import type { CheckupTaskView } from './contract';
import type { CheckupState } from './types';

/** 上一次回答到达时的本机毫秒时刻。刻意与服务端时钟毫不相干。 */
const AT = 42_000;

function running(overrides: Partial<CheckupTaskView> = {}, extra: Partial<CheckupState> = {}): CheckupState {
  return { phase: 'running', task: task(overrides), contact: { at: AT, serverTime: SERVER_TIME }, ...extra };
}

const after = (seconds: number) => AT + seconds * 1000;

describe('clock', () => {
  it('reads elapsed time off the server clock and keeps counting between answers', () => {
    const first = liveView(running(), AT)!;
    expect(first.kind).toBe('working');
    expect(first.elapsed).toBeCloseTo(4.5);
    expect(first.stageFor).toBeCloseTo(1.5);
    expect(first.stepFor).toBeCloseTo(0.5);
    expect(first.activityAgo).toBeCloseTo(0.5);
    expect(first.contactAgo).toBe(0);
    const later = liveView(running(), after(3))!;
    expect(later.elapsed).toBeCloseTo(7.5);
    expect(later.stageFor).toBeCloseTo(4.5);
    expect(later.activityAgo).toBeCloseTo(3.5);
    expect(later.contactAgo).toBeCloseTo(3);
  });

  it('does not let the offset between the two machines into any reading', () => {
    const skewed: CheckupState = { ...running(), contact: { at: 9_999_999_000, serverTime: SERVER_TIME } };
    const view = liveView(skewed, 9_999_999_000 + 2000)!;
    expect(view.elapsed).toBeCloseTo(6.5);
    expect(serverNow({ at: AT, serverTime: 100 }, after(2))).toBeCloseTo(102);
    // 本机时钟倒退（对时、休眠唤醒）时不往回推。
    expect(serverNow({ at: AT, serverTime: 100 }, AT - 5000)).toBe(100);
  });

  it('falls back to the reported elapsed time from a backend without a server clock', () => {
    const old = { ...task() } as Partial<CheckupTaskView>;
    for (const key of ['serverTime', 'startedAt', 'finishedAt', 'stageStartedAt', 'lastActivityAt', 'progress'] as const) {
      delete old[key];
    }
    const view = liveView({ phase: 'running', task: old as CheckupTaskView, contact: { at: AT } }, after(2))!;
    expect(view.elapsed).toBeCloseTo(6.5);
    expect(view.activityAgo).toBeNull();
    expect(view.step).toBeNull();
    expect(view.kind).toBe('working');
    expect(view.hint).toContain('任务正在运行');
  });
});

describe('three kinds of silence', () => {
  it('calls a long quiet stretch a suspected stall only once it reaches the threshold', () => {
    // 服务端最近一次活动在回答前 0.5 秒。
    const edge = STALL_SECONDS - 0.5;
    expect(liveView(running(), after(edge - 0.01))!.kind).toBe('working');
    const stalled = liveView(running(), after(edge))!;
    expect(stalled.kind).toBe('stalled');
    expect(stalled.title).toContain('疑似停滞');
    expect(stalled.hint).toContain('评估服务覆盖 · 医疗（第 2/3 类）');
    expect(stalled.hint).toContain('可取消');
  });

  it('gives a step that reports no counts its own, longer allowance before calling it stalled', () => {
    const quiet = running({ progress: { step: 'graph', label: '载入 OSM 步行路网', count: null, limit: null,
      unit: null, since: STARTED_AT + 1 } });
    expect(liveView(quiet, AT)!.stepCount).toBeNull();
    // 不计数的一步持续几分钟是正常的：计数步骤的 90 秒对它不适用。
    const minutes = liveView(quiet, after(STALL_SECONDS * 3))!;
    expect(minutes.kind).toBe('working');
    expect(minutes.stepFor).toBeCloseTo(3.5 + STALL_SECONDS * 3);
    expect(minutes.hint).toContain('最近进展');
    const stalled = liveView(quiet, after(QUIET_STALL_SECONDS))!;
    expect(stalled.kind).toBe('stalled');
    expect(stalled.hint).toContain('载入 OSM 步行路网');
  });

  it('puts a lost connection ahead of everything, and does not judge the task while it cannot ask', () => {
    const lost = running({}, { connection: 'lost', reconnect: { since: after(10), attempts: 3 } });
    const view = liveView(lost, after(STALL_SECONDS * 2))!;
    expect(view.kind).toBe('lost');
    expect(view.attempts).toBe(3);
    expect(view.lostFor).toBeCloseTo(STALL_SECONDS * 2 - 10);
    expect(view.hint).toContain('已重试 3 次');
    expect(view.hint).toContain('不会重复提交');
    // 已用时仍在走，只是按本机时钟推算。
    expect(view.elapsed).toBeCloseTo(4.5 + STALL_SECONDS * 2);
  });

  it('tells queued, submitting, restoring and cancelling apart', () => {
    const queued = liveView(running({ status: 'queued', stage: null, startedAt: null, stageStartedAt: null,
      progress: null }), after(1))!;
    expect(queued.kind).toBe('queued');
    expect(queued.elapsed).toBeNull();
    expect(queued.queued).toBeCloseTo(SERVER_TIME - CREATED_AT + 1);
    expect(liveView({ phase: 'submitting' }, AT)!.kind).toBe('submitting');
    expect(liveView({ phase: 'restoring' }, AT)!.kind).toBe('restoring');
    const cancelling = liveView(running({ status: 'cancelling', cancelRequested: true }, { phase: 'cancelling' }), AT)!;
    expect(cancelling.kind).toBe('cancelling');
    expect(cancelling.elapsed).toBeCloseTo(4.5);
  });

  it('stops reading once the task has a conclusion', () => {
    for (const phase of ['idle', 'fetching', 'completed', 'cancelled', 'error'] as const) {
      expect(liveView({ ...running(), phase }, AT)).toBeNull();
    }
  });
});

describe('wording', () => {
  it('counts what happened and names the limit for what it is', () => {
    const step = (count: number | null, limit: number | null, unit: string | null) =>
      ({ step: 's', label: 'x', count, limit, unit, since: 1 });
    expect(countText(step(37, 400, '次采样'))).toBe('已用 37 次采样（预算上限 400 次）');
    expect(countText(step(2, 6, '次请求'))).toBe('已发 2 次请求（本阶段上限 6 次）');
    expect(countText(step(120, null, '格'))).toBe('已判定 120 格');
    expect(countText(step(3, 11, '家'))).toBe('已核验 3 家（候选 11 家）');
    // 路网的边数是确数，写"共"；百万级的数分组书写。
    expect(countText(step(1_240_000, 2_318_457, '条边'))).toBe('已处理 1,240,000 条边（共 2,318,457 条）');
    expect(countText(step(null, null, null))).toBeNull();
  });

  it('never states a percentage or a fraction anywhere in a reading', () => {
    const views: LiveView[] = [
      liveView(running(), AT)!,
      liveView(running(), after(STALL_SECONDS))!,
      liveView(running({ progress: { step: 'sampling', label: '等时圈采样', count: 37, limit: 400,
        unit: '次采样', since: STARTED_AT + 1 } }), AT)!,
      liveView(running({}, { connection: 'lost', reconnect: { since: AT, attempts: 1 } }), after(4))!,
    ];
    for (const view of views) {
      // 步骤名里的"第 2/3 类"是后端说的第几类，不是完成比例；计数本身不写成分数。
      expect(`${view.title}${view.hint}${view.stepCount ?? ''}`).not.toMatch(/%|％|预计|剩余/);
      expect(view.stepCount ?? '').not.toContain('/');
    }
  });

  it('writes durations in whole seconds, minutes and hours', () => {
    expect(duration(null)).toBe('—');
    expect(duration(59.9)).toBe('59 秒');
    expect(duration(61)).toBe('1 分 1 秒');
    expect(duration(3725)).toBe('1 小时 2 分');
  });
});
