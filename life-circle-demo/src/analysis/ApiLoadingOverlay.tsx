import { Button } from 'antd';
import { isAnalysisBusy, type AnalysisState } from './types';
import styles from '../components/loading.module.css';

const stages: Record<string, string> = {
  initializing: '初始化采样',
  expanding: '检查并扩展范围',
  exploring: '探索采样',
  refining: '边界细化与补测',
  reconstructing: '重建时间场与几何',
  facilities: '检索设施与核对步行距离',
  osm_initializing: '初始化离线路网',
  osm_expanding: '离线路网扩展范围',
  osm_exploring: '离线路网采样',
  osm_refining: '离线路网边界细化',
  osm_reconstructing: '重建 OSM 等时圈',
  baidu_initializing: '百度在线采样',
  baidu_expanding: '百度在线扩展范围',
  baidu_exploring: '百度在线采样探索',
  baidu_refining: '百度在线边界细化',
  baidu_reconstructing: '百度在线重建等时圈',
  baidu_facilities: '百度设施核对',
};

export function ApiLoadingOverlay({ state, onCancel }: { state: AnalysisState; onCancel: () => void }) {
  const busy = isAnalysisBusy(state);
  if (!busy) return null;

  const task = state.task;
  const title = state.phase === 'submitting' ? '正在创建分析任务'
    : state.phase === 'fetching' ? '正在整理分析结果'
      : state.phase === 'cancelling' ? '正在取消分析任务'
        : stages[task?.stage ?? ''] || '正在计算等时圈';
  const offline = task?.analysisMode === 'osm_offline' || task?.dataSource === 'osm_offline';
  const hybrid = task?.analysisMode === 'hybrid' || task?.dataSource === 'hybrid';
  const detail = offline ? '离线路网计算，不产生百度网络调用'
    : hybrid ? '百度与 OSM 正在并行计算，仅保留两套结果进行对比'
      : '阶段来自后端真实任务状态，不模拟完成百分比';

  return <div className={styles.overlay} data-testid="api-loading-overlay" role="status" aria-live="polite" aria-busy="true">
    <div className={styles.card}>
      <div className={styles.stageVisual} aria-hidden="true">
        <span className={styles.ripple} />
        <span className={`${styles.ripple} ${styles.rippleLate}`} />
        <span className={`${styles.radar} ${styles.radarOn}`} />
        <span className={styles.pinCore} />
      </div>
      <h2 className={styles.stageTitle}>{title}</h2>
      <p className={styles.stageDetail}>{detail}</p>
      {task && <p className={styles.stageDetail}>
        {offline ? 'OSM 离线数据' : hybrid ? '百度 + OSM 并行对比' : `${task.requests} / ${task.budget} 次调用`}
        {' · '}{task.elapsedSeconds.toFixed(1)} 秒
      </p>}
      <Button onClick={onCancel} disabled={state.phase === 'cancelling'} loading={state.phase === 'cancelling'}>
        取消任务
      </Button>
    </div>
  </div>;
}
