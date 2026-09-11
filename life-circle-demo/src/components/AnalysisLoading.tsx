import { useEffect, useRef, useState } from 'react';
import { categories, categoryMeta } from '../types';
import { LOADING_STAGES, progressOnVisibleChange, stageForProgress, stageIndexForProgress } from './loadingStages';
import styles from './loading.module.css';

export type AnalysisLoadingProps = {
  /** 是否展示 Loading 覆盖层；由父组件控制。false→true 重新打开时进度在渲染阶段归零，不继承上一次的进度。 */
  visible: boolean;
  /** 模拟流程走完（到 100% 并短暂停留）后回调，由父组件决定后续动作。 */
  onFinish: () => void;
};

/** 进度每 tick +1，总时长约 5.5s，各阶段停留时长由锚点间隔决定。 */
const TICK_MS = 55;
/** 到达 100% 后的停留时长，让"完成"状态可被看见再回调。 */
const FINISH_DELAY_MS = 600;

/** 圆盘上散布的设施点位（相对圆盘的百分比坐标），检索阶段起逐个浮现。 */
const FACILITY_SPOTS: ReadonlyArray<{ left: number; top: number; category: (typeof categories)[number] }> = [
  { left: 24, top: 34, category: 'market' },
  { left: 66, top: 20, category: 'pharmacy' },
  { left: 78, top: 52, category: 'school' },
  { left: 34, top: 70, category: 'pharmacy' },
  { left: 58, top: 78, category: 'market' },
  { left: 14, top: 58, category: 'school' },
  { left: 72, top: 82, category: 'pharmacy' },
  { left: 46, top: 16, category: 'school' },
  { left: 86, top: 34, category: 'market' },
  { left: 40, top: 50, category: 'pharmacy' }
];

/** AI 体检过程 Loading 组件：定位波纹 → 雷达扫描 → 设施浮现 → 等时圈 → 报告/完成。独立组件，不耦合分析逻辑。 */
export function AnalysisLoading({ visible, onFinish }: AnalysisLoadingProps) {
  const [progress, setProgress] = useState(0);
  // 渲染阶段重置（React「props 变化时调整 state」模式）：visible false→true 重新打开时，
  // 在提交到 DOM 之前就把进度归零——首帧即 0%，不会先闪现上一次的进度再倒退。规则见 progressOnVisibleChange。
  const [prevVisible, setPrevVisible] = useState(visible);
  if (visible !== prevVisible) {
    const next = progressOnVisibleChange(prevVisible, visible, progress);
    setPrevVisible(visible);
    if (next !== progress) setProgress(next);
  }
  const stage = stageForProgress(progress);
  const stageIndex = stageIndexForProgress(progress);
  const done = stage.key === 'done';
  // 用 ref 持有最新回调，父组件传内联函数也不会重置完成计时器。
  const onFinishRef = useRef(onFinish);
  onFinishRef.current = onFinish;

  useEffect(() => {
    if (!visible) return;
    const timer = setInterval(() => setProgress(p => Math.min(100, p + 1)), TICK_MS);
    return () => clearInterval(timer);
  }, [visible]);

  useEffect(() => {
    if (!visible || !done) return;
    const timer = setTimeout(() => onFinishRef.current(), FINISH_DELAY_MS);
    return () => clearTimeout(timer);
  }, [visible, done]);

  if (!visible) return null;

  const searching = progress >= 30;
  const analyzing = progress >= 60;

  return (
    <div className={styles.overlay} role="status" aria-live="polite" data-testid="analysis-loading">
      <div className={styles.card}>
        <div className={styles.stageVisual}>
          {/* 等时圈：分析阶段浮现的虚线可达圈 */}
          <span className={`${styles.reachRing} ${analyzing ? styles.reachRingOn : ''}`} />
          {/* 雷达扫描扇形：检索阶段起持续旋转 */}
          <span className={`${styles.radar} ${searching ? styles.radarOn : ''}`} />
          {/* 定位点与扩散波纹 */}
          <span className={styles.pinCore} />
          <span className={styles.ripple} />
          <span className={`${styles.ripple} ${styles.rippleLate}`} />
          {/* 周边设施：检索阶段起按序浮现 */}
          {FACILITY_SPOTS.map((spot, i) => searching && (
            <span
              key={i}
              className={styles.spot}
              style={{ left: `${spot.left}%`, top: `${spot.top}%`, animationDelay: `${i * 90}ms`, color: categoryMeta[spot.category].color }}
            >
              {categoryMeta[spot.category].symbol}
            </span>
          ))}
          {/* 完成对勾：替换定位点 */}
          {done && <span className={styles.check}>✓</span>}
        </div>
        <h2 className={styles.stageTitle}>{stage.label}</h2>
        <p className={styles.stageDetail}>{stage.detail}</p>
        <div className={styles.progressBar}>
          <span className={styles.progressFill} style={{ width: `${progress}%` }} />
        </div>
        <div className={styles.progressMeta}>
          <span className={styles.stepDots}>
            {LOADING_STAGES.map((s, i) => (
              <i key={s.key} className={i <= stageIndex ? styles.stepDotOn : styles.stepDot} />
            ))}
          </span>
          <span className={styles.progressNumber}>{progress}%</span>
        </div>
      </div>
    </div>
  );
}
