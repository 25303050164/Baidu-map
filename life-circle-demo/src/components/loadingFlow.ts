import type { State } from '../state';

/** 体检 Loading 编排结果：overlay 是否继续显示、是否自动打开报告。 */
export type LoadingOutcome = { visible: boolean; openReport: boolean };

/**
 * 纯函数：根据「是否已点击开始、Loading 动画是否播完、分析状态」推导 overlay 行为。
 * 规则：
 * - 未开始体检：不显示。
 * - 分析失败 / 坐标不可用：立即关闭（错误横幅按原有逻辑接管，不被动画掩盖），不打开报告。
 * - 分析成功且动画播完：关闭并自动展示体检报告（AI 体检流程的最终交付物）。
 * - 其余情况（分析进行中，或已成功但动画未播完）：保持显示，让体检过程完整呈现。
 */
export function loadingOutcome(input: { started: boolean; animationDone: boolean; analysisStatus: State['status'] }): LoadingOutcome {
  if (!input.started) return { visible: false, openReport: false };
  if (input.analysisStatus === 'failed' || input.analysisStatus === 'unavailable') return { visible: false, openReport: false };
  if (input.analysisStatus === 'success' && input.animationDone) return { visible: false, openReport: true };
  return { visible: true, openReport: false };
}
