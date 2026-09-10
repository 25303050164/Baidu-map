import { createResult, customSample, samples } from './data';
import { findSample, inMapBounds } from './domain';
import type { AnalysisResult, AnalysisService, Conditions, TaskStatus } from './types';
export function createDemoService(delay = 650): AnalysisService {
  const jobs = new Map<string, { ready: number; status: TaskStatus; result?: AnalysisResult }>();
  const failures = new Set<string>();
  let sequence = 0;
  return {
    async getSamples() { return structuredClone(samples); },
    async createAnalysis(conditions: Conditions) {
      const id = `demo-${++sequence}`;
      const available = inMapBounds(conditions.center);
      const sample = findSample(samples, conditions.center) ?? customSample(conditions.center);
      const key = `${conditions.center.lng}:${conditions.center.lat}`;
      const fails = available && conditions.scenario === 'failure' && !failures.has(key);
      if (fails) failures.add(key);
      jobs.set(id, { ready: Date.now() + delay, status: !available ? 'unavailable' : fails ? 'failed' : 'completed', result: available && !fails ? createResult(sample, conditions.scenario) : undefined });
      // The demo has no history feature; retain only recent requests.
      if (jobs.size > 20) jobs.delete(jobs.keys().next().value!);
      return id;
    },
    async getStatus(id) {
      const job = jobs.get(id); if (!job) throw new Error('分析任务不存在，请重新体检。');
      return { status: Date.now() < job.ready ? 'running' : job.status };
    },
    async getResult(id) {
      const job = jobs.get(id);
      if (!job?.result || Date.now() < job.ready) throw new Error('分析结果尚不可用。');
      return structuredClone(job.result);
    }
  };
}
export const demoService = createDemoService();
