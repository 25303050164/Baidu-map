import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Card, Checkbox, InputNumber, Select, Space, Tag } from 'antd';
import type { Center } from '../types';
import { createApiService } from './service';
import { AnalysisController } from './controller';
import type { AnalysisState, Budget, Isochrone } from './types';
import { ApiMap, type Layers } from './ApiMap';
import { geometryMessage } from './geometry';
import './api.css';

const stages: Record<string, string> = { initializing: '初始化采样', expanding: '检查并扩展范围', exploring: '探索采样', refining: '边界细化与补测', reconstructing: '重建时间场与几何', completed: '分析完成', cancelled: '已取消', cancelling: '正在取消', failed: '分析失败' };
const reasons: Record<string, string> = { budget: '达到调用预算', deadline: '达到截止时间', resolution_limit: '达到网格分辨率或完成边界检查', maximum_range: '达到最大范围', permission: '步行接口权限异常', quota: '服务配额不足', invalid_parameter: '上游参数被拒绝', upstream_failure: '连续上游故障', geometry_error: '几何重建失败' };
const warnings: Record<string, string> = { range_unknown: '外缘存在未知样本，范围尚未核实', range_truncated: '可达边界可能被计算范围截断', unfinished_boundary: '部分边界尚未完成细化', endpoints_unverified: '部分路线端点尚未核验', geometry_error: '几何重建失败' };

function ResultSummary({ result }: { result: Isochrone }) {
  return <>
    <Alert type={result.quality === 'usable' ? 'success' : 'warning'} title={geometryMessage(result.geometry)} description={`质量：${{ usable: '可用', partial: '部分结果', insufficient: '证据不足' }[result.quality]}`} showIcon />
    <dl className="api-statistics">
      <dt>停止原因</dt><dd>{reasons[result.stopReason] || result.stopReason}</dd>
      <dt>Provider 调用</dt><dd>{result.statistics.requests} 次</dd>
      <dt>真实网络调用</dt><dd>{result.statistics.network_requests} 次</dd>
      <dt>重试</dt><dd>{result.statistics.retries} 次</dd>
      <dt>未知面积</dt><dd>{(result.statistics.unknown_area / 1e6).toFixed(3)} 平方公里</dd>
      <dt>未完成边界格</dt><dd>{result.statistics.unfinished_boundary}</dd>
      <dt>总耗时</dt><dd>{result.statistics.total_seconds.toFixed(2)} 秒</dd>
    </dl>
    {result.warnings.map(warning => <Alert key={warning} type="warning" title={warnings[warning] || warning} />)}
  </>;
}

export default function ApiApp() {
  const [center, setCenter] = useState<Center>({ lng: 116.404, lat: 39.915 });
  const [lng, setLng] = useState<number | null>(116.404);
  const [lat, setLat] = useState<number | null>(39.915);
  const [budget, setBudget] = useState<Budget>(400);
  const [state, setState] = useState<AnalysisState>({ phase: 'idle' });
  const [layers, setLayers] = useState<Layers>({ reachable: true, unknown: true, uncertain: true, extent: false });
  const controller = useRef<AnalysisController | null>(null);
  useEffect(() => {
    const instance = new AnalysisController(createApiService(), setState);
    controller.current = instance;
    return () => { instance.dispose(); controller.current = null; };
  }, []);
  const busy = ['submitting', 'running', 'cancelling'].includes(state.phase);
  const valid = lng !== null && lat !== null && Number.isFinite(lng) && Number.isFinite(lat) && lng >= -180 && lng <= 180 && lat > -85 && lat < 85;
  function choose(next: Center) {
    void controller.current?.reset();
    setCenter(next); setLng(next.lng); setLat(next.lat);
  }
  function edit(axis: 'lng' | 'lat', value: number | null) {
    void controller.current?.reset();
    if (axis === 'lng') setLng(value); else setLat(value);
  }
  function analyze() {
    if (!valid) return;
    const next = { lng: +lng!.toFixed(6), lat: +lat!.toFixed(6) };
    setCenter(next); setLng(next.lng); setLat(next.lat);
    void controller.current?.start({ center: next, budget });
  }
  return <div className="api-app">
    <header className="api-header"><div><span className="api-brand">15</span><div><h1>15 分钟生活圈</h1><p>自适应网格 · 步行等时圈分析</p></div></div><Tag color="teal">后端算法入口</Tag></header>
    <main className="api-layout">
      <section className="api-controls" aria-label="分析条件">
        <Card title="选择分析中心"><p className="api-muted">在地图上选点，或输入百度坐标。默认坐标仅用于选点起始位置，尚未进行社区实验验证。</p>
          <label className="api-label">经度<InputNumber aria-label="经度" value={lng} onChange={value => edit('lng', value)} precision={6} /></label>
          <label className="api-label">纬度<InputNumber aria-label="纬度" value={lat} onChange={value => edit('lat', value)} precision={6} /></label>
          <label className="api-label">调用预算<Select aria-label="调用预算" value={budget} onChange={value => { void controller.current?.reset(); setBudget(value); }} options={[200, 400, 800].map(value => ({ value, label: `${value} 次` }))} /></label>
          <p className="api-muted">步行阈值 900 秒 · 坐标系 BD09LL</p>
          {!valid && <Alert type="error" title="请输入有效坐标：经度 −180～180，纬度大于 −85 且小于 85" />}
          <Space wrap><Button type="primary" onClick={analyze} disabled={!valid || busy}>开始分析</Button>
            {(busy || (state.phase === 'error' && state.task)) && <Button onClick={() => void controller.current?.cancel()} disabled={state.phase === 'cancelling'}>取消任务</Button>}</Space>
        </Card>
        <Card title="地图图层"><div className="api-layer-list">{([['reachable', '可达区域'], ['unknown', '未知区域'], ['uncertain', '不确定区域'], ['extent', '计算范围']] as const).map(([key, label]) => <Checkbox key={key} checked={layers[key]} onChange={event => setLayers({ ...layers, [key]: event.target.checked })}><i className={`api-swatch ${key}`} />{label}</Checkbox>)}</div></Card>
        <Alert type="info" title="设施统计尚未接入" description="本轮只计算步行等时圈，不提供设施数量、服务覆盖率或设施盲区结论。" />
      </section>
      <section className="api-map-section"><ApiMap center={center} result={state.result?.isochrone} layers={layers} onPick={choose} /></section>
      <section className="api-results" aria-label="分析结果"><Card title="分析结果">
        {state.phase === 'idle' && <p className="api-muted">选择中心后开始分析。结果将展示可达区域及证据质量。</p>}
        {state.phase === 'submitting' && <p role="status">正在提交任务…</p>}
        {state.task && <div className="api-progress" role="status"><Tag color={state.task.dataSource === 'synthetic' ? 'orange' : 'green'}>{state.task.dataSource === 'synthetic' ? '合成数据 · 离线验收' : '百度步行数据'}</Tag><p>{stages[state.task.stage] || state.task.stage}</p><strong>{state.task.requests} / {state.task.budget} 次调用</strong><p>已用时 {state.task.elapsedSeconds.toFixed(1)} 秒</p></div>}
        {state.phase === 'cancelled' && <Alert title="任务已取消" type="info" />}
        {state.error && <Alert type="error" title={state.error} action={<Button aria-label="重试" size="small" onClick={() => void controller.current?.retry()}>重试</Button>} />}
        {state.result && <><ResultSummary result={state.result.isochrone} /><p className="api-muted">结果中心：{state.result.center.lng.toFixed(6)}, {state.result.center.lat.toFixed(6)}<br />{new Date(state.result.generatedAt * 1000).toLocaleString('zh-CN')}</p><details><summary>查看机器可读结果</summary><pre>{JSON.stringify(state.result, null, 2)}</pre></details></>}
      </Card></section>
    </main>
    <footer className="api-footer">质量提示随采样证据展示；未知区域不代表不可达。真实社区实验将在下一阶段开展。</footer>
  </div>;
}
