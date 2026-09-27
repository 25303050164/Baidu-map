import type { AnalysisService } from './types';
import { validTask } from './validate';
import { decodeAnalysisResult } from './adapter';

/**
 * `status` 为 0 表示没连上（断网、超时）：任务可能还在跑，控制器会退避后再问。
 * `busy` 表示后端唯一的分析名额被别的任务占着，`message` 是后端写明的占用情况。
 */
export class ApiError extends Error {
  constructor(message: string, public status: number, public busy = false) { super(message); }
}

/** 后端的忙碌说明只报已运行时长与调用数，不含对方的任务标识或坐标，可以原样显示。 */
const BUSY_PREFIX = '分析服务忙：';

export function createApiService(base = import.meta.env.VITE_API_BASE_URL?.trim() || '', fetcher: typeof fetch = fetch): AnalysisService {
  async function request<T>(path: string, method = 'GET', body?: unknown, signal?: AbortSignal, taskId?: string): Promise<T> {
    const timeout = AbortSignal.timeout(10_000);
    let response: Response;
    try {
      response = await fetcher(`${base.replace(/\/$/, '')}/api/analyses${path}`, {
        method, headers: { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
      });
    } catch {
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
      throw new ApiError('无法连接分析服务或请求超时，请检查网络和服务地址后重试', 0);
    }
    if (!response.ok) {
      if (response.status === 409 && path === '') {
        const detail = await response.json().then((value: { detail?: unknown }) => value?.detail, () => undefined);
        if (typeof detail === 'string' && detail.startsWith(BUSY_PREFIX) && detail.length < 200) {
          throw new ApiError(detail, 409, true);
        }
      }
      const messages: Record<number, string> = { 404: path === ''
        ? '分析 API 地址或服务配置异常，请检查服务地址'
        : '任务不存在或已过期，请重新分析',
        409: '任务状态冲突，服务可能仍在运行其他分析，请稍后重试',
        422: '分析参数无效，请检查中心坐标和调用预算',
        503: '分析服务当前不可用，请联系管理员检查步行服务配置' };
      throw new ApiError(messages[response.status] || (response.status >= 500
        ? '分析服务异常，请稍后重试' : '分析请求未成功，请检查服务配置后重试'), response.status);
    }
    try {
      const value: unknown = await response.json();
      const decoded = path.endsWith('/result') ? decodeAnalysisResult(value) : value;
      if (!path.endsWith('/result') && !validTask(decoded)) throw new Error('Invalid response structure');
      if (taskId !== undefined && decoded && typeof decoded === 'object' && 'taskId' in decoded
        && decoded.taskId !== taskId) throw new Error('Mismatched task');
      return decoded as T;
    }
    catch { throw new Error('后端返回格式异常，请检查服务版本'); }
  }
  const at = (id: string) => `/${encodeURIComponent(id)}`;
  return {
    create: input => request('', 'POST', { ...input, coordinateSystem: 'bd09ll' }),
    status: (id, signal) => request(at(id), 'GET', undefined, signal, id),
    result: (id, signal) => request(`${at(id)}/result`, 'GET', undefined, signal, id),
    cancel: id => request(`${at(id)}/cancel`, 'POST'),
    byRequest: (key, signal) => request(`/by-request/${encodeURIComponent(key)}`, 'GET', undefined, signal),
    cancelByRequest: key => request(`/by-request/${encodeURIComponent(key)}/cancel`, 'POST'),
  };
}
