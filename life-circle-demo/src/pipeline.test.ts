import { describe, it, expect } from 'vitest';
import { buildFacilities } from './pipeline';
import { filterFacilities, summarize } from './domain';
import { sampleCircle, sampleFacilities } from './fixtures/facilities.sample';
import { createResult, fixtures, samples } from './data';
import { categories } from './types';

const insideSquare = (x: number, y: number) => x >= 100 && x <= 450 && y >= 100 && y <= 450;

describe('facility pipeline', () => {
  it('classifies, deduplicates and assigns in-circle in one pass', () => {
    const result = buildFacilities(sampleFacilities, sampleCircle);
    expect(result.inputCount).toBe(sampleFacilities.length);
    expect(result.removedCount).toBe(2);
    expect(result.duplicates).toHaveLength(2);
    expect(result.facilities.length).toBe(result.uniqueCount - result.rejected.length);
    expect(result.facilities.every(f => f.category === 'market' || f.category === 'pharmacy' || f.category === 'school')).toBe(true);
    expect(new Set(result.facilities.map(f => f.id)).size).toBe(result.facilities.length);
    expect(result.facilities.every(f => f.inCircle === insideSquare(f.x, f.y))).toBe(true);
  });

  it('keeps training institutions out while counting the broad major categories', () => {
    const result = buildFacilities(sampleFacilities, sampleCircle);
    const rejectedNames = result.rejected.map(item => item.name);
    expect(rejectedNames).toContain('新东方英语培训中心');
    expect(rejectedNames).toContain('学而思辅导班');
    expect(rejectedNames).toContain('青禾美术兴趣班');
    expect(result.majorCounts.shopping).toBeGreaterThanOrEqual(6);
    expect(result.majorCounts.medical).toBeGreaterThanOrEqual(6);
    expect(result.majorCounts.education).toBeGreaterThanOrEqual(6);
    expect(result.majorCounts.care).toBeGreaterThanOrEqual(2);
  });

  it('classifies every demo fixture into its declared category', () => {
    const { facilities } = buildFacilities(
      fixtures.map(f => ({ uid: f.uid, name: f.name, x: f.x, y: f.y })),
      sampleCircle
    );
    const byUid = Object.fromEntries(facilities.map(f => [f.uid, f.category]));
    for (const fixture of fixtures) expect(byUid[fixture.uid]).toBe(fixture.category);
  });

  it('feeds the map and the statistics from the same deduplicated array', () => {
    for (const sample of samples) {
      const result = createResult(sample, 'normal');
      const summary = summarize(result);
      expect(summary.total).toBe(filterFacilities(result, 'all').length);
      for (const category of categories) expect(summary[category]).toBe(filterFacilities(result, category).length);
      const ids = result.facilities.map(f => f.id);
      expect(new Set(ids).size).toBe(ids.length);
    }
  });
});
