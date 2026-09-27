/**
 * 旧版两条分析的任务登记处：每个算法一个控制器，活在模块里，不活在组件里。
 *
 * 与 `checkup/sessions.ts` 同一个思路：组件挂载时订阅、卸载时退订，退订不取消任务；
 * 刷新后凭 `localStorage` 里的句柄向后端核对。本地只存句柄（请求条件、请求标识、任务
 * ID、是否已请求取消），不存结果 —— 旧版任务结束 30 分钟后后端就清掉了，存下的旧结果
 * 会让人以为它还能复核。
 */
import { useSyncExternalStore } from 'react';
import { AnalysisController } from './analysis/controller';
import { createApiService } from './analysis/service';
import type { AnalysisInput, AnalysisService, AnalysisState } from './analysis/types';
import { HybridController, type HybridInput, type HybridState } from './hybrid/controller';
import { createHybridClient } from './hybrid/client';
import { getBaiduSession, getHybridSession, saveBaiduSession, saveHybridSession } from './algorithmSessions';
import { isLegacyBusy, type LegacyHandle, type LegacyPhase } from './legacyController';
import { onWake } from './wakeups';

export type LegacyAlgorithm = 'baidu' | 'hybrid';
export const LEGACY_STORAGE_PREFIX = 'life-circle:legacy:v1:';
const SLUGS: Record<LegacyAlgorithm, string> = { baidu: 'e82', hybrid: 'hybrid' };

export type LegacySession<C, S> = {
  controller: C;
  getState: () => S;
  subscribe: (listener: () => void) => () => void;
};

const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const text = (value: unknown): value is string => typeof value === 'string' && value.length > 0;
const BUDGETS: Record<LegacyAlgorithm, (budget: unknown) => boolean> = {
  baidu: budget => budget === 200 || budget === 400 || budget === 800,
  hybrid: budget => Number.isInteger(budget) && (budget as number) >= 1 && (budget as number) <= 400,
};

function safeStorage(): Storage | undefined {
  try { return typeof localStorage === 'undefined' ? undefined : localStorage; }
  catch { return undefined; }
}

/** 读回来的句柄一律过一遍：旧版本或手改过的存档不能让页面起不来。 */
export function readLegacyHandle<I extends { center: { lng: number; lat: number }; budget: number; clientRequestId: string }>(
  algorithm: LegacyAlgorithm, storage: Storage | undefined = safeStorage(),
): LegacyHandle<I> | undefined {
  if (!storage) return undefined;
  let raw: unknown;
  try { raw = JSON.parse(storage.getItem(LEGACY_STORAGE_PREFIX + SLUGS[algorithm]) ?? 'null'); }
  catch { return undefined; }
  const handle = (raw as { handle?: LegacyHandle<I> } | null)?.handle;
  const input = handle?.input;
  if (!handle || !input || !text(input.clientRequestId) || !input.center
    || !finite(input.center.lng) || !finite(input.center.lat) || !BUDGETS[algorithm](input.budget)
    || (handle.taskId !== undefined && !text(handle.taskId))) return undefined;
  return {
    input: { center: { lng: input.center.lng, lat: input.center.lat }, budget: input.budget,
      clientRequestId: input.clientRequestId } as I,
    savedAt: finite(handle.savedAt) ? handle.savedAt : 0,
    ...(handle.taskId ? { taskId: handle.taskId } : {}),
    ...(handle.cancelRequested === true ? { cancelRequested: true } : {}),
  };
}

function writeHandle(algorithm: LegacyAlgorithm, handle: LegacyHandle<unknown> | undefined) {
  // 存不进去（隐私模式、配额满）时任务照跑，只是刷新后找不回来 —— 不为此打断用户。
  try { safeStorage()?.setItem(LEGACY_STORAGE_PREFIX + SLUGS[algorithm], JSON.stringify({ handle })); }
  catch { /* 见上 */ }
}

const listeners = new Set<() => void>();
const notify = () => { for (const listener of listeners) listener(); };

function register<C extends { nudge(): void; resume(handle: LegacyHandle<I>): Promise<void> }, S, I>(
  algorithm: LegacyAlgorithm,
  make: (publish: (state: S) => void, onHandle: (handle?: LegacyHandle<I>) => void, recall: () => LegacyHandle<I> | undefined) => C,
  initial: S,
): LegacySession<C, S> {
  let state = initial;
  const own = new Set<() => void>();
  const controller = make(next => {
    state = next;
    for (const listener of own) listener();
    notify();
  }, handle => writeHandle(algorithm, handle), () => readLegacyHandle(algorithm) as LegacyHandle<I> | undefined);
  onWake(() => controller.nudge());
  return {
    controller,
    getState: () => state,
    subscribe(listener) { own.add(listener); return () => { own.delete(listener); }; },
  };
}

let baidu: LegacySession<AnalysisController, AnalysisState> | undefined;
let hybrid: LegacySession<HybridController, HybridState> | undefined;

export function baiduTasks(service: AnalysisService = createApiService()) {
  if (baidu) return baidu;
  const handle = readLegacyHandle<AnalysisInput>('baidu');
  baidu = register<AnalysisController, AnalysisState, AnalysisInput>('baidu',
    (publish, onHandle, recall) => new AnalysisController(service, publish, onHandle, recall), { phase: 'idle' });
  if (handle) {
    // 刷新后左栏的中心与预算跟着任务走，不停在默认点上。
    const { center, budget } = handle.input;
    saveBaiduSession({ ...getBaiduSession(), center, lng: center.lng, lat: center.lat, budget });
    void baidu.controller.resume(handle);
  }
  return baidu;
}

export function hybridTasks(client = createHybridClient()) {
  if (hybrid) return hybrid;
  const handle = readLegacyHandle<HybridInput>('hybrid');
  hybrid = register<HybridController, HybridState, HybridInput>('hybrid',
    (publish, onHandle, recall) => new HybridController(client, publish, onHandle, recall), { phase: 'idle' });
  if (handle) {
    const { center, budget } = handle.input;
    saveHybridSession({ ...getHybridSession(), center, lng: center.lng, lat: center.lat, budget });
    void hybrid.controller.resume(handle);
  }
  return hybrid;
}

const LIVE_TEXT: Partial<Record<LegacyPhase, string>> = {
  submitting: '提交中', restoring: '核对中', running: '运行中', fetching: '取结果', cancelling: '取消中',
};

/** 算法切换条上的小标记：另一个算法还有旧版任务在跑时，切过去之前就能看见。 */
export const legacyActivity = {
  subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener); }; },
  get(algorithm: LegacyAlgorithm): string | null {
    const state = (algorithm === 'baidu' ? baiduTasks() : hybridTasks()).getState();
    return isLegacyBusy(state) ? LIVE_TEXT[state.phase] ?? null : null;
  },
};

export function useLegacyState<S>(session: LegacySession<unknown, S>): S {
  return useSyncExternalStore(session.subscribe, session.getState);
}

/** 测试用：丢掉控制器（不取消服务端任务），下一次取会话时重新从存储恢复。 */
export function resetLegacySessions() {
  baidu?.controller.dispose();
  hybrid?.controller.dispose();
  baidu = hybrid = undefined;
}
