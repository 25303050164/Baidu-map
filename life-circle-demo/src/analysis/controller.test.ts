import { afterEach, describe, expect, it, vi } from 'vitest';
import { AnalysisController } from './controller';
import { ApiError } from './service';
import type { AnalysisService, TaskStatus, AnalysisResult } from './types';
import { resultFixture } from './testFixtures';

const input = { center: { lng: 116.404, lat: 39.915 }, budget: 200 as const };
const status = (state = 'running'): TaskStatus => ({ schema_version: '1.0', responseType: 'task', taskId: 'one', status: state as TaskStatus['status'], businessStatus: state === 'failed' ? 'failed' : null, stage: 'initializing', requests: 2, networkRequests: 0, budget: 200, elapsedSeconds: 1, dataSource: 'synthetic', error: null });
const result = resultFixture();
function service(): AnalysisService {
  return { create: vi.fn(async () => status()), status: vi.fn(async () => status('completed')),
    result: vi.fn(async () => result), cancel: vi.fn(async () => status('cancelled')),
    cancelByRequest: vi.fn(async () => status('cancelled')) };
}
afterEach(() => vi.useRealTimers());

describe('analysis lifecycle', () => {
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
  });
  it('ignores duplicate starts in the reset gap after a completed run', async () => {
    const api = service();
    const controller = new AnalysisController(api, () => {});
    await controller.start(input);
    await Promise.all([controller.start(input), controller.start(input)]);
    expect(api.create).toHaveBeenCalledTimes(2);
  });
  it('shows fetching until the result arrives and cancellation ignores a late result', async () => {
    const api = service();
    let finish!: (value: AnalysisResult) => void;
    api.result = vi.fn(() => new Promise<AnalysisResult>(resolve => { finish = resolve; }));
    const controller = new AnalysisController(api, () => {});
    const work = controller.start(input);
    await vi.waitFor(() => expect(controller.state.phase).toBe('fetching'));
    await controller.start(input);
    expect(api.create).toHaveBeenCalledTimes(1);
    await controller.cancel();
    expect(controller.state.phase).toBe('cancelled');
    finish(result);
    await work;
    expect(controller.state.phase).toBe('cancelled');
    expect(controller.state.result).toBeUndefined();
  });
  it('keeps an explicit cancelled state while recovering a late creation response', async () => {
    const api = service();
    let finish!: (value: TaskStatus) => void;
    api.create = vi.fn(() => new Promise<TaskStatus>(resolve => { finish = resolve; }));
    const controller = new AnalysisController(api, () => {});
    const work = controller.start(input);
    await controller.cancel();
    expect(controller.state.phase).toBe('cancelled');
    finish(status());
    await work;
    expect(api.cancel).toHaveBeenCalledWith('one');
    expect(api.result).not.toHaveBeenCalled();
    expect(controller.state.phase).toBe('cancelled');
  });
  it('cancelling after a failed result fetch does not fetch again or reopen a report', async () => {
    const api = service();
    vi.mocked(api.result).mockRejectedValueOnce(new Error('result connection lost'));
    const controller = new AnalysisController(api, () => {});
    await controller.start(input);
    expect(controller.state.phase).toBe('error');
    await controller.cancel();
    expect(controller.state.phase).toBe('cancelled');
    expect(api.result).toHaveBeenCalledTimes(1);
    expect(api.cancel).not.toHaveBeenCalled();
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
  it('recovers and cancels a lost create response by request key before changing center', async () => {
    const api = service();
    let active = false;
    api.create = vi.fn(async () => {
      if (active) throw new ApiError('busy', 409);
      if (vi.mocked(api.create).mock.calls.length === 1) { active = true; throw new Error('response lost'); }
      return status();
    });
    api.cancelByRequest = vi.fn(async () => { active = false; return status('cancelled'); });
    const controller = new AnalysisController(api, () => {});
    await controller.start(input);
    const key = vi.mocked(api.create).mock.calls[0][0].clientRequestId;
    await controller.reset();
    expect(api.cancelByRequest).toHaveBeenCalledWith(key);
    expect(active).toBe(false);
    vi.mocked(api.result).mockResolvedValue({ ...result, center: { lng: 116.405, lat: 39.915 } });
    await controller.start({ ...input, center: { lng: 116.405, lat: 39.915 } });
    expect(controller.state.phase).toBe('completed');
    expect(api.create).toHaveBeenCalledTimes(2);
  });
  it('retains request-key cancellation after network failure and retries without creating work', async () => {
    const api = service();
    vi.mocked(api.create).mockRejectedValueOnce(new Error('response lost'));
    vi.mocked(api.cancelByRequest).mockRejectedValueOnce(new Error('offline'));
    const controller = new AnalysisController(api, () => {});
    await controller.start(input);
    await controller.reset();
    expect(controller.state.phase).toBe('error');
    await controller.retry();
    expect(controller.state.phase).toBe('idle');
    expect(api.cancelByRequest).toHaveBeenCalledTimes(2);
    expect(api.create).toHaveBeenCalledTimes(1);
  });
  it('recovers when an obsolete in-flight create finally fails after a center edit', async () => {
    const api = service();
    let reject!: (reason: Error) => void;
    api.create = vi.fn(() => new Promise<TaskStatus>((_resolve, r) => { reject = r; }));
    const controller = new AnalysisController(api, () => {});
    const work = controller.start(input);
    await controller.reset();
    reject(new Error('lost late response'));
    await work;
    expect(api.cancelByRequest).toHaveBeenCalledTimes(1);
    expect(controller.state.phase).toBe('idle');
  });
  it('uses server completion and result without a simulated timer', async () => {
    const api = service();
    const states: string[] = [];
    const controller = new AnalysisController(api, state => states.push(state.phase));
    await controller.start(input);
    expect(controller.state.result).toBe(result);
    expect(states).toContain('completed');
  });
  it('cancels a late-created task after the center changes', async () => {
    const api = service();
    let resolve!: (value: TaskStatus) => void;
    api.create = vi.fn(() => new Promise<TaskStatus>(r => { resolve = r; }));
    const controller = new AnalysisController(api, () => {});
    const run = controller.start(input);
    await controller.reset();
    resolve(status());
    await run;
    expect(api.cancel).toHaveBeenCalledWith('one');
    expect(api.result).not.toHaveBeenCalled();
    expect(controller.state.phase).toBe('idle');
  });
  it('polls once per second, cancels on user action and ignores late results', async () => {
    vi.useFakeTimers();
    const api = service();
    api.status = vi.fn(async () => status());
    const controller = new AnalysisController(api, () => {});
    const run = controller.start(input);
    await vi.advanceTimersByTimeAsync(0);
    expect(api.status).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(999);
    expect(api.status).toHaveBeenCalledTimes(1);
    await controller.cancel();
    await run;
    expect(controller.state.phase).toBe('cancelled');
    expect(api.result).not.toHaveBeenCalled();
  });
  it('retries a lost create response with the same idempotency key', async () => {
    const api = service();
    vi.mocked(api.create).mockRejectedValueOnce(new Error('网络不可用'));
    const controller = new AnalysisController(api, () => {});
    await controller.start(input);
    expect(controller.state.phase).toBe('error');
    await controller.retry();
    expect(vi.mocked(api.create).mock.calls[0][0].clientRequestId).toBe(vi.mocked(api.create).mock.calls[1][0].clientRequestId);
    expect(controller.state.phase).toBe('completed');
  });
  it('discards a late result after reset', async () => {
    const api = service();
    let resolve!: (result: AnalysisResult) => void;
    api.result = vi.fn(() => new Promise<AnalysisResult>(r => { resolve = r; }));
    const controller = new AnalysisController(api, () => {});
    const run = controller.start(input);
    await vi.waitFor(() => expect(api.result).toHaveBeenCalled());
    await controller.reset();
    resolve(result);
    await run;
    expect(controller.state.phase).toBe('idle');
    expect(controller.state.result).toBeUndefined();
  });
  it('allows retrying an unconfirmed cancellation after editing the center', async () => {
    vi.useFakeTimers();
    const api = service();
    api.status = vi.fn(async () => status());
    vi.mocked(api.cancel).mockRejectedValueOnce(new Error('offline'));
    const controller = new AnalysisController(api, () => {});
    const run = controller.start(input);
    await vi.advanceTimersByTimeAsync(0);
    await controller.reset();
    await run;
    expect(controller.state.phase).toBe('error');
    await controller.retry();
    expect(api.cancel).toHaveBeenCalledTimes(2);
    expect(controller.state.phase).toBe('idle');
  });
  it('can start a fresh task after the previous task expires', async () => {
    const api = service();
    vi.mocked(api.status).mockRejectedValueOnce(new ApiError('任务已过期', 404));
    vi.mocked(api.cancel).mockRejectedValueOnce(new ApiError('任务已过期', 404));
    const controller = new AnalysisController(api, () => {});
    await controller.start(input);
    expect(controller.state.phase).toBe('error');
    await controller.retry();
    expect(controller.state.phase).toBe('completed');
    expect(api.create).toHaveBeenCalledTimes(2);
    expect(vi.mocked(api.create).mock.calls[0][0].clientRequestId).not.toBe(vi.mocked(api.create).mock.calls[1][0].clientRequestId);
  });
});
