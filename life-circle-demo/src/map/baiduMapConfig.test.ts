import { afterEach, describe, expect, it, vi } from 'vitest';
import { BAIDU_MAP_CALLBACK, buildBaiduMapUrl, getBaiduMapAk, resolveMapMode } from './baiduMapConfig';

describe('buildBaiduMapUrl — 百度地图脚本地址', () => {
  it('builds the BMapGL(webgl) script url with ak and jsonp callback', () => {
    expect(buildBaiduMapUrl('test-ak')).toBe(`https://api.map.baidu.com/api?v=1.0&type=webgl&ak=test-ak&callback=${BAIDU_MAP_CALLBACK}`);
  });
  it('encodes ak and custom callback names', () => {
    expect(buildBaiduMapUrl('ak with spaces&=', 'cb(1)')).toBe('https://api.map.baidu.com/api?v=1.0&type=webgl&ak=ak%20with%20spaces%26%3D&callback=cb(1)');
  });
  it('returns null for a missing or blank ak', () => {
    expect(buildBaiduMapUrl('')).toBeNull();
    expect(buildBaiduMapUrl('   ')).toBeNull();
  });
});

describe('resolveMapMode — 配置缺失时的降级行为', () => {
  it('falls back to the demo map immediately when no ak is configured, whatever the script state', () => {
    for (const state of ['idle', 'loading', 'ready', 'failed'] as const) {
      expect(resolveMapMode('', state)).toBe('fallback');
      expect(resolveMapMode('   ', state)).toBe('fallback');
    }
  });
  it('shows a loading placeholder while the script loads', () => {
    expect(resolveMapMode('ak', 'loading')).toBe('loading');
    expect(resolveMapMode('ak', 'idle')).toBe('loading');
  });
  it('renders the real map once the script is ready', () => {
    expect(resolveMapMode('ak', 'ready')).toBe('real');
  });
  it('falls back when the script failed to load', () => {
    expect(resolveMapMode('ak', 'failed')).toBe('fallback');
  });
});

describe('getBaiduMapAk — 环境变量读取', () => {
  afterEach(() => { vi.unstubAllEnvs(); });
  it('reads and trims VITE_BAIDU_MAP_AK from the environment', () => {
    vi.stubEnv('VITE_BAIDU_MAP_AK', '  my-ak-123  ');
    expect(getBaiduMapAk()).toBe('my-ak-123');
  });
  it('returns an empty string when the variable is not configured', () => {
    vi.stubEnv('VITE_BAIDU_MAP_AK', '');
    expect(getBaiduMapAk()).toBe('');
  });
});
