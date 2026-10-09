/**
 * 浏览会话：保留期的第一条期限（§5 B2 决策 2）。
 *
 * 这个文件的每一条都在钉同一件事：**心跳是这条规则的输入，不是性能优化**。所以它必须
 * 在用户还在看的时候真的发出去，在会话到期时真的停下，在服务端连不上时真的不拦路，
 * 在页面卸载时真的说一声"我走了"（而且要活得比页面久）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { HEARTBEAT_MS, closeBrowserSession, currentSessionId, heartbeatBrowserSession,
  openBrowserSession, resetBrowserSession, sessionHeader, startBrowserSession } from './browserSession';
import { CheckupError, type CheckupService } from './client';
import { retainedView, sessionView } from './fixtures';

class MemoryStorage implements Storage {
  data = new Map<string, string>();
  get length() { return this.data.size; }
  clear() { this.data.clear(); }
  getItem(key: string) { return this.data.get(key) ?? null; }
  key(index: number) { return [...this.data.keys()][index] ?? null; }
  removeItem(key: string) { this.data.delete(key); }
  setItem(key: string, value: string) { this.data.set(key, value); }
}

const SESSION_KEY = 'life-circle:checkup:v1:session';
const TAB_KEY = 'life-circle:checkup:v1:tab';

function service(overrides: Partial<CheckupService> = {}): CheckupService {
  return {
    capabilities: vi.fn(), create: vi.fn(), status: vi.fn(), byRequest: vi.fn(), result: vi.fn(),
    layer: vi.fn(), cancel: vi.fn(), route: vi.fn(), extensionCreate: vi.fn(),
    extensionStatus: vi.fn(), extensionList: vi.fn(), extensionResult: vi.fn(),
    extensionCancel: vi.fn(), retryCreate: vi.fn(), retryStatus: vi.fn(), retryList: vi.fn(),
    retryCancel: vi.fn(),
    sessionOpen: vi.fn(async () => sessionView()),
    sessionHeartbeat: vi.fn(async () => sessionView({ resumed: true })),
    sessionClose: vi.fn(async () => sessionView({ openTabs: 0 })),
    retainedResult: vi.fn(async () => retainedView()),
    ...overrides,
  } as unknown as CheckupService;
}

let local: MemoryStorage;
let session: MemoryStorage;

beforeEach(() => {
  local = new MemoryStorage();
  session = new MemoryStorage();
  vi.stubGlobal('localStorage', local);
  vi.stubGlobal('sessionStorage', session);
  resetBrowserSession();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  resetBrowserSession();
});

describe('browser session', () => {
  it('opens a session and keeps both identifiers where their lifetimes differ', async () => {
    const api = service();
    const view = await openBrowserSession(api);
    expect(api.sessionOpen).toHaveBeenCalledWith({ schemaVersion: 'checkup-v1' });
    expect(view?.sessionId).toBe('session-1');
    // 会话在标签页之间共用 → localStorage；标签页只属于这一个 → sessionStorage。
    expect(local.getItem(SESSION_KEY)).toBe('session-1');
    expect(session.getItem(TAB_KEY)).toBe('tab-1');
    expect(sessionHeader()).toEqual({ 'X-Checkup-Session': 'session-1' });
  });

  it('brings the stored identifiers back so a refresh is the same tab', async () => {
    local.setItem(SESSION_KEY, 'session-9');
    session.setItem(TAB_KEY, 'tab-9');
    const api = service({ sessionOpen: vi.fn(async () => sessionView({
      sessionId: 'session-9', tabId: 'tab-9', resumed: true })) });
    await openBrowserSession(api);
    expect(api.sessionOpen).toHaveBeenCalledWith({
      schemaVersion: 'checkup-v1', sessionId: 'session-9', tabId: 'tab-9' });
  });

  it('starts a new session when the server says the stored one expired', async () => {
    local.setItem(SESSION_KEY, 'session-dead');
    session.setItem(TAB_KEY, 'tab-dead');
    const open = vi.fn()
      .mockRejectedValueOnce(new CheckupError('这个浏览会话已经到期', 409, 'checkup_session_expired'))
      .mockResolvedValueOnce(sessionView({ sessionId: 'session-new', tabId: 'tab-new' }));
    const view = await openBrowserSession(service({ sessionOpen: open }));
    // 到期就是到期：不是把旧的复活，而是换一个新的 —— 旧数据不再被这次会话认领。
    expect(open).toHaveBeenCalledTimes(2);
    expect(open.mock.calls[1][0]).toEqual({ schemaVersion: 'checkup-v1' });
    expect(view?.sessionId).toBe('session-new');
    expect(local.getItem(SESSION_KEY)).toBe('session-new');
  });

  it('does not stand in the way when the service cannot be reached', async () => {
    const api = service({ sessionOpen: vi.fn(async () => {
      throw new CheckupError('无法连接体检服务', 0, 'network'); }) });
    // 心跳失败不该让人不能体检：这一次体检只是没有会话归属（只剩"三次后续体检"这条期限）。
    expect(await openBrowserSession(api)).toBeNull();
    expect(currentSessionId()).toBeNull();
    expect(sessionHeader()).toEqual({});
  });

  it('forgets the identifiers once the session has expired', async () => {
    await openBrowserSession(service());
    const expired = service({ sessionHeartbeat: vi.fn(async () => {
      throw new CheckupError('这个浏览会话已经到期', 409, 'checkup_session_expired'); }) });
    expect(await heartbeatBrowserSession(expired)).toBe(false);
    expect(local.getItem(SESSION_KEY)).toBeNull();
    expect(session.getItem(TAB_KEY)).toBeNull();
  });

  it('says nothing when there is no session to keep alive', async () => {
    const api = service();
    expect(await heartbeatBrowserSession(api)).toBe(false);
    await closeBrowserSession(api);
    expect(api.sessionHeartbeat).not.toHaveBeenCalled();
    expect(api.sessionClose).not.toHaveBeenCalled();
  });

  it('keeps a keepalive flag on the farewell so it outlives the page', async () => {
    await openBrowserSession(service());
    const api = service();
    await closeBrowserSession(api);
    // 普通请求会在页面卸载时被取消掉，最后一个标签页的离开就丢了。
    expect(api.sessionClose).toHaveBeenCalledWith('session-1', 'tab-1', true);
  });

  it('heartbeats on a timer and stops when told to', async () => {
    vi.useFakeTimers();
    const api = service();
    const stop = startBrowserSession(api, { addEventListener: vi.fn(), removeEventListener: vi.fn() } as never);
    await vi.advanceTimersByTimeAsync(0);
    expect(api.sessionOpen).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(HEARTBEAT_MS * 2);
    expect(vi.mocked(api.sessionHeartbeat).mock.calls.length).toBe(2);
    stop();
    await vi.advanceTimersByTimeAsync(HEARTBEAT_MS * 3);
    expect(vi.mocked(api.sessionHeartbeat).mock.calls.length).toBe(2);
  });

  it('says farewell when the page goes away in any way', async () => {
    vi.useFakeTimers();
    const handlers: Record<string, () => void> = {};
    const target = {
      addEventListener: (name: string, handler: () => void) => { handlers[name] = handler; },
      removeEventListener: (name: string) => { delete handlers[name]; },
    } as never;
    const api = service();
    const stop = startBrowserSession(api, target);
    await vi.advanceTimersByTimeAsync(0);
    // pagehide 在刷新、关闭、跳转离开时都会触发：刷新那一次会被下一次打开抵消。
    handlers.pagehide();
    await vi.advanceTimersByTimeAsync(0);
    expect(api.sessionClose).toHaveBeenCalledWith('session-1', 'tab-1', true);
    stop();
    expect(handlers.pagehide).toBeUndefined();
  });
});
