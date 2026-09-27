import { useEffect, useState } from 'react';
import { Alert, Button, Card, Checkbox, InputNumber, Select, Space, Tag } from 'antd';
import type { Center } from '../types';
import type { HybridResultResponse } from '../api-contract';
import { ApiMap, type Layers } from '../analysis/ApiMap';
import { LocationControls } from '../analysis/LocationControls';
import '../analysis/api.css';
import { getHybridSession, saveHybridSession } from '../algorithmSessions';
import { hybridTasks, useLegacyState } from '../legacyTasks';
import { isLegacyBusy } from '../legacyController';

const quality = { usable: '可用', partial: '部分结果', insufficient: '证据不足' };
const stopReasons: Record<string, string> = { budget: '达到验证预算', deadline: '达到时间上限',
  refinement_complete: '完成本轮细化', no_candidates: '没有可继续核验的候选点',
  synthetic_contract_fixture: '离线演示数据', cancelled: '已取消', upstream_failure: '步行服务暂不可用' };
const taskLabels = { running: '运行中', cancelling: '取消中', cancelled: '已取消', completed: '已完成', failed: '失败' };

export default function HybridApp() {
  const session = getHybridSession();
  // 任务在模块里的控制器上：这个组件卸载（换页面、换算法）只是退订，任务照跑。
  const tasks = hybridTasks();
  const controller = tasks.controller;
  const state = useLegacyState(tasks);
  const [center, setCenter] = useState<Center>(session.center);
  const [lng, setLng] = useState<number | null>(session.lng);
  const [lat, setLat] = useState<number | null>(session.lat);
  const [budget, setBudget] = useState(session.budget);
  const [result, setResult] = useState<HybridResultResponse | undefined>(() => session.result
    ?? (state.phase === 'completed' ? state.result : undefined));
  const [layers, setLayers] = useState<Layers>(session.layers);
  useEffect(() => {
    if (state.phase === 'completed' && state.result) setResult(state.result);
  }, [state.phase, state.result]);
  useEffect(() => {
    saveHybridSession({ center, lng, lat, budget, result, layers });
  }, [center, lng, lat, budget, result, layers]);
  const task = state.task;
  const busy = isLegacyBusy(state);
  const live = controller.hasLiveTask;
  const valid = lng !== null && lat !== null && Number.isFinite(lng) && Number.isFinite(lat)
    && lng >= -180 && lng <= 180 && lat > -85 && lat < 85;
  // "条件已修改"按内容算：左栏的中心或预算与地图上这份结果的不同。
  const dirty = !!result && (lng === null || lat === null || result.center.lng !== +lng.toFixed(6)
    || result.center.lat !== +lat.toFixed(6) || result.isochrone.config.max_baidu_requests !== budget);
  // 选点、改坐标、改预算只改左栏的草稿，不碰正在跑的任务。
  function pick(next: Center) {
    setCenter(next); setLng(next.lng); setLat(next.lat);
  }
  function start() {
    if (!valid || busy || live) return;
    const origin = { lng: +lng!.toFixed(6), lat: +lat!.toFixed(6) };
    setCenter(origin); setLng(origin.lng); setLat(origin.lat);
    void controller.start({ center: origin, budget });
  }
  const core = result?.isochrone;
  return <div className="api-app">
    <header className="api-header"><div><span className="api-brand">15</span><div><h1>15 分钟生活圈</h1><p>OSM 路网＋百度步行验证</p></div></div><Tag color="teal">Hybrid v1.5</Tag></header>
    <main className="api-layout">
      <section className="api-controls" aria-label="分析条件">
        <Card title="选择分析中心">
          <p className="api-muted">默认使用项目固定测试点。请在已配置的 OSM 数据覆盖范围内选点。</p>
          <LocationControls center={center} onPick={pick} />
          <label className="api-label">经度<InputNumber aria-label="经度" value={lng} onChange={setLng} /></label>
          <label className="api-label">纬度<InputNumber aria-label="纬度" value={lat} onChange={setLat} /></label>
          <label className="api-label">百度验证预算<Select aria-label="百度验证预算" value={budget} onChange={setBudget} options={[200, 400].map(value => ({ value, label: `${value} 次` }))} /></label>
          <p className="api-muted">步行阈值 900 秒 · 坐标系 BD09LL</p>
          {!valid && <Alert type="error" title="请输入有效经纬度" />}
          <Space><Button type="primary" aria-label="开始分析" disabled={!valid || busy || live} loading={busy} onClick={start}>开始分析</Button>
            {live && <Button onClick={() => void controller.cancel()} disabled={state.phase === 'cancelling'}>取消任务</Button>}</Space>
          {live && <p className="api-muted" data-testid="legacy-live-note">当前任务尚未结束：切换页面、切换算法或刷新都不会取消它。
            要按新条件分析，请等它完成，或先点"取消任务"。</p>}
        </Card>
        <Card title="地图图层"><Checkbox checked={layers.reachable} onChange={e => setLayers({ ...layers, reachable: e.target.checked })}>15 分钟圈外轮廓</Checkbox><p className="api-muted">仅展示外轮廓，圈内不代表每处均可步行到达。</p></Card>
        <Alert type="info" title="设施统计尚未接入" description="当前仅展示生活圈及步行验证证据，不将空设施列表解释为缺少服务。" />
      </section>
      <section className="api-map-section" data-testid="legacy-map-section" data-task-id={result?.taskId ?? ''}>
        {dirty && result && <Alert type="warning" title="条件已修改，地图仍显示上次分析结果" />}
        <ApiMap center={center} onPick={pick} layers={layers} resultCenter={result?.center}
          result={core ? { geometry: core.geometry, outlineOnly: true,
            displayGeometry: core.displayGeometry ?? core.geometry, unknownRegion: core.unknown_region,
            uncertainRegion: null, computationExtent: core.computation_extent } : undefined} />
      </section>
      <section className="api-results" aria-label="分析结果"><Card title="分析结果">
        {state.connection === 'lost' && <Alert type="warning" showIcon data-testid="legacy-connection"
          title="与分析服务的连接中断，正在自动重连"
          description="任务仍在服务端继续，不会因为断网被取消或重复提交；连上后自动接着显示进度与结果。" />}
        {state.phase === 'restoring' && <Alert type="info" showIcon title="正在向后端核对上次的分析任务" />}
        {state.notice && <Alert type="info" showIcon data-testid="legacy-notice" title={state.notice} />}
        {state.error && <Alert type="error" title={state.error} data-testid="legacy-error" action={<Button aria-label="重试" size="small"
          onClick={() => void controller.retry()}>{state.recovery === 'unconfirmed' ? '重新提交'
            : state.recovery === 'expired' || task?.status === 'failed' ? '重新分析' : '重试'}</Button>} />}
        {state.phase === 'cancelled' && <Alert type="info" title="任务已取消" description="取消只停住后续请求；已经发出的调用仍计入步行服务额度。" />}
        {task && <p role="status" data-testid="legacy-phase" data-phase={state.phase}>任务 <span data-testid="legacy-task-id">{task.taskId}</span>：{taskLabels[task.status]} · 调用 {task.requests}/{task.budget} · {task.elapsedSeconds.toFixed(1)} 秒
          {state.input && <><br />中心 {state.input.center.lng.toFixed(6)}, {state.input.center.lat.toFixed(6)} · 预算 {state.input.budget} 次</>}</p>}
        {!task && state.phase === 'submitting' && <p role="status" data-testid="legacy-phase" data-phase={state.phase}>正在提交任务…</p>}
        {!task && state.phase === 'idle' && <p>选择中心后开始分析。</p>}
        {core && <>
          <Alert type="warning" title={`结果质量：${quality[core.quality]}`} description="任务完成不等于独立精度验收通过；推断填充与已核验证据需区别解读。" />
          {core.readiness.mode === 'degraded' && <Alert type="warning" title="OSM 或辅助数据不完整，当前为降级结果" description="路网、覆盖边界或辅助图层未全部就绪，请结合证据范围使用结果。" />}
          {core.extent_truncated && <Alert type="warning" title="生活圈可能超出计算范围" />}
          <dl className="api-statistics"><dt>有效百度样本</dt><dd>{core.valid_baidu_samples}</dd><dt>无效样本</dt><dd>{core.invalid_baidu_samples}</dd><dt>未知样本</dt><dd>{core.unknown_samples}</dd><dt>停止原因</dt><dd>{stopReasons[core.stop_reason] || '本轮分析已结束，详见完整结果'}</dd><dt>耗时</dt><dd>{core.timing_seconds.task_total.toFixed(1)} 秒</dd><dt>结果中心</dt><dd>{result!.center.lng}, {result!.center.lat}</dd></dl>
          {core.warnings.length > 0 && <Alert type="warning" title="结果包含证据或覆盖范围限制" description="完整结果保留全部质量提示和证据图层，未知区域不应当作已确认不可达。" />}
          <details><summary>查看完整分析报告与证据</summary><pre>{JSON.stringify(result, null, 2)}</pre></details>
        </>}
      </Card></section>
    </main>
    <footer className="api-footer">OSM 提供路网参考，百度路线提供步行核验；未知区域不代表不可达。</footer>
  </div>;
}
