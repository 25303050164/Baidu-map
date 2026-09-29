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
import type { CSSProperties } from 'react';
import { useMemo } from 'react';
import { Alert, Tag } from 'antd';
import type { CheckupSnapshot } from './contract';
import {
  area, coverageItems, dataSourcesView, evidenceNotes, gapSummary, overallView, percent,
  verificationView, type CoverageItem, type DataSourcesView, type GapSummary,
} from './report';
import { outdatedText, recomputedText, versionView, type WaterReviewRef } from './water';
import './checkup.css';

/**
 * 区间条：实色是下界 C / A，斜线是未知 U / A（可能覆盖），空白是其余。
 * 只画后端给的两个百分比；不支持的类别画成虚线空条，不画成 0。
 */
function Meter({ lower, upper, color }: { lower: number | null; upper: number | null; color?: string }) {
  if (lower === null || upper === null) return <span className="wb-meter wb-meter-empty" aria-hidden="true" />;
  return <span className="wb-meter" aria-hidden="true"
    style={color ? { '--meter': color } as CSSProperties : undefined}>
    <i className="wb-meter-known" style={{ width: `${lower}%` }} />
    <i className="wb-meter-unknown" style={{ left: `${lower}%`, width: `${Math.max(0, upper - lower)}%` }} />
  </span>;
}

const SECTION_NUMBERS = ['一', '二', '三', '四', '五', '六'];
const Section = ({ index, title }: { index: number; title: string }) =>
  <h2><span className="rp-no">{SECTION_NUMBERS[index]}、</span>{title}</h2>;

/** 分类覆盖一行：区间、两个比例与三类面积；不支持的类别整行只说"无法给出"，不写 0%。 */
function CoverageRow({ item }: { item: CoverageItem }) {
  const name = <th scope="row">
    <span className="rp-cat"><i style={{ background: item.color }} />{item.label}</span>
    {item.evidenceGrade && <small>{item.evidenceGrade === 'verified' ? '已核验' : '模型推定'}</small>}
  </th>;
  if (!item.supported) return <tr data-testid={`coverage-${item.category}`}>{name}
    <td colSpan={4} className="rp-unsupported">无法给出覆盖率：{item.unavailableReason
      ? `${item.unavailableReason}` : '缺少空间支持'}。未知不等于"没有设施"。</td></tr>;
  return <tr data-testid={`coverage-${item.category}`}>{name}
    <td className="rp-interval">
      <span className="rp-num">{percent(item.lowerPct)} ～ {percent(item.upperPct)}</span>
      <Meter lower={item.lowerPct} upper={item.upperPct} color={item.color} />
    </td>
    <td className="rp-num" data-label="可评估">{percent(item.assessablePct)}</td>
    <td className="rp-num" data-label="未知">{percent(item.unknownPct)}</td>
    <td className="rp-areas">
      <span><em>已覆盖</em>{area(item.coveredM2)}</span>
      <span><em>缺口</em>{area(item.gapM2)}</span>
      <span><em>未知</em>{area(item.unknownM2)}</span>
    </td>
  </tr>;
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
        data-testid={`zone-${zone.id}`} data-status={zone.queryStatus}>
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

function DataSources({ view }: { view: DataSourcesView }) {
  return <section data-testid="report-data-sources">
    <p className="checkup-muted">{view.obstacle}</p>
    {view.available && view.reviews.length === 0 && <p className="checkup-muted">评估域不在任何水系复核范围内：
      水系按 OSM 原样计算，未与影像或第二家地图核对。</p>}
    {view.reviews.map(review => <article key={review.label} className="checkup-card"
      data-testid={`water-review-${review.label}`}>
      <header><strong>{review.title}</strong><Tag>{review.label}</Tag></header>
      <dl className="checkup-facts">
        {review.reviewedAt && <><dt>复核日期</dt><dd>{review.reviewedAt}</dd></>}
        {review.scope && <><dt>适用范围</dt><dd>{review.scope}</dd></>}
        {review.sources.length > 0 && <><dt>依据</dt><dd>{review.sources.join('；')}</dd></>}
        {review.method && <><dt>方法</dt><dd>{review.method}</dd></>}
        {review.reaches.length > 0 && <><dt>已核实河道</dt><dd>{review.reaches.join('；')}</dd></>}
        <dt>已核实桥梁</dt><dd>{review.crossings} 座</dd>
        <dt>底图误绘</dt><dd>{review.misdrawn} 处（已核实为陆地，按陆地计算）</dd>
        <dt>数据冲突／未知</dt><dd>{review.conflicts.length === 0 ? '无' : review.conflicts.join('；')}</dd>
      </dl>
      {review.limitations.length > 0 && <ul className="checkup-notes">{review.limitations.map(item =>
        <li key={item} className="checkup-note-warning">{item}</li>)}</ul>}
    </article>)}
    {view.areas && <p className="checkup-muted">{view.areas}</p>}
    {view.rejected.map(item => <p key={item} className="checkup-muted">未采用的复核：{item}</p>)}
  </section>;
}

const NO_REVIEWS: readonly WaterReviewRef[] = [];

export function CheckupReport({ snapshot, stale, waterReviews = NO_REVIEWS }: {
  snapshot: CheckupSnapshot;
  stale?: boolean;
  /** 当前部署采用的水系复核（能力表）；用来认出早于复核的旧版本。 */
  waterReviews?: readonly WaterReviewRef[];
}) {
  // 按修订记忆：`snapshot` 的身份只在取到新的一版时变化，所以这一组派生值在一次体检
  // 里是稳定的，图表也就不会因为父组件重渲染而重建。
  const items = useMemo(() => coverageItems(snapshot), [snapshot]);
  const overall = useMemo(() => overallView(snapshot), [snapshot]);
  const gaps = useMemo(() => gapSummary(snapshot), [snapshot]);
  const verification = useMemo(() => verificationView(snapshot), [snapshot]);
  const notes = useMemo(() => evidenceNotes(snapshot), [snapshot]);
  const sources = useMemo(() => dataSourcesView(snapshot), [snapshot]);
  const version = useMemo(() => versionView(snapshot, waterReviews), [snapshot, waterReviews]);
  const domainAreaM2 = snapshot.accessibility?.domainAreaM2 ?? snapshot.report?.domainAreaM2 ?? null;

  return <article className="checkup-report" data-testid="checkup-report"
    data-task-id={snapshot.taskId} data-revision={snapshot.revision}>
    <p className="rp-kicker">社区服务覆盖体检 · 第 {snapshot.revision} 版 · {snapshot.engine.label}</p>
    <h1>15 分钟生活圈体检报告</h1>
    {stale && <Alert type="warning" showIcon title="条件已修改，本报告仍属于原中心点的那一次体检。" />}
    {version.outdatedBy.length > 0 && <Alert type="warning" showIcon data-testid="report-outdated"
      title="旧版本：水系数据已修订" description={outdatedText(version)} />}
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
      <dt>水系数据</dt><dd data-testid="report-water-version">{version.applied === null ? '早于水系复核（无记录）'
        : version.applied.length === 0 ? '未采用复核（OSM 原样）' : version.applied.join('、')}</dd>
      {version.recomputed && <><dt>版本来源</dt>
        <dd data-testid="report-recomputed">{recomputedText(version.recomputed)}</dd></>}
    </dl>

    <Section index={0} title="总体覆盖率区间" />
    {overall.available ? <div className="rp-overall">
      <p className="checkup-interval checkup-overall">
        <b>{percent(overall.lowerPct)}</b><span>～</span><b>{percent(overall.upperPct)}</b></p>
      <Meter lower={overall.lowerPct} upper={overall.upperPct} />
      <p className="wb-meter-key"><i className="k-known" />已知覆盖（下界）<i className="k-unknown" />未知面积
        <span>可评估 {percent(overall.assessablePct)} · 未知 {percent(overall.unknownPct)}</span></p>
      <p className="checkup-muted">下界是"已知覆盖"，上界是"已知覆盖 + 未知面积"，两者之间
        就是本次路面证据说不到的地方。</p>
    </div> : <Alert type="info" showIcon title="本次不给总体覆盖率"
      description={`${overall.reason === 'categories_not_analysed'
        ? '只评估了部分大类：重新加权成"三类总分"会让人以为三类都评估过了。'
        : overall.reason === 'category_without_spatial_support'
          ? '有大类缺少空间支持：缺图的大类不是低覆盖率，是不能加权。'
          : '评分阶段未产出总体分。'}${overall.missingCategories.length > 0
        ? `涉及：${overall.missingCategories.join('、')}。` : ''}`} />}
    <Section index={1} title="分类覆盖区间" />
    <div className="rp-table-wrap"><table className="rp-table">
      <thead><tr><th scope="col">类别</th><th scope="col">覆盖率区间</th><th scope="col">可评估</th>
        <th scope="col">未知</th><th scope="col">面积</th></tr></thead>
      <tbody>{items.map(item => <CoverageRow key={item.category} item={item} />)}</tbody>
    </table></div>
    <p className="checkup-muted">最低覆盖率 = C / A，最高覆盖率 = (C + U) / A。C 已覆盖、U 未知，
      A 为评估域面积 {area(domainAreaM2)}。</p>
    {items.filter(item => item.degenerate).map(item => <p key={item.category} className="checkup-muted">
      {item.label}：区间退化为一个点。本类别的每一格都已判定，没有未知面积 —— 这不是精度更高，
      只是恰好没有留下没查到的部分。</p>)}

    <Section index={2} title="服务盲区与灰区" />
    <ZoneList gaps={gaps} />

    <Section index={3} title="现实核验" />
    <p className="checkup-muted" data-testid="verification-summary">{verification.summary}
      {verification.provider && `（${verification.provider}）`}</p>
    {verification.reason && <p className="checkup-muted">{verification.reason}</p>}
    {!verification.available && <p className="checkup-muted">没有核验不是"核验过、没问题"：
      设施的可达性判断来自路网模型，实地情况仍可能不同。</p>}

    <Section index={4} title="证据说明" />
    <ul className="checkup-notes">{notes.map(note => <li key={note.key}
      className={note.level === 'warning' ? 'checkup-note-warning' : undefined}>{note.text}</li>)}</ul>
    {snapshot.warnings.length > 0 && <details><summary>算法质量标记（{snapshot.warnings.length} 条）</summary>
      {/* 这些标记往往共用同一个 code（一次 OSM 体检有五条 ALGORITHM_WARNING），只用 code
          当 key 会让 React 认不出孩子，而这一块的意义就是"一条不少"，所以按位置认。 */}
      <ul className="checkup-notes">{snapshot.warnings.map((warning, index) => <li key={index}>
        [{warning.severity}] {warning.code}：{warning.message}（{warning.scope}）</li>)}</ul>
    </details>}
    <Section index={5} title="数据来源与版本" />
    <DataSources view={sources} />
    <details><summary>查看机器可读修订</summary><pre>{JSON.stringify(snapshot, null, 2)}</pre></details>
  </article>;
}
