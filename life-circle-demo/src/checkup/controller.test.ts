/**
 * 控制器测的是**时序**：提交、轮询、取修订、取图层、取消，以及"迟到的响应不许上屏"。
 *
 * v2 比 v1 多出来的两条约束也在这里钉住：取修订必须取任务视图报告的那一版（不是"最新
 * 的一版"），图层按修订缓存（换版就整组丢掉）。这两条错了都不会抛异常，只会让图上画的
 * 是上一版的灰区、面板里写的是这一版的面积。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CheckupController } from './controller';
import { CheckupError, DETAIL_BUDGET_EXHAUSTED, type CheckupService } from './client';
import { STAGE_LABELS, isCheckupBusy, isStageReached } from './types';
import { CENTER, capabilities, layer, route, snapshot, task } from './fixtures';
import type { CheckupLayer, CheckupSnapshot, CheckupTaskView } from './contract';

const input = { center: CENTER, engine: 'baidu_e82', budget: 200 };

function service(): CheckupService {
  return {
    capabilities: vi.fn(async () => capabilities()),
    create: vi.fn(async () => task({ status: 'queued', stage: null, revision: 1 })),
    status: vi.fn(async () => task({ status: 'completed', stage: 'ready', revision: 5 })),
    byRequest: vi.fn(async (id: string) => task({ clientRequestId: id })),
    result: vi.fn(async () => snapshot()),
    layer: vi.fn(async (_task: string, id: string, revision: number) =>
      layer({ layerId: id, revision, resultHash: `hash-${revision}` })),
    cancel: vi.fn(async () => task({ status: 'cancelled' })),
    route: vi.fn(async () => route()),
  };
}
afterEach(() => vi.useRealTimers());

describe('checkup lifecycle', () => {
  it('submits once, then polls until the revision is fetched and shown', async () => {
    const api = service();
    const phases: string[] = [];
    const controller = new CheckupController(api, state => phases.push(state.phase));
    await controller.start(input);
    expect(controller.state.phase).toBe('completed');
    expect(api.create).toHaveBeenCalledTimes(1);
    expect(api.status).toHaveBeenCalledTimes(1);
    // 取的是任务视图报告的那一版修订。
    expect(api.result).toHaveBeenCalledWith('task-1', 5, expect.anything());
    expect(controller.state.snapshot?.revision).toBe(5);
    expect(phases).toContain('submitting');
    expect(phases).toContain('completed');
  });

  it('ignores a second start while the first one is still in flight', async () => {
    const api = service();
    let finish!: (value: CheckupTaskView) => void;
    api.create = vi.fn(() => new Promise<CheckupTaskView>(resolve => { finish = resolve; }));
    const controller = new CheckupController(api, () => {});
    const work = controller.start(input);
    expect(controller.state.phase).toBe('submitting');
    await Promise.all([controller.start(input), controller.retry()]);
    expect(api.create).toHaveBeenCalledTimes(1);
    finish(task({ status: 'queued', stage: null, revision: 1 }));
    await work;
    expect(controller.state.phase).toBe('completed');
  });

  it('refuses to render a revision other than the one the task view reported', async () => {
    const api = service();
    vi.mocked(api.result).mockResolvedValue(snapshot({ revision: 4 }));
    const controller = new CheckupController(api, () => {});
    await controller.start(input);
    expect(controller.state.phase).toBe('error');
    expect(controller.state.error).toContain('修订');
    expect(controller.state.snapshot).toBeUndefined();
  });

  it('reports a failed task with the reason the server gave', async () => {
    const api = service();
    vi.mocked(api.status).mockResolvedValue(task({ status: 'failed', revision: 3,
      error: '步行服务未配置' }));
    const controller = new CheckupController(api, () => {});
    await controller.start(input);
    expect(controller.state.phase).toBe('error');
    expect(controller.state.error).toContain('步行服务未配置');
    expect(api.result).not.toHaveBeenCalled();
  });

  it('polls once a second and ignores a completion that lands after cancel', async () => {
    vi.useFakeTimers();
    const api = service();
    vi.mocked(api.status).mockResolvedValue(task({ status: 'running', stage: 'accessibility',
      revision: 3 }));
    let release!: (value: CheckupSnapshot) => void;
    api.result = vi.fn(() => new Promise<CheckupSnapshot>(resolve => { release = resolve; }));
    const controller = new CheckupController(api, () => {});
    const run = controller.start(input);
    await vi.advanceTimersByTimeAsync(0);
    expect(api.status).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(999);
    expect(api.status).toHaveBeenCalledTimes(1);

    vi.mocked(api.status).mockResolvedValue(task({ status: 'completed', stage: 'ready',
      revision: 5 }));
    await vi.advanceTimersByTimeAsync(1);
    await vi.waitFor(() => expect(controller.state.phase).toBe('fetching'));
    await controller.cancel();
    expect(controller.state.phase).toBe('cancelled');
    release(snapshot());
    await run;
    expect(controller.state.phase).toBe('cancelled');
    expect(controller.state.snapshot).toBeUndefined();
  });

  it('restarts with a fresh request key after the task expired', async () => {
    const api = service();
    vi.mocked(api.status).mockRejectedValueOnce(
      new CheckupError('任务不存在或已过期', 404, 'checkup_task_not_found'));
    const controller = new CheckupController(api, () => {});
    await controller.start(input);
    expect(controller.state.phase).toBe('error');
    await controller.retry();
    expect(controller.state.phase).toBe('completed');
    expect(api.create).toHaveBeenCalledTimes(2);
    // 过期的任务不能再被复用：同一个 key 会被后端当成同一次请求认出来。
    const keys = vi.mocked(api.create).mock.calls.map(([body]) => body.clientRequestId);
    expect(keys[0]).not.toBe(keys[1]);
  });

  it('cleans up a lost create response by looking the task up by request key', async () => {
    const api = service();
    vi.mocked(api.create).mockRejectedValueOnce(new CheckupError('连接失败', 0, 'network'));
    const controller = new CheckupController(api, () => {});
    await controller.start(input);
    expect(controller.state.phase).toBe('error');
    const key = vi.mocked(api.create).mock.calls[0][0].clientRequestId;
    await controller.reset();
    expect(api.byRequest).toHaveBeenCalledWith(key);
    expect(api.cancel).toHaveBeenCalledWith('task-1');
  });

  it('does not go looking for a task the server explicitly refused to create', async () => {
    const api = service();
    vi.mocked(api.create).mockRejectedValueOnce(
      new CheckupError('体检条件无效', 422, 'checkup_invalid_request'));
    const controller = new CheckupController(api, () => {});
    await controller.start(input);
    await controller.reset();
    // 422 是"服务端说了没建"，再按请求 ID 去查只会打扰后端。
    expect(api.byRequest).not.toHaveBeenCalled();
    expect(api.cancel).not.toHaveBeenCalled();
  });
});

describe('checkup layers', () => {
  it('fetches a layer once per revision and re-fetches it when the revision moves', async () => {
    vi.useFakeTimers();
    const api = service();
    vi.mocked(api.status)
      .mockResolvedValueOnce(task({ status: 'running', stage: 'accessibility', revision: 3 }))
      .mockResolvedValueOnce(task({ status: 'completed', stage: 'ready', revision: 5 }));
    const controller = new CheckupController(api, () => {});
    const run = controller.start(input);
    await vi.advanceTimersByTimeAsync(0);
    expect(controller.state.phase).toBe('running');

    const early = await controller.layer('service_gaps');
    expect(early?.revision).toBe(3);
    expect(await controller.layer('service_gaps')).toBe(early);
    expect(api.layer).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(1000);
    await run;
    expect(controller.state.phase).toBe('completed');
    // 修订前进了：上一版的灰区不能留在图上，面板里的面积已经是新的了。
    const late = await controller.layer('service_gaps');
    expect(late?.revision).toBe(5);
    expect(api.layer).toHaveBeenCalledTimes(2);
  });

  it('drops a layer fetched across a task switch instead of drawing it', async () => {
    const api = service();
    let release!: (value: CheckupLayer) => void;
    api.layer = vi.fn(() => new Promise<CheckupLayer>(resolve => { release = resolve; }));
    const controller = new CheckupController(api, () => {});
    await controller.start(input);
    const work = controller.layer('service_gaps');
    await vi.waitFor(() => expect(api.layer).toHaveBeenCalled());
    await controller.reset();
    release(layer({ revision: 5 }));
    // 这张属于被放弃的那一次体检：画上去就是拿旧结论配新任务。
    expect(await work).toBeUndefined();
    expect(controller.state.layers?.service_gaps).toBeUndefined();
  });

  it('refuses to fetch a layer before there is a task', async () => {
    const controller = new CheckupController(service(), () => {});
    await expect(controller.layer('heatmap')).rejects.toMatchObject({ code: 'no_task' });
  });
});

describe('checkup facility detail', () => {
  it('keeps the clicked facility route out of the revision state', async () => {
    const api = service();
    const controller = new CheckupController(api, () => {});
    await controller.start(input);
    const revisions = controller.state.task?.revision;
    await controller.detail('synthetic:pharmacy-1');
    expect(controller.state.route?.routeDistanceM).toBe(805.05);
    // 详情不发布修订：点一下设施不改变任何结论，也不该让图层缓存失效。
    expect(controller.state.task?.revision).toBe(revisions);
    expect(controller.state.routeError).toBeUndefined();
  });

  it('shows why a detail route was refused, and never a stale one', async () => {
    const api = service();
    const controller = new CheckupController(api, () => {});
    await controller.start(input);
    await controller.detail('synthetic:pharmacy-1');
    vi.mocked(api.route).mockRejectedValueOnce(
      new CheckupError('本任务的详情路线预算已用尽', 429, DETAIL_BUDGET_EXHAUSTED));
    await controller.detail('synthetic:clinic-1');
    expect(controller.state.routeError).toContain('预算已用尽');
    expect(controller.state.route).toBeUndefined();
  });
});

describe('stage display', () => {
  it('orders stages without inventing a percentage', () => {
    expect(isStageReached('accessibility', 'reporting')).toBe(true);
    expect(isStageReached('verification', 'accessibility')).toBe(false);
    expect(isStageReached('isochrone', null)).toBe(false);
    expect(STAGE_LABELS.ready).toBe('完成');
    expect(isCheckupBusy({ phase: 'running' })).toBe(true);
    expect(isCheckupBusy({ phase: 'completed' })).toBe(false);
  });
});
