import { describe, it, expect } from 'vitest';
import { samples, createResult } from './data';
import { summarize, filterFacilities, findSample } from './domain';
import { reducer, initialState } from './state';
import { createDemoService } from './service';

describe('deterministic demo evidence', () => {
  it('all three samples produce distinct results and exclude outside facilities from totals', () => {
    for (const sample of samples) {
      const result = createResult(sample, 'normal');
      const counts = summarize(result);
      expect(counts.total).toBe(result.facilities.filter(f => f.inCircle).length);
      expect(result.facilities.some(f => !f.inCircle)).toBe(true);
      expect(result.sample.id).toBe(sample.id);
      expect(createResult(sample, 'normal').facilities).toEqual(result.facilities);
    }
    expect(samples.map(s => createResult(s, 'normal').circle)).not.toEqual([[], [], []]);
  });
  it('unknown category is not a confirmed blind spot or a false zero', () => {
    const result = createResult(samples[0], 'insufficient');
    expect(result.quality.pharmacy).toBe('unknown');
    expect(summarize(result).pharmacy).toBeNull();
    expect(result.zones.filter(z => z.category === 'pharmacy').every(z => z.status === 'unknown')).toBe(true);
    expect(summarize(result).blindCount).toBe(0);
  });
  it('missing scenario labels only the affected service', () => {
    const result = createResult(samples[0], 'missing');
    expect(result.zones.some(z => z.status === 'blind' && z.category === 'market')).toBe(true);
    expect(filterFacilities(result, 'school').every(f => f.category === 'school')).toBe(true);
  });
  it('does not assign non-preset coordinates to an unrelated sample', () => {
    expect(findSample(samples, samples[1].center)?.id).toBe(samples[1].id);
    expect(findSample(samples, { lng: 110, lat: 20 })).toBeUndefined();
  });
});

describe('result isolation', () => {
  it('ignores results returned for an outdated request', () => {
    const state = reducer(initialState, { type: 'start', requestId: 2 });
    const result = createResult(samples[0], 'normal');
    expect(reducer(state, { type: 'success', requestId: 1, result })).toEqual(state);
    expect(reducer(state, { type: 'success', requestId: 2, result }).result).toEqual(result);
  });
  it('editing conditions retains old result but invalidates the pending request', () => {
    const result = createResult(samples[0], 'normal');
    const state = { ...initialState, result, requestId: 1 };
    const next = reducer(state, { type: 'edit', center: samples[1].center });
    expect(next.result).toEqual(result);
    expect(next.dirty).toBe(true);
    expect(reducer(next, { type: 'success', requestId: 1, result }).dirty).toBe(true);
  });
});

describe('async demo service', () => {
  it('failure scenario fails once, then succeeds on retry', async () => {
    const service = createDemoService(0);
    const conditions = { center: samples[0].center, scenario: 'failure' as const };
    const first = await service.createAnalysis(conditions);
    expect((await service.getStatus(first)).status).toBe('failed');
    const retry = await service.createAnalysis(conditions);
    expect((await service.getStatus(retry)).status).toBe('completed');
    expect((await service.getResult(retry)).sample.id).toBe(samples[0].id);
  });
  it('unknown center returns an explicit unavailable state', async () => {
    const service = createDemoService(0);
    const id = await service.createAnalysis({ center: { lng: 0, lat: 0 }, scenario: 'normal' });
    expect((await service.getStatus(id)).status).toBe('unavailable');
  });
});
