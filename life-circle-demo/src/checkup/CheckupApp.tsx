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
import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { Alert, Button, Checkbox, Descriptions, Drawer, InputNumber, Select } from 'antd';
import type { Center } from '../types';
import { LocationControls } from '../analysis/LocationControls';
import { createCheckupService, CheckupError } from './client';
import { budgetFor, capabilityView, type CapabilityView } from './capabilities';
import { isCheckupBusy, STAGE_LABELS } from './types';
import type { CheckupState } from './types';
import { checkupSession, useCheckupState } from './sessions';
import { LAYER_IDS, type LayerId, type Stage } from './validate';
import { DENSITY_ALL, drawableLayer, LAYER_STYLES, serviceSamples, type LayerDrawable } from './layers';
import { CheckupMap, DEFAULT_CHECKUP_LAYERS, layerSwatch, type CheckupLayerToggles, type HeatLayer } from './CheckupMap';
import { CheckupReport } from './CheckupReport';
import { CATEGORY_ORDER, categoryLabel, coverageItems, evidenceNotes, percent } from './report';
import { SERVICE_COMPOSITE } from '../map/layers/serviceField';
import { outdatedText, recomputedText, versionView, waterView } from './water';
import { duration, liveView, TICKING_PHASES, type LiveKind } from './live';
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

/**
 * 区间条：实色是下界（已知覆盖），斜线是未知面积（可能覆盖），空白是其余。
 * 只把后端给的两个百分比画出来，不另算任何数。
 */
function RangeMeter({ lower, upper, color }: { lower: number | null; upper: number | null; color?: string }) {
  if (lower === null || upper === null) return <span className="wb-meter wb-meter-empty" aria-hidden="true" />;
  return <span className="wb-meter" aria-hidden="true" style={color ? { '--meter': color } as CSSProperties : undefined}>
    <i className="wb-meter-known" style={{ width: `${lower}%` }} />
    <i className="wb-meter-unknown" style={{ left: `${lower}%`, width: `${Math.max(0, upper - lower)}%` }} />
  </span>;
}

const fixed = (value: number | null | undefined) => value === null || value === undefined ? '—' : value.toFixed(1);

const PHASE_TONE: Partial<Record<CheckupState['phase'], string>> = {
  completed: 'done', cancelled: 'muted', error: 'error',
};

/** 一条设施路线的读法：判定与距离同进同出，没有距离就不说"在不在标准内"。 */
function RouteDetail({ route, error }: { route: CheckupState['route']; error?: string }) {
  if (error) return <Alert type="warning" title={error} showIcon />;
  if (!route) return <p className="api-muted">选择设施后可查询它的实际步行路线。点击详情与体检共用同一个路线闸门，额度用尽时这里会说明原因。</p>;
  return <>
    <Descriptions className="wb-facts" size="small" column={1} items={[
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

/** 实时读法的提示框：问不到后端与疑似停滞要人留意，其余只是说明。 */
const LIVE_ALERT: Record<LiveKind, 'info' | 'warning'> = {
  submitting: 'info', restoring: 'info', queued: 'info', working: 'info', cancelling: 'info',
  lost: 'warning', stalled: 'warning',
};

/** 只有时钟在走：任务未决时每秒取一次本机时刻重算读数，不做任何动画。 */
function useClock(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return now;
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

  const clock = useClock(TICKING_PHASES.includes(state.phase));
  // 新的回答可能比上一次时钟跳动更晚到：以两者中较晚的为"此刻"，读数不会倒退成负数。
  const now = Math.max(clock, state.contact?.at ?? 0, state.reconnect?.since ?? 0);
  const liveNow = liveView(state, now);

  const overall = snapshot?.report?.overall ?? snapshot?.scores?.overall ?? null;
  const notes = useMemo(() => snapshot ? evidenceNotes(snapshot) : [], [snapshot]);
  const items = useMemo(() => snapshot ? coverageItems(snapshot) : [], [snapshot]);

  const facts = task && <Descriptions className="wb-facts" size="small" column={1} items={[
    { key: 'task', label: '任务', children: <span data-testid="checkup-task-id">{task.taskId}</span> },
    { key: 'revision', label: '最新修订', children: <span data-testid="checkup-revision"
      data-revision={task.revision}>{`第 ${task.revision} 版`}</span> },
    { key: 'status', label: '状态', children: task.status +
      (task.businessStatus ? `（${BUSINESS_LABELS[task.businessStatus] ?? task.businessStatus}）` : '') },
    { key: 'spend', label: '本任务已用', children: <span data-testid="checkup-requests"
      data-count={task.networkRequests}>{`${task.networkRequests} 次网络尝试（等时圈、设施与核验各池合计；只数真的发出的请求）`}</span> },
    { key: 'tier', label: '等时圈档位', children: `${task.budget} 次上限` },
    ...(liveNow && liveNow.stage ? [{ key: 'stage', label: '当前阶段', children:
      <span data-testid="checkup-stage-now" data-stage={liveNow.stage}>
        {STAGE_LABELS[liveNow.stage] + (liveNow.stageFor === null ? ''
          : `（本阶段已 ${duration(liveNow.stageFor)}）`)}</span> }] : []),
    ...(liveNow && liveNow.step ? [{ key: 'step', label: '当前步骤', children:
      <span data-testid="checkup-step" data-step={liveNow.step.step}
        data-count={liveNow.step.count ?? ''}>
        {liveNow.step.label}{liveNow.stepCount ? ` · ${liveNow.stepCount}` : ''}
        {liveNow.stepFor === null ? '' : `（本步已 ${duration(liveNow.stepFor)}）`}</span> }] : []),
    ...(liveNow && liveNow.contactAgo !== null ? [{ key: 'contact', label: '最近一次成功连接',
      children: <span data-testid="checkup-contact" data-seconds={liveNow.contactAgo}>
        {`${duration(liveNow.contactAgo)}前`}</span> }] : []),
    ...(liveNow && (task.status === 'running' || task.status === 'cancelling') ? [{ key: 'activity',
      label: '服务端最近活动', children: <span data-testid="checkup-activity"
        data-seconds={liveNow.activityAgo ?? ''}>
        {liveNow.activityAgo === null ? '未记录（后端版本不报活动时间）'
          : `${duration(liveNow.activityAgo)}前`}</span> }] : []),
  ]} />;

  return <main className="wb-body" data-stage={state.phase === 'idle' ? 'idle' : 'active'}>
    <aside className="wb-panel wb-side" aria-label="体检条件">
      <div className="wb-scroll">
        <section className="wb-sec">
          <h2 className="wb-h">体检中心</h2>
          <LocationControls center={center} onPick={choose} />
          <div className="wb-coords">
            <label className="wb-field"><span>经度</span><InputNumber aria-label="经度" value={lng}
              onChange={value => edit('lng', value)} precision={6} controls={false} /></label>
            <label className="wb-field"><span>纬度</span><InputNumber aria-label="纬度" value={lat}
              onChange={value => edit('lat', value)} precision={6} controls={false} /></label>
          </div>
          <p className="wb-hint">也可以直接在地图上点选。评估域以该点为中心划定，域外面积不计入覆盖率。</p>
          {!valid && <Alert type="error" title="请输入有效坐标：经度 −180～180，纬度大于 −85 且小于 85" />}
        </section>
        <section className="wb-sec">
          <h2 className="wb-h">引擎与预算</h2>
          {capabilityError && <Alert type="error" title={capabilityError} showIcon />}
          <p className="wb-hint" data-testid="checkup-engine">引擎：{selectedEngine?.label
            ?? (view ? `${engine}（后端能力表未提供，无法提交）`
              : capabilityError ? '未能读取能力表，暂不能提交' : '正在读取能力表')}
            {selectedEngine?.engineVersion && <span className="wb-version">v{selectedEngine.engineVersion}</span>}</p>
          <label className="wb-field"><span>等时圈采样预算</span><Select aria-label="调用预算"
            value={budget ?? undefined} placeholder="—"
            onChange={setBudget}
            options={(selectedEngine?.budgets ?? []).map(value => ({ value, label: `${value} 次` }))} /></label>
          <p className="wb-hint">步行 900 秒 · 服务标准 1000 米 · 坐标 BD09LL</p>
          {selectedEngine?.caveat && <Alert type="warning" title={selectedEngine.caveat} />}
        </section>
        <section className="wb-sec">
          <h2 className="wb-h">地图图层</h2>
          <p className="wb-group">热力 · 二选一</p>
          <div className="wb-layer">
            <Checkbox checked={!!toggles.service}
              onChange={event => toggleHeat('service', event.target.checked)}>服务覆盖热力</Checkbox>
            {toggles.service && <Select className="wb-layer-select" size="small" aria-label="覆盖类别"
              value={serviceMode} onChange={setServiceMode} options={SERVICE_MODES} popupMatchSelectWidth={false} />}
          </div>
          <div className="wb-layer">
            <Checkbox checked={!!toggles.density}
              onChange={event => toggleHeat('density', event.target.checked)}>设施密度热力</Checkbox>
            {toggles.density && <Select className="wb-layer-select" size="small" aria-label="密度类别"
              value={densityCategory} onChange={setDensityCategory} options={DENSITY_CATEGORIES}
              popupMatchSelectWidth={false} />}
          </div>
          <p className="wb-group">叠加</p>
          <div className="wb-layer">
            <Checkbox checked={!!toggles.water}
              onChange={event => setToggles({ ...toggles, water: event.target.checked })}>
              <i className="api-swatch wb-swatch-water" />水系标注</Checkbox>
          </div>
          {MAP_LAYERS.map(id => <div className="wb-layer" key={id}>
            <Checkbox checked={!!toggles[id]}
              onChange={event => setToggles({ ...toggles, [id]: event.target.checked })}>
              <i className="api-swatch" style={layerSwatch(id)} />
              {LAYER_STYLES[id].label}</Checkbox>
            {layerErrors[id] && <p className="wb-layer-error">{layerErrors[id]}</p>}
          </div>)}
          <details className="wb-more wb-notes">
            <summary>读图说明</summary>
            <dl>
              <dt>服务覆盖热力</dt>
              <dd>后端评估格的结论连成渐变面：已覆盖按最近设施步行距离着色，服务不足为灰，数据未知为淡紫。模型估计，不是实测。</dd>
              <dt>设施密度热力</dt>
              <dd>圈内已接收设施等权计算，120 米核半径，单位个/公顷；同一疑似重复组只算一处。颜色表示设施扎堆程度，不表示覆盖率。与服务覆盖热力二选一。</dd>
              <dt>水系标注</dt>
              <dd data-testid="water-layer-note">
                {snapshot && !water.available ? '这一版没有水系证据（早于水系复核），无可标注的内容。'
                  : '计算用的水系：复核范围（虚线框）内的已核实河道按实测宽度成面，补录水体与桥梁已核对；'
                    + '底图水面画错处（灰斜线，实为陆地）与来源冲突未裁决处（橙斜线，数据冲突／未知）单独标出。'
                    + '底图水面只作参照，不代表计算结果。'}</dd>
              {MAP_LAYERS.map(id => <div key={id}><dt>{LAYER_STYLES[id].label}</dt>
                <dd>{LAYER_STYLES[id].note}</dd></div>)}
            </dl>
          </details>
        </section>
      </div>
      <footer className="wb-actions">
        <Button aria-label="开始体检" type="primary" size="large" block onClick={start}
          disabled={!canStart}>开始体检</Button>
        {(live || (!busy && state.phase !== 'idle')) && <div className="wb-actions-row">
          {live && <Button onClick={() => void controller.cancel()}
            disabled={state.phase === 'cancelling'}>取消任务</Button>}
          {!live && !busy && state.phase !== 'idle' && <Button onClick={clear}>清除结果</Button>}
        </div>}
        {live && <p className="wb-hint" data-testid="checkup-live-note">当前任务尚未结束：切换页面、切换算法或刷新都不会取消它。
          要按新条件体检，请等它完成，或先点"取消任务"。</p>}
      </footer>
    </aside>

    <section className="wb-map api-map-section" data-testid="checkup-map-section"
      data-task-id={task?.taskId ?? ''} data-revision={revision ?? ''}
      data-water-reviews={version?.applied?.join(',') ?? ''}
      data-outdated={version && version.outdatedBy.length > 0 ? 'yes' : 'no'}
      data-drawn-revisions={[...new Set(MAP_LAYERS.filter(id => drawables[id])
        .map(id => state.layers?.[id]?.revision))].join(',')}>
      <CheckupMap center={center} onPick={choose} resultCenter={taskCenter}
        layers={toggles} drawables={drawables} coverage={coverage} serviceMode={serviceMode}
        densityCategory={densityCategory} water={water}
        selectedId={selected} onSelect={setSelected} />
      <div className="wb-map-banners">
        {snapshot && stale && <Alert type="warning" showIcon
          title="条件已修改。地图与报告仍是上一次体检的结果，需重新体检才会更新。" />}
        {!snapshot && live && stale && <Alert type="info" showIcon
          title="进行中的任务仍按它提交时的中心计算；新选点要等它结束后再体检。" />}
        {version && version.outdatedBy.length > 0 && <Alert type="warning" showIcon
          data-testid="checkup-outdated" title={`旧版本（第 ${snapshot?.revision} 版）：水系数据已修订`}
          description={outdatedText(version)} />}
      </div>
    </section>

    <aside className="wb-panel wb-results" aria-label="体检结果">
      <div className="wb-scroll">
        <section className="wb-sec">
          <div className="wb-sec-head">
            <h2 className="wb-h">体检进度</h2>
            {state.phase !== 'idle' && <span className="wb-phase" data-testid="checkup-phase"
              data-phase={state.phase} data-tone={PHASE_TONE[state.phase] ?? 'live'}>
              {PHASE_LABELS[state.phase] ?? state.phase}</span>}
          </div>
          {state.phase === 'idle' && <div className="wb-empty">
            <p className="wb-empty-title">还没有体检结果</p>
            <p>在地图上选好中心，点左下角“开始体检”。一次体检会给出：</p>
            <ul>
              <li>购物、医疗、教育三类设施的步行覆盖率区间</li>
              <li>超出 1000 米服务标准的灰区与改进建议</li>
              <li>抽样设施的真实步行路线核验</li>
            </ul>
          </div>}
          {state.input && state.phase !== 'idle' && <p className="wb-hint wb-center">
            中心 {state.input.center.lng.toFixed(6)}, {state.input.center.lat.toFixed(6)}</p>}
          {liveNow && <div className="wb-live" role="status" data-testid="checkup-live"
            data-kind={liveNow.kind} data-tone={LIVE_ALERT[liveNow.kind]}>
            <strong>{liveNow.title}</strong>
            <p>{liveNow.hint + (liveNow.kind === 'restoring' && state.input
              ? `（请求标识 ${state.input.clientRequestId}）` : '')}</p>
          </div>}
          {task && <>
            <div className="wb-clock">
              <span>已用时</span>
              <b data-testid="checkup-elapsed"
                data-seconds={liveNow ? liveNow.elapsed ?? '' : task.elapsedSeconds}>
                {liveNow ? liveNow.kind === 'queued' ? `尚未开始（已排队 ${duration(liveNow.queued)}）`
                  : liveNow.elapsed === null ? '尚未开始' : duration(liveNow.elapsed)
                  : `${task.elapsedSeconds.toFixed(1)} 秒`}</b>
            </div>
            <StageProgress stage={task.stage} />
            {liveNow ? facts : <details className="wb-more"><summary>任务信息</summary>{facts}</details>}
            {version && snapshot?.taskId === task.taskId && <p className="wb-hint" data-testid="checkup-version"
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
        </section>

        {snapshot && <section className="wb-sec wb-summary" aria-label="覆盖区间摘要">
          <h2 className="wb-h">覆盖率区间</h2>
          {overall && overall.available ? <>
            <p className="wb-range"><b>{fixed(overall.coverageLowerPct)}</b><span>～</span>
              <b>{fixed(overall.coverageUpperPct)}</b><small>%</small></p>
            <RangeMeter lower={overall.coverageLowerPct} upper={overall.coverageUpperPct} />
            <p className="wb-meter-key"><i className="k-known" />已知覆盖<i className="k-unknown" />未知
              <span>可评估 {percent(overall.assessablePct)} · 未知 {percent(overall.unknownPct)}</span></p>
          </> : <Alert type="warning" showIcon
            title="总体区间暂不给出" description={overall?.reason ?? '缺少分类结论'} />}
          <ul className="wb-cats">{items.map(item => <li key={item.category}>
            <span className="wb-cat-name"><i style={{ background: item.color }} />{item.label}</span>
            <RangeMeter lower={item.lowerPct} upper={item.upperPct} color={item.color} />
            <span className="wb-cat-value">{item.supported
              ? `${fixed(item.lowerPct)}–${fixed(item.upperPct)}%` : '无法确定'}</span>
          </li>)}</ul>
          <Button block onClick={() => setReportOpen(true)}>查看体检报告</Button>
        </section>}

        {snapshot && <section className="wb-sec">
          <h2 className="wb-h">设施详情</h2>
          {selectedPoint ? <>
            <Descriptions className="wb-facts" size="small" column={1} items={[
              { key: 'id', label: '设施', children: selectedPoint.key },
              { key: 'title', label: '摘要', children: selectedPoint.title },
            ]} />
            <Button onClick={() => void controller.detail(selectedPoint.key)}
              disabled={busy}>查询步行路线</Button>
          </> : <p className="wb-hint">在地图上点一个设施点位，可查询它的实际步行路线。</p>}
          {(selectedPoint || state.route || state.routeError)
            && <RouteDetail route={state.route} error={state.routeError} />}
        </section>}

        {notes.length > 0 && <details className="wb-sec wb-more">
          <summary>证据说明 · {notes.length} 条</summary>
          <ul className="checkup-notes">{notes.map(note => <li key={note.text}>{note.text}</li>)}</ul>
        </details>}

        <footer className="wb-fine">
          {view?.quota.label && <p data-testid="quota-label">
            {/* 余额的说法逐字来自后端：它只算本应用自己的额度。 */}
            {view.quota.label}</p>}
          {view && view.quota.lines.map(line => <p key={line}>{line}</p>)}
          <p>未知不等于不可达：灰区只覆盖"路网与检索数据都足够、却仍超出服务标准"的连片区域。评分区间是上界与下界，不是一个确定的百分比。</p>
        </footer>
      </div>
    </aside>

    <Drawer title="体检报告" open={reportOpen} onClose={() => setReportOpen(false)} size={760}>
      {snapshot ? <CheckupReport snapshot={snapshot} stale={stale} waterReviews={view?.waterReviews} />
        : <p className="api-muted">体检完成后在这里显示报告。</p>}
    </Drawer>
  </main>;
}

