import { describe, it, expect } from 'vitest';
import { loadingOutcome } from './loadingFlow';
import type { State } from '../state';

type Status = State['status'];

describe('loadingOutcome — 体检 Loading 编排策略', () => {
  it('stays hidden before the user starts an analysis, whatever the status', () => {
    for (const status of ['idle', 'loading', 'success', 'failed', 'unavailable'] as Status[]) {
      expect(loadingOutcome({ started: false, animationDone: false, analysisStatus: status })).toEqual({ visible: false, openReport: false });
      expect(loadingOutcome({ started: false, animationDone: true, analysisStatus: status })).toEqual({ visible: false, openReport: false });
    }
  });
  it('shows immediately after clicking, while the analysis is running or still idle', () => {
    for (const status of ['idle', 'loading'] as Status[]) {
      const outcome = loadingOutcome({ started: true, animationDone: false, analysisStatus: status });
      expect(outcome.visible).toBe(true);
      expect(outcome.openReport).toBe(false);
    }
  });
  it('keeps the loading visible after success until the animation has finished', () => {
    const outcome = loadingOutcome({ started: true, animationDone: false, analysisStatus: 'success' });
    expect(outcome.visible).toBe(true);
    expect(outcome.openReport).toBe(false);
  });
  it('closes and auto-opens the report once success and animation are both done', () => {
    expect(loadingOutcome({ started: true, animationDone: true, analysisStatus: 'success' })).toEqual({ visible: false, openReport: true });
  });
  it('closes immediately on failure even mid-animation, without opening the report', () => {
    for (const status of ['failed', 'unavailable'] as Status[]) {
      expect(loadingOutcome({ started: true, animationDone: false, analysisStatus: status })).toEqual({ visible: false, openReport: false });
      expect(loadingOutcome({ started: true, animationDone: true, analysisStatus: status })).toEqual({ visible: false, openReport: false });
    }
  });
  it('is deterministic for the same input', () => {
    const input = { started: true, animationDone: true, analysisStatus: 'success' } as const;
    expect(loadingOutcome(input)).toEqual(loadingOutcome(input));
  });
});
