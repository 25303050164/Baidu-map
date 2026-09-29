"""§6.5 灰区合并与解释：按共享边把 gap 格并成连通分量，再裁剪回报告评估域。

六条口径都在这里，每条都有一个"看起来更省事但会出错"的做法被明确排除：

* **邻接是共享边，不是共享点。** 只有对角接触的两个格各自被障碍隔开；合并它们会把
  一条河的两岸写成一片连续缺口。判据是两块几何的交集**长度大于零**，且这条共享边上
  没有障碍：点接触不合并，障碍两侧不合并。
* **并集之后重新裁剪到评估域**，因此保留孔洞、分量与原始格 ID。孔洞是真实信息
  （中间那一格有服务），显示简化不得把它抹掉：这里不做闭运算、不做简化，
  统计几何与展示几何是同一份。
* **面积按并集计算**，不是把各格面积相加：一个格可能同时落在多个类别的灰区里，
  相加就是重复计数。
* **综合灰区固定为"三大类至少两类为 gap"**。unknown 不计为缺失：三类里两类未知、
  一类 gap 的格不是综合灰区。
* **小于 2500 m² 的区域不默认显示标签**，但几何与面积照常保留 —— 面积结论不因为
  标签规则而改变。
* **建议由模板生成**，指向补采、通道核查或设施核查。不把模型结果写成确定的建设选址
  结论，也不把"目录未独立验证"写成"这里确实没有设施"。

连通性只用共享边判定，因此**分量内部的"为什么是灰区"是逐格的**：合并后取成员里
信息量最高的那个理由和最近的那家已知设施，而不是发明一个新的综合理由。
"""
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from shapely import union_all
from shapely.geometry import mapping
from shapely.strtree import STRtree

from .grid import Cell, GAP

#: 小于这个面积的区域不默认显示标签（§6.5），几何与面积仍然保留。
MIN_LABEL_AREA_M2 = 2500.0
#: 综合灰区需要至少这么多大类同时为 gap（§6.5："三大类至少两类为 gap"）。
COMPOSITE_MIN_CATEGORIES = 2
#: 综合灰区的类别名：它自己不是一个类别，所以不参与"每类灰区"的统计。
COMPOSITE = "composite"

#: 建议模板：按原因给出下一步要做的核查动作，不是结论。
SUGGESTIONS = {
    "query_status": "建议先补全该类别的设施检索，再判断是否为服务不足。",
    "beyond_service_distance": "建议核查该区域到已知设施之间是否存在可步行通道，"
                               "并核对目录是否漏采该类别设施。",
    "entrance_unresolved_nearby": "建议核查附近设施的入口位置（原始点与导航点）及其接入道路。",
    "corridor": "建议核查该区域内的通道（门禁、天桥、过街口）是否可步行通过。",
    COMPOSITE: "建议优先核查该区域内至少两类设施同时缺失的原因（道路可达性或目录缺失）。",
    "default": "建议按类别补充实地采集，核对目录中该类设施是否漏采。",
}

#: 理由的信息量排序：越靠前越具体。"最近一家设施入口未解决"比笼统的"证据不足"更可复核。
#: ``beyond_service_distance`` 排在最后：一个格被判成 gap 时，它本来就是"所有支持点在两
#: 个视图上都超过服务距离"的同义反复，只有没有更具体的原因时才值得写进报告。它由阶段在
#: 建 :class:`CellRecord` 时填上（已定论的格没有细化理由，见 ``grid.Leaf.reason``）。
_REASON_ORDER = ("entrance_unresolved_nearby", "no_valid_entrance", "views_disagree",
                 "graph_disconnected", "distance_in_tolerance_band", "query_status",
                 "no_legal_attachment", "beyond_service_distance")


@dataclass(frozen=True)
class CellRecord:
    """一格在某类别下的结论与证据，是合并的输入。``geometry`` 已裁剪到评估域。"""
    cell: Cell
    category: str
    verdict: str
    geometry: object
    distance_m: float | None = None
    nearest_facility: str | None = None
    reason: str | None = None
    evidence_grade: str = "model"
    query_complete: bool = True

    @property
    def query_status(self) -> str:
        return "complete" if self.query_complete else "partial"


@dataclass
class Zone:
    """一片灰区。``id`` 对同一份输入稳定；``index`` 是给人看的编号（按面积降序）。"""
    id: str
    categories: tuple[str, ...]
    kind: str
    area_m2: float
    geometry: object
    cell_ids: tuple[str, ...]
    evidence_grade: str = "model"
    query_status: str = "complete"
    nearest_facility: str | None = None
    reason: str | None = None
    suggestion: str = SUGGESTIONS["default"]
    index: int = 0

    @property
    def parts(self) -> int:
        """几何的分量数：并集保留独立分量，报告要能说出"这是几片"。"""
        return _parts(self.geometry)

    @property
    def label_visible(self) -> bool:
        return self.area_m2 >= MIN_LABEL_AREA_M2

    @property
    def suspected(self) -> bool:
        """目录未独立验证时，gap 只能表述为"疑似服务不足"（§6.2）。"""
        return self.evidence_grade != "verified"

    def as_dict(self) -> dict:
        """导线形态用 camelCase，与修订文档其余部分一致（``rules`` 是唯一例外）。

        ``geometry`` 是**统计几何**的 GeoJSON（米制），与 ``areaM2`` 同一来源。发布方
        负责再给一份展示几何（bd09ll），两份必须来自同一个对象：展示几何自己重新构造
        一次，图上画的和报告里写的就不是同一块地方。
        """
        return {"id": self.id, "index": self.index, "categories": list(self.categories),
                "kind": self.kind, "areaM2": self.area_m2, "parts": self.parts,
                "cellIds": list(self.cell_ids), "labelVisible": self.label_visible,
                "suspected": self.suspected, "evidenceGrade": self.evidence_grade,
                "queryStatus": self.query_status, "nearestFacility": self.nearest_facility,
                "reason": self.reason, "suggestion": self.suggestion,
                "geometry": mapping(self.geometry)}


def _parts(geometry) -> int:
    if geometry is None or geometry.is_empty:
        return 0
    return 1 if geometry.geom_type == "Polygon" else len(geometry.geoms)


def merge_zones(records: Sequence[CellRecord], *, domain,
                majors: Iterable[str] | None = None,
                obstacle_between: Callable[[object], bool] | None = None) -> list[Zone]:
    """把 gap 格合并成灰区（§6.5）。

    ``records`` 是各类别的逐格结论。只有 ``gap`` 参与合并：``unknown`` 既不计为缺失，
    也不参与连通 —— 它不是"这里没有服务"，而是"这里还没有结论"，把它当缺口合并进去
    会让未知变成一条服务盲区的结论。

    ``obstacle_between`` 接收两块几何的共享边，返回这条边是否被障碍隔开。没有障碍层时
    不要传这个谓词：调用方应在数据质量里说明"未使用障碍分隔"，而不是让合并默默按
    无障碍处理得更乐观。
    """
    by_category: dict[str, list[CellRecord]] = {}
    for record in records:
        if record.verdict == GAP:
            by_category.setdefault(record.category, []).append(record)
    zones: list[Zone] = []
    for category, group in sorted(by_category.items()):
        zones.extend(_merge_group(group, kind="single", domain=domain,
                                  obstacle_between=obstacle_between))
    if majors:
        zones.extend(_composite_zones(records, majors=tuple(majors), domain=domain,
                                      obstacle_between=obstacle_between))
    zones.sort(key=lambda zone: (-zone.area_m2, zone.id))
    for position, zone in enumerate(zones, start=1):
        zone.index = position
    return zones


def _composite_zones(records: Sequence[CellRecord], *, majors: tuple[str, ...], domain,
                     obstacle_between) -> list[Zone]:
    """综合灰区：同一个格上"三大类至少两类为 gap"（unknown 不计为缺失）。"""
    missing: dict[str, set[str]] = {}
    for record in records:
        if record.category in majors and record.verdict == GAP:
            missing.setdefault(record.cell.id, set()).add(record.category)
    composite = [record for record in records
                 if record.category in majors and record.verdict == GAP
                 and len(missing[record.cell.id]) >= COMPOSITE_MIN_CATEGORIES]
    if not composite:
        return []
    return _merge_group(composite, kind=COMPOSITE, domain=domain,
                        obstacle_between=obstacle_between)


def _merge_group(group: Sequence[CellRecord], *, kind: str, domain,
                 obstacle_between) -> list[Zone]:
    """一个类别（或综合）内部的连通分量：共享边 + 无障碍才算连通。"""
    if not group:
        return []
    index = STRtree([record.geometry for record in group])
    parent = list(range(len(group)))

    def find(item: int) -> int:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    for position, record in enumerate(group):
        for hit in index.query(record.geometry):
            other = int(hit)
            if other <= position:
                continue
            shared = record.geometry.intersection(group[other].geometry)
            # 交集长度大于零才叫共享边；仅点接触不合并（§6.5）。
            if shared.is_empty or shared.length <= 0:
                continue
            if obstacle_between is not None and obstacle_between(shared):
                continue
            left, right = find(position), find(other)
            if left != right:
                parent[max(left, right)] = min(left, right)

    components: dict[int, list[CellRecord]] = {}
    for position, record in enumerate(group):
        components.setdefault(find(position), []).append(record)
    zones = []
    for members in components.values():
        members.sort(key=lambda record: record.cell)
        zone = _zone_of(members, kind=kind, domain=domain, obstacle_between=obstacle_between)
        if zone is not None:
            zones.append(zone)
    return zones


def _zone_of(members: Sequence[CellRecord], *, kind: str, domain,
             obstacle_between) -> Zone | None:
    # 类别是逐个分量取的：一整片综合灰区里，不同分量的缺失大类可能不同，写成一整片的
    # 并集会把"这片缺什么"说成"这片里曾经缺过什么"。
    categories = tuple(sorted({record.category for record in members}))
    merged = union_all([record.geometry for record in members])
    clipped = merged.intersection(domain) if domain is not None else merged
    if clipped.is_empty or clipped.area <= 0:
        # 与评估域只剩相切（面积为零）的格不构成一片灰区：面积为零的条目在报告里
        # 既无法显示也无法核查，留着只会让"灰区数量"虚增。
        return None
    reason = _reason_of(members)
    return Zone(
        id=f"{kind}:{'+'.join(categories)}:{members[0].cell.id}",
        categories=categories, kind=kind, area_m2=clipped.area, geometry=clipped,
        # 去重：同一个格在两类设施上都是 gap 时，它在综合灰区的成员里出现两次，
        # 但格子映射是"这一片由哪些格子构成"，重复列出会把格数说多。
        cell_ids=tuple(dict.fromkeys(record.cell.id for record in members)),
        evidence_grade=_grade_of(members), query_status=_query_of(members),
        nearest_facility=_nearest_of(members), reason=reason,
        suggestion=_suggestion(kind, reason, obstacle_between))


def _reason_of(members: Sequence[CellRecord]) -> str | None:
    """成员里信息量最高的理由：优先取具体原因，没有具体原因时按查询完成度收口。"""
    present = {record.reason for record in members if record.reason}
    for reason in _REASON_ORDER:
        if reason in present:
            return reason
    if any(record.query_status == "partial" for record in members):
        return "query_status"
    return None


def _nearest_of(members: Sequence[CellRecord]) -> str | None:
    """最近已知设施：取成员里目录距离最小的那家。距离缺失的成员不参与。"""
    known = [record for record in members if record.distance_m is not None]
    if not known:
        return None
    return min(known, key=lambda record: (record.distance_m, record.cell)).nearest_facility


def _grade_of(members: Sequence[CellRecord]) -> str:
    """证据等级取最弱的那个成员：一片区域里只有一部分是核验过的，整体就不是核验过的。"""
    grades = {record.evidence_grade for record in members}
    return "verified" if grades == {"verified"} else "model"


def _query_of(members: Sequence[CellRecord]) -> str:
    """只要有一个成员是部分检索，整片灰区就标为部分检索（§7.2 的查询状态传播）。"""
    return "complete" if all(record.query_complete for record in members) else "partial"


def _suggestion(kind: str, reason: str | None, obstacle_between) -> str:
    if reason in SUGGESTIONS:
        return SUGGESTIONS[reason]
    if kind == COMPOSITE:
        return SUGGESTIONS[COMPOSITE]
    if obstacle_between is not None:
        return SUGGESTIONS["corridor"]
    return SUGGESTIONS["default"]
