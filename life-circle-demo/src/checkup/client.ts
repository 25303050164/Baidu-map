/**
 * v2 体检客户端（`/api/v2/checkups`）。
 *
 * 与旧 `/api/analyses` 客户端分开：两个契约的任务视图、阶段和修订语义都不一样，把它
 * 们塞进一个函数里就得靠 `if (v2)` 分支来分辨字段，而分支写错的表现是静默读到
 * `undefined`。这里每个方法的返回值都先过 `validate.ts`，不合法就抛错，调用方拿到的
 * 一定是校验过的对象。
 *
 * 后端把错误写成 `{code, message}`：`code` 是机器可读的原因（`checkup_result_not_ready`、
 * `checkup_detail_budget_exhausted`……），`message` 是给读者的话。两个都留下 —— 界面用
 * `message` 显示，逻辑用 `code` 判断，不要拿 `message` 去比对字符串。
 */
import type { CheckupLayer, CheckupRequest, CheckupSnapshot, CheckupTaskView, FacilityRoute } from './contract';
import { validCapabilities, validFacilityRoute, validLayer, validSnapshot, validTaskView,
  type Capabilities, type LayerId } from './validate';

export class CheckupError extends Error {
  constructor(message: string, public status: number, public code: string) { super(message); }
}

/** 后端拒绝的原因码；界面按它决定"重试"还是"改条件"。 */
export const NOT_READY = 'checkup_result_not_ready';
export const DETAIL_BUDGET_EXHAUSTED = 'checkup_detail_budget_exhausted';
export const ROUTE_UNAVAILABLE = 'checkup_route_unavailable';

export type CheckupService = {
  /** 能力表：能选哪个引擎、哪一档预算，以及本应用预算余额的说法，都从这里来。 */
  capabilities: (signal?: AbortSignal) => Promise<Capabilities>;
  create: (body: CheckupRequest) => Promise<CheckupTaskView>;
  continueReport?: (taskId: string, body: { clientRequestId: string; baseRevision: number }) => Promise<CheckupTaskView>;
  status: (taskId: string, signal?: AbortSignal) => Promise<CheckupTaskView>;
  byRequest: (clientRequestId: string) => Promise<CheckupTaskView>;
  result: (taskId: string, revision?: number, signal?: AbortSignal) => Promise<CheckupSnapshot>;
  layer: (taskId: string, layerId: LayerId, revision: number) => Promise<CheckupLayer>;
  cancel: (taskId: string) => Promise<CheckupTaskView>;
  route: (taskId: string, facilityId: string) => Promise<FacilityRoute>;
};

const NOT_FOUND = new Set(['checkup_task_not_found', 'checkup_unknown_engine']);

/** 后端给了 `message` 就用它的，没给才用这些兜底文案。 */
const FALLBACK: Record<number, string> = {
  404: '任务不存在或已过期，请重新发起体检',
  409: '任务当前状态不允许该操作，请刷新任务状态后重试',
  422: '体检条件无效，请检查中心坐标、引擎与预算',
  429: '本任务的详情路线预算或服务配额已用尽，请稍后再试',
  503: '体检服务当前不可用，请联系管理员检查步行服务与路网数据配置',
};

export function createCheckupService(
  base = import.meta.env.VITE_API_BASE_URL?.trim() || '',
  fetcher: typeof fetch = fetch,
): CheckupService {
  const api = `${base.replace(/\/$/, '')}/api/v2`;
  const root = `${api}/checkups`;

  async function send(url: string, method: string, body?: unknown, signal?: AbortSignal) {
    const timeout = AbortSignal.timeout(15_000);
    let response: Response;
    try {
      response = await fetcher(url, {
        method, headers: { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
      });
    } catch {
      // 调用方的取消不是故障：把它原样抛出去，重试逻辑才分得清"我取消的"和"断网了"。
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
      throw new CheckupError('无法连接体检服务或请求超时，请检查网络和服务地址后重试', 0, 'network');
    }
    if (!response.ok) {
      const detail = await response.json().catch(() => null) as { code?: unknown; message?: unknown } | null;
      const code = typeof detail?.code === 'string' ? detail.code : `http_${response.status}`;
      const message = typeof detail?.message === 'string' && detail.message
        ? detail.message
        : FALLBACK[response.status] || '体检服务返回了未预期的错误，请稍后重试';
      throw new CheckupError(message, response.status, code);
    }
    return { response, body: await response.json().catch(() => undefined) as unknown };
  }

  const request = (path: string, method: string, body?: unknown, signal?: AbortSignal) =>
    send(`${root}${path}`, method, body, signal);

  /** 校验放在这里，不放调用方：漏掉一处校验就是一处静默的 `undefined`。 */
  function checked<T>(value: unknown, ok: (v: unknown) => boolean, label: string): T {
    if (!ok(value)) throw new CheckupError(`体检服务返回的${label}结构异常，请检查服务版本`, 0, 'invalid_response');
    return value as T;
  }

  return {
    async capabilities(signal) {
      // 能力表在 `/api/v2` 下，不在 `/checkups` 下：它是这一版接口的公共部分。
      const { body: value } = await send(`${api}/capabilities`, 'GET', undefined, signal);
      return checked<Capabilities>(value, validCapabilities, '能力表');
    },
    async create(body) {
      const { body: value } = await request('', 'POST', body);
      const task = checked<CheckupTaskView>(value, validTaskView, '任务视图');
      // 回来的必须是刚提交的那一次：错配的任务 ID 会让轮询去盯着别人的任务。
      if (task.clientRequestId !== body.clientRequestId) {
        throw new CheckupError('体检服务返回了另一次请求的任务，请检查服务版本', 0, 'mismatched_request');
      }
      return task;
    },
    async status(taskId, signal) {
      const { body: value } = await request(`/${encodeURIComponent(taskId)}`, 'GET', undefined, signal);
      const task = checked<CheckupTaskView>(value, validTaskView, '任务视图');
      if (task.taskId !== taskId) {
        throw new CheckupError('体检服务返回了另一个任务的状态，请检查服务版本', 0, 'mismatched_task');
      }
      return task;
    },
    async continueReport(taskId, body) {
      const { body: value } = await request(`/${encodeURIComponent(taskId)}/continue`, 'POST', body);
      const task = checked<CheckupTaskView>(value, validTaskView, '续查任务');
      if (task.taskId !== taskId) throw new CheckupError('续查任务不匹配', 0, 'mismatched_task');
      return task;
    },
    async byRequest(clientRequestId) {
      const { body: value } = await request(`/by-request/${encodeURIComponent(clientRequestId)}`, 'GET');
      const task = checked<CheckupTaskView>(value, validTaskView, '任务视图');
      if (task.clientRequestId !== clientRequestId) {
        throw new CheckupError('体检服务返回了另一次请求的任务，请检查服务版本', 0, 'mismatched_request');
      }
      return task;
    },
    async result(taskId, revision, signal) {
      const query = revision === undefined ? '' : `?revision=${revision}`;
      const { body: value } = await request(`/${encodeURIComponent(taskId)}/result${query}`, 'GET', undefined, signal);
      const snapshot = checked<CheckupSnapshot>(value, validSnapshot, '修订');
      // 要哪一版就检查哪一版：拿旧修订去渲染新结论是最难发现的一种错。
      if (snapshot.taskId !== taskId || (revision !== undefined && snapshot.revision !== revision)) {
        throw new CheckupError('体检修订与请求的任务或版本不符，请检查服务版本', 0, 'mismatched_revision');
      }
      return snapshot;
    },
    async layer(taskId, layerId, revision) {
      const path = `/${encodeURIComponent(taskId)}/layers/${encodeURIComponent(layerId)}?revision=${revision}`;
      const { body: value } = await request(path, 'GET');
      const layer = checked<CheckupLayer>(value, (v) => validLayer(v, layerId), '图层');
      if (layer.revision !== revision) {
        throw new CheckupError('图层与请求的修订不符，请检查服务版本', 0, 'mismatched_revision');
      }
      return layer;
    },
    async cancel(taskId) {
      const { body: value } = await request(`/${encodeURIComponent(taskId)}/cancel`, 'POST');
      return checked<CheckupTaskView>(value, validTaskView, '任务视图');
    },
    async route(taskId, facilityId) {
      const path = `/${encodeURIComponent(taskId)}/routes/${encodeURIComponent(facilityId)}`;
      const { body: value } = await request(path, 'POST');
      return checked<FacilityRoute>(value, validFacilityRoute, '设施路线');
    },
  };
}

export function isNotFound(error: unknown): boolean {
  return error instanceof CheckupError && (error.status === 404 || NOT_FOUND.has(error.code));
}
