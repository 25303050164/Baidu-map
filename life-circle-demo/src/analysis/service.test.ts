import { expect, it, vi } from 'vitest';
import { createApiService } from './service';

it('sends BD09 requests and propagates an abort signal for polling', async () => {
  const fetcher = vi.fn<typeof fetch>(async () => new Response('{}', { status: 200 }));
  const api = createApiService('http://127.0.0.1:8000/', fetcher);
  await api.create({ center: { lng: 116.4, lat: 39.9 }, budget: 400, clientRequestId: 'test' });
  expect(fetcher.mock.calls[0][0]).toBe('http://127.0.0.1:8000/api/analyses');
  expect(JSON.parse(fetcher.mock.calls[0][1]!.body as string).coordinateSystem).toBe('bd09ll');
  const controller = new AbortController();
  await api.status('one', controller.signal);
  expect(fetcher.mock.calls[1][0]).toContain('/api/analyses/one');
});

it('does not expose raw server or transport errors', async () => {
  const api = createApiService('', async () => new Response('sensitive upstream text', { status: 503 }));
  await expect(api.create({ center: { lng: 0, lat: 0 }, budget: 400, clientRequestId: 'test' })).rejects.toThrow('后端');
});
