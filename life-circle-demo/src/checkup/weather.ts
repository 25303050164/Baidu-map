/**
 * 天气卡片的数据读取。
 *
 * 数据源是 Open-Meteo 的当前天气（无需密钥）：坐标用体检中心，界面只展示温度、体感、
 * 湿度、风速与一句天气描述。天气与体检结论无关，不参与任何判断；取不到就说取不到。
 */
const WEATHER_ENDPOINT = 'https://api.open-meteo.com/v1/forecast';
const CURRENT_FIELDS = 'temperature_2m,relative_humidity_2m,apparent_temperature,'
  + 'weather_code,wind_speed_10m';

/** WMO 天气码 → 中文短语。缺的码按码数说"天气未知"，不猜。 */
const WMO_LABELS: Record<number, string> = {
  0: '晴', 1: '大致晴朗', 2: '局部多云', 3: '阴',
  45: '雾', 48: '雾凇',
  51: '小毛毛雨', 53: '毛毛雨', 55: '浓毛毛雨',
  56: '冻毛毛雨', 57: '浓冻毛毛雨',
  61: '小雨', 63: '中雨', 65: '大雨',
  66: '冻雨', 67: '强冻雨',
  71: '小雪', 73: '中雪', 75: '大雪', 77: '米雪',
  80: '小阵雨', 81: '阵雨', 82: '强阵雨',
  85: '小阵雪', 86: '大阵雪',
  95: '雷阵雨', 96: '雷阵雨伴冰雹', 99: '强雷阵雨伴冰雹',
};

export function weatherLabel(code: number | null): string {
  if (code === null) return '天气未知';
  return WMO_LABELS[code] ?? '天气未知';
}

export function weatherUrl(lng: number, lat: number): string {
  return `${WEATHER_ENDPOINT}?latitude=${lat.toFixed(6)}&longitude=${lng.toFixed(6)}`
    + `&current=${CURRENT_FIELDS}&timezone=auto`;
}

export type WeatherReading = {
  temperatureC: number;
  feelsLikeC: number | null;
  humidityPct: number | null;
  windKmh: number | null;
  code: number | null;
  label: string;
  /** 观测时间（数据源本地时区），缺失为 null。 */
  observedAt: string | null;
};

const finite = (value: unknown): number | null =>
  typeof value === 'number' && Number.isFinite(value) ? value : null;

/** 解析响应；缺温度或结构不对时返回 null，由调用方按"取不到"处理。 */
export function readWeather(payload: unknown): WeatherReading | null {
  if (payload === null || typeof payload !== 'object') return null;
  const current = (payload as { current?: unknown }).current;
  if (current === null || typeof current !== 'object') return null;
  const record = current as Record<string, unknown>;
  const temperature = finite(record.temperature_2m);
  if (temperature === null) return null;
  const code = finite(record.weather_code);
  return {
    temperatureC: temperature,
    feelsLikeC: finite(record.apparent_temperature),
    humidityPct: finite(record.relative_humidity_2m),
    windKmh: finite(record.wind_speed_10m),
    code,
    label: weatherLabel(code),
    observedAt: typeof record.time === 'string' && record.time.length > 0 ? record.time : null,
  };
}

/** 取某个坐标的当前天气；失败抛出，由界面统一显示"暂不可用"。 */
export async function fetchWeather(lng: number, lat: number,
  signal?: AbortSignal): Promise<WeatherReading> {
  const response = await fetch(weatherUrl(lng, lat), { signal });
  if (!response.ok) throw new Error(`天气服务返回 ${response.status}`);
  const reading = readWeather(await response.json());
  if (!reading) throw new Error('天气服务返回了无法解析的内容');
  return reading;
}
