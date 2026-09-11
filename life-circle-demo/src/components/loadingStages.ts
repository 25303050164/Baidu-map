/** 体检过程 Loading 的阶段定义与进度映射（纯逻辑，便于单测）。 */

export type StageKey = 'locate' | 'search' | 'analyze' | 'report' | 'done';

/** 单个分析阶段：key、主文案、副文案、起始进度锚点（%）。 */
export type LoadingStage = { key: StageKey; label: string; detail: string; progress: number };

/** 阶段时间轴：定位 → 检索 → 分析 → 生成 → 完成。锚点即用户任务中约定的 0/30/60/85/100。 */
export const LOADING_STAGES: readonly LoadingStage[] = [
  { key: 'locate', label: '正在定位社区位置', detail: '锁定社区中心点与周边路网', progress: 0 },
  { key: 'search', label: '正在检索周边生活设施', detail: '菜市场 · 药店 · 小学', progress: 30 },
  { key: 'analyze', label: '正在分析15分钟生活圈', detail: '计算 15 分钟步行可达范围', progress: 60 },
  { key: 'report', label: '正在生成体检报告', detail: '汇总设施统计与服务盲区', progress: 85 },
  { key: 'done', label: '完成', detail: '体检结果已就绪', progress: 100 }
] as const;

/** 纯函数：进度（0-100，越界值收敛到首/末阶段）→ 当前所处阶段。 */
export function stageForProgress(progress: number): LoadingStage {
  let current = LOADING_STAGES[0];
  for (const stage of LOADING_STAGES) {
    if (progress >= stage.progress) current = stage;
  }
  return current;
}

/** 纯函数：进度 → 阶段下标（步骤条高亮用）。 */
export function stageIndexForProgress(progress: number): number {
  return LOADING_STAGES.indexOf(stageForProgress(progress));
}

/**
 * 纯函数：visible 变化时的进度重置规则。
 * 重新打开（false→true）一律归零——绝不继承上一次的进度（如成功后的 100% 或失败中断时的 ~13%）；
 * 保持打开或隐藏时不重置（隐藏时组件不渲染，进度留待下次打开时由本规则归零）。
 */
export function progressOnVisibleChange(prevVisible: boolean, visible: boolean, progress: number): number {
  return !prevVisible && visible ? 0 : progress;
}
