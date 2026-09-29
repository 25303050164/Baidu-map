/**
 * 能力表 → 工作台要显示的东西。
 *
 * 与 `report.ts` 同一条规矩：**只翻译，不判断**。哪些引擎可选、哪几档预算、余额怎么称呼，
 * 后端已经说清了，这里只把嵌套的字段读成平面的一行行文字。读不到就说读不到，绝不替后端
 * 编一个默认值 —— 界面自己凑一档预算发过去，正好撞上"不支持的预算一律 422"。
 *
 * 特别地：**不因为路网未配置就把引擎藏起来或禁用**。要不要拒绝由后端判（§4.1 不支持即
 * 422，且"是否支持"随部署而变，不是浏览器能算的）。界面能做的是把风险说在明处 ——
 * 让用户自己决定，而不是替他悄悄降级。
 */
import type { Capabilities, EngineOption } from './validate';
import { waterReviewRefs, type WaterReviewRef } from './water';

type RecordValue = Record<string, unknown>;
const object = (value: unknown): value is RecordValue =>
  value !== null && typeof value === 'object' && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === 'string' && value.length > 0;
const number = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value);

export type QuotaSummary = {
  /** 后端给的说法，逐字显示。缺了就说明缺了，不另起一个名字。 */
  label: string | null;
  lines: string[];
};

export type EngineView = EngineOption & {
  /** 选了它之前该知道的事：需要路网而路网没配好，或者引擎自己写的备注。 */
  caveat: string | null;
};

export type CapabilityView = {
  engines: EngineView[];
  /** 首选引擎与首选预算：取后端给的默认值，不按档位大小猜。 */
  defaultEngine: string | null;
  defaultBudget: number | null;
  quota: QuotaSummary;
  /** 当前部署采用的水系复核（用来认出早于复核的旧版本）；旧后端不给时为空。 */
  waterReviews: WaterReviewRef[];
};

function nested(source: RecordValue, ...path: string[]): unknown {
  let current: unknown = source;
  for (const key of path) {
    if (!object(current)) return undefined;
    current = current[key];
  }
  return current;
}

/** 余额只报后端报出来的那几项：这里多写一个数，就等于替账本说话。 */
export function quotaSummary(value: Capabilities): QuotaSummary {
  const quota = value.quota;
  const label = text(quota.label) ? quota.label : null;
  const lines: string[] = [];
  const tier = nested(quota, 'tier');
  const day = nested(quota, 'day');
  if (text(tier)) lines.push(`本轮服务档位：${tier}`);
  if (text(day)) lines.push(`记账日：${day}`);
  const remaining = nested(quota, 'services', 'place', 'remainingToday');
  const daily = nested(quota, 'services', 'place', 'dailyBudget');
  if (number(remaining) && number(daily)) {
    lines.push(`设施检索今日剩余：${remaining} / ${daily}`);
  } else if (daily === null && number(nested(quota, 'services', 'place', 'qps'))) {
    // 没有日额度只有速率：说明"今天花到多少"这件事不适用于这个服务，不能写成 0。
    lines.push('设施检索不设每日额度，仅按速率限制');
  }
  return { label, lines };
}

function engineView(engine: EngineOption, graphConfigured: boolean | null): EngineView {
  const notes = engine.notes.filter(text);
  const caveat = engine.requiresOsmGraph && graphConfigured === false
    ? '该引擎需要本地 OSM 路网，本次部署未检测到路网缓存；提交后可能被后端拒绝。'
    : notes.length > 0 ? notes.join('；') : null;
  return { ...engine, notes, caveat };
}

export function capabilityView(value: Capabilities): CapabilityView {
  const graph = nested(value.coverage, 'graphConfigured');
  const graphConfigured = typeof graph === 'boolean' ? graph : null;
  const engines = value.engines.map(engine => engineView(engine, graphConfigured));
  const first = engines[0] ?? null;
  return {
    engines,
    defaultEngine: first?.engineId ?? null,
    // 默认预算由后端指定，且必须在这一档里：校验层已经查过，这里不再兜底。
    defaultBudget: first?.defaultBudget ?? null,
    quota: quotaSummary(value),
    waterReviews: waterReviewRefs(value.waterReviews),
  };
}

/** 换引擎就要换预算：档位是引擎自己的，沿用上一个引擎的档会撞 422。 */
export function budgetFor(view: CapabilityView, engineId: string): number | null {
  const engine = view.engines.find(item => item.engineId === engineId);
  return engine?.defaultBudget ?? null;
}
