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
import { CheckupController } from './controller';
import { budgetFor, capabilityView, type CapabilityView } from './capabilities';
import { isCheckupBusy, STAGE_LABELS } from './types';
import type { CheckupState } from './types';
import { LAYER_IDS, type LayerId, type Stage } from './validate';
import { drawableLayer, LAYER_STYLES, type LayerDrawable } from './layers';
import { CheckupMap, DEFAULT_CHECKUP_LAYERS, type CheckupLayerToggles } from './CheckupMap';
import { CheckupReport } from './CheckupReport';
import { evidenceNotes, percent } from './report';
// 复用旧分析页的排布类：工作台与它是同一个版式，另起一套只会让两页慢慢长歪。
import '../analysis/api.css';
import './checkup.css';

/** 地图上的六层；报告是文档，不占图层开关，从这里打开。 */
const MAP_LAYERS = LAYER_IDS.filter(id => id !== 'report') as LayerId[];

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

export default function CheckupApp() {
  const service = useMemo(() => createCheckupService(), []);
  const [center, setCenter] = useState<Center>({ lng: 116.404, lat: 39.915 });
  const [lng, setLng] = useState<number | null>(116.404);
  const [lat, setLat] = useState<number | null>(39.915);
  const [engine, setEngine] = useState<string | null>(null);
  const [budget, setBudget] = useState<number | null>(null);
  const [view, setView] = useState<CapabilityView | null>(null);
  const [capabilityError, setCapabilityError] = useState<string | null>(null);
  const [state, setState] = useState<CheckupState>({ phase: 'idle' });
  const [toggles, setToggles] = useState<CheckupLayerToggles>({ ...DEFAULT_CHECKUP_LAYERS });
  const [layerErrors, setLayerErrors] = useState<Partial<Record<LayerId, string>>>({});
  /** 哪一层在哪一版上失败过：同一版不反复重试，换版再试。 */
  const [failed, setFailed] = useState<Partial<Record<LayerId, number>>>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [reportOpen, setReportOpen] = useState(false);
  const controller = useRef<CheckupController | null>(null);

  useEffect(() => {
    const instance = new CheckupController(service, setState);
    controller.current = instance;
    return () => { instance.dispose(); controller.current = null; };
  }, [service]);

  useEffect(() => {
    const abort = new AbortController();
    service.capabilities(abort.signal).then(value => {
      const next = capabilityView(value);
      setView(next);
      setEngine(current => current ?? next.defaultEngine);
      setBudget(current => current ?? next.defaultBudget);
    }).catch((error: unknown) => {
      // 取消不是故障：组件已经卸载或重新挂载，这一轮的结果不该再上屏。
      if (abort.signal.aborted) return;
      setCapabilityError(error instanceof CheckupError ? error.message
        : '未能读取体检服务能力表，请检查服务地址后刷新页面');
    });
    return () => abort.abort();
  }, [service]);

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
    const pending = MAP_LAYERS.find(id => toggles[id]
      && state.layers?.[id]?.revision !== revision && failed[id] !== revision);
    if (pending === undefined) return;
    controller.current?.layer(pending).catch((error: unknown) => {
      setLayerErrors(previous => ({ ...previous, [pending]: error instanceof CheckupError
        ? error.message : '该图层未能加载' }));
      setFailed(previous => ({ ...previous, [pending]: revision }));
    });
  }, [state.phase, state.layers, revision, toggles, failed]);

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

  const selectedPoint = useMemo(() => selected === null ? null
    : (drawables.facilities?.points ?? []).find(item => item.key === selected) ?? null,
  [drawables, selected]);

  const valid = lng !== null && lat !== null && Number.isFinite(lng) && Number.isFinite(lat)
    && lng >= -180 && lng <= 180 && lat > -85 && lat < 85;
  const engines = view?.engines ?? [];
  const selectedEngine = engines.find(item => item.engineId === engine) ?? null;
  const stale = snapshot !== undefined && (snapshot.center.lng !== center.lng
    || snapshot.center.lat !== center.lat || snapshot.engine.engineId !== engine);

  function choose(next: Center) {
    void controller.current?.reset();
    setCenter(next); setLng(next.lng); setLat(next.lat); setSelected(null);
  }
  function edit(axis: 'lng' | 'lat', value: number | null) {
    void controller.current?.reset();
    if (axis === 'lng') setLng(value); else setLat(value);
  }
  function start() {
    if (!valid || busy || !engine) return;
    const next = { lng: +lng!.toFixed(6), lat: +lat!.toFixed(6) };
    setCenter(next); setLng(next.lng); setLat(next.lat); setSelected(null);
    setLayerErrors({});
    void controller.current?.start({ center: next, engine,
      ...(budget === null ? {} : { budget }) });
  }
  useEffect(() => { if (state.phase === 'completed') setReportOpen(true); }, [state.phase]);

  const overall = snapshot?.report?.overall ?? snapshot?.scores?.overall ?? null;
  const notes = useMemo(() => snapshot ? evidenceNotes(snapshot) : [], [snapshot]);

  return <div className="api-app">
    <header className="api-header"><div><span className="api-brand">15</span><div>
      <h1>15 分钟生活圈 · 体检</h1><p>服务覆盖、灰区与评分区间（v2 修订）</p></div></div>
      <Tag color="geekblue">checkup-v1</Tag></header>
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
          <label className="api-label">引擎<Select aria-label="引擎" value={engine ?? undefined}
            placeholder={view ? undefined : '正在读取能力表'} loading={!view && !capabilityError}
            onChange={next => { void controller.current?.reset(); setEngine(next);
              // 档位是引擎自己的：换引擎必须换到它自己的默认档，沿用上一个会撞 422。
              if (view) setBudget(budgetFor(view, next)); }}
            options={engines.map(item => ({ value: item.engineId, label: item.label }))} /></label>
          <label className="api-label">等时圈采样预算<Select aria-label="调用预算"
            value={budget ?? undefined} placeholder="—"
            onChange={setBudget}
            options={(selectedEngine?.budgets ?? []).map(value => ({ value, label: `${value} 次` }))} /></label>
          <p className="api-muted">步行阈值 900 秒 · 服务标准 1000 米 · 坐标系 BD09LL</p>
          {selectedEngine?.engineVersion && <p className="api-muted">引擎版本 {selectedEngine.engineVersion}</p>}
          {selectedEngine?.caveat && <Alert type="warning" title={selectedEngine.caveat} />}
          <Space wrap>
            <Button aria-label="开始体检" type="primary" onClick={start}
              disabled={!valid || busy || !engine}>开始体检</Button>
            {(busy || (state.phase === 'error' && task)) &&
              <Button onClick={() => void controller.current?.cancel()}
                disabled={state.phase === 'cancelling'}>取消任务</Button>}
            {state.phase === 'error' && task && <Button aria-label="重试"
              onClick={() => void controller.current?.retry()}>重试</Button>}
          </Space>
        </Card>
        <Card title="地图图层">
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
      <section className="api-map-section">
        {snapshot && stale && <Alert type="warning" showIcon
          title="条件已修改。地图与报告仍是上一次体检的结果，需重新体检才会更新。" />}
        <CheckupMap center={center} onPick={choose} resultCenter={snapshot?.center}
          layers={toggles} drawables={drawables} selectedId={selected} onSelect={setSelected} />
      </section>
      <section className="api-results" aria-label="体检结果">
        <Card title="体检进度">
          {state.phase === 'idle' && <p className="api-muted">选好中心与引擎后开始体检。结果包含覆盖区间、服务灰区与真实步行核验。</p>}
          {task && <>
            <StageProgress stage={task.stage} />
            <Descriptions size="small" column={1} items={[
              { key: 'task', label: '任务', children: task.taskId },
              { key: 'revision', label: '当前修订', children: `第 ${task.revision} 版` },
              { key: 'status', label: '状态', children: task.status +
                (task.businessStatus ? `（${BUSINESS_LABELS[task.businessStatus] ?? task.businessStatus}）` : '') },
              { key: 'spend', label: '本任务已用', children: `${task.networkRequests} 次网络尝试 / 预算 ${task.budget}` },
              { key: 'elapsed', label: '已用时', children: `${task.elapsedSeconds.toFixed(1)} 秒` },
            ]} />
          </>}
          {state.phase === 'cancelled' && <Alert type="info" title="任务已取消"
            description="取消只停住后续请求；已经发出的调用仍会计入本应用的预算账本。" />}
          {state.error && <Alert type="error" title={state.error} showIcon
            action={<Button aria-label="重试" size="small"
              onClick={() => void controller.current?.retry()}>重试</Button>} />}
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
            <Button onClick={() => void controller.current?.detail(selectedPoint.key)}
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
      {snapshot ? <CheckupReport snapshot={snapshot} stale={stale} />
        : <p className="api-muted">体检完成后在这里显示报告。</p>}
    </Drawer>
  </div>;
}
