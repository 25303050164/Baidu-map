import { categories, categoryMeta, scenarioLabels, type AnalysisResult, type Category, type Center, type Scenario, type Zone } from './types';
import { summarize } from './domain';

// TODO: waiting for backend contract —— 接入真实数据后，数据来源与更新时间应由后端返回。
const DEMO_DATA_SOURCE = '本地固定模拟数据，无真实更新时间';
// TODO: waiting for backend contract —— 等时圈与盲区的真实口径（路网、距离参数）待后端算法确认。
const DEMO_DATA_LIMITATION = '示意地图范围内的点位；15 分钟范围与 1 公里盲区分别展示，距离口径尚待正式确认';

/** 单个设施类别的统计条目。数据不足时数量为 null，不能折算成 0。 */
export type FacilityStat = {
  category: Category;
  label: string;
  /** 15 分钟圈内已记录的设施数量；null 表示该类别数据不足，无法给出确定数量。 */
  inCircleCount: number | null;
  /** 数据状态：complete = 演示数据完整；unknown = 数据不足。 */
  dataQuality: 'complete' | 'unknown';
};

/** 单个类别的问题区域统计。unknown（数据不足）不计入盲区。 */
export type ZoneStat = {
  category: Category;
  label: string;
  /** 确认为服务盲区的数量。 */
  blindCount: number;
  /** 数据不足、暂时无法判断的数量（不是盲区）。 */
  unknownCount: number;
};

/** 报告专用的视图模型：只承载报告需要的、已转换好的数据，UI 不再直接读取 AnalysisResult。 */
export type ReportViewModel = {
  // 基础信息
  centerName: string;
  center: Center;
  /** ISO 时间字符串，展示格式由 UI 决定。 */
  generatedAt: string;
  scenario: Scenario;
  scenarioLabel: string;
  dataSource: string;
  dataLimitation: string;
  // 设施统计
  facilityStats: FacilityStat[];
  /** 圈内已记录设施总数。即使某类别数据不足，已记录的点位仍计入（unknown 不虚构为 0，也不凭空丢弃）。 */
  totalInCircle: number;
  /** 是否存在数据不足的类别。 */
  hasUnknownCategory: boolean;
  // 盲区统计
  zoneStats: ZoneStat[];
  blindZoneTotal: number;
  unknownZoneTotal: number;
  // 问题区域明细，复用已有 Zone 类型
  zones: Zone[];
};

/** 纯函数：AnalysisResult → ReportViewModel。不修改入参，同一输入产生同一输出。 */
export function analysisResultToReportView(result: AnalysisResult): ReportViewModel {
  const summary = summarize(result);
  const facilityStats: FacilityStat[] = categories.map(category => ({
    category,
    label: categoryMeta[category].label,
    inCircleCount: summary[category],
    dataQuality: result.quality[category]
  }));
  const zoneStats: ZoneStat[] = categories.map(category => {
    const zones = result.zones.filter(z => z.category === category);
    return {
      category,
      label: categoryMeta[category].label,
      blindCount: zones.filter(z => z.status === 'blind').length,
      unknownCount: zones.filter(z => z.status === 'unknown').length
    };
  });
  return {
    centerName: result.sample.name,
    center: result.sample.center,
    generatedAt: result.generatedAt,
    scenario: result.scenario,
    // failure 场景的任务在重试成功后才会产出结果，报告需说明这一点
    scenarioLabel: scenarioLabels[result.scenario] + (result.scenario === 'failure' ? '（重试成功）' : ''),
    dataSource: DEMO_DATA_SOURCE,
    dataLimitation: DEMO_DATA_LIMITATION,
    facilityStats,
    totalInCircle: summary.total,
    hasUnknownCategory: facilityStats.some(s => s.inCircleCount === null),
    zoneStats,
    blindZoneTotal: summary.blindCount,
    unknownZoneTotal: summary.unknownCount,
    zones: result.zones
  };
}

/** 柱状图数据项。value 为 null 表示该类别数据不足：不绘制柱形，也不能当作 0。 */
export type BarSeriesItem = { category: Category; label: string; value: number | null; color: string; /** 图表 y 轴显示名；数据不足时自带标注，避免空柱被误读为 0。 */ axisLabel: string };

/** 纯函数：从报告视图模型导出三类设施柱状图数据，保持 null 语义。 */
export function reportBarSeries(view: ReportViewModel): BarSeriesItem[] {
  return view.facilityStats.map(stat => ({
    category: stat.category,
    label: stat.label,
    value: stat.inCircleCount,
    color: categoryMeta[stat.category].color,
    axisLabel: stat.inCircleCount === null ? `${stat.label}（数据不足）` : stat.label
  }));
}

export type ReportWarningKind = 'stale' | 'failed';
export type ReportWarning = { kind: ReportWarningKind; message: string };

/** 纯函数：计算报告顶部提示。条件已变更 / 最近一次分析未成功时，报告必须显式说明，避免被误读为当前最新结果。 */
export function reportWarnings(flags: { stale: boolean; lastAttemptFailed: boolean }): ReportWarning[] {
  const warnings: ReportWarning[] = [];
  if (flags.stale) warnings.push({ kind: 'stale', message: '分析条件已修改，本报告仍属于下列原分析条件。' });
  if (flags.lastAttemptFailed) warnings.push({ kind: 'failed', message: '最近一次分析未成功，本报告展示的是上一次成功分析的结果。' });
  return warnings;
}
