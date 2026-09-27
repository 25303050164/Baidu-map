/**
 * E8.2 旧版分析的任务时序。生命周期的规矩（不随页面取消、先存句柄、断网退避、按请求
 * 标识找回、忙碌时认出自己的任务）在通用控制器里，这里用 E8.2 的客户端错误把它们逐条钉住。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AnalysisController } from './controller';
import { ApiError } from './service';
import type { AnalysisInput, AnalysisResult, AnalysisService, TaskStatus } from './types';
import type { LegacyHandle } from '../legacyController';
import { resultFixture } from './testFixtures';

const input = { center: { lng: 116.404, lat: 39.915 }, budget: 200 as const };
const status = (state = 'running', taskId = 'one'): TaskStatus => ({ schema_version: '1.0', responseType: 'task', taskId,
  status: state as TaskStatus['status'], businessStatus: state === 'failed' ? 'failed' : null, stage: 'initializing',
  requests: 2, networkRequests: 0, budget: 200, elapsedSeconds: 1, dataSource: 'synthetic', error: null });
const result = resultFixture();
const offline = () => new ApiError('无法连接分析服务或请求超时，请检查网络和服务地址后重试', 0);
const missing = () => new ApiError('任务不存在或已过期，请重新分析', 404);
function service(): AnalysisService {
  return { create: vi.fn(async () => status()), status: vi.fn(async () => status('completed')),
    result: vi.fn(async () => result), cancel: vi.fn(async () => status('cancelled')),
    byRequest: vi.fn(async () => status()), cancelByRequest: vi.fn(async () => status('cancelled')) };
}
function recorder() {
  const handles: (LegacyHandle<AnalysisInput> | undefined)[] = [];
  return { handles, onHandle: (handle?: LegacyHandle<AnalysisInput>) => { handles.push(handle); },
    last: () => handles[handles.length - 1] };
}
afterEach(() => vi.useRealTimers());

describe('E8.2 analysis lifecycle', () => {
  it('ignores repeated start and retry while creation is in flight', async () => {
    const api = service();
    let finish!: (value: TaskStatus) => void;
    api.create = vi.fn(() => new Promise<TaskStatus>(resolve => { finish = resolve; }));
    const controller = new AnalysisController(api, () => {});
    const work = controller.start(input);
    expect(controller.state.phase).toBe('submitting');
    await Promise.all([controller.start(input), controller.retry()]);
    expect(api.create).toHaveBeenCalledTimes(1);
    finish(status());
    await work;
    expect(controller.state.phase).toBe('completed');
    expect(controller.state.result).toBe(result);
  });

  it('starts a new task with a new request key once the previous one has finished', async () => {
    const api = service();
    const controller = new AnalysisController(api, () => {});
    await controller.start(input);
    await controller.start({ ...input, center: { lng: 116.41, lat: 39.92 } });
    expect(api.create).toHaveBeenCalledTimes(2);
    const [first, second] = vi.mocked(api.create).mock.calls.map(([body]) => body.clientRequestId);
    expect(first).not.toBe(second);
  });

  it('refuses a second task while one is live, instead of cancelling the first', async () => {
    const api = service();
    vi.mocked(api.status).mockResolvedValue(status('running'));
    const controller = new AnalysisController(api, () => {});
    void controller.start(input);
    await vi.waitFor(() => expect(controller.state.phase).toBe('running'));
    expect(controller.hasLiveTask).toBe(true);
    await controller.start({ ...input, center: { lng: 116.5, lat: 39.9 } });
    expect(api.create).toHaveBeenCalledTimes(1);
    expect(api.cancel).not.toHaveBeenCalled();
    controller.dispose();
  });

  it('shows fetching until the result arrives; a user cancel after completion keeps the result', async () => {
    const api = service();
    let finish!: (value: AnalysisResult) => void;
    api.result = vi.fn(() => new Promise<AnalysisResult>(resolve => { finish = resolve; }));
    const controller = new AnalysisController(api, () => {});
    const work = controller.start(input);
    await vi.waitFor(() => expect(controller.state.phase).toBe('fetching'));
    // 任务在服务端已经完成：此时的"取消"没有可停的工作，不发请求、不丢结果。
    await controller.cancel();
    expect(api.cancel).not.toHaveBeenCalled();
    finish(result);
    await work;
    expect(controller.state.phase).toBe('completed');
  });

  it('a failed result fetch is retried against the same task, never a new one', async () => {
    const api = service();
    vi.mocked(api.result).mockRejectedValueOnce(new Error('后端返回格式异常，请检查服务版本'));
    const controller = new AnalysisController(api, () => {});
    await controller.start(input);
    expect(controller.state.phase).toBe('error');
    await controller.retry();
    expect(controller.state.phase).toBe('completed');
    expect(api.create).toHaveBeenCalledTimes(1);
    expect(api.result).toHaveBeenCalledTimes(2);
  });

  it('rejects completed results for a different center, budget, source or task', async () => {
    for (const wrong of [
      { ...result, center: { lng: 120, lat: 39 } },
      { ...result, isochrone: { ...result.isochrone, config: { ...result.isochrone.config, budget: 800 } } },
      { ...result, dataSource: 'baidu_walking' as const },
      { ...result, taskId: 'other' },
    ]) {
      const api = service();
      vi.mocked(api.result).mockResolvedValue(wrong);
      const controller = new AnalysisController(api, () => {});
      await controller.start(input);
      expect(controller.state.phase).toBe('error');
      expect(controller.state.error).toContain('提交条件不一致');
      expect(controller.state.result).toBeUndefined();
    }
  });

  it('polls once a second and ignores a running status that lands after the user cancels', async () => {
    vi.useFakeTimers();
    const api = service();
    let late!: (value: TaskStatus) => void;
    vi.mocked(api.status).mockResolvedValueOnce(status('running'))
      .mockImplementationOnce(() => new Promise<TaskStatus>(resolve => { late = resolve; }));
    const controller = new AnalysisController(api, () => {});
    void controller.start(input);
    await vi.advanceTimersByTimeAsync(0);
    expect(api.status).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1000);
    expect(api.status).toHaveBeenCalledTimes(2);
    const cancelling = controller.cancel();
    late(status('running'));
    await cancelling;
    expect(api.cancel).toHaveBeenCalledWith('one');
    expect(controller.state.phase).toBe('cancelled');
  });
});

describe('E8.2 task lifetime', () => {
  it('saves the request key before the POST and never cancels on dispose', async () => {
    const api = service();
    const { handles, onHandle } = recorder();
    let finish!: (value: TaskStatus) => void;
    api.create = vi.fn(() => new Promise<TaskStatus>(resolve => { finish = resolve; }));
    const controller = new AnalysisController(api, () => {}, onHandle);
    const work = controller.start(input);
    expect(handles).toHaveLength(1);
    expect(handles[0]!.input.clientRequestId).toBe(vi.mocked(api.create).mock.calls[0][0].clientRequestId);
    expect(handles[0]!.taskId).toBeUndefined();
    controller.dispose();
    finish(status());
    await work;
    expect(api.cancel).not.toHaveBeenCalled();
    expect(api.cancelByRequest).not.toHaveBeenCalled();
  });

  it('resumes a saved task by id after a refresh and shows its real progress and result', async () => {
    const api = service();
    vi.mocked(api.status).mockResolvedValueOnce(status('running')).mockResolvedValue(status('completed'));
    vi.useFakeTimers();
    const controller = new AnalysisController(api, () => {});
    const work = controller.resume({ input: { ...input, clientRequestId: 'saved' }, taskId: 'one', savedAt: 1 });
    expect(controller.state.phase).toBe('restoring');
    await vi.advanceTimersByTimeAsync(0);
    expect(controller.state.phase).toBe('running');
    expect(controller.state.task?.requests).toBe(2);
    await vi.advanceTimersByTimeAsync(1000);
    await work;
    expect(controller.state.phase).toBe('completed');
    expect(api.create).not.toHaveBeenCalled();
  });

  it('recovers a lost create response by request key instead of submitting again', async () => {
    const api = service();
    vi.mocked(api.create).mockRejectedValueOnce(offline());
    const { onHandle, last } = recorder();
    const controller = new AnalysisController(api, () => {}, onHandle);
    await controller.start(input);
    const key = vi.mocked(api.create).mock.calls[0][0].clientRequestId;
    expect(api.byRequest).toHaveBeenCalledWith(key, expect.anything());
    expect(api.create).toHaveBeenCalledTimes(1);
    expect(last()?.taskId).toBe('one');
    expect(controller.state.phase).toBe('completed');
  });

  it('says a create never arrived, and resubmits only with the same key when asked', async () => {
    const api = service();
    vi.mocked(api.create).mockRejectedValueOnce(offline());
    vi.mocked(api.byRequest).mockRejectedValueOnce(missing());
    const controller = new AnalysisController(api, () => {});
    await controller.start(input);
    expect(controller.state.phase).toBe('error');
    expect(controller.state.recovery).toBe('unconfirmed');
    // 查过了，服务端没有：不挡住"开始分析"，但"重新提交"沿用原请求标识。
    expect(controller.hasLiveTask).toBe(false);
    expect(api.create).toHaveBeenCalledTimes(1);
    await controller.retry();
    expect(api.create).toHaveBeenCalledTimes(2);
    const [first, second] = vi.mocked(api.create).mock.calls.map(([body]) => body.clientRequestId);
    expect(second).toBe(first);
    expect(controller.state.phase).toBe('completed');
  });

  it('says a task has expired when the backend no longer knows its id, and starts fresh on retry', async () => {
    const api = service();
    vi.mocked(api.status).mockRejectedValueOnce(missing());
    const { onHandle, last } = recorder();
    const controller = new AnalysisController(api, () => {}, onHandle);
    await controller.resume({ input: { ...input, clientRequestId: 'old' }, taskId: 'gone', savedAt: 1 });
    expect(controller.state.recovery).toBe('expired');
    expect(controller.state.error).toContain('30 分钟');
    expect(controller.hasLiveTask).toBe(false);
    await controller.retry();
    expect(api.create).toHaveBeenCalledTimes(1);
    expect(vi.mocked(api.create).mock.calls[0][0].clientRequestId).not.toBe('old');
    expect(last()?.taskId).toBe('one');
  });

  it('re-sends a cancel that was requested before the refresh', async () => {
    const api = service();
    const controller = new AnalysisController(api, () => {});
    await controller.resume({ input: { ...input, clientRequestId: 'k' }, taskId: 'one', cancelRequested: true, savedAt: 1 });
    expect(api.cancel).toHaveBeenCalledWith('one');
    expect(api.status).not.toHaveBeenCalled();
    expect(controller.state.phase).toBe('cancelled');
  });

  it('keeps polling through a network outage and resumes as soon as it is nudged', async () => {
    vi.useFakeTimers();
    const api = service();
    vi.mocked(api.status).mockResolvedValueOnce(status('running'))
      .mockRejectedValueOnce(offline()).mockRejectedValueOnce(offline())
      .mockResolvedValue(status('completed'));
    const controller = new AnalysisController(api, () => {});
    const work = controller.start(input);
    await vi.advanceTimersByTimeAsync(1000);
    expect(controller.state.connection).toBe('lost');
    expect(controller.state.phase).toBe('running');
    await vi.advanceTimersByTimeAsync(1000);
    expect(api.status).toHaveBeenCalledTimes(3);
    controller.nudge();
    await vi.advanceTimersByTimeAsync(0);
    await work;
    expect(controller.state.phase).toBe('completed');
    expect(controller.state.connection).toBeUndefined();
    expect(api.create).toHaveBeenCalledTimes(1);
  });

  it('holds a cancel made while the create is in flight and sends it once the id arrives', async () => {
    const api = service();
    let finish!: (value: TaskStatus) => void;
    api.create = vi.fn(() => new Promise<TaskStatus>(resolve => { finish = resolve; }));
    const controller = new AnalysisController(api, () => {});
    const work = controller.start(input);
    await controller.cancel();
    expect(controller.state.phase).toBe('cancelling');
    finish(status());
    await work;
    expect(api.cancel).toHaveBeenCalledWith('one');
    expect(api.result).not.toHaveBeenCalled();
    expect(controller.state.phase).toBe('cancelled');
  });

  it('treats a 404 on create as a wrong address: nothing was created, nothing to look up', async () => {
    const api = service();
    vi.mocked(api.create).mockRejectedValueOnce(new ApiError('分析 API 地址或服务配置异常，请检查服务地址', 404));
    const { onHandle, last } = recorder();
    const controller = new AnalysisController(api, () => {}, onHandle);
    await controller.start(input);
    expect(controller.state.error).toBe('分析 API 地址或服务配置异常，请检查服务地址');
    expect(controller.state.recovery).toBeUndefined();
    expect(api.byRequest).not.toHaveBeenCalled();
    expect(last()).toBeUndefined();
    expect(controller.hasLiveTask).toBe(false);
  });

  it('after a 5xx on create, looks the key up and shows the server error only if nothing was created', async () => {
    const api = service();
    vi.mocked(api.create).mockRejectedValueOnce(new ApiError('分析服务异常，请稍后重试', 500));
    vi.mocked(api.byRequest).mockRejectedValueOnce(missing());
    const controller = new AnalysisController(api, () => {});
    await controller.start(input);
    expect(api.byRequest).toHaveBeenCalledTimes(1);
    expect(controller.state.error).toBe('分析服务异常，请稍后重试');
    expect(controller.state.recovery).toBe('unconfirmed');
    await controller.retry();
    const [first, second] = vi.mocked(api.create).mock.calls.map(([body]) => body.clientRequestId);
    expect(second).toBe(first);
    expect(controller.state.phase).toBe('completed');
  });

  it('after a 5xx on create, adopts the task if the server did create it', async () => {
    const api = service();
    vi.mocked(api.create).mockRejectedValueOnce(new ApiError('分析服务异常，请稍后重试', 500));
    const controller = new AnalysisController(api, () => {});
    await controller.start(input);
    expect(api.create).toHaveBeenCalledTimes(1);
    expect(controller.state.phase).toBe('completed');
    expect(controller.state.error).toBeUndefined();
  });

  it('treats a missing AK (503) as a refusal: no lookup, no second task', async () => {
    const api = service();
    vi.mocked(api.create).mockRejectedValueOnce(new ApiError('分析服务当前不可用，请联系管理员检查步行服务配置', 503));
    const controller = new AnalysisController(api, () => {});
    await controller.start(input);
    expect(controller.state.phase).toBe('error');
    expect(api.byRequest).not.toHaveBeenCalled();
    expect(controller.hasLiveTask).toBe(false);
  });
});

describe('E8.2 busy service', () => {
  const busy = () => new ApiError('分析服务忙：另一项分析正在进行（已运行 42 秒，已用 17/400 次调用）。请等待其完成或取消后重试', 409, true);

  it('explains who holds the slot and puts the previous handle back', async () => {
    const api = service();
    vi.mocked(api.create).mockRejectedValueOnce(busy());
    vi.mocked(api.status).mockResolvedValue(status('completed', 'mine'));
    const previous = { input: { ...input, clientRequestId: 'mine' }, taskId: 'mine', savedAt: 1 };
    const { onHandle, last } = recorder();
    const controller = new AnalysisController(api, () => {}, onHandle, () => previous);
    await controller.start({ ...input, center: { lng: 116.5, lat: 39.9 } });
    expect(controller.state.phase).toBe('error');
    expect(controller.state.error).toContain('已运行 42 秒');
    expect(last()).toBe(previous);
    // 存储里的那个任务已经结束，占着名额的是别人：不切过去。
    expect(controller.state.notice).toBeUndefined();
    expect(api.byRequest).not.toHaveBeenCalled();
    expect(controller.hasLiveTask).toBe(false);
  });

  it('switches back to this browser\'s own task when that is what holds the slot', async () => {
    const api = service();
    vi.mocked(api.create).mockRejectedValueOnce(busy());
    vi.mocked(api.status).mockResolvedValueOnce(status('running', 'mine')).mockResolvedValueOnce(status('running', 'mine'))
      .mockResolvedValue(status('completed', 'mine'));
    vi.mocked(api.result).mockResolvedValue({ ...result, taskId: 'mine' });
    // 另一个页签提交后还没拿到任务 ID：按请求标识找到它。
    vi.mocked(api.byRequest).mockResolvedValue(status('running', 'mine'));
    const previous = { input: { ...input, clientRequestId: 'mine' }, savedAt: 1 };
    const { onHandle, last } = recorder();
    vi.useFakeTimers();
    const controller = new AnalysisController(api, () => {}, onHandle, () => previous);
    const work = controller.start({ ...input, center: { lng: 116.5, lat: 39.9 } });
    await vi.advanceTimersByTimeAsync(0);
    expect(controller.state.notice).toContain('已切回该任务');
    expect(controller.state.input?.clientRequestId).toBe('mine');
    expect(last()?.taskId).toBe('mine');
    await vi.advanceTimersByTimeAsync(3000);
    await work;
    expect(controller.state.phase).toBe('completed');
    expect(controller.state.result?.taskId).toBe('mine');
    expect(api.create).toHaveBeenCalledTimes(1);
  });
});
