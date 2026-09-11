import { Alert } from 'antd';
import { categoryMeta } from './types';
import { reportWarnings, type ReportViewModel } from './report';
import { ReportBarChart } from './Chart';
import styles from './styles.module.css';

type ReportProps = {
  view: ReportViewModel;
  /** 报告生成后分析条件被修改（state.dirty），报告内容滞后于当前设置。 */
  stale?: boolean;
  /** 最近一次分析未成功，报告展示的是上一次成功分析的结果。 */
  lastAttemptFailed?: boolean;
};

/** 报告 MVP 页面：只消费 ReportViewModel，不直接读取 AnalysisResult。 */
export function ReportPage({ view, stale = false, lastAttemptFailed = false }: ReportProps) {
  const warnings = reportWarnings({ stale, lastAttemptFailed });
  const unknownLabels = view.facilityStats.filter(s => s.inCircleCount === null).map(s => s.label);
  // 0 与 unknown 含义不同，两者任一出现时都向读者解释区别，避免把 0 当成"没有问题"或把数据不足当成 0。
  const needZeroVsUnknownNote = view.hasUnknownCategory || view.facilityStats.some(s => s.inCircleCount === 0);
  return (
    <article className={styles.report} data-testid="report">
      <div className={styles.eyebrow}>COMMUNITY CHECKUP / DEMO</div>
      <h1>{view.centerName}</h1>
      <p className={styles.reportLead}>15 分钟生活圈 · 可视化体检报告</p>
      {warnings.map(w => <Alert key={w.kind} type="warning" title={w.message} showIcon/>)}
      <dl>
        <div><dt>分析中心点</dt><dd>{view.center.lng.toFixed(6)}, {view.center.lat.toFixed(6)}</dd></div>
        <div><dt>分析时间</dt><dd>{new Date(view.generatedAt).toLocaleString('zh-CN', { hour12: false })}</dd></div>
        <div><dt>演示场景</dt><dd>{view.scenarioLabel}</dd></div>
        <div><dt>数据来源</dt><dd>{view.dataSource}</dd></div>
        <div><dt>数据限制</dt><dd>{view.dataLimitation}</dd></div>
      </dl>
      <h2>01 / 圈内设施统计</h2>
      <p>全部类别，不受主界面筛选影响，仅统计 15 分钟圈内设施。</p>
      <table className={styles.reportTable} data-testid="facility-stats-table">
        <thead><tr><th>设施类别</th><th>圈内数量</th><th>数据状态</th></tr></thead>
        <tbody>
          {view.facilityStats.map(s => (
            <tr key={s.category}>
              <td>{s.label}</td>
              <td>{s.inCircleCount ?? '无法确定'}</td>
              <td>{s.dataQuality === 'complete' ? '演示数据完整' : '数据不足'}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <ReportBarChart view={view}/>
      {unknownLabels.length > 0 && <p>{unknownLabels.join('、')}数据不足，柱状图不绘制对应柱形，也不计为 0。</p>}
      <p>合计已记录 {view.totalInCircle} 处{view.hasUnknownCategory ? '；含数据不足类别中已记录的点位，该类别数量仍无法确定' : ''}。</p>
      {needZeroVsUnknownNote && <p>数量 0 表示圈内已确认无此类设施记录；「无法确定」表示数据不足、暂时无法判断，两者含义不同。</p>}
      <h2>02 / 1 公里服务问题</h2>
      <p>以下为演示场景的预设结论，1 公里口径尚待正式确认，不代表真实社区的服务判定。</p>
      <div className={styles.listCaption}>1 公里服务情况（演示预设） <span>{view.blindZoneTotal} 个盲区 · {view.unknownZoneTotal} 处待补充</span></div>
      <table className={styles.reportTable} data-testid="zone-stats-table">
        <thead><tr><th>类别</th><th>盲区数量</th><th>数据不足 / 待补充</th></tr></thead>
        <tbody>
          {view.zoneStats.map(s => (
            <tr key={s.category}>
              <td>{s.label}</td>
              <td>{s.blindCount}</td>
              <td>{s.unknownCount}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {view.zones.length ? view.zones.map(z => (
        <div key={z.id} className={styles.reportIssue}>
          <strong>{z.name} · {categoryMeta[z.category].label} · {z.status === 'blind' ? '服务盲区' : '无法判断'}</strong>
          <p>{z.reason}</p>
        </div>
      )) : <p>当前演示场景未设置服务盲区，不代表真实社区服务充分。</p>}
      <h2>03 / 结果适用边界</h2>
      <p>本报告仅展示产品交互。等时圈和盲区为预设示意结果，未经路网、距离或现场核查。单点结果不代表全社区人口覆盖率，设施可达不代表容量、质量或使用资格得到满足。</p>
      <div className={styles.reportEnd}>— 演示报告结束 —</div>
    </article>
  );
}
