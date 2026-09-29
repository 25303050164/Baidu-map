/**
 * 校验层测的是"它拒绝了什么"，不是"它接受什么"。
 *
 * 接受的用例只有一条（夹具本身），其余每一条都构造一种**自相矛盾**，然后要求校验层
 * 当场拒绝。理由：这些校验存在的唯一目的就是不让不自洽的文档上屏，如果只测合法输入
 * 通过，把它们全删掉测试照样绿。
 */
import { describe, expect, it } from 'vitest';
import { areaTolerance, readLayerGeometry, validCapabilities, validFacilityRoute, validLayer, validSnapshot,
  validTaskView } from './validate';
import { AREA, SERVER_TIME, STARTED_AT, capabilities, collection, feature, layer, point, report, route,
  snapshot, task, water, waterReview, zone } from './fixtures';

describe('task view', () => {
  it('accepts the fixture and a queued task with no stage yet', () => {
    expect(validTaskView(task())).toBe(true);
    expect(validTaskView(task({ status: 'queued', stage: null }))).toBe(true);
  });

  it('refuses a failed task that will not say why', () => {
    // "failed" 加上空的 error 只告诉人"出事了"，不告诉人下一步做什么。
    expect(validTaskView(task({ status: 'failed', error: null }))).toBe(false);
    expect(validTaskView(task({ status: 'failed', error: '步行服务未配置' }))).toBe(true);
  });

  it('refuses a stage the pipeline never publishes', () => {
    expect(validTaskView({ ...task(), stage: 'scoring' })).toBe(false);
    expect(validTaskView({ ...task(), revision: -1 })).toBe(false);
  });

  it('refuses a step counted past its own limit, or a unit with nothing counted', () => {
    const progress = task().progress!;
    // 上限是"不会越过的数"：越过了，要么计数错了，要么上限是编的。
    expect(validTaskView(task({ progress: { ...progress, count: 5, limit: 4, unit: '次请求' } }))).toBe(false);
    expect(validTaskView(task({ progress: { ...progress, count: null, unit: '格' } }))).toBe(false);
    expect(validTaskView(task({ progress: { ...progress, count: -1 } }))).toBe(false);
    expect(validTaskView(task({ progress: { ...progress, count: 1.5 } }))).toBe(false);
    expect(validTaskView(task({ progress: { ...progress, label: '' } }))).toBe(false);
    expect(validTaskView(task({ progress: { ...progress, since: 0 } }))).toBe(false);
    expect(validTaskView(task({ progress: { ...progress, count: 4, limit: 4, unit: '次请求' } }))).toBe(true);
    expect(validTaskView(task({ progress: { ...progress, count: null, unit: null } }))).toBe(true);
    expect(validTaskView(task({ progress: null }))).toBe(true);
  });

  it('refuses timings that contradict each other, and accepts a backend that predates them', () => {
    expect(validTaskView(task({ stageStartedAt: STARTED_AT - 10 }))).toBe(false);
    expect(validTaskView(task({ finishedAt: STARTED_AT - 10 }))).toBe(false);
    expect(validTaskView(task({ lastActivityAt: SERVER_TIME + 60 }))).toBe(false);
    expect(validTaskView(task({ startedAt: SERVER_TIME + 60 }))).toBe(false);
    expect(validTaskView(task({ startedAt: 0 }))).toBe(false);
    expect(validTaskView({ ...task(), serverTime: 'now' })).toBe(false);
    // 排队中的任务还没开始：开始、阶段、步骤都是 null。
    expect(validTaskView(task({ status: 'queued', stage: null, startedAt: null, stageStartedAt: null,
      progress: null }))).toBe(true);
    const old: Record<string, unknown> = { ...task() };
    for (const key of ['serverTime', 'startedAt', 'finishedAt', 'stageStartedAt', 'lastActivityAt', 'progress']) {
      delete old[key];
    }
    expect(validTaskView(old)).toBe(true);
  });
});

describe('snapshot', () => {
  it('accepts the fixture', () => {
    expect(validSnapshot(snapshot())).toBe(true);
  });

  it('refuses revision zero', () => {
    // 修订从 1 起：0 只可能是"没读到"被默认成的数。
    expect(validSnapshot({ ...snapshot(), revision: 0 })).toBe(false);
  });

  it('refuses grey zones and scores from a failed accessibility assessment', () => {
    const failed = snapshot({ accessibilityStatus: 'failed',
      accessibility: { ...snapshot().accessibility!, status: 'failed' } });
    expect(validSnapshot(failed)).toBe(false);
    // 同一份文档把灰区与分数拿掉，就是可信的："这次没算出来"是可以说出口的结论。
    expect(validSnapshot({ ...failed, serviceGaps: null, scores: null, report: null })).toBe(true);
  });

  it('refuses a zone that would render without anything to act on', () => {
    const bad = snapshot({ serviceGaps: { ...snapshot().serviceGaps!, zones: [zone({ suggestion: '' })] } });
    expect(validSnapshot(bad)).toBe(false);
  });

  it('refuses a grey zone whose area is not a number', () => {
    const bad = snapshot({ serviceGaps: { ...snapshot().serviceGaps!, zones: [zone({ areaM2: NaN })] } });
    expect(validSnapshot(bad)).toBe(false);
  });
});

describe('report', () => {
  it('refuses a supported row whose C + G + U misses the assessment domain', () => {
    // §11.3：三类面积必须与评估域相符。对不上，说明分母被动过，百分比就不该被渲染。
    const broken = snapshot({ report: report({ categories: [
      { ...report().categories[0], unknownM2: AREA * 0.4 + 1.5 }] }) });
    expect(validSnapshot(broken)).toBe(false);
    // 容差之内（max(1 m², A×10⁻⁶) = 1 m²）是允许的：面积是按几何算的，不是按整数拼的。
    const rounded = snapshot({ report: report({ categories: [
      { ...report().categories[0], unknownM2: AREA * 0.4 + 0.5 }] }) });
    expect(validSnapshot(rounded)).toBe(true);
  });

  it('measures that tolerance the way the plan does', () => {
    expect(areaTolerance(AREA)).toBe(1);
    expect(areaTolerance(50_000_000)).toBe(50);
  });

  it('refuses a report that claims verification the snapshot says never ran', () => {
    const none = { status: 'not_integrated' as const, provider: null, checked: 0, failed: 0,
      unresolved: 0, facilities: [], conflicts: [], spotChecks: [], spotCheckSummary: {},
      localOverrides: [], queries: {}, reason: '未接入核验服务', notes: [] };
    const honest = snapshot({ verification: none,
      report: report({ verification: { ...report().verification, available: false,
        status: 'not_integrated', provider: null, reason: '未接入核验服务' } }) });
    expect(validSnapshot(honest)).toBe(true);
    // "没有核验"被写成"核验可用"，读者会理解成"核验过、没问题" —— 这一条必须拦住。
    const lying = snapshot({ verification: none,
      report: report({ verification: { ...report().verification, available: true,
        status: 'not_integrated' } }) });
    expect(validSnapshot(lying)).toBe(false);
  });

  it('refuses a report with no limitations', () => {
    expect(validSnapshot(snapshot({ report: report({ limitations: [] }) }))).toBe(false);
  });
});

describe('layer', () => {
  it('refuses a layer that is not the one requested', () => {
    expect(validLayer(layer(), 'service_gaps')).toBe(true);
    expect(validLayer(layer(), 'heatmap')).toBe(false);
    expect(validLayer(layer({ revision: 0 }), 'service_gaps')).toBe(false);
    expect(validLayer(layer({ resultHash: '' }), 'service_gaps')).toBe(false);
  });

  it('accepts a layer with no geometry, because "this time nothing" is a real answer', () => {
    expect(validLayer(layer({ geometry: null, displayGeometry: null }), 'service_gaps')).toBe(true);
    // 半张形状则不是：坐标缺失的环会画成一条穿过整个城市的线。
    expect(validLayer(layer({ geometry: { type: 'Polygon', coordinates: [[[1, 2], [3, 4]]] } }),
      'service_gaps')).toBe(false);
  });

  it('accepts the feature collection five of the seven layers actually return', () => {
    // 后端只有等时圈这一层给单个面；设施、覆盖、灰区、热力、核验给的都是要素集合。
    // 只认面的校验会把图上最要紧的五层整层丢掉。
    const facilities = layer({ layerId: 'facilities', displayGeometry: null,
      geometry: collection([feature(point(116.4, 39.9), { id: 'f-1', majorCategory: 'medical' })]) });
    expect(validLayer(facilities, 'facilities')).toBe(true);
    expect(readLayerGeometry(facilities)).toMatchObject({
      kind: 'collection', features: [{ type: 'Feature' }], properties: {} });
  });

  it('refuses a collection with one broken feature rather than drawing the rest', () => {
    // 静默跳过坏要素的表现是"地图上少一个设施"，而报告里的设施数是不会少的。
    expect(validLayer(layer({ geometry: collection([feature(point(116.4, 39.9)),
      { type: 'Feature', properties: {} } ]) }), 'service_gaps')).toBe(false);
    expect(validLayer(layer({ geometry: { type: 'FeatureCollection', features: 'all' } }),
      'service_gaps')).toBe(false);
  });

  it('refuses a shape it does not recognise instead of drawing it', () => {
    expect(validLayer(layer({ geometry: { cells: ['0:1:2'], area: 12 } }), 'service_gaps')).toBe(false);
    expect(validLayer(layer({ geometry: { type: 'Point', coordinates: [116.4, 39.9] } }),
      'service_gaps')).toBe(false);
  });

  it('reads the three layer forms apart, including the document-only report layer', () => {
    expect(readLayerGeometry(layer())?.kind).toBe('polygon');
    expect(readLayerGeometry(layer({ layerId: 'report', geometry: null, displayGeometry: null,
      document: { reportId: 'task-1:5' } })))
      .toMatchObject({ kind: 'document' });
    // 没有形状也没有文档：合法状态（等时圈失败），但绝不能读成一个空集合了事。
    expect(readLayerGeometry(layer({ geometry: null, displayGeometry: null, document: null })))
      .toEqual({ kind: 'none' });
  });
});

describe('facility route', () => {
  it('accepts the fixture', () => {
    expect(validFacilityRoute(route())).toBe(true);
  });

  it('refuses a verdict that comes without the distance it was made from', () => {
    // 只有判定没有距离，读者无法复核；判据只有后端一处，界面从不自己判 1000 米。
    expect(validFacilityRoute(route({ routeDistanceM: null }))).toBe(false);
    expect(validFacilityRoute(route({ withinRule: true, accessDistanceM: null }))).toBe(false);
    expect(validFacilityRoute(route({ withinRule: null, routeDistanceM: null,
      durationS: null, observedDurationS: null }))).toBe(true);
    // 误差带里的路线：报出距离，但不下判定。
    expect(validFacilityRoute(route({ withinRule: null, routeDistanceM: 1040, accessDistanceM: 1062,
      verificationLayer: 'endpoint_tolerance' }))).toBe(true);
  });

  it('reads the endpoint-tolerance layer and still accepts an older backend without it', () => {
    expect(validFacilityRoute(route({ verificationLayer: 'endpoint_tolerance', accessDistanceM: 512 })))
      .toBe(true);
    expect(validFacilityRoute(route({ verificationLayer: 'guessed' as never }))).toBe(false);
    expect(validFacilityRoute(route({ accessDistanceM: 'far' as never }))).toBe(false);
  });

  it('refuses a route that is neither verified nor modelled', () => {
    expect(validFacilityRoute(route({ evidenceGrade: 'assumed' as never }))).toBe(false);
    expect(validFacilityRoute(route({ poiStatus: 'unknown' as never }))).toBe(false);
    expect(validFacilityRoute(route({ durationS: Infinity }))).toBe(false);
  });
});

describe('water evidence', () => {
  it('accepts a snapshot with water evidence, with none, and from a backend that predates it', () => {
    expect(validSnapshot(snapshot({ water: water() }))).toBe(true);
    expect(validSnapshot(snapshot({ water: null }))).toBe(true);
    const { water: _omitted, ...older } = snapshot();
    expect(validSnapshot(older)).toBe(true);
  });

  it('refuses a conflict area that cannot be drawn', () => {
    // 画不出来的冲突面比没有更糟：图上少了"数据冲突／未知"，底图水面就被读成已核实。
    const review = waterReview({ conflicts: [{ id: 'x', geometry: { type: 'Polygon', coordinates: [[[1, 2]]] } }] });
    expect(validSnapshot(snapshot({ water: water({ reviews: [review] }) }))).toBe(false);
    expect(validSnapshot(snapshot({ water: water({ reviews: [waterReview({ extent: { type: 'Point' } })] }) }))).toBe(false);
  });

  it('refuses negative areas and statements that are not text', () => {
    expect(validSnapshot(snapshot({ water: water({ conflictAreaM2: -1 }) }))).toBe(false);
    expect(validSnapshot(snapshot({ water: water({ statements: [1 as unknown as string] }) }))).toBe(false);
  });

  it('refuses a recompute that claims to come from itself or a later revision', () => {
    const trace = (fromRevision: number) => ({ ...snapshot().trace, recomputed: { fromRevision } });
    expect(validSnapshot(snapshot({ revision: 7, trace: trace(5) }))).toBe(true);
    expect(validSnapshot(snapshot({ revision: 7, trace: trace(7) }))).toBe(false);
    expect(validSnapshot(snapshot({ revision: 7, trace: trace(9) }))).toBe(false);
  });

  it('refuses capabilities whose water review list is not a list', () => {
    expect(validCapabilities(capabilities())).toBe(true);
    expect(validCapabilities({ ...capabilities(), waterReviews: {} })).toBe(false);
  });
});
