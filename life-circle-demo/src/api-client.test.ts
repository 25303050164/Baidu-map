import { readFileSync } from 'node:fs';
import { describe, expect, it, vi } from 'vitest';
import { AnalysisHttpError, createAnalysisClient, type MockScenario } from './api-client';
import type { AnalysisResponse } from './api-contract';

const fixture = (name: string): AnalysisResponse => JSON.parse(readFileSync(new URL(`../../backend/mocks/${name}.json`, import.meta.url), 'utf8'));

describe('N05 geographic contract client', () => {
  it.each<MockScenario>(['complete', 'partial', 'failed', 'empty'])('accepts shared %s fixture', async scenario => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(fixture(scenario))));
    const body = await createAnalysisClient('http://localhost:8000/', fetcher).mock(scenario);
    expect(body.status).toBe(scenario);
    expect(body.data.categories).toHaveLength(3);
    expect(body.data.categories.every(c => c.major_category && c.minor_category === c.category)).toBe(true);
    expect(fetcher.mock.calls[0][0]).toBe(`http://localhost:8000/api/v1/analysis/mock/${scenario}`);
  });
  it('preserves HTTP failure body and keeps empty different from unknown', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(fixture('failed')), { status: 500 }));
    await expect(createAnalysisClient('', fetcher).mock('failed')).rejects.toBeInstanceOf(AnalysisHttpError);
    expect(fixture('failed').data.facilities).toBeNull();
    expect(fixture('empty').data.facilities).toEqual([]);
  });
  it('sends explicit BD09LL and forwards cancellation without credentials', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(fixture('partial'))));
    const signal = new AbortController().signal;
    await createAnalysisClient('', fetcher).synthetic({origin:{lng:116.4,lat:39.9},coordinate_system:'bd09ll'}, signal);
    const init = fetcher.mock.calls[0][1];
    expect(JSON.parse(init.body).coordinate_system).toBe('bd09ll');
    expect(init.signal).toBe(signal);
    expect(init.credentials).toBeUndefined();
  });
  it('rejects incompatible payload', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response('{}'));
    await expect(createAnalysisClient('', fetcher).mock('complete')).rejects.toThrow('格式不匹配');
  });
});
