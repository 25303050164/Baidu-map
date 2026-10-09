/** 定位与 POI 检索封装：把 BMapGL 回调式接口转为 Promise，统一错误映射。
 *
 * 坐标说明：Geolocation 与 LocalSearch 返回的均为 BD09LL，与后端分析坐标系一致，
 * 不做任何本地坐标转换。SDK 由参数注入，便于离线单测。
 */

import type { Center } from '../types';
import type {
  BaiduMapApi, BMapAddressComponent, BMapBounds, BMapGeolocation, BMapGeolocationResult,
  BMapLocalSearch, BMapLocalSearchResult, BMapPoint, BMapPositionOptions,
} from '../map/baiduMapTypes';
import type { MapMode } from '../map/baiduMapConfig';

export type LocationErrorCode =
  | 'unsupported' | 'permission' | 'unavailable' | 'timeout' | 'invalid' | 'no-results' | 'failed';

export class LocationError extends Error {
  constructor(public code: LocationErrorCode, message: string) {
    super(message);
    this.name = 'LocationError';
  }
}

export type LocatedPosition = {
  center: Center;
  /** 定位精度半径（米）；SDK 未提供时为 null。 */
  accuracy: number | null;
  address: string | null;
};

export type PlaceResult = {
  id: string;
  title: string;
  address: string | null;
  center: Center;
  /** 仅全国范围地址解析兜底命中的结果会标记为 address。 */
  source?: 'address';
};

/** 检索结果：nearby 为 5 公里内结果（优先展示），far 为少量较远选项。 */
export type PlaceSearchOutcome = { nearby: PlaceResult[]; far: PlaceResult[] };

export type SearchOptions = { radius?: number; limit?: number };

const LOCATE_TIMEOUT_MS = 12_000;
const SEARCH_TIMEOUT_MS = 12_000;
const GEOCODE_TIMEOUT_MS = 12_000;
const STATUS_SUCCESS = 0;
const STATUS_PERMISSION_DENIED = 6;
const STATUS_TIMEOUT = 8;
/** 超过该精度（米）视为粗略定位，需要用户在地图上核对。 */
export const COARSE_ACCURACY_M = 200;
export const SEARCH_RADIUS_M = 5_000;
/** 较远结果的城市范围兜底：中心点 ±0.45°（约 50 公里）。 */
export const SEARCH_BOUNDS_SPAN_DEG = 0.45;
export const SEARCH_LIMIT = 10;
/** 较远选项最多展示条数。 */
export const FAR_LIMIT = 5;

const round6 = (value: number) => Number(value.toFixed(6));

function toCenter(lng: unknown, lat: unknown): Center | null {
  if (typeof lng !== 'number' || typeof lat !== 'number') return null;
  if (!Number.isFinite(lng) || !Number.isFinite(lat)) return null;
  if (lng < -180 || lng > 180 || lat <= -85 || lat >= 85) return null;
  return { lng: round6(lng), lat: round6(lat) };
}

/** 拼接地址分量；直辖市等省与市同名时去掉连续重复段。 */
export function formatAddress(address?: BMapAddressComponent): string | null {
  if (!address || typeof address !== 'object') return null;
  const parts = [address.province, address.city, address.district, address.street, address.streetNumber]
    .filter((part): part is string => typeof part === 'string' && part.trim().length > 0)
    .map(part => part.trim());
  const unique = parts.filter((part, index) => index === 0 || part !== parts[index - 1]);
  return unique.length ? unique.join('') : null;
}

/** 地图模式对应的控件提示；real 返回 null 表示控件可用。 */
export function locationNotice(mode: MapMode): string | null {
  if (mode === 'real') return null;
  if (mode === 'loading') return '正在加载地图服务，稍后可使用定位与地点搜索。';
  return '配置浏览器地图密钥后，可使用获取当前位置与地点搜索；仍可手动输入坐标。';
}

function geolocationError(status: number): LocationError {
  if (status === STATUS_PERMISSION_DENIED) {
    return new LocationError('permission', '定位权限被拒绝，请在浏览器设置中允许定位后重试');
  }
  if (status === STATUS_TIMEOUT) {
    return new LocationError('timeout', '定位超时，请重试或手动输入坐标');
  }
  return new LocationError('unavailable', '当前环境无法获取位置，请搜索地点或手动输入坐标');
}

/** 浏览器定位；成功返回 BD09LL 坐标、精度与地址，失败按状态码给出可操作错误。 */
export function locate(api: BaiduMapApi, options?: BMapPositionOptions): Promise<LocatedPosition> {
  const Geolocation = api.Geolocation;
  if (!Geolocation) {
    return Promise.reject(new LocationError('unsupported', '当前地图脚本不支持浏览器定位，请搜索地点或手动输入坐标'));
  }
  return new Promise((resolve, reject) => {
    let settled = false;
    let instance: BMapGeolocation;
    const finish = (run: () => void) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      run();
    };
    const timer = setTimeout(
      () => finish(() => reject(new LocationError('timeout', '定位超时，请重试或手动输入坐标'))),
      LOCATE_TIMEOUT_MS
    );
    try {
      instance = new Geolocation();
      instance.getCurrentPosition(function (this: BMapGeolocation | undefined, result: BMapGeolocationResult | null) {
        const status = typeof this?.getStatus === 'function' ? this.getStatus() : instance.getStatus();
        if (status !== STATUS_SUCCESS) {
          finish(() => reject(geolocationError(status)));
          return;
        }
        const center = result?.point ? toCenter(result.point.lng, result.point.lat) : null;
        if (!center) {
          finish(() => reject(new LocationError('invalid', '定位返回的坐标无效，请重试或手动输入坐标')));
          return;
        }
        const rawAccuracy = result?.accuracy;
        const accuracy = typeof rawAccuracy === 'number' && Number.isFinite(rawAccuracy) && rawAccuracy >= 0
          ? Math.round(rawAccuracy) : null;
        finish(() => resolve({ center, accuracy, address: formatAddress(result?.address) }));
      }, options ?? { enableHighAccuracy: true, timeout: 10_000 });
    } catch {
      finish(() => reject(new LocationError('failed', '定位失败，请重试或手动输入坐标')));
    }
  });
}

function normalizeResults(results: BMapLocalSearchResult | null, limit: number): PlaceResult[] {
  if (!results || typeof results.getPoi !== 'function') return [];
  const count = typeof results.getCurrentNumPois === 'function'
    ? results.getCurrentNumPois()
    : typeof results.getNumPois === 'function' ? results.getNumPois() : 0;
  const items: PlaceResult[] = [];
  for (let index = 0; index < count && items.length < limit; index += 1) {
    try {
      const poi = results.getPoi(index);
      const center = poi?.point ? toCenter(poi.point.lng, poi.point.lat) : null;
      if (!poi || !center) continue;
      items.push({
        id: typeof poi.uid === 'string' && poi.uid ? poi.uid : `index-${index}`,
        title: typeof poi.title === 'string' && poi.title.trim() ? poi.title.trim() : '未命名地点',
        address: typeof poi.address === 'string' && poi.address.trim() ? poi.address.trim() : null,
        center,
      });
    } catch {
      // 单条结果结构异常时跳过，不影响其余结果。
    }
  }
  return items;
}

/** 创建覆盖中心点所在城市的矩形检索范围；未 clamp 到合法经纬度。 */
function makeBounds(api: BaiduMapApi, center: Center): BMapBounds | null {
  const Bounds = api.Bounds;
  if (!Bounds) return null;
  const span = SEARCH_BOUNDS_SPAN_DEG;
  const sw = new api.Point(Math.max(center.lng - span, -180), Math.max(center.lat - span, -85));
  const ne = new api.Point(Math.min(center.lng + span, 180), Math.min(center.lat + span, 85));
  return new Bounds(sw, ne);
}

/** 单次检索执行：创建实例并触发指定检索动作；空结果解析为 []，超时与调用失败抛出 LocationError。 */
function runSearch(
  api: BaiduMapApi, query: string, limit: number, point: BMapPoint,
  action: (instance: BMapLocalSearch) => void
): Promise<PlaceResult[]> {
  const LocalSearch = api.LocalSearch;
  if (!LocalSearch) {
    return Promise.reject(new LocationError('unsupported', '当前地图脚本不支持地点检索，请手动输入坐标'));
  }
  return new Promise((resolve, reject) => {
    let settled = false;
    let instance: BMapLocalSearch | undefined;
    const finish = (run: () => void) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      run();
    };
    const timer = setTimeout(
      () => finish(() => reject(new LocationError('timeout', '搜索服务无响应，请确认浏览器地图密钥已开通地点检索服务'))),
      SEARCH_TIMEOUT_MS
    );
    try {
      instance = new LocalSearch(point, {
        pageCapacity: limit,
        onSearchComplete: results => finish(() => resolve(normalizeResults(results, limit))),
      });
      action(instance);
    } catch {
      finish(() => reject(new LocationError('failed', '地点搜索失败，请重试')));
    }
  });
}

/** 近似平面距离（米）；仅用于界面分组与排序展示，不作为业务距离结论。 */
export function approxDistanceM(from: Center, to: Center): number {
  const rad = Math.PI / 180;
  const x = (to.lng - from.lng) * Math.cos(((from.lat + to.lat) / 2) * rad);
  const y = to.lat - from.lat;
  return Math.hypot(x, y) * rad * 6_371_000;
}

/** 较远结果的严格关键词匹配：标题需包含全部关键词（空白分词），避免城市范围检索的模糊召回。 */
export function strictTitleMatch(title: string, query: string): boolean {
  const haystack = title.trim().toLowerCase();
  const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  return terms.length > 0 && terms.every(term => haystack.includes(term));
}

/** 从城市范围结果中挑出较远选项：标题严格匹配关键词、与附近结果按 id 去重、排除半径内地点、最多 FAR_LIMIT 条。 */
function selectFar(
  wide: PlaceResult[], nearby: PlaceResult[], center: Center, radius: number, query: string
): PlaceResult[] {
  const seen = new Set(nearby.map(item => item.id));
  const far: PlaceResult[] = [];
  for (const item of wide) {
    if (seen.has(item.id)) continue;
    seen.add(item.id);
    if (!strictTitleMatch(item.title, query)) continue;
    if (approxDistanceM(center, item.center) <= radius) continue;
    far.push(item);
    if (far.length >= FAR_LIMIT) break;
  }
  return far;
}

/** 全国范围地址/行政区解析兜底：仅当附近与城市范围均无结果时使用，如“苍南县”这类跨城名称。 */
function geocodePlace(api: BaiduMapApi, query: string): Promise<PlaceResult | null> {
  const Geocoder = api.Geocoder;
  if (!Geocoder) return Promise.resolve(null);
  return new Promise(resolve => {
    let settled = false;
    const finish = (value: PlaceResult | null) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };
    const timer = setTimeout(() => finish(null), GEOCODE_TIMEOUT_MS);
    try {
      new Geocoder().getPoint(query, point => {
        const center = point ? toCenter(point.lng, point.lat) : null;
        finish(center ? { id: `address:${query}`, title: query, address: null, center, source: 'address' } : null);
      });
    } catch {
      finish(null);
    }
  });
}

/**
 * POI 关键词检索：先搜 center 周边 radius 米（优先结果），再在城市范围矩形内补充严格匹配的较远选项；
 * 两者皆空时用地址解析兜底全国范围的行政区/地址名称。全部失败时报 no-results（超时优先反馈）。
 */
export async function searchPlaces(
  api: BaiduMapApi, keyword: string, center: Center, options: SearchOptions = {}
): Promise<PlaceSearchOutcome> {
  const query = keyword.trim();
  if (!query) throw new LocationError('invalid', '请输入要搜索的地点关键词');
  if (!api.LocalSearch) {
    throw new LocationError('unsupported', '当前地图脚本不支持地点检索，请手动输入坐标');
  }
  const radius = options.radius ?? SEARCH_RADIUS_M;
  const limit = options.limit ?? SEARCH_LIMIT;
  let point: BMapPoint;
  try {
    point = new api.Point(center.lng, center.lat);
  } catch {
    throw new LocationError('failed', '地点搜索失败，请重试');
  }
  let nearby = await runSearch(api, query, limit, point, instance => instance.searchNearby(query, point, radius));
  let far: PlaceResult[] = [];
  let wideError: unknown = null;
  const bounds = makeBounds(api, center);
  if (bounds) {
    try {
      const wide = await runSearch(api, query, limit, point, instance => {
        if (typeof instance.searchInBounds !== 'function') throw new Error('searchInBounds unavailable');
        instance.searchInBounds(query, bounds);
      });
      far = selectFar(wide, nearby, center, radius, query);
    } catch (error) {
      wideError = error;
    }
  }
  if (!nearby.length && !far.length) {
    const resolved = await geocodePlace(api, query);
    if (resolved) {
      if (approxDistanceM(center, resolved.center) <= radius) nearby = [resolved];
      else far = [resolved];
    }
  }
  if (!nearby.length && !far.length) {
    if (wideError instanceof LocationError && wideError.code === 'timeout') throw wideError;
    throw new LocationError('no-results', '未找到相关地点，请尝试其他关键词');
  }
  return { nearby, far };
}
