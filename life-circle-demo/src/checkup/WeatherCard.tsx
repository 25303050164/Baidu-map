/**
 * 天气卡片：体检中心所在位置的当前天气。
 *
 * 位置随选点变化（搜索、定位、输入坐标都算），取数带 600 毫秒防抖与取消 —— 输入坐标时
 * 每敲一个数字都会换一次中心，不发一串没人要的请求。取不到时只说明取不到：天气不参与
 * 体检结论，卡片上也这么写。
 */
import { useEffect, useState } from 'react';
import type { Center } from '../types';
import { fetchWeather, type WeatherReading } from './weather';

type WeatherState =
  | { status: 'idle' | 'loading' | 'unavailable' }
  | { status: 'ready'; reading: WeatherReading };

const DEBOUNCE_MS = 600;

const number = (value: number | null, unit: string, digits = 0) =>
  value === null ? '未知' : `${value.toFixed(digits)}${unit}`;

export function WeatherCard({ center }: { center: Center | null }) {
  const [state, setState] = useState<WeatherState>({ status: 'idle' });
  const lng = center?.lng ?? null;
  const lat = center?.lat ?? null;

  useEffect(() => {
    if (lng === null || lat === null) { setState({ status: 'idle' }); return; }
    const controller = new AbortController();
    setState({ status: 'loading' });
    const timer = window.setTimeout(() => {
      fetchWeather(lng, lat, controller.signal).then(reading => {
        if (!controller.signal.aborted) setState({ status: 'ready', reading });
      }).catch(() => {
        if (!controller.signal.aborted) setState({ status: 'unavailable' });
      });
    }, DEBOUNCE_MS);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [lng, lat]);

  return <section className="wb-sec" data-testid="weather-card" data-status={state.status}>
    <div className="wb-sec-head">
      <h2 className="wb-h">天气</h2>
      <span className="wb-weather-tag" data-tone={state.status === 'ready' ? 'ready' : 'muted'}>
        {state.status === 'ready' ? '实况' : state.status === 'loading' ? '更新中'
          : state.status === 'unavailable' ? '不可用' : '待选点'}</span>
    </div>
    {state.status === 'ready' ? <>
      <p className="wb-weather" data-temperature={state.reading.temperatureC}>
        <b>{state.reading.temperatureC.toFixed(1)}<small>°C</small></b>
        <span>{state.reading.label}</span>
      </p>
      <ul className="wb-weather-facts">
        <li>体感 {number(state.reading.feelsLikeC, '°C', 1)}</li>
        <li>湿度 {number(state.reading.humidityPct, '%')}</li>
        <li>风速 {number(state.reading.windKmh, ' km/h')}</li>
      </ul>
      <p className="wb-hint">Open-Meteo{state.reading.observedAt
        ? ` · ${state.reading.observedAt}` : ''}</p>
    </> : null}
  </section>;
}
