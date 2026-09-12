/** 百度地图配置：AK 读取、脚本 URL 构建、地图模式解析（纯函数，可单测）。 */

/** 脚本就绪回调挂到 window 上的全局函数名（单例加载，固定名即可）。 */
export const BAIDU_MAP_CALLBACK = '__baiduMapReady__';

/** 从环境变量读取 AK；未配置返回空串（地图回退为本地示意地图）。 */
export function getBaiduMapAk(): string {
  return import.meta.env.VITE_BAIDU_MAP_AK?.trim() ?? '';
}

/** 构建 BMapGL（WebGL 版）脚本地址；AK 缺失时返回 null，由调用方降级。 */
export function buildBaiduMapUrl(ak: string, callback: string = BAIDU_MAP_CALLBACK): string | null {
  const key = ak.trim();
  if (!key) return null;
  return `https://api.map.baidu.com/api?v=1.0&type=webgl&ak=${encodeURIComponent(key)}&callback=${encodeURIComponent(callback)}`;
}

export type MapScriptState = 'idle' | 'loading' | 'ready' | 'failed';
export type MapMode = 'fallback' | 'loading' | 'real';

/**
 * 纯函数：解析当前地图模式。
 * - AK 缺失：无论脚本状态如何都立即回退（降级到示意地图）。
 * - 脚本就绪：真实地图；脚本失败：回退。
 * - 其余（脚本加载中）：显示加载占位。
 */
export function resolveMapMode(ak: string, scriptState: MapScriptState): MapMode {
  if (!ak.trim()) return 'fallback';
  if (scriptState === 'ready') return 'real';
  if (scriptState === 'failed') return 'fallback';
  return 'loading';
}
