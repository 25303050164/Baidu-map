import { expect, it, vi } from 'vitest';
import { requestFacilityRoute } from './routes';
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
