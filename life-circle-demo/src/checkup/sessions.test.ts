/**
 * 任务登记处：任务活在模块里，刷新靠存储里的句柄找回。这里钉住"存了什么、读回来怎么
 * 校验、找回时发了什么请求"—— 找回任务只许问状态、按请求标识查，不许重新提交。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CHECKUP_STORAGE_PREFIX, checkupSession, readStored, resetCheckupSessions } from './sessions';
import type { CheckupService } from './client';
import { CENTER, capabilities, extensionDocument, extensionView, layer, retryView, route, sessionView, snapshot,
  task } from './fixtures';

class MemoryStorage {
  data = new Map<string, string>();
  get length() { return this.data.size; }
  clear() { this.data.clear(); }
  getItem(key: string) { return this.data.get(key) ?? null; }
  key(index: number) { return [...this.data.keys()][index] ?? null; }
  removeItem(key: string) { this.data.delete(key); }
  setItem(key: string, value: string) { this.data.set(key, value); }
}

function service(): CheckupService {
  return {
    capabilities: vi.fn(async () => capabilities()),
    create: vi.fn(async () => task({ status: 'queued', stage: null, revision: 1 })),
    status: vi.fn(async () => task({ status: 'completed', stage: 'ready', revision: 5 })),
    byRequest: vi.fn(async (id: string) => task({ clientRequestId: id })),
    result: vi.fn(async () => snapshot()),
    layer: vi.fn(async (_task: string, id: string, revision: number) => layer({ layerId: id, revision })),
    cancel: vi.fn(async () => task({ status: 'cancelled' })),
    route: vi.fn(async () => route()),
    extensionCreate: vi.fn(async () => extensionView()),
    extensionStatus: vi.fn(async () => extensionView()),
    extensionList: vi.fn(async () => [extensionView()]),
    extensionResult: vi.fn(async () => extensionDocument()),
    extensionCancel: vi.fn(async () => extensionView({ status: 'cancelled' })),
    retryCreate: vi.fn(async () => retryView()),
    retryStatus: vi.fn(async () => retryView()),
    retryList: vi.fn(async () => []),
    retryCancel: vi.fn(async () => retryView({ status: 'cancelled' })),
    sessionOpen: vi.fn(async () => sessionView()),
    sessionHeartbeat: vi.fn(async () => sessionView({ resumed: true })),
    sessionClose: vi.fn(async () => sessionView({ openTabs: 0 })),
  };
}

let storage: MemoryStorage;
beforeEach(() => { storage = new MemoryStorage(); vi.stubGlobal('localStorage', storage); });
afterEach(() => { resetCheckupSessions(); vi.unstubAllGlobals(); });

const stored = (engine: string) => JSON.parse(storage.getItem(CHECKUP_STORAGE_PREFIX + engine) ?? 'null');

describe('checkup sessions', () => {
  it('stores the request key before the POST goes out, then the task id', async () => {
    const api = service();
    let seen: unknown;
    api.create = vi.fn(async body => {
      seen = stored('baidu_e82');
      return task({ status: 'queued', stage: null, revision: 1, clientRequestId: body.clientRequestId });
    });
    const session = checkupSession('baidu_e82', api);
    await session.controller.start({ center: CENTER, engine: 'baidu_e82', budget: 200 });
    const key = vi.mocked(api.create).mock.calls[0][0].clientRequestId;
    expect(seen).toMatchObject({ handle: { input: { clientRequestId: key } } });
    expect((seen as { handle: { taskId?: string } }).handle.taskId).toBeUndefined();
    expect(stored('baidu_e82').handle).toMatchObject({ taskId: 'task-1', input: { clientRequestId: key } });
  });

  it('after a refresh, resumes the stored task by id without submitting anything', async () => {
    storage.setItem(CHECKUP_STORAGE_PREFIX + 'osm_hybrid', JSON.stringify({
      handle: { input: { center: CENTER, engine: 'osm_hybrid', budget: 400, clientRequestId: 'k1' },
        taskId: 'task-1', savedAt: 1 },
      prefs: { toggles: { density: true }, serviceMode: 'composite', reportOpen: true } }));
    const api = service();
    const session = checkupSession('osm_hybrid', api);
    expect(session.getState().phase).toBe('restoring');
    expect(session.prefs()).toMatchObject({ toggles: { density: true }, reportOpen: true });
    await vi.waitFor(() => expect(session.getState().phase).toBe('completed'));
    expect(api.create).not.toHaveBeenCalled();
    expect(api.status).toHaveBeenCalledWith('task-1', expect.anything());
    // 另一个引擎有自己的会话、自己的句柄，互不串门。
    expect(checkupSession('baidu_e82', api).getState().phase).toBe('idle');
  });

  it('a task whose create response was lost is looked up by its request key', async () => {
    storage.setItem(CHECKUP_STORAGE_PREFIX + 'baidu_e82', JSON.stringify({
      handle: { input: { center: CENTER, engine: 'baidu_e82', clientRequestId: 'lost' }, savedAt: 1 } }));
    const api = service();
    const session = checkupSession('baidu_e82', api);
    await vi.waitFor(() => expect(session.getState().phase).toBe('completed'));
    expect(api.byRequest).toHaveBeenCalledWith('lost');
    expect(api.create).not.toHaveBeenCalled();
    expect(stored('baidu_e82').handle.taskId).toBe('task-1');
  });

  it('dropping the page (dispose) never cancels the task', async () => {
    const api = service();
    vi.mocked(api.status).mockResolvedValue(task({ status: 'running' }));
    const session = checkupSession('baidu_e82', api);
    void session.controller.start({ center: CENTER, engine: 'baidu_e82', budget: 200 });
    await vi.waitFor(() => expect(session.getState().phase).toBe('running'));
    resetCheckupSessions();
    expect(api.cancel).not.toHaveBeenCalled();
    expect(stored('baidu_e82').handle.taskId).toBe('task-1');
  });

  it('rejects malformed or foreign archives instead of failing to start', () => {
    const put = (value: unknown) => storage.setItem(CHECKUP_STORAGE_PREFIX + 'baidu_e82', JSON.stringify(value));
    storage.setItem(CHECKUP_STORAGE_PREFIX + 'baidu_e82', '{not json');
    expect(readStored('baidu_e82', storage as Storage)).toEqual({});
    put({ handle: { input: { center: CENTER, engine: 'osm_hybrid', clientRequestId: 'k' }, savedAt: 1 } });
    expect(readStored('baidu_e82', storage as Storage).handle).toBeUndefined();
    put({ handle: { input: { center: { lng: 'x', lat: 1 }, engine: 'baidu_e82', clientRequestId: 'k' } } });
    expect(readStored('baidu_e82', storage as Storage).handle).toBeUndefined();
    put({ handle: { input: { center: CENTER, engine: 'baidu_e82', clientRequestId: 'k' }, taskId: 7, savedAt: 1 } });
    expect(readStored('baidu_e82', storage as Storage).handle).toBeUndefined();
    put({ prefs: { toggles: { density: 'yes', service: true }, budget: 'big', reportOpen: 1 } });
    expect(readStored('baidu_e82', storage as Storage).prefs).toEqual({ toggles: { service: true } });
  });
});
