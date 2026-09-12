/** 百度地图初始化 hook：读取 AK → 加载脚本 → 暴露地图模式与 API 实例。 */

import { useEffect, useMemo, useState } from 'react';
import { getBaiduMapAk, resolveMapMode, type MapMode, type MapScriptState } from './baiduMapConfig';
import { loadBaiduMap } from './baiduMapLoader';
import type { BaiduMapApi } from './baiduMapTypes';

export type BaiduMapStatus = { mode: MapMode; api: BaiduMapApi | null };

/**
 * AK 未配置：mode 恒为 fallback（调用方渲染示意地图）。
 * AK 已配置：loading → real（成功）/ fallback（脚本加载失败，降级）。
 */
export function useBaiduMap(): BaiduMapStatus {
  const ak = useMemo(() => getBaiduMapAk(), []);
  const [scriptState, setScriptState] = useState<MapScriptState>(() => (ak ? 'loading' : 'idle'));
  const [api, setApi] = useState<BaiduMapApi | null>(null);
  useEffect(() => {
    if (!ak) return;
    let cancelled = false;
    loadBaiduMap(ak)
      .then(loaded => { if (!cancelled) { setApi(loaded); setScriptState('ready'); } })
      .catch(() => { if (!cancelled) setScriptState('failed'); });
    return () => { cancelled = true; };
  }, [ak]);
  return { mode: resolveMapMode(ak, scriptState), api };
}
