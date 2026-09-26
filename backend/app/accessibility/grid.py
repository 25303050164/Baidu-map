"""§6.1 的评估网格：投影坐标全局对齐的 50 米格，必要时细化到 25 米。

三条口径写在这里，而不是散在调用方：

* **格是米制坐标上的全局格，不是围着分析中心排的格。** 索引直接由 ``floor(x/step)``
  得到，所以两套成圈算法、不同任务在同一片区域上得到同一批格子，格子中心也不会随
  中心点漂移（§5.5 要求面积、长度、缓冲、吸附、网格都在米制下运算）。
* **只评估与报告评估域相交的格，且细分是替换而不是叠加。** 一个 50 米格细分成四个
  25 米格后父格不再存在，因此所有叶格两两不重叠、并集恰好是评估域与格网的交集 ——
  §7.1 的 ``C + G + U = A`` 才可能成立，重复计数会直接破坏这个恒等式。
* **叶格上限 5000，到顶就停。** 停下来的格保持未知并给出原因，不静默粗化、不把
  没结论的格并成一个"大概没问题"的大格。

支持点按 §6.1：格内部的 ``representative_point`` 加落在评估域内的边界支持点（格边
中点）。一个格被河流或评估域边界切开时，代表点可能落在没有路网的一侧，边界支持点
是让这种格子仍然能被评估的唯一方式；没有任何支持点带合法接入时，该格是未知。
"""
from dataclasses import dataclass, field
from math import floor
from typing import Callable, Iterable

from shapely.geometry import Point, box
from shapely.prepared import prep

#: 基准格边长（米）：§6.1 的 50 米全局对齐格。
GRID_STEP_M = 50.0
#: 细化格边长（米）：圈边界/障碍相交、内部状态不一致、距离落在误差带、模型与核验冲突。
REFINED_STEP_M = 25.0
#: 叶格上限（§6.1）：到顶不再细化，未形成一致结论的格保持未知。
MAX_LEAF_CELLS = 5000

#: 未形成结论的中间态：由 :func:`explore` 决定是继续细化还是落到 unknown。
REFINE = "refine"
COVERED = "covered"
GAP = "gap"
UNKNOWN = "unknown"
VERDICTS = (COVERED, GAP, UNKNOWN)


def _index(level: int) -> float:
    return GRID_STEP_M if level == 0 else REFINED_STEP_M


@dataclass(frozen=True, order=True)
class Cell:
    """一个格：``level`` 0 是 50 米，1 是 25 米；``ix``/``iy`` 是全局格索引。"""
    ix: int
    iy: int
    level: int = 0

    @property
    def step(self) -> float:
        return _index(self.level)

    @property
    def id(self) -> str:
        """稳定 ID，跨任务可比较：同一步长的格在任何任务里都是同一个 ID。"""
        return f"{self.level}:{self.ix}:{self.iy}"

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        step = self.step
        return (self.ix * step, self.iy * step, (self.ix + 1) * step, (self.iy + 1) * step)

    @property
    def center(self) -> Point:
        minx, miny, maxx, maxy = self.bounds
        return Point((minx + maxx) / 2, (miny + maxy) / 2)

    def geometry(self):
        return box(*self.bounds)

    def children(self) -> tuple["Cell", "Cell", "Cell", "Cell"]:
        """四个 25 米子格；顺序固定为 左下/右下/左上/右上，便于稳定输出。"""
        if self.level != 0:
            raise ValueError("grid_already_refined")
        kids = [(2 * self.ix + dx, 2 * self.iy + dy) for dy in (0, 1) for dx in (0, 1)]
        return tuple(Cell(ix, iy, 1) for ix, iy in kids)

    def parent(self) -> "Cell":
        if self.level != 1:
            raise ValueError("grid_not_refined")
        return Cell(self.ix // 2, self.iy // 2, 0)

    def contains(self, point: Point) -> bool:
        minx, miny, maxx, maxy = self.bounds
        return minx <= point.x <= maxx and miny <= point.y <= maxy


def cells_covering(domain, *, step: float = GRID_STEP_M) -> Iterable[Cell]:
    """与 ``domain`` 相交的全局格，按索引（列优先）稳定排序。

    ``domain`` 是米制坐标下的多边形；判定用 prepared geometry，避免在大范围上反复
    构造。边界相切也算相交：格子只要碰到评估域就必须被评估，不能因为面积为零而漏掉。
    """
    if domain is None or domain.is_empty:
        return []
    level = 0 if step == GRID_STEP_M else 1
    prepared = prep(domain)
    minx, miny, maxx, maxy = domain.bounds
    result = []
    for ix in range(int(floor(minx / step)), int(floor(maxx / step)) + 1):
        for iy in range(int(floor(miny / step)), int(floor(maxy / step)) + 1):
            cell = Cell(ix, iy, level)
            if prepared.intersects(cell.geometry()):
                result.append(cell)
    return result


def support_points(cell: Cell, domain, *, limit: int = 5) -> list[Point]:
    """§6.1 的支持点：内部代表点 + 落在评估域内的格边中点。

    代表点用 ``representative_point``（一定落在几何内部）。边界支持点取四条格边的
    中点，只在它属于评估域时保留 —— 格被评估域边界切掉一角时，代表点可能整个落在
    没有路网的一侧，边界支持点是这种格子唯一的评估入口。

    ``limit`` 是硬上限：支持点越多评估越慢，而它们只是在回答"这一格有没有服务"。
    """
    if domain is None or domain.is_empty:
        return []
    clipped = cell.geometry().intersection(domain)
    if clipped.is_empty:
        return []
    points = [clipped.representative_point()]
    minx, miny, maxx, maxy = cell.bounds
    mids = [Point((minx + maxx) / 2, miny), Point((minx + maxx) / 2, maxy),
            Point(minx, (miny + maxy) / 2), Point(maxx, (miny + maxy) / 2)]
    prepared = prep(domain)
    for candidate in mids:
        if len(points) >= limit:
            break
        # 只收域内、且不重复的支持点：边界上的点在两侧都算，取一次就够。
        if prepared.covers(candidate) and all(candidate.distance(item) > 1e-9 for item in points):
            points.append(candidate)
    return points


@dataclass
class Leaf:
    cell: Cell
    verdict: str
    reason: str | None = None
    area_m2: float = 0.0
    #: 被评估域裁出的几何，与 ``area_m2`` 由同一个对象算出。灰区合并必须用这一份，
    #: 不能自己重算一次：重算与面积之间哪怕只差一点，报告里的面积和图上画的区域
    #: 就不是同一块地方，而这种偏差在核对时几乎不可能被发现。
    geometry: object | None = None


@dataclass
class ExploreOutcome:
    leaves: list[Leaf] = field(default_factory=list)
    refined: int = 0
    capped: bool = False

    @property
    def area_m2(self) -> float:
        return sum(leaf.area_m2 for leaf in self.leaves)

    def by_verdict(self, verdict: str) -> list[Leaf]:
        return [leaf for leaf in self.leaves if leaf.verdict == verdict]


#: ``evaluate(cell, samples) -> (verdict, reason)``；``refine`` 表示证据不足以定论。
Evaluator = Callable[[Cell, list[Point]], tuple[str, str | None]]


def explore(domain, evaluate: Evaluator, *, max_leaves: int = MAX_LEAF_CELLS) -> ExploreOutcome:
    """按 §6.1 遍历网格：先 50 米，证据不足的格再细化到 25 米，直到叶格上限。

    细化的四个触发条件由 ``evaluate`` 判断（它看得见距离、误差带、冲突和障碍），
    网格只负责三件事：把一个未定论的 50 米格换成四个与评估域相交的子格、在 25 米上
    仍无定论时落成 ``unknown``、以及在叶格到顶时把仍未定论的格落成 ``unknown`` 并
    标注 ``refinement_capped``。到顶之后不再细化，也不把未定论的格偷偷并成一个大格。
    """
    outcome = ExploreOutcome()
    queue: list[Cell] = [cell for cell in cells_covering(domain)]
    leaf_count = len(queue)
    if leaf_count > max_leaves:
        # 评估域本身就装不下这么多 50 米格：不假装评估过，整批标为未知。
        raise ValueError("assessment_domain_exceeds_grid_limit")
    while queue:
        cell = queue.pop(0)
        try:
            verdict, reason = evaluate(cell, support_points(cell, domain))
        except Exception:
            verdict, reason = UNKNOWN, "assessment_failed"
        if verdict == REFINE:
            if cell.level != 0:
                # 25 米是最终分辨率：到此仍有歧义就是未知，不再往下细分（§6.1）。
                verdict, reason = UNKNOWN, reason or "refinement_exhausted"
            else:
                # 只收与评估域相交的子格：一格被域边界切掉一半时，域外的那个子格
                # 面积为零，留着它只会往报告里写一条"没有合法接入"的假条目。
                children = [child for child in cell.children()
                            if domain is not None and child.geometry().intersects(domain)]
                if not children:
                    # 父格只是碰到了评估域（面积为零）却没有可细分的部分。
                    verdict, reason = UNKNOWN, reason or "refinement_exhausted"
                elif leaf_count - 1 + len(children) <= max_leaves:
                    # 只有换得起才换：子格替换父格，净增 len(children) - 1 个叶格。
                    queue.extend(children)
                    leaf_count += len(children) - 1
                    outcome.refined += 1
                    continue
                else:
                    outcome.capped = True
                    verdict, reason = UNKNOWN, "refinement_capped"
        if verdict not in VERDICTS:
            verdict, reason = UNKNOWN, "invalid_verdict"
        clipped = cell.geometry().intersection(domain) if domain is not None else None
        outcome.leaves.append(Leaf(cell=cell, verdict=verdict, reason=reason, geometry=clipped,
                                   area_m2=0.0 if clipped is None else clipped.area))
    outcome.leaves.sort(key=lambda leaf: leaf.cell)
    return outcome
