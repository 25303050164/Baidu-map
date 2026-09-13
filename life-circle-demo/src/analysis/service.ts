import type { AnalysisService } from './types';
import { validTask } from './validate';
import { decodeAnalysisResult } from './adapter';

export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}

export function createApiService(base = import.meta.env.VITE_API_BASE_URL?.trim() || '', fetcher: typeof fetch = fetch): AnalysisService {
  async function request<T>(path: string, method = 'GET', body?: unknown, signal?: AbortSignal): Promise<T> {
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
      throw new Error('无法连接分析服务或请求超时，请检查网络和服务地址后重试');
    }
    if (!response.ok) {
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
      const body: unknown = await response.json();
      const value = path.endsWith('/result') ? decodeAnalysisResult(body) : body;
      if (!path.endsWith('/result') && !validTask(value)) throw new Error('Invalid response structure');
      if (method === 'GET' && value && typeof value === 'object' && 'taskId' in value
        && value.taskId !== decodeURIComponent(path.split('/')[1])) throw new Error('Mismatched task');
      return value as T;
    }
    catch { throw new Error('后端返回格式异常，请检查服务版本'); }
  }
  return {
    create: input => request('', 'POST', { ...input, coordinateSystem: 'bd09ll' }),
    status: (id, signal) => request(`/${encodeURIComponent(id)}`, 'GET', undefined, signal),
    result: (id, signal) => request(`/${encodeURIComponent(id)}/result`, 'GET', undefined, signal),
    cancel: id => request(`/${encodeURIComponent(id)}/cancel`, 'POST'),
    cancelByRequest: key => request(`/by-request/${encodeURIComponent(key)}/cancel`, 'POST'),
  };
}
