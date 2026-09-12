import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { buildBaiduMapUrl } from './baiduMapConfig';

/** 浏览器全局的最小桩：仅覆盖加载器用到的 window/document 能力。 */
type StubScript = { src: string; onerror: ((error: unknown) => void) | null };
type StubWindow = { BMapGL?: object; __baiduMapReady__?: () => void };

let appended: StubScript[];
let stubWindow: StubWindow;
const fakeApi = { marker: 'BMapGL-fixture' };

beforeEach(() => {
  appended = [];
  stubWindow = {};
  vi.resetModules();
  vi.stubGlobal('window', stubWindow);
  vi.stubGlobal('document', {
    createElement: (): StubScript => ({ src: '', onerror: null }),
    head: { appendChild: (script: StubScript) => { appended.push(script); } }
  });
});
afterEach(() => { vi.unstubAllGlobals(); });

async function freshLoader() {
  return await import('./baiduMapLoader');
}

describe('loadBaiduMap — 脚本加载逻辑', () => {
  it('appends the script once and dedupes concurrent loads, resolving via the jsonp callback', async () => {
    const { loadBaiduMap } = await freshLoader();
    const first = loadBaiduMap('test-ak');
    const second = loadBaiduMap('test-ak');
    expect(appended).toHaveLength(1);
    expect(appended[0].src).toBe(buildBaiduMapUrl('test-ak'));
    stubWindow.BMapGL = fakeApi;
    stubWindow.__baiduMapReady__?.();
    const [a, b] = await Promise.all([first, second]);
    expect(a).toBe(fakeApi);
    expect(b).toBe(fakeApi);
    expect(appended).toHaveLength(1);
  });

  it('resolves immediately with the existing global when BMapGL is already on window (e.g. HMR)', async () => {
    stubWindow.BMapGL = fakeApi;
    const { loadBaiduMap } = await freshLoader();
    await expect(loadBaiduMap('test-ak')).resolves.toBe(fakeApi);
    expect(appended).toHaveLength(0);
  });

  it('rejects when the script fails to load', async () => {
    const { loadBaiduMap } = await freshLoader();
    const promise = loadBaiduMap('test-ak');
    appended[0].onerror?.(new Error('network'));
    await expect(promise).rejects.toThrow('百度地图脚本加载失败');
  });

  it('rejects after a timeout when the jsonp callback never fires (e.g. invalid ak)', async () => {
    vi.useFakeTimers();
    try {
      const { loadBaiduMap } = await freshLoader();
      const promise = loadBaiduMap('test-ak');
      expect(appended).toHaveLength(1);
      const assertion = expect(promise).rejects.toThrow('百度地图脚本加载超时');
      await vi.advanceTimersByTimeAsync(10_000);
      await assertion;
    } finally {
      vi.useRealTimers();
    }
  });

  it('resolves normally before the timeout when the callback fires in time', async () => {
    vi.useFakeTimers();
    try {
      const { loadBaiduMap } = await freshLoader();
      const promise = loadBaiduMap('test-ak');
      stubWindow.BMapGL = fakeApi;
      stubWindow.__baiduMapReady__?.();
      await expect(promise).resolves.toBe(fakeApi);
      await vi.advanceTimersByTimeAsync(10_000);
    } finally {
      vi.useRealTimers();
    }
  });

  it('rejects without appending a script when the ak is blank, and later loads still work', async () => {
    const { loadBaiduMap } = await freshLoader();
    await expect(loadBaiduMap('   ')).rejects.toThrow('未配置 VITE_BAIDU_MAP_AK');
    expect(appended).toHaveLength(0);
    const promise = loadBaiduMap('valid-ak');
    expect(appended).toHaveLength(1);
    stubWindow.BMapGL = fakeApi;
    stubWindow.__baiduMapReady__?.();
    await expect(promise).resolves.toBe(fakeApi);
  });
});
