import { afterEach, describe, expect, it, vi } from 'vitest';
import { AnalysisController } from './controller';
import { ApiError } from './service';
import type { AnalysisService, TaskStatus, AnalysisResult } from './types';

const input = { center: { lng: 116.404, lat: 39.915 }, budget: 200 as const };
const status = (state = 'running'): TaskStatus => ({ taskId: 'one', status: state as TaskStatus['status'], stage: 'initializing', requests: 2, networkRequests: 0, budget: 200, elapsedSeconds: 1, dataSource: 'synthetic', error: null });
const result = { taskId: 'one', dataSource: 'synthetic' } as AnalysisResult;
function service(): AnalysisService {
  return { create: vi.fn(async () => status()), status: vi.fn(async () => status('completed')),
    result: vi.fn(async () => result), cancel: vi.fn(async () => status('cancelled')) };
}
afterEach(() => vi.useRealTimers());

describe('analysis lifecycle', () => {
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
