import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LEGACY_STORAGE_PREFIX, baiduTasks, hybridTasks, legacyActivity, readLegacyHandle, resetLegacySessions } from './legacyTasks';
import { getBaiduSession, getHybridSession, saveBaiduSession, saveHybridSession } from './algorithmSessions';
import type { AnalysisService, TaskStatus } from './analysis/types';
import { resultFixture } from './analysis/testFixtures';
import { createHybridClient } from './hybrid/client';

class MemoryStorage {
  data = new Map<string, string>();
  get length() { return this.data.size; }
  clear() { this.data.clear(); }
  getItem(key: string) { return this.data.get(key) ?? null; }
  key(index: number) { return [...this.data.keys()][index] ?? null; }
  removeItem(key: string) { this.data.delete(key); }
  setItem(key: string, value: string) { this.data.set(key, value); }
}

const status = (state: TaskStatus['status'], taskId = 'one'): TaskStatus => ({ schema_version: '1.0', responseType: 'task',
  taskId, status: state, businessStatus: null, stage: 'exploring', requests: 5, networkRequests: 5, budget: 200,
  elapsedSeconds: 3, dataSource: 'synthetic', error: null });
function service(): AnalysisService {
  return { create: vi.fn(async () => status('running')), status: vi.fn(async () => status('running')),
    result: vi.fn(async () => resultFixture()), cancel: vi.fn(async () => status('cancelled')),
    byRequest: vi.fn(async () => status('running')), cancelByRequest: vi.fn(async () => status('cancelled')) };
}

let storage: MemoryStorage;
const baiduDefaults = getBaiduSession();
const hybridDefaults = getHybridSession();
beforeEach(() => { storage = new MemoryStorage(); vi.stubGlobal('localStorage', storage); });
afterEach(() => {
  resetLegacySessions(); vi.unstubAllGlobals();
  saveBaiduSession(baiduDefaults); saveHybridSession(hybridDefaults);
});

describe('legacy task registry', () => {
  it('resumes a stored E8.2 task after a refresh and points the form at its center', async () => {
    const center = { lng: 116.41, lat: 39.92 };
    storage.setItem(LEGACY_STORAGE_PREFIX + 'e82', JSON.stringify({
      handle: { input: { center, budget: 800, clientRequestId: 'k' }, taskId: 'one', savedAt: 1 } }));
    const api = service();
    const tasks = baiduTasks(api);
    expect(tasks.getState().phase).toBe('restoring');
    expect(getBaiduSession()).toMatchObject({ center, lng: 116.41, lat: 39.92, budget: 800 });
    await vi.waitFor(() => expect(tasks.getState().phase).toBe('running'));
    expect(legacyActivity.get('baidu')).toBe('运行中');
    expect(api.create).not.toHaveBeenCalled();
    resetLegacySessions();
    expect(api.cancel).not.toHaveBeenCalled();
  });

  it('keeps the E8.2 and Hybrid handles apart', async () => {
    const api = service();
    const tasks = baiduTasks(api);
    void tasks.controller.start({ center: { lng: 116.4, lat: 39.9 }, budget: 200 });
    await vi.waitFor(() => expect(tasks.getState().phase).toBe('running'));
    const saved = JSON.parse(storage.getItem(LEGACY_STORAGE_PREFIX + 'e82')!);
    expect(saved.handle).toMatchObject({ taskId: 'one', input: { budget: 200 } });
    expect(storage.getItem(LEGACY_STORAGE_PREFIX + 'hybrid')).toBeNull();
    const fetcher = vi.fn<typeof fetch>();
    expect(hybridTasks(createHybridClient('', fetcher)).getState().phase).toBe('idle');
    expect(fetcher).not.toHaveBeenCalled();
    expect(getHybridSession().center).toEqual(hybridDefaults.center);
  });

  it('ignores handles with an out-of-range budget or a missing request key', () => {
    const put = (slug: string, handle: unknown) => storage.setItem(LEGACY_STORAGE_PREFIX + slug, JSON.stringify({ handle }));
    put('e82', { input: { center: { lng: 1, lat: 1 }, budget: 300, clientRequestId: 'k' }, savedAt: 1 });
    expect(readLegacyHandle('baidu', storage as Storage)).toBeUndefined();
    put('hybrid', { input: { center: { lng: 1, lat: 1 }, budget: 800, clientRequestId: 'k' }, savedAt: 1 });
    expect(readLegacyHandle('hybrid', storage as Storage)).toBeUndefined();
    put('hybrid', { input: { center: { lng: 1, lat: 1 }, budget: 400 }, savedAt: 1 });
    expect(readLegacyHandle('hybrid', storage as Storage)).toBeUndefined();
    put('hybrid', { input: { center: { lng: 1, lat: 1 }, budget: 400, clientRequestId: 'k', extra: 1 }, taskId: 't', savedAt: 1 });
    expect(readLegacyHandle('hybrid', storage as Storage)).toEqual(
      { input: { center: { lng: 1, lat: 1 }, budget: 400, clientRequestId: 'k' }, taskId: 't', savedAt: 1 });
  });
});
