import { describe, it, expect } from 'vitest';
import { samples, createResult, customSample } from './data';
import { summarize, filterFacilities, findSample, pointInPolygon, pointToCenter } from './domain';
import { reducer, initialState } from './state';
import { createDemoService } from './service';

describe('deterministic demo evidence', () => {
  it('all three samples produce distinct results and exclude outside facilities from totals', () => {
    for (const sample of samples) {
      const result = createResult(sample, 'normal');
      const counts = summarize(result);
      expect(counts.total).toBe(result.facilities.filter(f => f.inCircle).length);
      expect(result.facilities.some(f => !f.inCircle)).toBe(true);
      expect(filterFacilities(result, 'all').every(f => f.inCircle)).toBe(true);
      expect(result.sample.id).toBe(sample.id);
      expect(createResult(sample, 'normal').facilities).toEqual(result.facilities);
    }
    expect(samples.map(s => createResult(s, 'normal').circle)).not.toEqual([[], [], []]);
  });
  it('keeps facilities at fixed geography while each center sees a different scan', () => {
    const results = samples.map(s => createResult(s, 'normal'));
    const footprint = (r: (typeof results)[number]) => JSON.stringify(r.facilities.map(f => [f.name, f.x, f.y]).sort());
    expect(results.map(footprint)).toEqual([footprint(results[0]), footprint(results[0]), footprint(results[0])]);
    const scans = results.map(r => r.facilities.filter(f => f.inCircle).map(f => f.name).sort().join('|'));
    expect(new Set(scans).size).toBe(samples.length);
  });
  it('classifies a point against the reach polygon', () => {
    const square = [{ x: 0, y: 0 }, { x: 10, y: 0 }, { x: 10, y: 10 }, { x: 0, y: 10 }];
    expect(pointInPolygon({ x: 5, y: 5 }, square)).toBe(true);
    expect(pointInPolygon({ x: 15, y: 5 }, square)).toBe(false);
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
  it('produces a custom result for any in-map coordinate', async () => {
    const service = createDemoService(0);
    const center = pointToCenter({ x: 500, y: 400 });
    const id = await service.createAnalysis({ center, scenario: 'normal' });
    expect((await service.getStatus(id)).status).toBe('completed');
    const result = await service.getResult(id);
    expect(result.sample.id).toBe('custom');
    expect(result.sample.position.x).toBeCloseTo(500, 3);
    expect(result.sample.position.y).toBeCloseTo(400, 3);
  });
  it('keeps coordinates outside the map unavailable', async () => {
    const service = createDemoService(0);
    const id = await service.createAnalysis({ center: pointToCenter({ x: -50, y: 400 }), scenario: 'normal' });
    expect((await service.getStatus(id)).status).toBe('unavailable');
  });
  it('shows a preset blind zone only when the center is near it', () => {
    const near = customSample(pointToCenter({ x: 450, y: 355 }));
    const far = customSample(pointToCenter({ x: 900, y: 700 }));
    expect(createResult(near, 'missing').zones).toHaveLength(1);
    expect(createResult(far, 'missing').zones).toHaveLength(0);
  });
});
