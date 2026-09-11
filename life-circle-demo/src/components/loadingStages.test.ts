import { describe, it, expect } from 'vitest';
import { LOADING_STAGES, progressOnVisibleChange, stageForProgress, stageIndexForProgress } from './loadingStages';

describe('LOADING_STAGES — 阶段时间轴定义', () => {
  it('defines the five required stages in order with the agreed anchors 0/30/60/85/100', () => {
    expect(LOADING_STAGES.map(s => s.key)).toEqual(['locate', 'search', 'analyze', 'report', 'done']);
    expect(LOADING_STAGES.map(s => s.progress)).toEqual([0, 30, 60, 85, 100]);
  });
  it('gives every stage a non-empty label and detail', () => {
    for (const stage of LOADING_STAGES) {
      expect(stage.label.trim().length).toBeGreaterThan(0);
      expect(stage.detail.trim().length).toBeGreaterThan(0);
    }
  });
  it('starts at 0 and ends at 100 so the timeline covers the whole progress bar', () => {
    expect(LOADING_STAGES[0].progress).toBe(0);
    expect(LOADING_STAGES[LOADING_STAGES.length - 1].progress).toBe(100);
    expect(LOADING_STAGES[LOADING_STAGES.length - 1].label).toBe('完成');
  });
});

describe('stageForProgress — 进度到阶段的映射', () => {
  it('switches stages exactly at the anchor boundaries', () => {
    expect(stageForProgress(0).key).toBe('locate');
    expect(stageForProgress(29).key).toBe('locate');
    expect(stageForProgress(30).key).toBe('search');
    expect(stageForProgress(59).key).toBe('search');
    expect(stageForProgress(60).key).toBe('analyze');
    expect(stageForProgress(84).key).toBe('analyze');
    expect(stageForProgress(85).key).toBe('report');
    expect(stageForProgress(99).key).toBe('report');
    expect(stageForProgress(100).key).toBe('done');
  });
  it('clamps out-of-range progress to the first/last stage', () => {
    expect(stageForProgress(-5).key).toBe('locate');
    expect(stageForProgress(150).key).toBe('done');
  });
  it('never skips or repeats a stage while progress sweeps 0→100', () => {
    const seen: string[] = [];
    for (let p = 0; p <= 100; p++) {
      const key = stageForProgress(p).key;
      if (seen[seen.length - 1] !== key) seen.push(key);
    }
    expect(seen).toEqual(['locate', 'search', 'analyze', 'report', 'done']);
  });
  it('returns the stage with the largest anchor not exceeding the progress', () => {
    for (let p = 0; p <= 100; p++) {
      const stage = stageForProgress(p);
      expect(stage.progress).toBeLessThanOrEqual(p);
      const next = LOADING_STAGES[stageIndexForProgress(p) + 1];
      if (next) expect(next.progress).toBeGreaterThan(p);
    }
  });
});

describe('stageIndexForProgress — 步骤条下标', () => {
  it('matches the stage position in LOADING_STAGES across the sweep', () => {
    expect(stageIndexForProgress(0)).toBe(0);
    expect(stageIndexForProgress(45)).toBe(1);
    expect(stageIndexForProgress(72)).toBe(2);
    expect(stageIndexForProgress(92)).toBe(3);
    expect(stageIndexForProgress(100)).toBe(4);
    for (let p = 0; p <= 100; p++) {
      expect(LOADING_STAGES[stageIndexForProgress(p)]).toBe(stageForProgress(p));
    }
  });
});

describe('progressOnVisibleChange — 重新打开时的进度重置', () => {
  it('a reopen (false→true) resets progress so the previous 100% is never inherited', () => {
    expect(progressOnVisibleChange(false, true, 100)).toBe(0);
    expect(progressOnVisibleChange(false, true, 85)).toBe(0);
    // 失败中断时残留的中低进度（约 13%）同样不继承
    expect(progressOnVisibleChange(false, true, 13)).toBe(0);
  });
  it('a reopened loading starts at 0% in the first stage', () => {
    const progress = progressOnVisibleChange(false, true, 100);
    expect(progress).toBe(0);
    expect(stageForProgress(progress).key).toBe('locate');
    expect(stageForProgress(progress).label).toBe(LOADING_STAGES[0].label);
  });
  it('does not reset while the overlay stays open (ticks keep their value)', () => {
    expect(progressOnVisibleChange(true, true, 55)).toBe(55);
    expect(progressOnVisibleChange(true, true, 100)).toBe(100);
  });
  it('keeps the last value when hiding; only the next open resets', () => {
    expect(progressOnVisibleChange(true, false, 100)).toBe(100);
    expect(progressOnVisibleChange(true, false, 13)).toBe(13);
  });
});
