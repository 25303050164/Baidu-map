import { validRoute } from './validate';

export async function requestFacilityRoute(taskId: string, facilityId: string, signal: AbortSignal,
  fetcher: typeof fetch = fetch) {
  const base = (import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000').replace(/\/$/, '');
  const response = await fetcher(`${base}/api/analyses/${encodeURIComponent(taskId)}/routes/${encodeURIComponent(facilityId)}`, {
    method: 'POST', signal: AbortSignal.any([signal, AbortSignal.timeout(25_000)]),
  });
  if (!response.ok) throw new Error(response.status === 429
    ? '本次新增路线查询已达3次，请使用已有路线。' : '路线暂不可用，请重试。');
  const value: unknown = await response.json();
  if (!validRoute(value)) throw new Error('路线格式异常');
  signal.throwIfAborted();
  return { ...value, path: value.path.map(([lng, lat]): [number, number] => [lng, lat]) };
}
