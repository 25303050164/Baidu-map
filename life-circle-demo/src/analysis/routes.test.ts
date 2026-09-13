import { expect, it, vi } from 'vitest';
import { requestFacilityRoute, routeApiBase } from './routes';
import { validRoute } from './validate';

const route = { distance_m: 100, duration_s: 80, endpoint_verified: true, reason: null, path: [[121,31], [121.001,31]] };

it('rejects invalid metrics, coordinates, and unverified paths', () => {
  expect(validRoute(route)).toBe(true);
  for (const change of [{distance_m: -1}, {duration_s: '80'}, {path: [[999,31]]}, {endpoint_verified: false}]) {
    expect(validRoute({...route,...change})).toBe(false);
  }
});

it('discards a late response even if a transport ignores abort', async () => {
  const controller = new AbortController();
  const fetcher = vi.fn(async () => {
    controller.abort();
    return new Response(JSON.stringify(route));
  });
  await expect(requestFacilityRoute('task', 'poi', controller.signal, fetcher)).rejects.toThrow();
});

it('builds a same-origin route url when no api base is configured', async () => {
  vi.stubEnv('VITE_API_BASE_URL', '');
  try {
    const fetcher = vi.fn(async (_url: RequestInfo | URL) => new Response(JSON.stringify(route)));
    await requestFacilityRoute('task one', 'poi/2', new AbortController().signal, fetcher);
    expect(fetcher.mock.calls[0][0]).toBe('/api/analyses/task%20one/routes/poi%2F2');
  } finally { vi.unstubAllEnvs(); }
});

it('keeps the configured api base without a trailing slash', async () => {
  vi.stubEnv('VITE_API_BASE_URL', '  http://127.0.0.1:8018/ ');
  try {
    expect(routeApiBase()).toBe('http://127.0.0.1:8018');
    const fetcher = vi.fn(async (_url: RequestInfo | URL) => new Response(JSON.stringify(route)));
    await requestFacilityRoute('t', 'f', new AbortController().signal, fetcher);
    expect(fetcher.mock.calls[0][0]).toBe('http://127.0.0.1:8018/api/analyses/t/routes/f');
  } finally { vi.unstubAllEnvs(); }
});

it.each([
  [429, '本次新增路线查询已达3次，请使用已有路线。'],
  [409, '设施结果尚未就绪，无法查询路线。'],
  [404, '设施不属于本次分析或任务已过期，请重新分析。'],
  [500, '路线暂不可用，请重试。'],
])('maps route http %i to an actionable message', async (status, message) => {
  const fetcher = vi.fn(async () => new Response('{}', { status }));
  await expect(requestFacilityRoute('t', 'f', new AbortController().signal, fetcher)).rejects.toThrow(message);
});
