/**
 * 主入口：页面 × 算法。
 *
 * - **体检 v2**（默认）：`/api/v2/checkups`。任务持久化在后端，带修订、图层、服务覆盖热力与
 *   设施密度热力；两种算法各是一个引擎（`baidu_e82` / `osm_hybrid`），各有自己的任务。
 * - **旧版成圈分析**：`/api/analyses`（E8.2）与 `/api/v1/analysis/hybrid`（OSM＋百度）。保留作
 *   对照；任务只在后端内存里，后端重启或完成 30 分钟后清掉。
 *
 * 选择写在地址的 hash 里（`#/checkup/e82`、`#/legacy/hybrid`……），刷新、前进后退都回到
 * 同一个页面；切换页面或算法只是换一个订阅者，**不取消任何任务**。
 */
import { useEffect, useState, useSyncExternalStore } from 'react';
import { Badge, Segmented } from 'antd';
import BaiduApp from './analysis/ApiApp';
import HybridApp from './hybrid/HybridApp';
import CheckupApp from './checkup/CheckupApp';
import { checkupSession } from './checkup/sessions';
import { isCheckupBusy } from './checkup/types';
import { legacyActivity } from './legacyTasks';

export type View = 'checkup' | 'legacy';
export type Algorithm = 'baidu' | 'hybrid';
export type ShellRoute = { view: View; algorithm: Algorithm };

export const ENGINES: Record<Algorithm, string> = { baidu: 'baidu_e82', hybrid: 'osm_hybrid' };
const SLUGS: Record<Algorithm, string> = { baidu: 'e82', hybrid: 'hybrid' };
const ROUTE_KEY = 'life-circle:shell:v1';

export function parseRoute(hash: string): ShellRoute | null {
  const match = /^#\/(checkup|legacy)\/(e82|hybrid)$/.exec(hash);
  if (!match) return null;
  return { view: match[1] as View, algorithm: match[2] === 'hybrid' ? 'hybrid' : 'baidu' };
}
export const formatRoute = (route: ShellRoute) => `#/${route.view}/${SLUGS[route.algorithm]}`;

function initialRoute(): ShellRoute {
  const fromHash = parseRoute(window.location.hash);
  if (fromHash) return fromHash;
  try {
    const stored = parseRoute(localStorage.getItem(ROUTE_KEY) ?? '');
    if (stored) return stored;
  } catch { /* 存储不可用时按配置的默认值 */ }
  return { view: 'checkup',
    algorithm: import.meta.env.VITE_ANALYSIS_MODE === 'hybrid' ? 'hybrid' : 'baidu' };
}

const LIVE_TEXT: Record<string, string> = {
  submitting: '提交中', restoring: '核对中', queued: '排队中', running: '运行中', fetching: '取结果',
  cancelling: '取消中',
};

const quiet = () => () => {};

/**
 * 算法选项上的小标记：另一个算法还有任务在跑时，切过去之前就能看见。
 * 只看当前页面那一套任务（v2 或旧版）：另一套的会话要等用户切过去才建立。
 */
function useActivity(view: View, algorithm: Algorithm): string | null {
  const session = view === 'checkup' ? checkupSession(ENGINES[algorithm]) : null;
  const phase = useSyncExternalStore(session ? session.subscribe : quiet,
    () => session ? session.getState().phase : 'idle');
  const legacy = useSyncExternalStore(view === 'legacy' ? legacyActivity.subscribe : quiet,
    () => view === 'legacy' ? legacyActivity.get(algorithm) : null);
  if (view === 'checkup') return isCheckupBusy({ phase }) ? LIVE_TEXT[phase] ?? null : null;
  return legacy;
}

function AlgorithmLabel({ view, algorithm, text }: { view: View; algorithm: Algorithm; text: string }) {
  const activity = useActivity(view, algorithm);
  return <span data-testid={`algorithm-${SLUGS[algorithm]}`} data-activity={activity ?? ''}>
    {text}{activity && <Badge status="processing" text={activity} className="algorithm-activity" />}
  </span>;
}

export default function AlgorithmApp() {
  const [route, setRoute] = useState<ShellRoute>(initialRoute);

  useEffect(() => {
    const onHash = () => { const next = parseRoute(window.location.hash); if (next) setRoute(next); };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  useEffect(() => {
    const hash = formatRoute(route);
    // 首次进入没有 hash 时补上，但不多留一条历史记录。
    if (window.location.hash !== hash) window.history.replaceState(null, '', hash);
    try { localStorage.setItem(ROUTE_KEY, hash); } catch { /* 同上 */ }
  }, [route]);

  function go(next: ShellRoute) {
    const hash = formatRoute(next);
    if (window.location.hash !== hash) window.location.hash = hash;
    setRoute(next);
  }

  const { view, algorithm } = route;
  return <>
    <nav className="algorithm-switch" aria-label="页面与算法">
      <Segmented<View>
        aria-label="页面选择"
        value={view}
        onChange={next => go({ view: next, algorithm })}
        options={[
          { label: '体检 v2（热力与报告）', value: 'checkup' },
          { label: '旧版成圈分析', value: 'legacy' },
        ]}
      />
      <Segmented<Algorithm>
        aria-label="算法选择"
        value={algorithm}
        onChange={next => go({ view, algorithm: next })}
        options={[
          { label: <AlgorithmLabel view={view} algorithm="baidu" text="百度边界搜索（E8.2）" />, value: 'baidu' },
          { label: <AlgorithmLabel view={view} algorithm="hybrid" text="OSM＋百度" />, value: 'hybrid' },
        ]}
      />
    </nav>
    {view === 'checkup' ? <CheckupApp key={algorithm} engine={ENGINES[algorithm]} />
      : algorithm === 'baidu' ? <BaiduApp /> : <HybridApp />}
  </>;
}
