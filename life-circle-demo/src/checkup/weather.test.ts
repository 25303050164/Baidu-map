/**
 * 天气读取：只认结构里真的给了的数。结构不对、缺温度时返回 null —— 界面据此说"暂不可用"，
 * 而不是把缺的数当 0 展示成"0°C、湿度 0%"。
 */
import { describe, expect, it } from 'vitest';
import { readWeather, weatherLabel, weatherUrl } from './weather';

const payload = {
  current_units: { temperature_2m: '°C' },
  current: { time: '2026-09-29T18:00', temperature_2m: 23.9, relative_humidity_2m: 63,
    apparent_temperature: 25.1, weather_code: 1, wind_speed_10m: 7.7 },
};

describe('weather labels', () => {
  it('maps known WMO codes and refuses to invent an unknown one', () => {
    expect(weatherLabel(0)).toBe('晴');
    expect(weatherLabel(61)).toBe('小雨');
    expect(weatherLabel(95)).toBe('雷阵雨');
    expect(weatherLabel(12345)).toBe('天气未知');
    expect(weatherLabel(null)).toBe('天气未知');
  });
});

describe('weather url', () => {
  it('carries the coordinates and the current fields', () => {
    const url = weatherUrl(116.404, 39.915);
    expect(url).toContain('latitude=39.915000');
    expect(url).toContain('longitude=116.404000');
    expect(url).toContain('timezone=auto');
    expect(url).toContain('weather_code');
  });
});

describe('reading the response', () => {
  it('reads every field it can prove', () => {
    expect(readWeather(payload)).toEqual({
      temperatureC: 23.9, feelsLikeC: 25.1, humidityPct: 63, windKmh: 7.7,
      code: 1, label: '大致晴朗', observedAt: '2026-09-29T18:00',
    });
  });

  it('returns null instead of zeros when the shape is wrong or temperature is missing', () => {
    expect(readWeather(null)).toBeNull();
    expect(readWeather({})).toBeNull();
    expect(readWeather({ current: {} })).toBeNull();
    expect(readWeather({ current: { temperature_2m: '23.9' } })).toBeNull();
  });

  it('keeps missing optional fields as null, not as a fabricated reading', () => {
    const reading = readWeather({ current: { temperature_2m: 20, weather_code: 400 } });
    expect(reading).toMatchObject({ temperatureC: 20, feelsLikeC: null, humidityPct: null,
      windKmh: null, label: '天气未知' });
  });
});
