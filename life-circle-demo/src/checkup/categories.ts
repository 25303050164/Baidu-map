/**
 * 类别选择器：能力表里的设施目录 → 可选的大类、展示分组和预算算术。
 *
 * 与 `capabilities.ts` 同一条规矩：**只翻译，不判断**。哪几类是核心口径、每个大类会
 * 展开成几个检索小类，都由后端给（`/api/v2/capabilities` 的 `facilityCategories`）。
 * 读不到就返回 null，让界面说"后端没报这一项"，绝不在这里再写一份分类表 —— 自己维护的
 * 那份会和后端真正执行的检索计划悄悄分叉，而分叉的表现是"界面算的预算一直是错的"。
 *
 * 三条口径写在类型上，不散在界面里：
 *
 * * **核心口径**（`:class:`MajorOption.core`）是总体区间分的范围。选中的核心大类不齐备时
 *   总体分是"给不出"，不是把剩下的类别重新加权。
 * * **小类数**是首轮页数估计的分子：按最多分块数算出的"分块数 × 小类数"是**冷启动首轮
 *   页数**的估计，不是总请求数的下界（实际分块由圈面包络决定，翻页、细分与重试还要更多
 *   页）。界面在提交前用它估计选的类别够不够预算。
 * * **扩展大类**不进主请求，走按需补查（`facility-extensions`）：主请求只带核心口径，
 *   扩展类别复用已经算出的圈面再查一次，既不改评分也不让主请求变重。
 */
import type { MajorCategory } from './contract';

type RecordValue = Record<string, unknown>;

const object = (value: unknown): value is RecordValue =>
  value !== null && typeof value === 'object' && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === 'string' && value.length > 0;
const positive = (value: unknown): value is number =>
  typeof value === 'number' && Number.isInteger(value) && value >= 1;

export type MajorOption = {
  key: MajorCategory;
  label: string;
  /** 所属展示组；后端没给分组时为 null。 */
  displayGroup: string | null;
  /** 核心口径：总体区间分只按这些大类加权。 */
  core: boolean;
  /** 这个大类会展开成几个检索小类：预算算术的分子。 */
  minorCategories: number;
};

export type MajorGroup = { key: string; label: string; majors: MajorOption[] };

export type FacilityCatalogView = {
  version: string | null;
  /** 核心口径的大类，顺序由后端给。 */
  coreMajors: MajorCategory[];
  groups: MajorGroup[];
  majors: MajorOption[];
  /**
   * 首轮检索的查询分块数上界（当前实现是 2×2，最多 4 块）。真实分块数由算出的圈面包络
   * 决定（≤ 这个上界），所以按它算出的是**冷启动首轮页数**的估计：翻页、细分与重试还要
   * 更多页，首轮够用**不代表**一定查得完。
   */
  blocksUpperBound: number | null;
};

/** 后端没给分组的大类落到的兜底分组：宁可多一组，也不要让一个类别从选择器里消失。 */
const UNGROUPED = { key: 'other', label: '其他' };

function majorOption(value: unknown): MajorOption | null {
  if (!object(value)) return null;
  const { key, label, displayGroup, core, minorCategories } = value;
  // 大类的机器键按契约放行，不在前端复制一份目录去比对：真写错了一个键，后端会用
  // 参数校验的 422 拒掉，而不是让界面拿一份自己编的分类表先把错误藏起来。
  if (!text(key) || !text(label) || !positive(minorCategories)) return null;
  if (typeof core !== 'boolean') return null;
  return {
    key: key as MajorCategory, label, core, minorCategories,
    displayGroup: text(displayGroup) ? displayGroup : null,
  };
}

/**
 * 能力表的 `facilityCategories` + `budgets.poiBlocksUpperBound` → 选择器模型。
 *
 * 旧后端不带这两项时返回 null：界面据此说明"这个后端不提供类别选择"，而不是给出一份
 * 空的或猜出来的类别清单 —— 提交一个自己编的类别集合只会换来一个参数错误。
 */
export function facilityCatalogView(capabilities: {
  facilityCategories?: RecordValue;
  budgets?: RecordValue;
}): FacilityCatalogView | null {
  const source = capabilities.facilityCategories;
  if (!object(source) || !Array.isArray(source.majors) || source.majors.length === 0) return null;
  const majors: MajorOption[] = [];
  for (const item of source.majors) {
    const option = majorOption(item);
    if (option === null) return null;
    majors.push(option);
  }
  const declaredGroups = Array.isArray(source.displayGroups) ? source.displayGroups : [];
  const groups: MajorGroup[] = [];
  for (const raw of declaredGroups) {
    if (!object(raw) || !text(raw.key) || !text(raw.label) || !Array.isArray(raw.majors)) continue;
    const members = raw.majors
      .map(key => majors.find(major => major.key === key))
      .filter((major): major is MajorOption => major !== undefined);
    if (members.length > 0) groups.push({ key: raw.key, label: raw.label, majors: members });
  }
  const grouped = new Set(groups.flatMap(group => group.majors.map(major => major.key)));
  const leftovers = majors.filter(major => !grouped.has(major.key));
  if (leftovers.length > 0) groups.push({ ...UNGROUPED, majors: leftovers });

  const declaredCore = Array.isArray(source.coreMajors) ? source.coreMajors : [];
  const known = new Set(majors.map(major => major.key));
  // 顺序沿用后端声明的那一份，不按目录顺序重排：只选核心三类时，提交的请求体要和
  // 不传 `facilities` 时的默认请求体一致，否则同一个幂等键会算出两个不同的指纹。
  const coreMajors = declaredCore.filter(
    (key): key is MajorCategory => typeof key === 'string' && known.has(key as MajorCategory));
  const blocks = object(capabilities.budgets)
    ? (capabilities.budgets as RecordValue).poiBlocksUpperBound
    : undefined;
  return {
    version: text(source.version) ? source.version : null,
    coreMajors,
    groups,
    majors,
    blocksUpperBound: positive(blocks) ? blocks : null,
  };
}

/**
 * 选中的类别按"最多分块数 × 小类数"估出的**冷启动首轮页数**，或 null（后端没给分块上界，
 * 或出现了目录里没有的大类）。
 *
 * 它是估计，既不是下界也不是承诺：后端每块每小类先取主关键词一次，所以一次检索的首轮页数
 * 约为"分块数 × 小类数"；实际分块数由算出的圈面包络决定（最多 `blocksUpperBound` 块），
 * 而翻页、细分与重试一定需要更多页 —— 首轮装得下**不代表**一定查得完。命中缓存的页面不
 * 消耗网络额度，所以真正新增的网络调用通常比这个数少（见补查面板的首轮估算）。
 * 刻意不给"够/不够"的结论：那是调用方拿本任务的预算去比的。
 */
export function estimatedFirstRoundPages(
  view: FacilityCatalogView, selected: readonly MajorCategory[]): number | null {
  if (view.blocksUpperBound === null) return null;
  let minors = 0;
  for (const key of selected) {
    const option = view.majors.find(major => major.key === key);
    if (option === undefined) return null;
    minors += option.minorCategories;
  }
  return view.blocksUpperBound * minors;
}

/**
 * 选中的大类 → 主请求的核心口径与走按需补查的扩展大类。
 *
 * 顺序沿用目录顺序，不按点击顺序：同一组条件在不同的点击顺序下必须提交同一个请求体，
 * 否则幂等键（`clientRequestId`）背后的指纹会因为顺序不同而被判成两次不同的体检。
 */
export function splitScope(view: FacilityCatalogView, selected: readonly MajorCategory[]) {
  const chosen = new Set(selected);
  const core = new Set(view.coreMajors);
  return {
    // 两批各自沿用它们的权威顺序：核心按后端声明的口径顺序，扩展按目录顺序。
    core: view.coreMajors.filter(key => chosen.has(key)),
    extended: view.majors.map(major => major.key)
      .filter(key => chosen.has(key) && !core.has(key)),
  };
}

/**
 * 选中的集合里缺了哪几个核心大类：缺了就给不出总体区间分。
 *
 * 界面据此提前说明，而不是等报告出来才发现"没有总体分" —— 那句话在报告里是
 * `categories_not_analysed`，提前说出来才是用户能改的。
 */
export function missingCoreMajors(view: FacilityCatalogView, selected: readonly MajorCategory[]): MajorCategory[] {
  const chosen = new Set(selected);
  return view.coreMajors.filter(key => !chosen.has(key));
}
