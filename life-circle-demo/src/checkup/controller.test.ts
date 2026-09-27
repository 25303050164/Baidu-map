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
import { STAGE_LABELS, isCheckupBusy, isStageReached, type CheckupHandle } from './types';
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

  it('polls once a second and ignores a running status that lands after cancel', async () => {
    vi.useFakeTimers();
    const api = service();
    let late!: (value: CheckupTaskView) => void;
    vi.mocked(api.status)
      .mockResolvedValueOnce(task({ status: 'running', stage: 'accessibility', revision: 3 }))
      .mockImplementationOnce(() => new Promise<CheckupTaskView>(resolve => { late = resolve; }))
      .mockResolvedValue(task({ status: 'cancelled', revision: 3 }));
    vi.mocked(api.cancel).mockResolvedValue(task({ status: 'cancelling', revision: 3, cancelRequested: true }));
    const controller = new CheckupController(api, () => {});
    const run = controller.start(input);
    await vi.advanceTimersByTimeAsync(0);
    expect(api.status).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(999);
    expect(api.status).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(api.status).toHaveBeenCalledTimes(2);
    const cancelling = controller.cancel();
    await vi.advanceTimersByTimeAsync(0);
    expect(api.cancel).toHaveBeenCalledWith('task-1');
    // 取消前发出的那次轮询现在才回来，说的还是"运行中"：不许把界面拉回"运行中"。
    late(task({ status: 'running', stage: 'accessibility', revision: 3 }));
    await vi.advanceTimersByTimeAsync(0);
    expect(controller.state.phase).toBe('cancelling');
    await vi.advanceTimersByTimeAsync(1000);
    await Promise.all([run, cancelling]);
    expect(controller.state.phase).toBe('cancelled');
  });

  it('keeps a completed task when cancel is pressed too late: there is nothing left to stop', async () => {
    const api = service();
    let release!: (value: CheckupSnapshot) => void;
    api.result = vi.fn(() => new Promise<CheckupSnapshot>(resolve => { release = resolve; }));
    const controller = new CheckupController(api, () => {});
    const run = controller.start(input);
    await vi.waitFor(() => expect(controller.state.phase).toBe('fetching'));
    await controller.cancel();
    expect(api.cancel).not.toHaveBeenCalled();
    release(snapshot());
    await run;
    expect(controller.state.phase).toBe('completed');
  });

  it('restarts with a fresh request key after the task expired', async () => {
    const api = service();
    vi.mocked(api.status).mockRejectedValueOnce(
      new CheckupError('任务不存在或已过期', 404, 'checkup_task_not_found'));
    const controller = new CheckupController(api, () => {});
    await controller.start(input);
    expect(controller.state.phase).toBe('error');
    expect(controller.state.recovery).toBe('expired');
    await controller.retry();
    expect(controller.state.phase).toBe('completed');
    expect(api.create).toHaveBeenCalledTimes(2);
    // 过期的任务不能再被复用：同一个 key 会被后端当成同一次请求认出来。
    const keys = vi.mocked(api.create).mock.calls.map(([body]) => body.clientRequestId);
    expect(keys[0]).not.toBe(keys[1]);
  });

  it('recovers a lost create response by request key instead of submitting again', async () => {
    const api = service();
    vi.mocked(api.create).mockRejectedValueOnce(new CheckupError('连接失败', 0, 'network'));
    const controller = new CheckupController(api, () => {});
    await controller.start(input);
    const key = vi.mocked(api.create).mock.calls[0][0].clientRequestId;
    expect(api.byRequest).toHaveBeenCalledWith(key);
    // 找回的是同一个任务，接着看到完成；没有第二次创建，也没有替用户取消。
    expect(api.create).toHaveBeenCalledTimes(1);
    expect(api.cancel).not.toHaveBeenCalled();
    expect(controller.state.phase).toBe('completed');
  });

  it('does not go looking for a task the server explicitly refused to create', async () => {
    const api = service();
    const handles: (CheckupHandle | undefined)[] = [];
    vi.mocked(api.create).mockRejectedValueOnce(
      new CheckupError('体检条件无效', 422, 'checkup_invalid_request'));
    const controller = new CheckupController(api, () => {}, handle => handles.push(handle));
    await controller.start(input);
    expect(controller.state.phase).toBe('error');
    expect(controller.clear()).toBe(true);
    // 422 是"服务端说了没建"，再按请求 ID 去查只会打扰后端；句柄也随之作废。
    expect(api.byRequest).not.toHaveBeenCalled();
    expect(api.cancel).not.toHaveBeenCalled();
    expect(handles.at(-1)).toBeUndefined();
  });
});

describe('checkup task lifetime', () => {
  it('saves the handle before the POST, then again with the task id', async () => {
    const api = service();
    const handles: (CheckupHandle | undefined)[] = [];
    vi.mocked(api.create).mockImplementation(async body => {
      // 请求发出的那一刻，句柄已经存下：此时关页面，刷新回来能按请求标识找回。
      expect(handles).toHaveLength(1);
      expect(handles[0]?.input.clientRequestId).toBe(body.clientRequestId);
      expect(handles[0]?.taskId).toBeUndefined();
      return task({ status: 'queued', stage: null, revision: 1, clientRequestId: body.clientRequestId });
    });
    const controller = new CheckupController(api, () => {}, handle => handles.push(handle));
    await controller.start(input);
    expect(handles[1]?.taskId).toBe('task-1');
    expect(handles[1]?.input.center).toEqual(CENTER);
  });

  it('never cancels on dispose: leaving the page is not a cancel', async () => {
    vi.useFakeTimers();
    const api = service();
    vi.mocked(api.status).mockResolvedValue(task({ status: 'running' }));
    const controller = new CheckupController(api, () => {});
    void controller.start(input);
    await vi.advanceTimersByTimeAsync(0);
    controller.dispose();
    await vi.advanceTimersByTimeAsync(5000);
    expect(api.cancel).not.toHaveBeenCalled();
    // 也不再轮询：卸载后没有人看，任务在服务端照跑。
    expect(api.status).toHaveBeenCalledTimes(1);
  });

  it('will not start a second task while the first is still live', async () => {
    vi.useFakeTimers();
    const api = service();
    vi.mocked(api.status).mockResolvedValue(task({ status: 'running' }));
    vi.mocked(api.cancel).mockRejectedValue(new CheckupError('内部错误', 500, 'http_500'));
    const controller = new CheckupController(api, () => {});
    void controller.start(input);
    await vi.advanceTimersByTimeAsync(0);
    await controller.cancel();
    expect(controller.state.phase).toBe('error');
    expect(controller.hasLiveTask).toBe(true);
    await controller.start({ ...input, center: { lng: 116.5, lat: 39.9 } });
    expect(api.create).toHaveBeenCalledTimes(1);
    // 还在跑的任务不能被"清除"掉：那等于在用户不知情时丢下一个在扣额度的任务。
    expect(controller.clear()).toBe(false);
    controller.dispose();
  });

  it('resumes a saved task id: polls it and fetches the reported revision, never re-creates', async () => {
    const api = service();
    const controller = new CheckupController(api, () => {});
    await controller.resume({ input: { ...input, clientRequestId: 'request-1' }, taskId: 'task-1', savedAt: 1 });
    expect(api.create).not.toHaveBeenCalled();
    expect(api.status).toHaveBeenCalledWith('task-1', expect.anything());
    expect(api.result).toHaveBeenCalledWith('task-1', 5, expect.anything());
    expect(controller.state.phase).toBe('completed');
    expect(controller.state.input?.center).toEqual(CENTER);
  });

  it('resumes a handle without a task id by request key, and says so when nothing was created', async () => {
    const api = service();
    vi.mocked(api.byRequest).mockRejectedValueOnce(
      new CheckupError('任务不存在', 404, 'checkup_task_not_found'));
    const controller = new CheckupController(api, () => {});
    await controller.resume({ input: { ...input, clientRequestId: 'lost-1' }, savedAt: 1 });
    expect(controller.state.phase).toBe('error');
    expect(controller.state.recovery).toBe('unconfirmed');
    expect(controller.state.error).toContain('没有自动重新提交');
    expect(api.create).not.toHaveBeenCalled();
    // 用户点"重新提交"：同一请求标识，服务端若其实建过会直接认出来。
    vi.mocked(api.create).mockImplementation(async body =>
      task({ status: 'queued', stage: null, revision: 1, clientRequestId: body.clientRequestId }));
    await controller.retry();
    expect(api.create).toHaveBeenCalledTimes(1);
    expect(vi.mocked(api.create).mock.calls[0][0].clientRequestId).toBe('lost-1');
    expect(controller.state.phase).toBe('completed');
  });

  it('re-sends a cancel that was requested before the refresh', async () => {
    const api = service();
    vi.mocked(api.cancel).mockResolvedValue(task({ status: 'cancelled', cancelRequested: true }));
    const controller = new CheckupController(api, () => {});
    await controller.resume({ input: { ...input, clientRequestId: 'request-1' }, taskId: 'task-1',
      cancelRequested: true, savedAt: 1 });
    expect(api.cancel).toHaveBeenCalledWith('task-1');
    expect(api.status).not.toHaveBeenCalled();
    expect(controller.state.phase).toBe('cancelled');
  });

  it('keeps polling through a network outage and says the connection is lost, not failed', async () => {
    vi.useFakeTimers();
    const api = service();
    const offline = new CheckupError('无法连接', 0, 'network');
    vi.mocked(api.status)
      .mockResolvedValueOnce(task({ status: 'running' }))
      .mockRejectedValueOnce(offline)
      .mockRejectedValueOnce(offline)
      .mockResolvedValue(task({ status: 'completed', stage: 'ready', revision: 5 }));
    const controller = new CheckupController(api, () => {});
    const run = controller.start(input);
    await vi.advanceTimersByTimeAsync(1000);
    expect(api.status).toHaveBeenCalledTimes(2);
    expect(controller.state.phase).toBe('running');
    expect(controller.state.connection).toBe('lost');
    // 退避：第一次失败后等 1 秒，第二次失败后等 2 秒。
    await vi.advanceTimersByTimeAsync(1000);
    expect(api.status).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(1999);
    expect(api.status).toHaveBeenCalledTimes(3);
    // 网络回来的信号不必等满退避。
    controller.nudge();
    await vi.advanceTimersByTimeAsync(0);
    await run;
    expect(controller.state.phase).toBe('completed');
    expect(controller.state.connection).toBeUndefined();
    expect(api.create).toHaveBeenCalledTimes(1);
  });

  it('holds a cancel made while the create is in flight and sends it once the id arrives', async () => {
    const api = service();
    let finish!: (value: CheckupTaskView) => void;
    api.create = vi.fn(() => new Promise<CheckupTaskView>(resolve => { finish = resolve; }));
    const controller = new CheckupController(api, () => {});
    const work = controller.start(input);
    await controller.cancel();
    expect(controller.state.phase).toBe('cancelling');
    expect(api.cancel).not.toHaveBeenCalled();
    finish(task({ status: 'queued', stage: null, revision: 1,
      clientRequestId: vi.mocked(api.create).mock.calls[0][0].clientRequestId }));
    await work;
    expect(api.cancel).toHaveBeenCalledWith('task-1');
    expect(controller.state.phase).toBe('cancelled');
  });

  it('cancels an unconfirmed create by looking it up, and clears it when the server has none', async () => {
    const api = service();
    const handles: (CheckupHandle | undefined)[] = [];
    vi.mocked(api.create).mockRejectedValueOnce(new CheckupError('连接失败', 0, 'network'));
    vi.mocked(api.byRequest).mockRejectedValue(new CheckupError('任务不存在', 404, 'checkup_task_not_found'));
    const controller = new CheckupController(api, () => {}, handle => handles.push(handle));
    await controller.start(input);
    expect(controller.state.recovery).toBe('unconfirmed');
    await controller.cancel();
    expect(api.cancel).not.toHaveBeenCalled();
    expect(controller.state.phase).toBe('cancelled');
    expect(handles.at(-1)).toBeUndefined();
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
    expect(controller.clear()).toBe(true);
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
