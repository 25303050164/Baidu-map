/**
 * 体检 v2 工作台入口。页面固定为体检 v2，仅切换成圈算法。
 */
import { useEffect, useState, useSyncExternalStore } from 'react';
import { Badge, Segmented } from 'antd';
import CheckupApp from './checkup/CheckupApp';
import { checkupSession } from './checkup/sessions';
import { isCheckupBusy } from './checkup/types';

export type Algorithm = 'baidu' | 'hybrid';
export const ENGINES: Record<Algorithm, string> = { baidu: 'baidu_e82', hybrid: 'osm_hybrid' };
const SLUGS: Record<Algorithm, string> = { baidu: 'e82', hybrid: 'hybrid' };
const ROUTE_KEY = 'life-circle:shell:v1';

function parseAlgorithm(hash: string): Algorithm | null {
  const match = /^#\/(?:checkup|legacy)\/(e82|hybrid)$/.exec(hash);
  if (!match) return null;
  return match[1] === 'hybrid' ? 'hybrid' : 'baidu';
}

function initialAlgorithm(): Algorithm {
  const fromHash = parseAlgorithm(window.location.hash);
  if (fromHash) return fromHash;
  try {
    const stored = parseAlgorithm(localStorage.getItem(ROUTE_KEY) ?? '');
    if (stored) return stored;
  } catch { /* 存储不可用时按配置的默认值 */ }
  return import.meta.env.VITE_ANALYSIS_MODE === 'hybrid' ? 'hybrid' : 'baidu';
}

const LIVE_TEXT: Record<string, string> = {
  submitting: '提交中', restoring: '核对中', queued: '排队中', running: '运行中', fetching: '取结果',
  cancelling: '取消中',
};
function AlgorithmLabel({ algorithm, text }: { algorithm: Algorithm; text: string }) {
  const session = checkupSession(ENGINES[algorithm]);
  const phase = useSyncExternalStore(session.subscribe, () => session.getState().phase);
  const activity = isCheckupBusy({ phase }) ? LIVE_TEXT[phase] ?? null : null;
  return <span data-testid={`algorithm-${SLUGS[algorithm]}`} data-activity={activity ?? ''}>
    {text}{activity && <Badge status="processing" text={activity} className="algorithm-activity" />}
  </span>;
}

export default function AlgorithmApp() {
  const [algorithm, setAlgorithm] = useState<Algorithm>(initialAlgorithm);

  useEffect(() => {
    const onHash = () => {
      const next = parseAlgorithm(window.location.hash);
      if (next) setAlgorithm(next);
    };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  useEffect(() => {
    const hash = `#/checkup/${SLUGS[algorithm]}`;
    // 旧版链接也会被规范到体检 v2，算法选择仍予以保留。
    if (window.location.hash !== hash) window.history.replaceState(null, '', hash);
    try { localStorage.setItem(ROUTE_KEY, hash); } catch { /* 存储不可用时继续运行 */ }
  }, [algorithm]);

  function selectAlgorithm(next: Algorithm) {
    const hash = `#/checkup/${SLUGS[next]}`;
    if (window.location.hash !== hash) window.location.hash = hash;
    setAlgorithm(next);
  }

  const algorithmSwitch = <div className="algorithm-switch">
    <Segmented<Algorithm>
      aria-label="算法选择"
      block
      value={algorithm}
      onChange={selectAlgorithm}
      options={[
        { label: <AlgorithmLabel algorithm="baidu" text="百度边界搜索" />, value: 'baidu' },
        { label: <AlgorithmLabel algorithm="hybrid" text="百度 + OSM" />, value: 'hybrid' },
      ]}
    />
  </div>;

  return <div className="wb">
    <header className="wb-top">
      <div className="wb-brand">
        <span className="wb-logo" aria-hidden="true">
          <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
            <circle cx="9" cy="9" r="7.25" stroke="#fff" strokeOpacity=".55" strokeWidth="1.5" />
            <circle cx="9" cy="9" r="4" stroke="#fff" strokeWidth="1.5" />
            <circle cx="9" cy="9" r="1.6" fill="#fff" />
          </svg>
        </span>
        <h1>15 分钟生活圈体检</h1>
      </div>
    </header>
    <CheckupApp key={algorithm} engine={ENGINES[algorithm]} algorithmSwitch={algorithmSwitch} />
  </div>;
}
