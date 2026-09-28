/**
 * v2 体检工作台。
 *
 * 与旧分析页的分工：**这里只负责"提交什么、显示什么、把哪一层画上去"**。判据在
 * `capabilities.ts`（能选什么）、`report.ts`（数字怎么读）、`layers.ts`（画成什么），
 * 三个都是纯函数，所以这个组件里剩下的东西都是排版。
 *
 * 三处刻意的选择：
 *
 * - **引擎与预算从能力表里来**，不写死档位。引擎换掉时预算跟着换：档位是引擎自己的，
 *   沿用上一个引擎的档会撞上"不支持的预算一律 422"（§4.1）。
 * - **余额照抄后端给的说法**（§9 的"本应用预算余额"），不加也不改。它只包含本应用自己的
 *   额度，浏览器 SDK、其他应用和旧接口的流量都不在里面 —— 换个说法就会被认为是账号总余额。
 * - **图层按修订取、按层失败按层说**。一层没就绪（409 带名字）不该让整页失败，也不该
 *   被画成"这一层是空的"。
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Card, Checkbox, Descriptions, Drawer, InputNumber, Select, Space, Tag } from 'antd';
import type { Center } from '../types';
import { LocationControls } from '../analysis/LocationControls';
import { createCheckupService, CheckupError } from './client';
import { budgetFor, capabilityView, type CapabilityView } from './capabilities';
import { isCheckupBusy, STAGE_LABELS } from './types';
import type { CheckupState } from './types';
import { checkupSession, useCheckupState } from './sessions';
import { LAYER_IDS, type LayerId, type Stage } from './validate';
import { DENSITY_ALL, drawableLayer, LAYER_STYLES, serviceSamples, type LayerDrawable } from './layers';
import { CheckupMap, DEFAULT_CHECKUP_LAYERS, type CheckupLayerToggles, type HeatLayer } from './CheckupMap';
import { CheckupReport } from './CheckupReport';
import { CATEGORY_ORDER, categoryLabel, evidenceNotes, percent } from './report';
import { SERVICE_COMPOSITE } from '../map/layers/serviceField';
import { outdatedText, recomputedText, versionView, waterView } from './water';
// 复用旧分析页的排布类：工作台与它是同一个版式，另起一套只会让两页慢慢长歪。
import '../analysis/api.css';
import './checkup.css';

/** 地图上的六层；报告是文档，不占图层开关，从这里打开。 */
const MAP_LAYERS = LAYER_IDS.filter(id => id !== 'report') as LayerId[];

/** 两种热力各自要先取到的图层：打开热力时，即使这些图层本身没勾选也要取。 */
const HEAT_DEPENDENCIES: Record<HeatLayer, LayerId[]> = {
  service: ['heatmap', 'isochrone'],
  density: ['facilities', 'isochrone'],
};

const SERVICE_MODES = [
  { value: SERVICE_COMPOSITE, label: '综合（三类均已知处）' },
  ...CATEGORY_ORDER.map(category => ({ value: category, label: categoryLabel(category) })),
];

const DENSITY_CATEGORIES = [
  { value: DENSITY_ALL, label: '全部设施' },
  ...CATEGORY_ORDER.map(category => ({ value: category, label: categoryLabel(category) })),
];

/** 存下来的选项不在当前列表里（旧版本存的、被手改过的）就回到默认，不把未知值交给地图。 */
const known = (options: { value: string }[], value: string | undefined, fallback: string) =>
  value !== undefined && options.some(option => option.value === value) ? value : fallback;

const BUSINESS_LABELS: Record<string, string> = {
  complete: '证据完整', partial: '部分证据', insufficient: '证据不足',
};
const ROUTE_VERDICT: Record<string, string> = {
  verified_reachable: '已核验可达', verified_unreachable: '已核验不可达', pending: '尚未核验',
};
const EVIDENCE_LABELS: Record<string, string> = { verified: '真实路线', model: '模型推定' };

function StageProgress({ stage }: { stage: Stage | null | undefined }) {
  const order = Object.keys(STAGE_LABELS) as Stage[];
  const current = stage === null || stage === undefined ? -1 : order.indexOf(stage);
  return <ol className="checkup-stages" data-testid="checkup-stages">
    {order.map((name, index) => <li key={name}
      data-reached={current >= index ? 'yes' : 'no'}
      aria-current={current === index ? 'step' : undefined}>
      <span className="checkup-stage-index">{index + 1}</span>{STAGE_LABELS[name]}</li>)}
  </ol>;
}

/** 一条设施路线的读法：判定与距离同进同出，没有距离就不说"在不在标准内"。 */
function RouteDetail({ route, error }: { route: CheckupState['route']; error?: string }) {
  if (error) return <Alert type="warning" title={error} showIcon />;
  if (!route) return <p className="api-muted">选择设施后可查询它的实际步行路线。点击详情与体检共用同一个路线闸门，额度用尽时这里会说明原因。</p>;
  return <>
    <Descriptions size="small" column={1} items={[
      { key: 'facility', label: '设施', children: route.facilityId },
      { key: 'category', label: '类别', children: `${route.category}（${route.majorCategory}）` },
      { key: 'status', label: '检索状态', children: ROUTE_VERDICT[route.poiStatus] ?? route.poiStatus },
      { key: 'straight', label: '直线距离', children: route.straightLineM === null ? '无法确定' : `${route.straightLineM.toFixed(0)} 米` },
      // 判定跟着距离一起给；只有判定没有距离的响应在校验层就被拒了，这里不必兜底。
      { key: 'distance', label: '步行距离', children: route.routeDistanceM === null ? '无法确定' : `${route.routeDistanceM.toFixed(0)} 米` },
      { key: 'verdict', label: '服务标准', children: route.withinRule === null ? '无法判定'
        : route.withinRule ? '在 1000 米步行范围内' : '超出 1000 米步行范围' },
      { key: 'grade', label: '证据等级', children: EVIDENCE_LABELS[route.evidenceGrade] ?? route.evidenceGrade },
      { key: 'provider', label: '来源', children: `${route.provider}${route.network ? '' : '（本地缓存）'}` },
    ]} />
    {route.reason && <Alert type="info" title={route.reason} />}
    {route.notes.length > 0 && <ul className="checkup-notes">{route.notes.map(note =>
      <li key={note}>{note}</li>)}</ul>}
  </>;
}

const DEFAULT_CENTER: Center = { lng: 116.404, lat: 39.915 };
/** 这些阶段出现过，完成时才自动打开报告；从刷新恢复出来的"已完成"按上次的开合状态来。 */
const LIVE_PHASES = new Set(['submitting', 'queued', 'running']);

const PHASE_LABELS: Partial<Record<CheckupState['phase'], string>> = {
  submitting: '正在提交', restoring: '正在核对上次的任务', queued: '排队中', running: '运行中',
  fetching: '正在取结果', cancelling: '正在取消', completed: '已完成', cancelled: '已取消', error: '需要处理',
};

/**
 * 体检工作台。`engine` 由外层的算法切换决定：每个引擎有自己的任务登记（`sessions.ts`），
 * 这个组件只订阅它 —— 卸载时退订，任务照跑；只有"取消任务"按钮会让服务端停下。
 */
export default function CheckupApp({ engine }: { engine: string }) {
  const service = useMemo(() => createCheckupService(), []);
  const session = useMemo(() => checkupSession(engine), [engine]);
  const controller = session.controller;
  const state = useCheckupState(session);
  const saved = useMemo(() => session.prefs(), [session]);
  const initial = saved.draft ?? state.input?.center ?? DEFAULT_CENTER;
  const [center, setCenter] = useState<Center>(initial);
  const [lng, setLng] = useState<number | null>(initial.lng);
  const [lat, setLat] = useState<number | null>(initial.lat);
  const [budget, setBudget] = useState<number | null>(saved.budget ?? null);
  const [view, setView] = useState<CapabilityView | null>(null);
  const [capabilityError, setCapabilityError] = useState<string | null>(null);
  const [toggles, setToggles] = useState<CheckupLayerToggles>({ ...DEFAULT_CHECKUP_LAYERS, ...saved.toggles });
  const [serviceMode, setServiceMode] = useState<string>(known(SERVICE_MODES, saved.serviceMode, SERVICE_COMPOSITE));
  const [densityCategory, setDensityCategory] = useState<string>(
    known(DENSITY_CATEGORIES, saved.densityCategory, DENSITY_ALL));
  const [layerErrors, setLayerErrors] = useState<Partial<Record<LayerId, string>>>({});
  /** 哪一层在哪一版上失败过：同一版不反复重试，换版再试。 */
  const [failed, setFailed] = useState<Partial<Record<LayerId, number>>>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [reportOpen, setReportOpen] = useState(saved.reportOpen ?? false);

  // 界面偏好随改随存：刷新或切回来时，图层开关、热力模式、报告开合都按离开时的样子。
  useEffect(() => {
    session.setPrefs({ draft: center, ...(budget === null ? {} : { budget }),
      toggles: toggles as Record<string, boolean>, serviceMode, densityCategory, reportOpen });
  }, [session, center, budget, toggles, serviceMode, densityCategory, reportOpen]);

  useEffect(() => {
    const abort = new AbortController();
    service.capabilities(abort.signal).then(value => {
      const next = capabilityView(value);
      setView(next);
      // 档位是引擎自己的：存下来的档位不在这个引擎的档位表里，就换成它自己的默认档。
      const budgets = next.engines.find(item => item.engineId === engine)?.budgets ?? [];
      setBudget(current => current !== null && budgets.includes(current) ? current : budgetFor(next, engine));
    }).catch((error: unknown) => {
      // 取消不是故障：组件已经卸载或重新挂载，这一轮的结果不该再上屏。
      if (abort.signal.aborted) return;
      setCapabilityError(error instanceof CheckupError ? error.message
        : '未能读取体检服务能力表，请检查服务地址后刷新页面');
    });
    return () => abort.abort();
  }, [service, engine]);

  const task = state.task;
  const snapshot = state.snapshot;
  const revision = task?.revision;
  const busy = isCheckupBusy(state);

  /**
   * 图层：按当前修订按需取，取到就缓存（控制器按"图层:修订"记），**一次只取一层**。
   *
   * 写成"取一层、状态一变再取下一层"，而不是在循环里连着 await 五层：循环版本会在每次
   * 状态更新时被重启，于是同一层被取两遍；更糟的是先启动的那一圈在重启后被判成"作废"，
   * 它最后几层的失败会被静默吞掉 —— 表现是某一层永远画不出来，而且界面上一个字都不说。
   *
   * 失败按"层 + 修订"记账：同一版不反复重试，换了修订再试一次。
   */
  useEffect(() => {
    if (state.phase !== 'completed' || revision === undefined) return;
    const pending = MAP_LAYERS.find(id => (toggles[id]
      || (Object.keys(HEAT_DEPENDENCIES) as HeatLayer[])
        .some(heat => toggles[heat] && HEAT_DEPENDENCIES[heat].includes(id)))
      && state.layers?.[id]?.revision !== revision && failed[id] !== revision);
    if (pending === undefined) return;
    controller.layer(pending).catch((error: unknown) => {
      setLayerErrors(previous => ({ ...previous, [pending]: error instanceof CheckupError
        ? error.message : '该图层未能加载' }));
      setFailed(previous => ({ ...previous, [pending]: revision }));
    });
  }, [controller, state.phase, state.layers, revision, toggles, failed]);

  const drawables = useMemo(() => {
    const result: Partial<Record<LayerId, LayerDrawable>> = {};
    for (const id of MAP_LAYERS) {
      const layer = state.layers?.[id];
      // 修订对不上的一律不画：图上画着上一版的灰区、面板写着这一版的面积，是最难发现的错。
      if (!layer || layer.revision !== revision) continue;
      result[id] = drawableLayer(id, layer);
    }
    return result;
  }, [state.layers, revision]);

  /** 服务覆盖热力的输入：同样只认这一版的模型网格。 */
  const heatmapLayer = state.layers?.heatmap;
  const coverage = useMemo(() => heatmapLayer && heatmapLayer.revision === revision
    ? serviceSamples(heatmapLayer) : null, [heatmapLayer, revision]);

  /** 两种热力互斥（§8.1）：打开一种就关掉另一种，两张半透明画布叠在一起谁也读不清。 */
  function toggleHeat(heat: HeatLayer, on: boolean) {
    setToggles(current => ({ ...current, [heat]: on,
      ...(on ? { [heat === 'service' ? 'density' : 'service']: false } : {}) }));
  }

  const selectedPoint = useMemo(() => selected === null ? null
    : (drawables.facilities?.points ?? []).find(item => item.key === selected) ?? null,
  [drawables, selected]);

  const valid = lng !== null && lat !== null && Number.isFinite(lng) && Number.isFinite(lat)
    && lng >= -180 && lng <= 180 && lat > -85 && lat < 85;
  const engines = view?.engines ?? [];
  const selectedEngine = engines.find(item => item.engineId === engine) ?? null;
  const live = controller.hasLiveTask;
  /** 这一轮任务提交时的中心：结果、进行中的任务都按它画，不按选点草稿画。 */
  const taskCenter = snapshot?.center ?? state.input?.center;
  const draft = valid ? { lng: +lng!.toFixed(6), lat: +lat!.toFixed(6) } : null;
  const stale = taskCenter !== undefined && draft !== null
    && (taskCenter.lng !== draft.lng || taskCenter.lat !== draft.lat);
  const canStart = valid && !busy && !live && !!view && !!selectedEngine;

  // 换选点只改草稿，不取消、不清除任何任务：结果仍属于提交它的那个中心，直到用户重新体检。
  function choose(next: Center) {
    setCenter(next); setLng(next.lng); setLat(next.lat);
  }
  function edit(axis: 'lng' | 'lat', value: number | null) {
    if (axis === 'lng') setLng(value); else setLat(value);
  }
  function start() {
    if (!canStart || !draft) return;
    setCenter(draft); setSelected(null);
    setLayerErrors({}); setFailed({});
    void controller.start({ center: draft, engine, ...(budget === null ? {} : { budget }) });
  }
  function clear() {
    if (controller.clear()) { setSelected(null); setLayerErrors({}); setFailed({}); }
  }
  const sawLive = useRef(false);
  useEffect(() => {
    if (LIVE_PHASES.has(state.phase)) sawLive.current = true;
    if (state.phase === 'completed' && sawLive.current) { sawLive.current = false; setReportOpen(true); }
  }, [state.phase]);

  /** 水系标注与版本标识都只认当前这一版修订：换版就换，不留上一版的标注。 */
  const water = useMemo(() => waterView(snapshot?.water), [snapshot]);
  const version = useMemo(() => snapshot ? versionView(snapshot, view?.waterReviews ?? []) : null,
    [snapshot, view]);

  const overall = snapshot?.report?.overall ?? snapshot?.scores?.overall ?? null;
  const notes = useMemo(() => snapshot ? evidenceNotes(snapshot) : [], [snapshot]);

  return <div className="api-app">
    <header className="api-header"><div><span className="api-brand">15</span><div>
      <h1>15 分钟生活圈 · 体检</h1><p>服务覆盖、灰区与评分区间（v2 修订）</p></div></div>
      <Space><Tag color="cyan" data-testid="checkup-engine-tag">{selectedEngine?.label ?? engine}</Tag>
        <Tag color="geekblue">checkup-v1</Tag></Space></header>
    <main className="api-layout">
      <section className="api-controls" aria-label="体检条件">
        <Card title="体检中心"><p className="api-muted">在地图上选点、获取当前位置或输入百度坐标。评估域以该点为中心划定，域外的面积一律不计入覆盖率。</p>
          <LocationControls center={center} onPick={choose} />
          <label className="api-label">经度<InputNumber aria-label="经度" value={lng}
            onChange={value => edit('lng', value)} precision={6} /></label>
          <label className="api-label">纬度<InputNumber aria-label="纬度" value={lat}
            onChange={value => edit('lat', value)} precision={6} /></label>
          {!valid && <Alert type="error" title="请输入有效坐标：经度 −180～180，纬度大于 −85 且小于 85" />}
        </Card>
        <Card title="引擎与预算">
          {capabilityError && <Alert type="error" title={capabilityError} showIcon />}
          <p className="api-muted" data-testid="checkup-engine">引擎：{selectedEngine?.label
            ?? (view ? `${engine}（后端能力表未提供，无法提交）` : '正在读取能力表')}。在页面顶部切换算法；
            两个引擎的任务各自保留，切换不会取消任何一个。</p>
          <label className="api-label">等时圈采样预算<Select aria-label="调用预算"
            value={budget ?? undefined} placeholder="—"
            onChange={setBudget}
            options={(selectedEngine?.budgets ?? []).map(value => ({ value, label: `${value} 次` }))} /></label>
          <p className="api-muted">步行阈值 900 秒 · 服务标准 1000 米 · 坐标系 BD09LL</p>
          {selectedEngine?.engineVersion && <p className="api-muted">引擎版本 {selectedEngine.engineVersion}</p>}
          {selectedEngine?.caveat && <Alert type="warning" title={selectedEngine.caveat} />}
          <Space wrap>
            <Button aria-label="开始体检" type="primary" onClick={start}
              disabled={!canStart}>开始体检</Button>
            {live && <Button onClick={() => void controller.cancel()}
                disabled={state.phase === 'cancelling'}>取消任务</Button>}
            {!live && !busy && state.phase !== 'idle' && <Button onClick={clear}>清除结果</Button>}
          </Space>
          {live && <p className="api-muted" data-testid="checkup-live-note">当前任务尚未结束：切换页面、切换算法或刷新都不会取消它。
            要按新条件体检，请等它完成，或先点"取消任务"。</p>}
        </Card>
        <Card title="地图图层">
          <Checkbox checked={!!toggles.service}
            onChange={event => toggleHeat('service', event.target.checked)}>
            服务覆盖热力</Checkbox>
          <p className="api-muted checkup-layer-note">后端评估格的结论连成渐变面：已覆盖按最近设施步行距离着色，服务不足为灰，数据未知为淡紫。模型估计，不是实测。</p>
          {toggles.service && <label className="api-label checkup-layer-note">覆盖类别<Select
            aria-label="覆盖类别" value={serviceMode} onChange={setServiceMode} options={SERVICE_MODES} /></label>}
          <Checkbox checked={!!toggles.density}
            onChange={event => toggleHeat('density', event.target.checked)}>
            设施密度热力</Checkbox>
          <p className="api-muted checkup-layer-note">圈内已接收设施等权计算，120 米核半径，单位个/公顷；同一疑似重复组只算一处。颜色表示设施扎堆程度，不表示覆盖率。与服务覆盖热力二选一。</p>
          {toggles.density && <label className="api-label checkup-layer-note">密度类别<Select
            aria-label="密度类别" value={densityCategory} onChange={setDensityCategory}
            options={DENSITY_CATEGORIES} /></label>}
          <Checkbox checked={!!toggles.water}
            onChange={event => setToggles({ ...toggles, water: event.target.checked })}>
            水系标注</Checkbox>
          <p className="api-muted checkup-layer-note" data-testid="water-layer-note">
            {snapshot && !water.available ? '这一版没有水系证据（早于水系复核），无可标注的内容。'
              : '计算用的水系：复核范围（虚线框）内的已核实河道按实测宽度成面，补录水体与桥梁已核对；'
                + '底图水面画错处（灰斜线，实为陆地）与来源冲突未裁决处（橙斜线，数据冲突／未知）单独标出。'
                + '底图水面只作参照，不代表计算结果。'}</p>
          <div className="api-layer-list">{MAP_LAYERS.map(id => <div key={id}>
            <Checkbox checked={!!toggles[id]}
              onChange={event => setToggles({ ...toggles, [id]: event.target.checked })}>
              <i className="api-swatch" style={{ background: LAYER_STYLES[id].fillColor }} />
              {LAYER_STYLES[id].label}</Checkbox>
            <p className="api-muted checkup-layer-note">{LAYER_STYLES[id].note}</p>
            {layerErrors[id] && <Alert type="warning" title={layerErrors[id]} />}
          </div>)}</div>
        </Card>
      </section>
      <section className="api-map-section" data-testid="checkup-map-section"
        data-task-id={task?.taskId ?? ''} data-revision={revision ?? ''}
        data-water-reviews={version?.applied?.join(',') ?? ''}
        data-outdated={version && version.outdatedBy.length > 0 ? 'yes' : 'no'}
        data-drawn-revisions={[...new Set(MAP_LAYERS.filter(id => drawables[id])
          .map(id => state.layers?.[id]?.revision))].join(',')}>
        {snapshot && stale && <Alert type="warning" showIcon
          title="条件已修改。地图与报告仍是上一次体检的结果，需重新体检才会更新。" />}
        {!snapshot && live && stale && <Alert type="info" showIcon
          title="进行中的任务仍按它提交时的中心计算；新选点要等它结束后再体检。" />}
        {version && version.outdatedBy.length > 0 && <Alert type="warning" showIcon
          data-testid="checkup-outdated" title={`旧版本（第 ${snapshot?.revision} 版）：水系数据已修订`}
          description={outdatedText(version)} />}
        <CheckupMap center={center} onPick={choose} resultCenter={taskCenter}
          layers={toggles} drawables={drawables} coverage={coverage} serviceMode={serviceMode}
          densityCategory={densityCategory} water={water}
          selectedId={selected} onSelect={setSelected} />
      </section>
      <section className="api-results" aria-label="体检结果">
        <Card title="体检进度">
          {state.phase === 'idle' && <p className="api-muted">选好中心与引擎后开始体检。结果包含覆盖区间、服务灰区与真实步行核验。</p>}
          {state.phase !== 'idle' && <p className="api-muted" data-testid="checkup-phase"
            data-phase={state.phase}>状态：{PHASE_LABELS[state.phase] ?? state.phase}
            {state.input && `（中心 ${state.input.center.lng.toFixed(6)}, ${state.input.center.lat.toFixed(6)}）`}</p>}
          {state.connection === 'lost' && <Alert type="warning" showIcon data-testid="checkup-connection"
            title="与体检服务的连接中断，正在自动重连"
            description="任务仍在服务端继续，不会因为断网被取消或重复提交；连上后自动接着显示进度与结果。" />}
          {state.phase === 'restoring' && <Alert type="info" showIcon
            title="正在向后端核对上次的体检任务"
            description={state.input ? `请求标识 ${state.input.clientRequestId}` : undefined} />}
          {state.phase === 'queued' && <Alert type="info" showIcon title="排队中"
            description="体检服务一次只执行一个任务（两个引擎共用），本任务会在前面的任务结束后自动开始。" />}
          {task && <>
            <StageProgress stage={task.stage} />
            <Descriptions size="small" column={1} items={[
              { key: 'task', label: '任务', children: <span data-testid="checkup-task-id">{task.taskId}</span> },
              { key: 'revision', label: '当前修订', children: `第 ${task.revision} 版` },
              { key: 'status', label: '状态', children: task.status +
                (task.businessStatus ? `（${BUSINESS_LABELS[task.businessStatus] ?? task.businessStatus}）` : '') },
              { key: 'spend', label: '本任务已用', children:
                `${task.networkRequests} 次网络尝试（等时圈、设施与核验各池合计）` },
              { key: 'tier', label: '等时圈档位', children: `${task.budget} 次上限` },
              { key: 'elapsed', label: '已用时', children: `${task.elapsedSeconds.toFixed(1)} 秒` },
            ]} />
            {version && snapshot?.taskId === task.taskId && <p className="api-muted" data-testid="checkup-version"
              data-recomputed={version.recomputed ? 'yes' : 'no'}>
              {`结果版本：第 ${snapshot.revision} 版 · 水系 `
                + (version.applied === null ? '早于水系复核' : version.applied.length === 0 ? 'OSM 原样（未采用复核）'
                  : version.applied.join('、'))}
              {version.recomputed && <><br />{recomputedText(version.recomputed)}</>}</p>}
          </>}
          {state.phase === 'cancelled' && <Alert type="info" title="任务已取消"
            description="取消只停住后续请求；已经发出的调用仍会计入本应用的预算账本。" />}
          {state.error && <Alert type="error" title={state.error} showIcon data-testid="checkup-error"
            action={<Button aria-label="重试" size="small"
              onClick={() => void controller.retry()}>{state.recovery === 'unconfirmed' ? '重新提交'
                : state.recovery === 'expired' || task?.status === 'failed' ? '重新体检' : '重试'}</Button>} />}
          {view?.quota.label && <p className="api-muted" data-testid="quota-label">
            {/* 余额的说法逐字来自后端：它只算本应用自己的额度。 */}
            {view.quota.label}</p>}
          {view && view.quota.lines.map(line => <p className="api-muted" key={line}>{line}</p>)}
        </Card>
        {snapshot && <Card title="覆盖区间摘要">
          <Descriptions size="small" column={1} items={[
            { key: 'lower', label: '最低覆盖率', children: percent(overall?.coverageLowerPct ?? null) },
            { key: 'upper', label: '最高覆盖率', children: percent(overall?.coverageUpperPct ?? null) },
            { key: 'assessable', label: '可评估比例', children: percent(overall?.assessablePct ?? null) },
            { key: 'unknown', label: '未知比例', children: percent(overall?.unknownPct ?? null) },
          ]} />
          {overall && !overall.available && <Alert type="warning" showIcon
            title="总体区间暂不给出" description={overall.reason ?? '缺少分类结论'} />}
          <Button block onClick={() => setReportOpen(true)}>查看体检报告</Button>
        </Card>}
        {snapshot && <Card title="设施详情">
          {selectedPoint ? <>
            <Descriptions size="small" column={1} items={[
              { key: 'id', label: '设施', children: selectedPoint.key },
              { key: 'title', label: '摘要', children: selectedPoint.title },
            ]} />
            <Button onClick={() => void controller.detail(selectedPoint.key)}
              disabled={busy}>查询步行路线</Button>
          </> : <p className="api-muted">在地图上点一个设施点位，可查询它的实际步行路线。</p>}
          <RouteDetail route={state.route} error={state.routeError} />
        </Card>}
        {notes.length > 0 && <Card title="证据说明"><ul className="checkup-notes">
          {notes.map(note => <li key={note.text}>{note.text}</li>)}</ul></Card>}
      </section>
    </main>
    <footer className="api-footer">未知不等于不可达：灰区只覆盖"路网与检索数据都足够、却仍超出服务标准"的连片区域。评分区间是上界与下界，不是一个确定的百分比。</footer>
    <Drawer title="体检报告" open={reportOpen} onClose={() => setReportOpen(false)} size={760}>
      {snapshot ? <CheckupReport snapshot={snapshot} stale={stale} waterReviews={view?.waterReviews} />
        : <p className="api-muted">体检完成后在这里显示报告。</p>}
    </Drawer>
  </div>;
}
