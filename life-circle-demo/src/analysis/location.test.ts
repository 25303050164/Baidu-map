import { afterEach, describe, expect, it, vi } from 'vitest';
import type {
  BaiduMapApi, BMapGeolocationResult, BMapLocalResultPoi, BMapLocalSearchOptions, BMapLocalSearchResult,
} from '../map/baiduMapTypes';
import { approxDistanceM, formatAddress, locate, locationNotice, searchPlaces, strictTitleMatch } from './location';

afterEach(() => vi.useRealTimers());

const Point = class { constructor(public lng: number, public lat: number) {} };

function createGeolocationApi() {
  let callback: ((result: BMapGeolocationResult | null) => void) | null = null;
  let status = 0;
  class Geolocation {
    getCurrentPosition(cb: (result: BMapGeolocationResult | null) => void) { callback = cb; }
    getStatus() { return status; }
  }
  return {
    api: { Point, Geolocation } as unknown as BaiduMapApi,
    respond(result: BMapGeolocationResult | null, nextStatus = 0) {
      status = nextStatus;
      callback?.(result);
    },
  };
}

type FakePoi = Partial<BMapLocalResultPoi> & { point?: { lng: number; lat: number } };

type SearchCall = { kind: 'nearby' | 'bounds'; keyword: string; radius?: number };

function resultOf(pois: FakePoi[]): BMapLocalSearchResult {
  return {
    getPoi: index => pois[index] as BMapLocalResultPoi,
    getCurrentNumPois: () => pois.length,
    getNumPois: () => pois.length,
  };
}

type SearchApiOptions = {
  bounds?: boolean;
  geocoder?: { result?: { lng: number; lat: number } | null; silent?: boolean };
};

function createSearchApi(nearbyPois: FakePoi[] = [], widePois: FakePoi[] = [], options: SearchApiOptions = {}) {
  let complete: ((results: BMapLocalSearchResult | null) => void) | null = null;
  let current: FakePoi[] = [];
  const calls: SearchCall[] = [];
  const bounds: unknown[] = [];
  class LocalSearch {
    constructor(public location: unknown, public options: BMapLocalSearchOptions) {}
    searchNearby(keyword: string, center: unknown, radius: number) {
      calls.push({ kind: 'nearby', keyword, radius });
      current = nearbyPois;
      complete = this.options.onSearchComplete ?? null;
    }
    searchInBounds(keyword: string, value: unknown) {
      calls.push({ kind: 'bounds', keyword });
      bounds.push(value);
      current = widePois;
      complete = this.options.onSearchComplete ?? null;
    }
    search() {}
    clearResults() {}
  }
  const Bounds = class { constructor(public sw: unknown, public ne: unknown) {} };
  const base: Record<string, unknown> = { Point, LocalSearch };
  if (options.bounds !== false) base.Bounds = Bounds;
  const geocoder = options.geocoder;
  if (geocoder) {
    const fixture = geocoder;
    class Geocoder {
      getPoint(_address: string, callback: (point: { lng: number; lat: number } | null) => void) {
        if (fixture.silent) return;
        setTimeout(() => callback(fixture.result ?? null), 0);
      }
    }
    base.Geocoder = Geocoder;
  }
  return {
    api: base as unknown as BaiduMapApi,
    calls,
    bounds,
    finish: (value: BMapLocalSearchResult | null = resultOf(current)) => complete?.(value),
  };
}

describe('locate', () => {
  it('returns rounded BD09LL coordinates, accuracy and deduplicated address', async () => {
    const { api, respond } = createGeolocationApi();
    const promise = locate(api);
    respond({
      point: { lng: 116.404123456, lat: 39.915123456 }, accuracy: 35.6,
      address: { province: '北京市', city: '北京市', district: '东城区', street: '东华门大街', streetNumber: '20号' },
    });
    await expect(promise).resolves.toEqual({
      center: { lng: 116.404123, lat: 39.915123 }, accuracy: 36, address: '北京市东城区东华门大街20号',
    });
  });

  it('maps permission denial, timeout and unavailable status codes', async () => {
    const denied = createGeolocationApi();
    const deniedPromise = locate(denied.api);
    denied.respond(null, 6);
    await expect(deniedPromise).rejects.toMatchObject({ code: 'permission' });

    const timedOut = createGeolocationApi();
    const timeoutPromise = locate(timedOut.api);
    timedOut.respond(null, 8);
    await expect(timeoutPromise).rejects.toMatchObject({ code: 'timeout' });

    const unavailable = createGeolocationApi();
    const unavailablePromise = locate(unavailable.api);
    unavailable.respond(null, 2);
    await expect(unavailablePromise).rejects.toMatchObject({ code: 'unavailable' });
  });

  it('rejects invalid coordinates and unavailable SDK', async () => {
    const { api, respond } = createGeolocationApi();
    const promise = locate(api);
    respond({ point: { lng: Number.NaN, lat: 39.915 } });
    await expect(promise).rejects.toMatchObject({ code: 'invalid' });

    await expect(locate({ Point } as unknown as BaiduMapApi)).rejects.toMatchObject({ code: 'unsupported' });
  });

  it('rejects when the SDK never calls back or the constructor throws', async () => {
    vi.useFakeTimers();
    const { api } = createGeolocationApi();
    const promise = locate(api);
    const assertion = expect(promise).rejects.toMatchObject({ code: 'timeout' });
    await vi.advanceTimersByTimeAsync(12_000);
    await assertion;
    vi.useRealTimers();

    class Broken { constructor() { throw new Error('boom'); } }
    await expect(locate({ Point, Geolocation: Broken } as unknown as BaiduMapApi))
      .rejects.toMatchObject({ code: 'failed' });
  });
});

describe('searchPlaces', () => {
  it('returns nearby results first and skips invalid points', async () => {
    const { api, calls, finish } = createSearchApi([
      { title: '人民公园', address: '测试路1号', uid: 'poi-1', point: { lng: 116.41, lat: 39.92 } },
      { title: ' ', point: { lng: 116.42, lat: 39.93 } },
      { title: '坏点', point: { lng: Number.NaN, lat: 39.94 } },
    ]);
    const promise = searchPlaces(api, '  公园  ', { lng: 116.4, lat: 39.9 });
    finish();
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    finish();
    await expect(promise).resolves.toEqual({
      nearby: [
        { id: 'poi-1', title: '人民公园', address: '测试路1号', center: { lng: 116.41, lat: 39.92 } },
        { id: 'index-1', title: '未命名地点', address: null, center: { lng: 116.42, lat: 39.93 } },
      ],
      far: [],
    });
    expect(calls).toEqual([
      { kind: 'nearby', keyword: '公园', radius: 5_000 },
      { kind: 'bounds', keyword: '公园' },
    ]);
  });

  it('honors radius and limit options', async () => {
    const { api, calls, finish } = createSearchApi([
      { title: 'A', point: { lng: 116.41, lat: 39.92 } },
      { title: 'B', point: { lng: 116.42, lat: 39.93 } },
    ]);
    const promise = searchPlaces(api, '公园', { lng: 116.4, lat: 39.9 }, { radius: 800, limit: 1 });
    finish();
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    finish();
    await expect(promise).resolves.toMatchObject({ nearby: [{ title: 'A' }], far: [] });
    expect(calls[0]).toEqual({ kind: 'nearby', keyword: '公园', radius: 800 });
  });

  it('adds deduplicated far options beyond the nearby radius, capped by FAR_LIMIT', async () => {
    const near = { title: '近点', uid: 'near', point: { lng: 116.41, lat: 39.92 } };
    const inside = { title: '半径内重复项', uid: 'inside', point: { lng: 116.42, lat: 39.93 } };
    const widePois = [
      near,
      inside,
      ...Array.from({ length: 7 }, (_, index) => ({
        title: `公园远点${index}`, uid: `far-${index}`, point: { lng: 116.4 + index * 0.05, lat: 40.1 },
      })),
    ];
    const { api, calls, finish } = createSearchApi([near, inside], widePois);
    const promise = searchPlaces(api, '公园', { lng: 116.4, lat: 39.9 });
    finish();
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    finish();
    const outcome = await promise;
    expect(outcome.nearby.map(item => item.id)).toEqual(['near', 'inside']);
    expect(outcome.far.map(item => item.id)).toEqual(['far-0', 'far-1', 'far-2', 'far-3', 'far-4']);
  });

  it('returns far options when nearby is empty', async () => {
    const { api, calls, finish } = createSearchApi([], [
      { title: '远郊公园', address: '远郊路9号', uid: 'poi-far', point: { lng: 117.2, lat: 40.3 } },
    ]);
    const promise = searchPlaces(api, '公园', { lng: 116.4, lat: 39.9 });
    finish();
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    finish();
    await expect(promise).resolves.toEqual({
      nearby: [],
      far: [{ id: 'poi-far', title: '远郊公园', address: '远郊路9号', center: { lng: 117.2, lat: 40.3 } }],
    });
  });

  it('requires far options to strictly match the keyword in the title', async () => {
    const { api, calls, finish } = createSearchApi([], [
      { title: '远郊公园', uid: 'exact', point: { lng: 117.2, lat: 40.3 } },
      { title: '远郊花园', uid: 'loose', point: { lng: 117.3, lat: 40.3 } },
    ]);
    const promise = searchPlaces(api, '公园', { lng: 116.4, lat: 39.9 });
    finish();
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    finish();
    await expect(promise).resolves.toMatchObject({ nearby: [], far: [{ id: 'exact' }] });
  });

  it('matches far titles against every whitespace-separated keyword', async () => {
    const { api, calls, finish } = createSearchApi([], [
      { title: '北京南站停车场', uid: 'match', point: { lng: 117.2, lat: 40.3 } },
      { title: '北京西站', uid: 'partial', point: { lng: 117.3, lat: 40.3 } },
    ]);
    const promise = searchPlaces(api, '北京 南站', { lng: 116.4, lat: 39.9 });
    finish();
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    finish();
    await expect(promise).resolves.toMatchObject({ far: [{ id: 'match' }] });
  });

  it('resolves nationwide names through the address geocoder fallback', async () => {
    const { api, calls, finish } = createSearchApi([], [], { geocoder: { result: { lng: 120.43, lat: 27.52 } } });
    const promise = searchPlaces(api, '苍南县', { lng: 116.4, lat: 39.9 });
    finish();
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    finish();
    await expect(promise).resolves.toEqual({
      nearby: [],
      far: [{ id: 'address:苍南县', title: '苍南县', address: null, center: { lng: 120.43, lat: 27.52 }, source: 'address' }],
    });
  });

  it('places a geocoded result inside the nearby radius into the nearby group', async () => {
    const { api, calls, finish } = createSearchApi([], [], { geocoder: { result: { lng: 116.41, lat: 39.92 } } });
    const promise = searchPlaces(api, '某区', { lng: 116.4, lat: 39.9 });
    finish();
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    finish();
    await expect(promise).resolves.toMatchObject({ nearby: [{ source: 'address' }], far: [] });
  });

  it('reports no-results when the geocoder cannot resolve or never answers', async () => {
    const unresolved = createSearchApi([], [], { geocoder: { result: null } });
    const unresolvedPromise = searchPlaces(unresolved.api, '不存在的地名', { lng: 116.4, lat: 39.9 });
    unresolved.finish();
    await vi.waitFor(() => expect(unresolved.calls).toHaveLength(2));
    unresolved.finish();
    await expect(unresolvedPromise).rejects.toMatchObject({ code: 'no-results' });

    vi.useFakeTimers();
    const silent = createSearchApi([], [], { geocoder: { silent: true } });
    const silentPromise = searchPlaces(silent.api, '苍南县', { lng: 116.4, lat: 39.9 });
    silent.finish();
    await vi.advanceTimersByTimeAsync(0);
    expect(silent.calls).toHaveLength(2);
    silent.finish();
    const assertion = expect(silentPromise).rejects.toMatchObject({ code: 'no-results' });
    await vi.advanceTimersByTimeAsync(12_000);
    await assertion;
    vi.useRealTimers();
  });

  it('keeps nearby results when the wide search times out', async () => {
    vi.useFakeTimers();
    const partial = createSearchApi([{ title: 'A', uid: 'a', point: { lng: 1, lat: 1 } }]);
    const promise = searchPlaces(partial.api, '公园', { lng: 0, lat: 0 });
    partial.finish();
    await vi.advanceTimersByTimeAsync(0);
    expect(partial.calls).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(12_000);
    await expect(promise).resolves.toEqual({
      nearby: [{ id: 'a', title: 'A', address: null, center: { lng: 1, lat: 1 } }],
      far: [],
    });
    vi.useRealTimers();
  });

  it('rejects empty keyword, missing SDK, empty groups, null results and timeouts', async () => {
    await expect(searchPlaces({ Point } as unknown as BaiduMapApi, '  ', { lng: 0, lat: 0 }))
      .rejects.toMatchObject({ code: 'invalid' });
    await expect(searchPlaces({ Point } as unknown as BaiduMapApi, '公园', { lng: 0, lat: 0 }))
      .rejects.toMatchObject({ code: 'unsupported' });

    const empty = createSearchApi([]);
    const emptyPromise = searchPlaces(empty.api, '公园', { lng: 0, lat: 0 });
    empty.finish();
    await vi.waitFor(() => expect(empty.calls).toHaveLength(2));
    empty.finish();
    await expect(emptyPromise).rejects.toMatchObject({ code: 'no-results' });

    const nulled = createSearchApi([{ title: 'A', point: { lng: 1, lat: 1 } }], [], { bounds: false });
    const nullPromise = searchPlaces(nulled.api, '公园', { lng: 0, lat: 0 });
    nulled.finish(null);
    await expect(nullPromise).rejects.toMatchObject({ code: 'no-results' });

    vi.useFakeTimers();
    const { api } = createSearchApi();
    const timeoutPromise = searchPlaces(api, '公园', { lng: 0, lat: 0 });
    const assertion = expect(timeoutPromise).rejects.toMatchObject({ code: 'timeout' });
    await vi.advanceTimersByTimeAsync(12_000);
    await assertion;
    vi.useRealTimers();
  });

  it('approxDistanceM separates nearby and far points', () => {
    expect(approxDistanceM({ lng: 116.4, lat: 39.9 }, { lng: 116.41, lat: 39.9 })).toBeLessThan(1_000);
    expect(approxDistanceM({ lng: 116.4, lat: 39.9 }, { lng: 117.2, lat: 40.3 })).toBeGreaterThan(50_000);
  });
});

describe('location helpers', () => {
  it('formats partial addresses and null when nothing is usable', () => {
    expect(formatAddress({ city: '北京市', district: '东城区' })).toBe('北京市东城区');
    expect(formatAddress({ province: '北京市', city: '北京市' })).toBe('北京市');
    expect(formatAddress({})).toBeNull();
    expect(formatAddress(undefined)).toBeNull();
  });

  it('only enables controls in real map mode', () => {
    expect(locationNotice('real')).toBeNull();
    expect(locationNotice('loading')).toContain('正在加载');
    expect(locationNotice('fallback')).toContain('浏览器地图密钥');
  });

  it('strictly matches titles ignoring case and outer whitespace', () => {
    expect(strictTitleMatch(' City Mall ', ' city ')).toBe(true);
    expect(strictTitleMatch('城市广场', '城市 广场')).toBe(true);
    expect(strictTitleMatch('城市广场', '城市 公园')).toBe(false);
    expect(strictTitleMatch('任意标题', '   ')).toBe(false);
  });
});
