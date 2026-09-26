/**
 * v2 体检报告：覆盖率区间、灰区清单与证据说明。
 *
 * 这个组件**不含判断**：每个数字、每条建议、每句限制说明都来自冻结的那一版修订，这里
 * 只负责排版（`report.ts` 负责翻译）。所以它没有分支去"算一下"—— 一旦这里出现
 * `coveredM2 / domainAreaM2`，判据就有了第二处，而第二处迟早会和第一处不一样。
 *
 * 三处措辞是被刻意写死的，改动前要先想清楚：
 * - 缺空间支持的类别显示"无法给出覆盖率"，不显示 0%；
 * - 没做过覆盖评估时显示"未评估"，不显示"0 处灰区"；
 * - 核验未接入时显示"模型推定"，不显示"未发现问题"。
 */
import { useEffect, useMemo, useRef } from 'react';
import { Alert, Tag } from 'antd';
import * as echarts from 'echarts/core';
import { BarChart, RadarChart } from 'echarts/charts';
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components';
import { SVGRenderer } from 'echarts/renderers';
import type { CheckupSnapshot } from './contract';
import {
  area, coverageBars, coverageItems, evidenceNotes, gapSummary, overallView, percent,
  radarView, verificationView, type CoverageBar, type CoverageItem, type GapSummary, type RadarView,
} from './report';
import './checkup.css';

echarts.use([BarChart, RadarChart, GridComponent, LegendComponent, TooltipComponent, SVGRenderer]);

function useChart(option: unknown, height: number, label: string) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current, undefined, { renderer: 'svg' });
    chart.setOption(option as never);
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(ref.current);
    return () => { observer.disconnect(); chart.dispose(); };
  }, [option]);
  return <div ref={ref} style={{ height, width: '100%' }} role="img" aria-label={label} />;
}

/** 覆盖率区间：一根柱从下界画到上界。柱长为零的类别不是 0%，是"无法确定"。 */
function CoverageBarChart({ bars }: { bars: CoverageBar[] }) {
  // 图表配置按数据记忆：每次渲染都新建一个 option 会让 effect 重跑、把整张图重建一遍，
  // 用户看到的是一次无故的重绘（选中态、缩放都会被重置）。
  const option = useMemo(() => ({
    animation: false,
    grid: { left: 64, right: 40, top: 8, bottom: 24 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' },
      formatter: (params: Array<{ dataIndex: number }>) => {
        const bar = bars[params[0]?.dataIndex ?? 0];
        if (!bar) return '';
        return bar.available
          ? `${bar.label}：${bar.lower.toFixed(1)}% ～ ${(bar.lower + bar.span).toFixed(1)}%`
          : `${bar.label}：无法确定覆盖率`;
      } },
    xAxis: { type: 'value', min: 0, max: 100, axisLabel: { formatter: '{value}%', color: '#8b979f',
      fontSize: 10 }, splitLine: { lineStyle: { color: '#eef1f3' } } },
    yAxis: { type: 'category', inverse: true, data: bars.map(bar => bar.label),
      axisTick: { show: false }, axisLine: { show: false },
      axisLabel: { color: '#667680', fontSize: 11 } },
    series: [
      { type: 'bar', stack: 'interval', silent: true, barWidth: 12,
        itemStyle: { color: 'transparent' }, data: bars.map(bar => bar.lower) },
      { type: 'bar', stack: 'interval', barWidth: 12,
        data: bars.map(bar => ({ value: bar.span, itemStyle: { color: bar.color, borderRadius: 3 } })) },
    ],
  }), [bars]);
  return useChart(option, 148, `覆盖率区间柱状图：${bars.map(bar => bar.available
    ? `${bar.label}${bar.lower.toFixed(1)}%到${(bar.lower + bar.span).toFixed(1)}%`
    : `${bar.label}无法确定`).join('，')}`);
}

/** 雷达图：两条线分别是区间的下界与上界，中间那片就是"还不能确定"的范围。 */
function CoverageRadar({ view }: { view: RadarView }) {
  const option = useMemo(() => ({
    animation: false,
    legend: { bottom: 0, itemWidth: 12, itemHeight: 8, textStyle: { fontSize: 11, color: '#667680' } },
    tooltip: {},
    radar: { indicator: view.indicators, radius: '62%', center: ['50%', '46%'],
      axisName: { color: '#667680', fontSize: 11 },
      splitLine: { lineStyle: { color: '#e3ebeb' } }, splitArea: { show: false } },
    series: [{ type: 'radar', symbolSize: 4, data: view.series.map((series, index) => ({
      name: series.name, value: series.values,
      lineStyle: { color: index === 0 ? '#147d70' : '#c78b36' },
      itemStyle: { color: index === 0 ? '#147d70' : '#c78b36' },
      areaStyle: { opacity: index === 0 ? 0.18 : 0.08,
        color: index === 0 ? '#147d70' : '#c78b36' },
    })) }],
  }), [view]);
  return useChart(option, 260, `覆盖率雷达图：${view.series.map(series =>
    `${series.name}${view.indicators.map((indicator, index) =>
      `${indicator.name}${series.values[index]}%`).join('、')}`).join('；')}`);
}

function IntervalCard({ item, domainAreaM2 }: { item: CoverageItem; domainAreaM2: number | null }) {
  return <article className="checkup-card" data-testid={`coverage-${item.category}`}>
    <header>
      <i className="checkup-dot" style={{ background: item.color }} />
      <strong>{item.label}</strong>
      {item.evidenceGrade && <Tag>{item.evidenceGrade === 'verified' ? '已核验' : '模型推定'}</Tag>}
    </header>
    {!item.supported ? <p className="checkup-muted">无法给出覆盖率：{item.unavailableReason
      ? `${item.unavailableReason}` : '缺少空间支持'}。未知不等于"没有设施"。</p>
    : <>
      <p className="checkup-interval"><b>{percent(item.lowerPct)}</b> ～ <b>{percent(item.upperPct)}</b></p>
      <dl className="checkup-facts">
        <dt>最低覆盖率</dt><dd>C / A = {percent(item.lowerPct)}</dd>
        <dt>最高覆盖率</dt><dd>(C + U) / A = {percent(item.upperPct)}</dd>
        <dt>可评估比例</dt><dd>{percent(item.assessablePct)}</dd>
        <dt>未知比例</dt><dd>{percent(item.unknownPct)}</dd>
        <dt>已覆盖 / 缺口 / 未知</dt>
        <dd>{area(item.coveredM2)} / {area(item.gapM2)} / {area(item.unknownM2)}</dd>
        <dt>评估域</dt><dd>{area(domainAreaM2)}</dd>
      </dl>
      {item.degenerate && <p className="checkup-muted">区间退化为一个点：本类别的每一格都已判定，
        没有未知面积 —— 这不是精度更高，只是恰好没有留下没查到的部分。</p>}
    </>}
  </article>;
}

function ZoneList({ gaps }: { gaps: GapSummary }) {
  if (!gaps.assessed) return <Alert type="warning" showIcon
    title="本次未进行服务覆盖评估，因此没有灰区清单"
    description="灰区面积与条数都需要评估域和步行路网；缺少其中之一时就只能写“未评估”，不能写成 0。" />;
  return <>
    <p className="checkup-muted">灰区合计 {gaps.gapAreaText}（其中至少两类同时缺失的综合灰区
      {gaps.compositeAreaText}）；按类别：{gaps.byCategory.length === 0 ? '无'
      : gaps.byCategory.map(row => `${row.label} ${row.areaText}`).join('，')}。
      {gaps.unlabelled > 0 && `另有 ${gaps.unlabelled} 处面积小于标注阈值${gaps.minLabelAreaM2
        ? `（${area(gaps.minLabelAreaM2)}）` : ''}，地图上不标注，但仍在灰区面积内。`}</p>
    {gaps.zones.length === 0 ? <p className="checkup-muted">本次评估没有产生灰区。</p>
      : <ol className="checkup-zones">{gaps.zones.map(zone => <li key={zone.id}
        data-testid={`zone-${zone.id}`}>
        <header><b>{zone.title}</b><Tag>{zone.kindLabel}</Tag>
          <span className="checkup-zone-area">{zone.areaText}</span>
          {!zone.labelled && <Tag>地图不标注</Tag>}
          {zone.queryStatus !== 'complete' && <Tag color="orange">设施检索未完成</Tag>}</header>
        <p className="checkup-muted">涉及类别：{zone.categoryLabels.join('、')}；几何分量
          {zone.parts} 片；证据等级 {zone.evidenceGrade === 'verified' ? '已核验' : '模型推定'}。
          {zone.reasonLabel && `原因：${zone.reasonLabel}。`}
          {zone.nearestFacility && `最近设施：${zone.nearestFacility}。`}</p>
        <p>{zone.suggestion}</p>
      </li>)}</ol>}
    {gaps.notes.map((note, index) => <p key={index} className="checkup-muted">{note}</p>)}
  </>;
}

export function CheckupReport({ snapshot, stale }: { snapshot: CheckupSnapshot; stale?: boolean }) {
  // 按修订记忆：`snapshot` 的身份只在取到新的一版时变化，所以这一组派生值在一次体检
  // 里是稳定的，图表也就不会因为父组件重渲染而重建。
  const items = useMemo(() => coverageItems(snapshot), [snapshot]);
  const bars = useMemo(() => coverageBars(items), [items]);
  const radar = useMemo(() => radarView(items), [items]);
  const overall = useMemo(() => overallView(snapshot), [snapshot]);
  const gaps = useMemo(() => gapSummary(snapshot), [snapshot]);
  const verification = useMemo(() => verificationView(snapshot), [snapshot]);
  const notes = useMemo(() => evidenceNotes(snapshot), [snapshot]);
  const domainAreaM2 = snapshot.accessibility?.domainAreaM2 ?? snapshot.report?.domainAreaM2 ?? null;

  return <article className="checkup-report" data-testid="checkup-report">
    <div className="checkup-eyebrow">COMMUNITY CHECKUP / 服务覆盖体检</div>
    <h1>15 分钟生活圈体检报告</h1>
    {stale && <Alert type="warning" showIcon title="条件已修改，本报告仍属于原中心点的那一次体检。" />}
    {snapshot.businessStatus !== 'complete' && <Alert type="warning" showIcon
      title={snapshot.businessStatus === 'insufficient' ? '证据不足：结论只覆盖已评估的部分'
        : '部分结果：有阶段未能完成，缺失的部分按"未知"计，不计入覆盖率。'} />}
    <dl className="checkup-facts">
      <dt>任务 / 修订</dt><dd>{snapshot.taskId} · 第 {snapshot.revision} 版</dd>
      <dt>中心点</dt><dd>{snapshot.center.lng.toFixed(6)}, {snapshot.center.lat.toFixed(6)}</dd>
      <dt>生成时间</dt>
      <dd>{new Date(snapshot.generatedAt * 1000).toLocaleString('zh-CN', { hour12: false })}</dd>
      <dt>算法</dt><dd>{snapshot.engine.label}（{snapshot.engine.engineId}）</dd>
      <dt>判定依据</dt><dd>步行距离 {snapshot.rules.threshold_m} 米 ·
        {snapshot.rules.metric === 'walking_route' ? '步行路线' : snapshot.rules.metric} ·
        坐标系 BD09LL</dd>
      <dt>结果指纹</dt><dd>{snapshot.trace.resultHash}</dd>
    </dl>

    <h2>01 / 总体覆盖率区间</h2>
    {overall.available ? <>
      <p className="checkup-interval checkup-overall">
        <b>{percent(overall.lowerPct)}</b> ～ <b>{percent(overall.upperPct)}</b></p>
      <p className="checkup-muted">下界是"已知覆盖"，上界是"已知覆盖 + 未知面积"，两者之间
        就是本次路面证据说不到的地方。可评估比例 {percent(overall.assessablePct)}，未知比例
        {percent(overall.unknownPct)}。</p>
    </> : <Alert type="info" showIcon title="本次不给总体覆盖率"
      description={`${overall.reason === 'categories_not_analysed'
        ? '只评估了部分大类：重新加权成"三类总分"会让人以为三类都评估过了。'
        : overall.reason === 'category_without_spatial_support'
          ? '有大类缺少空间支持：缺图的大类不是低覆盖率，是不能加权。'
          : '评分阶段未产出总体分。'}${overall.missingCategories.length > 0
        ? `涉及：${overall.missingCategories.join('、')}。` : ''}`} />}
    {overall.available && <div className="checkup-charts">
      <CoverageBarChart bars={bars} />
      {radar.available ? <CoverageRadar view={radar} />
        : <p className="checkup-muted">无法绘制雷达图：{radar.missing.join('、')}
          缺少区间上下界。补 0 会画成一个凹角，那比不画更容易被误读。</p>}
    </div>}

    <h2>02 / 分类覆盖区间</h2>
    <div className="checkup-cards">{items.map(item =>
      <IntervalCard key={item.category} item={item} domainAreaM2={domainAreaM2} />)}</div>

    <h2>03 / 服务盲区与灰区</h2>
    <ZoneList gaps={gaps} />

    <h2>04 / 现实核验</h2>
    <p className="checkup-muted" data-testid="verification-summary">{verification.summary}
      {verification.provider && `（${verification.provider}）`}</p>
    {verification.reason && <p className="checkup-muted">{verification.reason}</p>}
    {!verification.available && <p className="checkup-muted">没有核验不是"核验过、没问题"：
      设施的可达性判断来自路网模型，实地情况仍可能不同。</p>}

    <h2>05 / 证据说明</h2>
    <ul className="checkup-notes">{notes.map(note => <li key={note.key}
      className={note.level === 'warning' ? 'checkup-note-warning' : undefined}>{note.text}</li>)}</ul>
    {snapshot.warnings.length > 0 && <details><summary>算法质量标记（{snapshot.warnings.length} 条）</summary>
      {/* 这些标记往往共用同一个 code（一次 OSM 体检有五条 ALGORITHM_WARNING），只用 code
          当 key 会让 React 认不出孩子，而这一块的意义就是"一条不少"，所以按位置认。 */}
      <ul className="checkup-notes">{snapshot.warnings.map((warning, index) => <li key={index}>
        [{warning.severity}] {warning.code}：{warning.message}（{warning.scope}）</li>)}</ul>
    </details>}
    <details><summary>查看机器可读修订</summary><pre>{JSON.stringify(snapshot, null, 2)}</pre></details>
  </article>;
}
