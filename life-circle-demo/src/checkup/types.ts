import type { Center } from '../types';
import type { CheckupLayer, CheckupSnapshot, CheckupTaskView, FacilityRoute } from './contract';
import { STAGES, type LayerId, type Stage } from './validate';

/** v2 的引擎是具名的：两个算法各自服务各自的预算档，不互相顶替。 */
export type CheckupInput = {
  center: Center;
  engine: string;
  budget?: number;
  clientRequestId: string;
};

/** 阶段是后端说的，界面只按它排序和显示进度；没有百分比，也没有"预计剩余"。 */
export type CheckupPhase =
  | 'idle' | 'submitting' | 'queued' | 'running' | 'fetching' | 'cancelling'
  | 'completed' | 'cancelled' | 'error';

export type CheckupState = {
  phase: CheckupPhase;
  task?: CheckupTaskView;
  snapshot?: CheckupSnapshot;
  /** 已取到的图层，按 "图层:修订" 缓存：同一次体检里重开面板不该再发一次请求。 */
  layers?: Partial<Record<LayerId, CheckupLayer>>;
  route?: FacilityRoute;
  routeError?: string;
  error?: string;
};

export const BUSY_PHASES: CheckupPhase[] = ['submitting', 'queued', 'running', 'fetching', 'cancelling'];

export function isCheckupBusy(state: CheckupState): boolean {
  return BUSY_PHASES.includes(state.phase);
}

/** 终态：轮询停下，界面不再等待。 */
export function isTerminal(task: CheckupTaskView | undefined): boolean {
  return task !== undefined
    && ['completed', 'failed', 'cancelled'].includes(task.status);
}

export const STAGE_LABELS: Record<Stage, string> = {
  isochrone: '等时圈',
  poi: '设施检索',
  accessibility: '服务覆盖',
  verification: '步行核验',
  reporting: '报告',
  ready: '完成',
};

/** 阶段推进只用于显示"进行到哪一步"：它不参与任何判断，也不是进度条。 */
export function isStageReached(stage: Stage, current: Stage | null | undefined): boolean {
  return current !== null && current !== undefined && STAGES.indexOf(stage) <= STAGES.indexOf(current);
}
