/** 百度地图脚本加载器：单例 Promise，避免重复插入 <script>；失败后回退示意地图。 */

import { buildBaiduMapUrl } from './baiduMapConfig';
import type { BaiduMapApi } from './baiduMapTypes';

let pending: Promise<BaiduMapApi> | null = null;

/** 脚本就绪超时：AK 无效等原因会导致 JSONP 回调永不触发（脚本本身加载成功），超时后降级。 */
const LOAD_TIMEOUT_MS = 10_000;

/** 获取已加载的 BMapGL 全局对象；未加载返回 null。 */
export function getBaiduMapApi(): BaiduMapApi | null {
  return window.BMapGL ?? null;
}

/**
 * 加载百度地图 JavaScript API（幂等：并发/重复调用共享同一个 Promise）。
 * 脚本通过 JSONP 回调通知就绪，随后 resolve window.BMapGL。
 */
export function loadBaiduMap(ak: string): Promise<BaiduMapApi> {
  if (pending) return pending;
  const existing = getBaiduMapApi();
  if (existing) return Promise.resolve(existing);
  const url = buildBaiduMapUrl(ak);
  if (!url) return Promise.reject(new Error('未配置 VITE_BAIDU_MAP_AK，无法加载百度地图。'));
  pending = new Promise<BaiduMapApi>((resolve, reject) => {
    const timeout = setTimeout(() => { reject(new Error('百度地图脚本加载超时，已回退到本地示意地图。')); }, LOAD_TIMEOUT_MS);
    window.__baiduMapReady__ = () => {
      clearTimeout(timeout);
      const loaded = getBaiduMapApi();
      if (loaded) resolve(loaded);
      else reject(new Error('百度地图脚本已加载，但未找到 BMapGL 全局对象。'));
    };
    const script = document.createElement('script');
    script.src = url;
    script.onerror = () => { clearTimeout(timeout); reject(new Error('百度地图脚本加载失败，已回退到本地示意地图。')); };
    document.head.appendChild(script);
  });
  return pending;
}
