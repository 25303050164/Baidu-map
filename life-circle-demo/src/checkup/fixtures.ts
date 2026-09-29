/**
 * 测试用夹具：一份内部自洽的 v2 修订、任务视图与图层。
 *
 * 都是**合成**数据（中心在北京，设施是编的），只用来验证校验与流程；它们不代表任何
 * 真实社区，也不参与算法判据。每个字段都能被 `validate.ts` 接受，任何一处改动都要
 * 让它继续自洽 —— 夹具自相矛盾时，测试会去责怪被测代码。
 *
 * 所以这里刻意写得啰嗦：字段名、枚举、可空性全部照 `contract.ts` 写足，不用
 * `as unknown as` 蒙过去。夹具一旦靠断言绕过类型，它就再也证明不了契约本身。
 */
import type { CheckupLayer, CheckupSnapshot, CheckupTaskView, CoverageRow, FacilityRoute,
  ReportEvidence, ServiceZone, WaterDataEvidence } from './contract';
import type { Capabilities, EngineOption } from './validate';

export const CENTER = { lng: 116.404, lat: 39.915 };

/** 评估域面积；三类面积按 40% / 20% / 40% 拆，和下面的百分比一致。 */
export const AREA = 600_000;
export const CATEGORIES = ['shopping', 'medical', 'education'] as const;

export function polygon(offset = 0): Record<string, unknown> {
  const [x, y] = [116.39 + offset, 39.89];
  return { type: 'Polygon', coordinates: [[[x, y], [x + 0.02, y], [x + 0.02, y + 0.02],
    [x, y + 0.02], [x, y]]] };
}

/** 夹具任务的服务端时钟：创建后 2 秒开始，此刻是开始后 4.5 秒。 */
export const CREATED_AT = 1_700_000_000;
export const STARTED_AT = CREATED_AT + 2;
export const SERVER_TIME = STARTED_AT + 4.5;

export function task(overrides: Partial<CheckupTaskView> = {}): CheckupTaskView {
  return {
    taskId: 'task-1', clientRequestId: 'request-1', engine: 'baidu_e82', status: 'running',
    businessStatus: null, stage: 'accessibility', revision: 3, budget: 200, requests: 12,
    networkRequests: 12, elapsedSeconds: 4.5, createdAt: CREATED_AT, cancelRequested: false,
    error: null, serverTime: SERVER_TIME, startedAt: STARTED_AT, finishedAt: null,
    stageStartedAt: STARTED_AT + 3, lastActivityAt: SERVER_TIME - 0.5,
    progress: { step: 'category', label: '评估服务覆盖 · 医疗（第 2/3 类）', count: 120, limit: null,
      unit: '格', since: STARTED_AT + 4 },
    ...overrides,
  };
}

export function zone(overrides: Partial<ServiceZone> = {}): ServiceZone {
  return {
    id: 'zone-0', index: 0, categories: ['medical'], kind: 'single', areaM2: 12_500,
    parts: 1, cellIds: ['0:1:2'], labelVisible: true, suspected: true, evidenceGrade: 'model',
    queryStatus: 'complete', nearestFacility: 'synthetic:pharmacy-1',
    reason: 'beyond_service_distance', suggestion: '核查该片区的药店点位与通道接入。',
    geometry: polygon(), geometrySystem: 'metric', displayGeometry: polygon(), ...overrides,
  };
}

/** 报告里的覆盖行：九个分数栏 + 三类面积 + 证据等级。 */
export function row(overrides: Partial<CoverageRow> = {}): CoverageRow {
  return {
    category: 'shopping', supported: true, coverageLowerPct: 40, coverageUpperPct: 70,
    assessablePct: 80, unknownPct: 20, intervalWidthPct: 30, intervalDegenerate: false,
    unavailableReason: null, coveredM2: AREA * 0.4, gapM2: AREA * 0.2, unknownM2: AREA * 0.4,
    cells: {}, entrances: {}, evidenceGrade: 'model', ...overrides,
  };
}

const OVERALL = {
  available: true, reason: null, missingCategories: [], coverageLowerPct: 40,
  coverageUpperPct: 70, assessablePct: 80, unknownPct: 20, categories: [...CATEGORIES],
  weights: { shopping: 1 / 3, medical: 1 / 3, education: 1 / 3 },
};

export function report(overrides: Partial<ReportEvidence> = {}): ReportEvidence {
  return {
    reportId: 'task-1:5', generatedAt: 1_700_000_010, schemaVersion: 'checkup-v1',
    domain: polygon(), domainAreaM2: AREA, categories: CATEGORIES.map(category => row({ category })),
    overall: OVERALL,
    gaps: { status: 'partial', reason: null, zones: [zone()], byCategoryM2: { medical: 12_500 },
      compositeAreaM2: 0, gapAreaM2: 12_500, minLabelAreaM2: 2500, compositeMinCategories: 2,
      obstacleLayerAvailable: false, notes: [] },
    verification: { available: true, status: 'partial', provider: 'synthetic:checkup-routes',
      checked: 4, failed: 0, unresolved: 0, facilities: [], conflicts: [], queries: {},
      reason: null, notes: [] },
    evidence: { schemaVersion: 'checkup-v1', ruleVersion: 'walk-distance-1000-v1',
      sourceResultHash: 'hash-4', views: {}, grid: {}, excludedAreaM2: 0,
      obstacleLayerAvailable: false, heatmapEstimated: true, catalogCompleteness: 'unverified',
      queryStatus: 'complete', notes: [] },
    limitations: ['目录完整性未经独立核实。'],
    dataSources: null,
    ...overrides,
  };
}

export function snapshot(overrides: Partial<CheckupSnapshot> = {}): CheckupSnapshot {
  return {
    schemaVersion: 'checkup-v1', taskId: 'task-1', revision: 5, generatedAt: 1_700_000_010,
    center: CENTER, coordinateSystem: 'bd09ll', stage: 'reporting', businessStatus: 'partial',
    engine: { engineId: 'baidu_e82', engineVersion: '1.5.0', label: '百度 E8.2' },
    isochrone: { geometry: polygon() },
    rules: { metric: 'walking_route', threshold_m: 1000, inclusive: true, tolerance_m: 100,
      assessment_scope: 'community', category_policy: 'per_category' },
    scope: { projection: 'local-multicross-e82', dataVersion: '2026-09-01',
      coverageSupported: true, assessmentDomainAvailable: true, excludedAreaM2: 0,
      modelSupportAvailable: true, notes: [] },
    trace: { dataVersions: {}, ruleVersions: {}, isochroneHash: 'abc', resultHash: 'hash-5',
      budgets: {}, recomputed: null },
    facilities: null, facilitiesStatus: 'complete',
    accessibility: {
      status: 'partial', domain: polygon(), domainAreaM2: AREA, excludedAreaM2: 0, gridStepM: 50,
      refinedStepM: 25, maxLeafCells: 5000, searchCutoffM: 1100, views: {}, notes: [],
      categories: CATEGORIES.map(category => ({
        category, supported: true, coveredM2: AREA * 0.4, gapM2: AREA * 0.2, unknownM2: AREA * 0.4,
        domainAreaM2: AREA, unavailableReason: null, cells: {}, entrances: {}, coverage: null })),
    },
    accessibilityStatus: 'partial',
    serviceGaps: {
      status: 'partial', zones: [zone()], byCategoryM2: { medical: 12_500 }, compositeAreaM2: 0,
      gapAreaM2: 12_500, compositeMinCategories: 2, minLabelAreaM2: 2500,
      obstacleLayerAvailable: false, notes: [],
    },
    heatmap: { metric: 'walking_route', estimated: true, stepM: 50, domain: polygon(),
      categories: { medical: [{ cell: '0:0:0', lng: 116.4, lat: 39.9, distanceM: 320,
        nearestFacility: 'synthetic:pharmacy-1', status: 'covered' }] }, notes: [] },
    scores: {
      ruleVersion: 'walk-distance-1000-v1', domainAreaM2: AREA, formula: {},
      // 分数栏是报告行的子集：三类面积与证据等级只存在于报告里。
      categories: CATEGORIES.map(category => {
        const { coveredM2, gapM2, unknownM2, cells, entrances, evidenceGrade, ...score } =
          row({ category });
        return score;
      }),
      overall: OVERALL,
    },
    verification: {
      status: 'partial', provider: 'synthetic:checkup-routes', checked: 4, failed: 0,
      unresolved: 0, facilities: [], conflicts: [],
      queries: { routeAttempts: 4, candidates: 6, checked: 4, unverified: 2, stopReason: null },
      reason: null, notes: [],
    },
    report: report(),
    water: null,
    warnings: [],
    ...overrides,
  };
}

/** 要素集合：设施、覆盖、灰区、热力、核验这五层实际返回的就是这个形态。 */
export function collection(features: Array<Record<string, unknown>> = [],
  properties: Record<string, unknown> = {}): Record<string, unknown> {
  return { type: 'FeatureCollection', coordinateSystem: 'bd09ll', features, properties };
}

export function feature(geometry: Record<string, unknown>,
  properties: Record<string, unknown> = {}): Record<string, unknown> {
  return { type: 'Feature', geometry, properties };
}

export function point(lng: number, lat: number): Record<string, unknown> {
  return { type: 'Point', coordinates: [lng, lat] };
}

export function layer(overrides: Partial<CheckupLayer> = {}): CheckupLayer {
  return {
    layerId: 'service_gaps', revision: 5, geometry: polygon(), displayGeometry: polygon(),
    document: null, resultHash: 'hash-5', ...overrides,
  };
}

export function engine(overrides: Partial<EngineOption> = {}): EngineOption {
  return {
    engineId: 'baidu_e82', label: '百度边界搜索（E8.2）', engineVersion: '1.5.0',
    budgets: [200, 400, 800], defaultBudget: 400, requiresOsmGraph: false, notes: [], ...overrides,
  };
}

/** 能力表：两个引擎各自带自己的预算档，余额的说法由后端给（§9 的"本应用预算余额"）。 */
export function capabilities(overrides: Partial<Capabilities> = {}): Capabilities {
  return {
    schemaVersion: 'checkup-v1',
    engines: [engine(), engine({ engineId: 'osm_hybrid', label: 'OSM＋百度',
      engineVersion: '1.5.0', budgets: [200, 400], requiresOsmGraph: true,
      notes: ['以本地 OSM 路网计算步行距离'] })],
    quota: { label: '本应用预算余额（不含浏览器 SDK、其他应用及旧接口流量）',
      tier: 'first-release', day: '2026-09-27',
      services: { direction: { qps: 16, maxInflight: 16, dailyBudget: null, spentToday: null,
        remainingToday: null },
      place: { qps: 8, maxInflight: 8, dailyBudget: 1600, spentToday: 150, remainingToday: 1450 } },
      matrixEnabled: false, claimsAccountBalance: false },
    budgets: { poiRequests: 60, routeRequests: 120, detailRouteRequests: 20 },
    coverage: { metricCrs: 'EPSG:3857', queryPaddingM: 50, graphConfigured: true,
      coverageBoundaryConfigured: true, completeDirectory: false },
    rules: { ruleVersion: 'walk-distance-1000-v1', statusThresholdSeconds: 900,
      assessmentScope: 'isochrone' },
    waterReviews: [],
    ...overrides,
  };
}

export function route(overrides: Partial<FacilityRoute> = {}): FacilityRoute {
  return {
    taskId: 'task-1', revision: 5, facilityId: 'synthetic:pharmacy-1', category: 'pharmacy',
    majorCategory: 'medical', origin: CENTER, destination: { lng: 116.41, lat: 39.92 },
    straightLineM: 700, withinRule: true, routeDistanceM: 805.05, durationS: 670.87,
    observedDurationS: 670.87, poiStatus: 'verified_reachable', poiReason: null,
    evidenceGrade: 'verified', routeOrigin: CENTER, routeDestination: { lng: 116.41, lat: 39.92 },
    originOffsetM: 0, destinationOffsetM: 0, reason: null,
    provider: 'synthetic:checkup-routes', network: true, attempts: 1, budget: {}, notes: [],
    ...overrides,
  };
}

/** 合成的一份水系复核：范围、一段河道、一处补录、一处底图误绘、一处冲突与一座桥。 */
export function waterReview(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    reviewId: 'synthetic-river', version: '2026-09-01.1', label: 'synthetic-river@2026-09-01.1',
    title: '合成河段复核', reviewedAt: '2026-09-01', scope: '合成社区', appliesTo: {},
    sources: ['合成影像'], method: '合成', limitations: ['合成数据'],
    extent: polygon(),
    reaches: [{ osmId: 101, name: '合成河', widthM: 18, osmWidthTag: '10', status: 'verified',
      measurement: {}, baiduOffset: { minM: 23, medianM: 80, maxM: 193 }, note: '合成',
      geometry: polygon(0.001) }],
    crossings: [{ osmId: 202, highway: 'primary', river: '合成河', lengthM: 31.6,
      status: 'verified', note: '合成桥', anchor: { lng: 116.4, lat: 39.9 }, geometry: null }],
    supplements: [{ id: 'pond-a', status: 'verified', areaM2: 1200, sources: [], note: '合成池塘',
      anchor: { lng: 116.401, lat: 39.901 }, geometry: polygon(0.002) }],
    conflicts: [{ id: 'pond-b', status: 'unverified', areaM2: 660, sources: [], note: '来源矛盾',
      anchor: { lng: 116.402, lat: 39.902 }, geometry: polygon(0.003) }],
    basemapMisdrawn: [{ id: 'ghost-1', kind: 'river_displaced', verifiedAs: 'land', areaM2: 3100,
      note: '底图误绘', anchor: { lng: 116.403, lat: 39.903 }, geometry: polygon(0.004) }],
    ...overrides,
  };
}

export function water(overrides: Partial<WaterDataEvidence> = {}): WaterDataEvidence {
  return {
    obstacleLayerAvailable: true, osmDataVersion: 'synthetic-osm', sourcePbfSha256: 'sha-1',
    reviews: [waterReview()], rejectedReviews: [], domainAreaM2: AREA, reviewedAreaM2: AREA * 0.9,
    unreviewedAreaM2: AREA * 0.1, conflictAreaM2: 660,
    statements: ['水系障碍取自合成 OSM。'], ...overrides,
  };
}
