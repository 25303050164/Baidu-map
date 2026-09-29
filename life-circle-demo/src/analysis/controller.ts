/**
 * E8.2 旧版分析（`/api/analyses`）接到通用的旧版任务控制器上。
 *
 * 任务生命周期的规矩都在 `legacyController.ts`；这里只回答三件 E8.2 自己的事：错误怎么
 * 归类、给读者看哪句话、取回来的结果算不算这一次提交的。
 */
import { LegacyController, ResultMismatchError, type Failure, type LegacyApi, type LegacyHandle } from '../legacyController';
import { ApiError } from './service';
import { matchesAnalysisInput } from './adapter';
import type { AnalysisInput, AnalysisResult, AnalysisService, AnalysisState, TaskStatus } from './types';

export function classifyAnalysisError(error: unknown): Failure {
  if (!(error instanceof ApiError)) return 'other';
  if (error.status === 0 || [502, 504].includes(error.status)) return 'network';
  if (error.busy) return 'busy';
  if (error.status === 404) return 'missing';
  // 其余 4xx 与 503（后端没配 AK）都是服务端明确的回答：没有建任务。
  if ((error.status >= 400 && error.status < 500) || error.status === 503) return 'refused';
  return 'other';
}

export function analysisApi(service: AnalysisService): LegacyApi<AnalysisInput, TaskStatus, AnalysisResult> {
  return {
    create: input => service.create(input),
    byRequest: (key, signal) => service.byRequest(key, signal),
    status: (id, signal) => service.status(id, signal),
    result: (id, signal) => service.result(id, signal),
    cancel: id => service.cancel(id),
    classify: classifyAnalysisError,
    describe: error => error instanceof ResultMismatchError || error instanceof Error ? error.message : '分析失败，请重试',
    matches: (result, task, input) => result.taskId === task.taskId && result.dataSource === task.dataSource
      && matchesAnalysisInput(result, input),
  };
}

export class AnalysisController extends LegacyController<AnalysisInput, TaskStatus, AnalysisResult> {
  constructor(
    service: AnalysisService,
    publish: (state: AnalysisState) => void,
    onHandle?: (handle: LegacyHandle<AnalysisInput> | undefined) => void,
    recall?: () => LegacyHandle<AnalysisInput> | undefined,
  ) { super(analysisApi(service), publish, onHandle, recall); }
}
