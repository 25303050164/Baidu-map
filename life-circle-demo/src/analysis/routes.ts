import { validRoute } from './validate';

/** 与 service.ts 相同的基址解析：未配置时同源请求，不硬编码端口。 */
export function routeApiBase(): string {
  return (import.meta.env.VITE_API_BASE_URL?.trim() || '').replace(/\/$/, '');
}

export async function requestFacilityRoute(taskId: string, facilityId: string, signal: AbortSignal,
  fetcher: typeof fetch = fetch) {
  const base = routeApiBase();
  const response = await fetcher(`${base}/api/analyses/${encodeURIComponent(taskId)}/routes/${encodeURIComponent(facilityId)}`, {
    method: 'POST', signal: AbortSignal.any([signal, AbortSignal.timeout(25_000)]),
  });
  if (!response.ok) throw new Error(response.status === 429
    ? '本次新增路线查询已达3次，请使用已有路线。'
    : response.status === 409
      ? '设施结果尚未就绪，无法查询路线。'
      : response.status === 404
        ? '设施不属于本次分析或任务已过期，请重新分析。'
        : '路线暂不可用，请重试。');
  const value: unknown = await response.json();
  if (!validRoute(value)) throw new Error('路线格式异常');
  signal.throwIfAborted();
  return { ...value, path: value.path.map(([lng, lat]): [number, number] => [lng, lat]) };
}
