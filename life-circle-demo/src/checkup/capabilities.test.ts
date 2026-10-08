/**
 * 能力表测两件事：**读不出来的时候有没有说实话**，以及**不因为路网没配好就把引擎藏起来**。
 *
 * 后者是这个文件里最要紧的一条：要不要拒绝某个引擎是后端的判断（§4.1 不支持即 422），
 * 浏览器替它决定一次，就等于把"这个部署支持什么"写死在了一份前端构建里。
 */
import { describe, expect, it } from 'vitest';
import { budgetFor, capabilityView, quotaSummary } from './capabilities';
import { validCapabilities } from './validate';
import { capabilities, engine } from './fixtures';

describe('capabilities document', () => {
  it('accepts the fixture', () => {
    expect(validCapabilities(capabilities())).toBe(true);
  });

  it('refuses an engine whose default budget is not one of its tiers', () => {
    // 默认预算不在档位里，说明后端和这份能力表至少有一处是错的；两边都别信。
    expect(validCapabilities(capabilities({
      engines: [engine({ defaultBudget: 300 })] }))).toBe(false);
    expect(validCapabilities(capabilities({ engines: [] }))).toBe(false);
    expect(validCapabilities(capabilities({ engines: [engine({ budgets: [0, 400] })] }))).toBe(false);
    expect(validCapabilities(capabilities({
      engines: [engine({ engineId: '' })] }))).toBe(false);
  });
});

describe('engine list', () => {
  it('keeps the engines in the order the backend lists them', () => {
    const view = capabilityView(capabilities());
    expect(view.engines.map(item => item.engineId)).toEqual(['baidu_e82', 'osm_hybrid']);
    expect(view.defaultEngine).toBe('baidu_e82');
    expect(view.defaultBudget).toBe(400);
  });

  it('never hides an engine that needs a road network it cannot see', () => {
    const view = capabilityView(capabilities({ coverage: { graphConfigured: false } }));
    // 两个引擎都还在：选不选得住是后端说了算。
    expect(view.engines).toHaveLength(2);
    expect(view.engines[0].caveat).toBeNull();
    expect(view.engines[1].caveat).toContain('未检测到路网缓存');
  });

  it('says nothing about the road network when the backend did not say', () => {
    // `graphConfigured` 缺失是"不知道"，不是"没有"：拿不知道当没有，会凭空警告一次。
    // 引擎自己的备注照旧带出来 —— 那是它对自己的说明，不依赖路网配置。
    const view = capabilityView(capabilities({ coverage: {},
      engines: [engine({ requiresOsmGraph: true })] }));
    expect(view.engines[0].caveat).toBeNull();
    const noted = capabilityView(capabilities({ coverage: {} }));
    expect(noted.engines[1].caveat).not.toContain('未检测到路网缓存');
  });

  it('carries the engine notes through so the choice is explained', () => {
    const view = capabilityView(capabilities({ coverage: {} }));
    expect(view.engines[1].notes).toEqual(['以本地 OSM 路网计算步行距离']);
    expect(view.engines[1].caveat).toBe('以本地 OSM 路网计算步行距离');
    expect(view.engines[0].notes).toEqual([]);
  });

  it('raises only a blocker or a stand-in next to the start button', () => {
    // 普通备注收进说明；路网缺失与合成替身必须在提交前就看得见。
    expect(capabilityView(capabilities({ coverage: {} })).engines.map(item => item.alert))
      .toEqual([null, null]);
    const missing = capabilityView(capabilities({ coverage: { graphConfigured: false } }));
    expect(missing.engines[1].alert).toContain('未检测到路网缓存');
    const synthetic = capabilityView(capabilities({ engines: [engine({
      label: '百度边界搜索（E8.2）· 合成替身', notes: ['边界是半径约 1080 米的正圆，不是百度实测'] })] }));
    expect(synthetic.engines[0].alert).toBe('边界是半径约 1080 米的正圆，不是百度实测');
  });

  it('switches the budget tier with the engine, because the tiers differ', () => {
    const view = capabilityView(capabilities());
    expect(budgetFor(view, 'osm_hybrid')).toBe(400);
    expect(budgetFor(view, 'unknown_engine')).toBeNull();
    expect(view.engines[1].budgets).toEqual([200, 400]);
  });
});

describe('quota line', () => {
  it('shows the backend wording verbatim, and invents none when it is missing', () => {
    const view = quotaSummary(capabilities());
    expect(view.label).toBe('本应用请求限制（不含浏览器 SDK、其他应用及旧接口流量）');
    // 余额的说法只能是后端给的：界面自己起个名字，读者就会以为它是账号总余额。
    expect(quotaSummary(capabilities({ quota: {} })).label).toBeNull();
  });

  it('reports the remaining place budget as the numbers it was given', () => {
    expect(quotaSummary(capabilities()).lines).toContain('设施检索今日剩余：1450 / 1600');
  });

  it('says "no daily budget" instead of reporting zero left', () => {
    // 日额度为 null 是"不适用"，写成 0 就是"今天已经用光了" —— 两种说法做的事完全不同。
    const summary = quotaSummary(capabilities({ quota: { label: '本应用请求限制', day: '2026-10-04',
      services: { place: { qps: 8, dailyBudget: null, remainingToday: null } } } }));
    expect(summary.lines).toContain('设施检索不设本地每日额度；单次体检最多 60 次，仍受速率限制');
    expect(summary.lines.some(line => line.includes('0 /'))).toBe(false);
    expect(summary.lines.some(line => line.startsWith('记账日'))).toBe(false);
  });
});
