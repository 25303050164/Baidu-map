import { expect, it, vi } from 'vitest';
import { createApiService } from './service';
import { resultFixture } from './testFixtures';

const task = { schema_version: '1.0', responseType: 'task', taskId: 'one', status: 'completed',
  businessStatus: 'partial', stage: 'completed', requests: 200, networkRequests: 0, budget: 200,
  elapsedSeconds: 1, dataSource: 'synthetic', error: null };

it('adapts valid HTTP results and strips unknown top-level fields', async () => {
  const body = { ...resultFixture(), extra: 'not part of the frontend contract' };
  const api = createApiService('', async () => new Response(JSON.stringify(body)));
  expect(await api.result('one')).toEqual(resultFixture());
});

it('uses same-origin API paths when no base URL is configured', async () => {
  vi.stubEnv('VITE_API_BASE_URL', '');
  try {
    const fetcher = vi.fn<typeof fetch>(async () => new Response(JSON.stringify(task)));
    await createApiService(undefined, fetcher).status('one');
    expect(fetcher.mock.calls[0][0]).toBe('/api/analyses/one');
  } finally { vi.unstubAllEnvs(); }
});

it('sends BD09 requests and propagates an abort signal for polling', async () => {
  const fetcher = vi.fn<typeof fetch>(async () => new Response(JSON.stringify(task), { status: 200 }));
  const api = createApiService('http://127.0.0.1:8000/', fetcher);
  await api.create({ center: { lng: 116.4, lat: 39.9 }, budget: 400, clientRequestId: 'test' });
  expect(fetcher.mock.calls[0][0]).toBe('http://127.0.0.1:8000/api/analyses');
  expect(JSON.parse(fetcher.mock.calls[0][1]!.body as string).coordinateSystem).toBe('bd09ll');
  const controller = new AbortController();
  await api.status('one', controller.signal);
  expect(fetcher.mock.calls[1][0]).toContain('/api/analyses/one');
});

it('rejects incompatible successful JSON before rendering it', async () => {
  const api = createApiService('', async () => new Response('{}', { status: 200 }));
  await expect(api.result('one')).rejects.toThrow('服务版本');
  await expect(api.status('one')).rejects.toThrow('服务版本');
});

it('rejects a response belonging to a different task', async () => {
  const api = createApiService('', async () => new Response(JSON.stringify(task), { status: 200 }));
  await expect(api.status('another')).rejects.toThrow('服务版本');
});

it('cancels by idempotency key without creating a replacement task', async () => {
  const fetcher = vi.fn<typeof fetch>(async () => new Response(JSON.stringify(task), { status: 202 }));
  await createApiService('', fetcher).cancelByRequest('original-key');
  expect(fetcher.mock.calls[0][0]).toBe('/api/analyses/by-request/original-key/cancel');
});

it('does not expose raw server or transport errors', async () => {
  const api = createApiService('', async () => new Response('sensitive upstream text', { status: 503 }));
  await expect(api.create({ center: { lng: 0, lat: 0 }, budget: 400, clientRequestId: 'test' })).rejects.toThrow('分析服务当前不可用');
});

it('distinguishes a missing creation endpoint from a missing existing task', async () => {
  const api = createApiService('', async () => new Response('private error details', { status: 404 }));
  await expect(api.create({ center: { lng: 0, lat: 0 }, budget: 400, clientRequestId: 'test' })).rejects.toThrow('分析 API 地址或服务配置异常');
  await expect(api.status('one')).rejects.toThrow('任务不存在或已过期');
  await expect(api.result('one')).rejects.toThrow('任务不存在或已过期');
});

it.each([
  [422, '分析参数无效，请检查中心坐标和调用预算'],
  [503, '分析服务当前不可用，请联系管理员检查步行服务配置'],
  [500, '分析服务异常，请稍后重试'],
  [502, '分析服务异常，请稍后重试'],
])('maps HTTP %s without exposing its response body', async (status, message) => {
  const api = createApiService('', async () => new Response('private upstream stack', { status }));
  await expect(api.status('one')).rejects.toThrow(message);
});

it('sanitizes transport failure and keeps user abort distinct from network failure', async () => {
  const api = createApiService('', async () => { throw new Error('private transport details'); });
  await expect(api.status('one')).rejects.toThrow('无法连接分析服务或请求超时，请检查网络和服务地址后重试');
  const signal = AbortSignal.abort();
  await expect(api.status('one', signal)).rejects.toMatchObject({ name: 'AbortError' });
});
