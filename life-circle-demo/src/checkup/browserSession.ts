/**
 * 浏览会话：保留期的第一条期限（§5 B2 决策 2）。
 *
 * 运营者的口径是"关闭浏览器"——落实为**本应用最后一个标签页关闭**，允许最多 5 分钟的
 * 检测宽限。落实它需要两个标识，寿命不一样所以分两处存：
 *
 * * **会话**在这个浏览器的标签页之间共用 → `localStorage`；
 * * **标签页**只属于一个标签页，刷新之后还要是同一个 → `sessionStorage`。
 *
 * 这里不判断"数据还在不在"，那是后端的话。它只做两件事：把这两个标识带给服务端，以及
 * 在用户还在看的时候不时说一声"我还在"。心跳停了租约就到期，所以心跳不是性能优化，
 * 而是这条规则的**输入**。
 *
 * 两种失败都按"降级、不拦路"处理：连不上服务端时照常体检（这次体检只剩"三次后续体检"
 * 这一条期限）；会话已经到期时换一个新的会话再继续 —— 那正是"到期就是到期"的表现，
 * 而不是把它复活。
 */
import { CheckupError, type CheckupService } from './client';
import type { SessionView } from './contract';

const SESSION_KEY = 'life-circle:checkup:v1:session';
const TAB_KEY = 'life-circle:checkup:v1:tab';

/** 心跳间隔。租约是 300 秒：隐藏的标签页被浏览器降频到约每分钟一次，仍然远短于租约。 */
export const HEARTBEAT_MS = 30_000;

/** 当前会话标识，或 null。请求头从这里取，所以它是模块状态而不是组件状态。 */
let current: string | null = null;

export function currentSessionId(): string | null {
  return current;
}

/** 给每个 v2 请求加上的会话标识；没有会话时是一个空对象（什么都不加）。 */
export function sessionHeader(): Record<string, string> {
  return current === null ? {} : { 'X-Checkup-Session': current };
}

/** 测试用：忘掉模块里的会话（不动存储，下一次打开会按存储里的标识续上）。 */
export function resetBrowserSession() {
  current = null;
}

function store(kind: 'local' | 'session'): Storage | undefined {
  try {
    const storage = kind === 'local' ? localStorage : sessionStorage;
    return typeof storage === 'undefined' ? undefined : storage;
  } catch {
    // 隐私模式、被策略禁用：读不到就当没有存过，服务端那边会开一个新的。
    return undefined;
  }
}

function readIds(): { sessionId?: string; tabId?: string } {
  const sessionId = store('local')?.getItem(SESSION_KEY) ?? undefined;
  const tabId = store('session')?.getItem(TAB_KEY) ?? undefined;
  return { ...(sessionId ? { sessionId } : {}), ...(tabId ? { tabId } : {}) };
}

function remember(view: { sessionId: string; tabId: string }): void {
  current = view.sessionId;
  try {
    store('local')?.setItem(SESSION_KEY, view.sessionId);
    store('session')?.setItem(TAB_KEY, view.tabId);
  } catch { /* 存不进去时当前页面仍然可用，只是刷新后会开一个新会话 */ }
}

function forget(): void {
  current = null;
  try {
    store('local')?.removeItem(SESSION_KEY);
    store('session')?.removeItem(TAB_KEY);
  } catch { /* 见上 */ }
}

const isExpired = (error: unknown): boolean =>
  error instanceof CheckupError && error.code === 'checkup_session_expired';

/**
 * 开一个会话或回到已有的那一个。
 *
 * 返回 null 表示**这次没有会话**（服务端连不上）：体检照做，只是这次体检的明细只剩
 * "三次后续体检"这一条期限。这不是错误，用户不该因为心跳失败而不能体检。
 */
export async function openBrowserSession(api: CheckupService): Promise<SessionView | null> {
  const ids = readIds();
  try {
    const view = await api.sessionOpen({ schemaVersion: 'checkup-v1', ...ids });
    remember(view);
    return view;
  } catch (error) {
    if (!isExpired(error)) return null;
    // 本地那两个标识已经没用了。开一个新的会话，而不是把旧的复活。
    forget();
    try {
      const view = await api.sessionOpen({ schemaVersion: 'checkup-v1' });
      remember(view);
      return view;
    } catch {
      return null;
    }
  }
}

/** 续租一次。返回是否续上了；没续上意味着这个会话已经到期。 */
export async function heartbeatBrowserSession(api: CheckupService): Promise<boolean> {
  const { sessionId, tabId } = readIds();
  if (sessionId === undefined || tabId === undefined) return false;
  try {
    remember(await api.sessionHeartbeat(sessionId, tabId));
    return true;
  } catch (error) {
    if (isExpired(error)) forget();
    return false;
  }
}

/** 说一声"这个标签页走了"。它只是记录：会话是否到期由最后离开的时刻加宽限决定。 */
export async function closeBrowserSession(api: CheckupService): Promise<void> {
  const { sessionId, tabId } = readIds();
  if (sessionId === undefined || tabId === undefined) return;
  try {
    await api.sessionClose(sessionId, tabId, true);
  } catch {
    // 说不上就走了（关页面时的竞态、断网）：租约会在宽限期后自己到期，不需要补偿。
  }
}

/**
 * 打开会话并保活。返回停止函数：卸载或测试结束时调用，它只停心跳，**不**关会话 ——
 * 页面卸载时的关闭由 `pagehide` 负责，那是另一件事。
 */
export function startBrowserSession(api: CheckupService, target: Window | undefined = undefined
): () => void {
  void openBrowserSession(api);
  const timer = setInterval(() => { void heartbeatBrowserSession(api); }, HEARTBEAT_MS);
  const page = target ?? (typeof window === 'undefined' ? undefined : window);
  const onHide = () => { void closeBrowserSession(api); };
  // pagehide 在刷新时也会触发：那一次关闭之后，页面重新加载会带着同一个标签页标识回来，
  // 于是这一次关闭被下一次打开抵消 —— 刷新因此不会误清，也不会漏清。
  page?.addEventListener('pagehide', onHide);
  return () => {
    clearInterval(timer);
    page?.removeEventListener('pagehide', onHide);
  };
}
