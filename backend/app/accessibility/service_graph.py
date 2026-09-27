"""§5.2 的两张只读图视图、§5.3 的入口接入、§5.4 的反向多源最短路。

三件事必须一起读，否则任何一件都会把结论推向相反的方向：

* **两张视图只差在"条件不明"上。** 明确允许图只走明确可用的连接；可能允许图额外包含
  条件不明但可能通行的连接，仍然排除明确禁止的通路。未知门禁不得当成关闭 ——
  那会凭空造出灰区；也不得当成开放 —— 那会把未知写成覆盖。两个视图结论不同即未知。
* **入口只是候选，接入只有 50 米。** POI 原始点和导航点都是入口候选，接入距离超过
  50 米、候选之间不相容、连接段穿过水体或围墙、门禁不明，都进未知与重点核验队列。
  旧图最大 200 米吸附是给成圈原点用的工程配置，不代表新服务模型可以把 200 米当成
  可靠连接（§5.3）。未被解决的入口会影响附近的缺口判断，所以它必须留下影响范围。
* **反向搜索的种子要重新构造。** 原图的 source seeds 是"从原点到路网"的成本，方向
  与语义都反了；这里以设施入口所在边的两个端点为种子，成本是投影点沿边到该端点的
  距离，在反向上做 1100 米截止的多源 Dijkstra（§5.4）。

视图是 networkx 的只读子图视图，不是整城图的副本：按类别、按格深复制路网在这里是
不允许的开销（§5.2）。
"""
from dataclasses import dataclass
from typing import Callable, Iterable, Literal

import networkx as nx
from shapely.geometry import Point, box

from ..algorithms.osm_offline.routing import (LENGTH_WEIGHT, cutoff_dijkstra,
                                              cutoff_dijkstra_owners)

ALLOWED = "allowed"
POSSIBLE = "possible"
DENIED = "denied"
VIEWS = (ALLOWED, POSSIBLE)

#: §5.4 的搜索截止：1000 米规则加上 100 米误差带，接入成本计入总距离后仍在带内。
SEARCH_CUTOFF_M = 1100.0
#: §5.3 的入口接入上限（米）。
ENTRANCE_LIMIT_M = 50.0
#: 评估点支持点的合法接入上限（米）：与入口同一条口径，不做 200 米的老式放宽。
SUPPORT_LIMIT_M = 50.0
#: 两个候选被判定为相容的最大间距（米）。
CANDIDATE_AGREEMENT_M = 50.0

_ALLOWED_FOOT = frozenset({"yes", "designated", "permissive", "official"})
_DENIED_FOOT = frozenset({"no", "private", "use_sidepath"})
_ALLOWED_ACCESS = frozenset({"yes", "public", "permissive", "designated", "official"})
_DENIED_ACCESS = frozenset({"no", "private"})
_CONDITIONAL_ACCESS = frozenset({"customers", "residents", "permit", "delivery", "agricultural",
                                 "forestry", "military", "unknown"})
#: 法定禁止步行的道路等级：高速公路及其匝道、在建与规划道路。主干道是风险因素，不是禁止。
_DENIED_HIGHWAY = frozenset({"motorway", "motorway_link", "construction", "proposed", "raceway",
                             "escape", "bus_guideway"})
#: 对行人可通行的门：旋转门、闸机之类的存在就是为了让行人通过。
_PASSABLE_BARRIERS = frozenset({"turnstile", "kissing_gate", "stile", "cycle_barrier", "bollard",
                                "kerb", "cattle_grid", "entrance", "door", "wicket_gate",
                                "toll_booth", "bump_gate", "planter", "motorcycle_barrier",
                                "sump_buster", "height_restrictor"})
#: 明确的物理阻挡：墙、栅栏之类不是"可能通行"，是过不去。
_BLOCKING_BARRIERS = frozenset({"wall", "fence", "hedge", "retaining_wall", "city_wall", "ditch",
                                "guard_rail", "jersey_barrier", "chain", "block", "coupure",
                                "debris", "yes_no"})
# 其余门类（gate / lift_gate / swing_gate / sliding_gate / sally_port / barrier=yes …）
# 一律视为条件不明：可能允许图包含，明确允许图不包含。


def classify_edge(data: dict) -> str:
    """一条有向边的通行判定（§5.2）。

    判定顺序是有意的，换一下就会在真实数据上给出相反结论：

    1. **法定禁行的道路等级最先判**（高速公路及其匝道、在建与规划道路）。这是法律或
       物理上就不存在通行权的情形，映射者的 ``foot=yes`` 不能把高速公路变成可步行的。
    2. 然后是步行标签，它是关于行人的最具体陈述：``foot=no`` 禁止，``foot=yes`` 允许；
       ``foot=yes`` 的私有道路对行人是开放的 —— 私人通行权不排除行人。
    3. 再是通用通行标签：``access=no/private`` 禁止；``access=customers/residents``
       之类是**条件通行**，只进可能允许图，绝不假定开放。
    4. 什么都没有时按道路等级的通行默认处理。

    ``oneway:foot`` 在这里**不重复使用**：图缓存在准备阶段已经按它决定过哪些有向边
    存在，再判一次等于把同一条规则算两遍，会把本可通行的方向误判成禁止。方向的合法性
    由"这条有向边是否存在"表达，不由标签表达。
    """
    if data.get("highway") in _DENIED_HIGHWAY:
        return DENIED
    foot = data.get("foot")
    if foot in _DENIED_FOOT:
        return DENIED
    if foot in _ALLOWED_FOOT:
        return ALLOWED
    access = data.get("access")
    if access in _DENIED_ACCESS:
        return DENIED
    if access in _CONDITIONAL_ACCESS or foot == "unknown":
        return POSSIBLE
    return ALLOWED


def classify_barrier(barrier: str | None) -> str | None:
    """节点门禁的通行判定；``None`` 表示这个节点没有门禁信息。"""
    if not barrier or barrier in ("no", "none"):
        return None
    if barrier in _BLOCKING_BARRIERS:
        return DENIED
    if barrier in _PASSABLE_BARRIERS:
        return ALLOWED
    return POSSIBLE


@dataclass(frozen=True)
class Attachment:
    """一个位置到路网的合法接入：接入边、投影点、接入距离，以及沿边到两端点的距离。"""
    edge: tuple
    projected: Point
    distance_m: float
    length_m: float
    seeds: dict


def usable_edge(data: dict, *, u=None, v=None, graph=None) -> bool:
    """这条边能不能承载步行接入：不是明确禁行的道路，两端也没有明确的物理阻挡。"""
    if classify_edge(data) == DENIED:
        return False
    if graph is not None:
        for node in (u, v):
            if classify_barrier(graph.nodes[node].get("barrier")) == DENIED:
                return False
    return True


def attach_point(store, xy, *, limit_m: float = SUPPORT_LIMIT_M,
                 permitted: Callable[[dict], bool] | None = None) -> tuple[Attachment | None, str | None]:
    """把一个米制坐标接到最近的**可用**路网上（§5.3 的接入）。

    三条口径：

    * **只接可用边。** 先按半径框取候选，再在半径内挑最近的可用边；最近的边如果是
      ``foot=no`` 或它两端有墙，那不是"接入"，那是把一个入口挂到行人走不了的路上。
      ``permitted`` 允许调用方换一套判定（默认是 :func:`usable_edge`）。
    * **不做 200 米放宽。** 超过 ``limit_m`` 就是没有合法接入，调用方据此给未知。
      旧图的最大 200 米吸附是成圈原点的工程配置，不是新服务模型的可靠连接（§5.3）。
    * **等距时取索引最小的边**（与 ``snap.py`` 同一套稳定规则），两次评估同一点
      必须得到同一个答案。

    返回接入对象或原因字符串。原因分两种，因为它们在核验队列里是两条不同的结论：
    ``no_legal_attachment``（半径内根本没有路）与 ``no_usable_attachment``（有路但都不可用）。
    """
    if not store.edge_ids:
        return None, "nearest_edge_not_found"
    point = Point(xy)
    predicate = permitted or (lambda data: classify_edge(data) != DENIED)
    try:
        candidates = store.index.query(box(point.x - limit_m, point.y - limit_m,
                                           point.x + limit_m, point.y + limit_m))
    except Exception:
        return None, "edge_geometry_invalid"
    best = None
    refused = False
    for index in candidates:
        rank = int(index)
        edge = store.edge_ids[rank]
        data = store.graph.edges[edge]
        if not predicate(data) or not usable_edge(data, u=edge[0], v=edge[1], graph=store.graph):
            refused = True
            continue
        geometry = data["geometry"]
        position = geometry.project(point)
        projected = geometry.interpolate(position)
        distance = point.distance(projected)
        if distance > limit_m:
            continue
        if best is None or (distance, rank) < (best[0], best[1]):
            best = (distance, rank, edge, projected, position, geometry.length)
    if best is None:
        return None, "no_usable_attachment" if refused else "no_legal_attachment"
    distance, _, edge, projected, position, length = best
    return Attachment(edge=edge, projected=projected, distance_m=distance, length_m=length,
                      seeds={edge[1]: max(0.0, length - position), edge[0]: max(0.0, position)}), None


class PointAttachments:
    """Task-local bounded cache of category-independent support-point attachments.

    Coordinates are exact (no rounding). The store is immutable for one assessment;
    a fresh assessment gets a fresh cache, so graph/rule revisions cannot leak.
    """

    def __init__(self, store, *, max_entries=50_000):
        self.store, self.max_entries = store, max_entries
        self._items = {}

    def resolve(self, xy):
        key = tuple(xy)
        if key not in self._items:
            result = attach_point(self.store, key, limit_m=SUPPORT_LIMIT_M)
            if len(self._items) < self.max_entries:
                self._items[key] = result
            return result
        return self._items[key]


@dataclass(frozen=True)
class Entrance:
    """一个设施入口候选的接入结果。``status`` 为 unresolved 时带着原因进核验队列。

    ``point`` 可以是 ``None``：记录里连坐标都没有时为未知。不编造 ``(0, 0)``
    之类的占位坐标 —— 它会被后续的距离计算当成真实位置使用。
    """
    facility_id: str
    category: str
    source: Literal["point", "navigation"]
    point: Point | None
    attachment: Attachment | None
    status: Literal["valid", "unresolved"]
    reason: str | None = None


def resolve_entrances(facilities: Iterable[dict], *, store, category_of: Callable[[dict], str],
                      guard: Callable[[Point, Point], bool] | None = None,
                      limit_m: float = ENTRANCE_LIMIT_M) -> list[Entrance]:
    """把设施记录解析成入口候选（§5.3）。

    每个设施最多看两个候选：原始点与导航点。判定顺序是"能接入 → 不穿障碍 → 候选彼此
    相容"，任何一步失败都进未知并记录原因，而不是换个候选凑出一个看起来可用的入口：

    * ``no_legal_attachment``：50 米内没有可接入的路；
    * ``connector_crosses_obstacle``：接入段穿过已知水体或围墙（``guard`` 由调用方注入，
      缺少障碍资料时不会走到这一步，而应由调用方在质量标记里说明）；
    * ``multiple_incompatible_road_candidates``：两个候选落到互不相容的道路上，
      无法判断哪一个才是真正的入口。
    """
    resolved: list[Entrance] = []
    for facility in facilities:
        candidates: list[tuple[str, Point]] = [(source, Point(xy))
                                               for source, xy in _candidates(facility)]
        if not candidates:
            resolved.append(Entrance(str(facility.get("id")), category_of(facility), "point",
                                     None, None, "unresolved", "no_location"))
            continue
        usable: list[tuple[str, Attachment]] = []
        failure: str | None = None
        for source, point in candidates:
            attachment, reason = attach_point(store, (point.x, point.y), limit_m=limit_m)
            if attachment is None:
                failure = failure or reason
                continue
            if guard is not None and guard(point, attachment.projected):
                failure = failure or "connector_crosses_obstacle"
                continue
            usable.append((source, attachment))
        if not usable:
            resolved.append(Entrance(str(facility.get("id")), category_of(facility),
                                     candidates[0][0], candidates[0][1], None, "unresolved",
                                     failure or "no_legal_attachment"))
            continue
        if len(usable) > 1 and _incompatible(store, [item[1] for item in usable]):
            resolved.append(Entrance(str(facility.get("id")), category_of(facility),
                                     usable[0][0], usable[0][1].projected, None, "unresolved",
                                     "multiple_incompatible_road_candidates"))
            continue
        source, attachment = min(usable, key=lambda item: item[1].distance_m)
        resolved.append(Entrance(str(facility.get("id")), category_of(facility), source,
                                 attachment.projected, attachment, "valid", None))
    return resolved


def _candidates(facility: dict) -> list[tuple[str, tuple[float, float]]]:
    """设施记录里的入口候选：原始点与导航点，两者相同的只算一个。"""
    found: list[tuple[str, tuple[float, float]]] = []
    for source, key in (("point", "location"), ("navigation", "navigationLocation")):
        value = facility.get(key)
        if not isinstance(value, dict):
            continue
        try:
            xy = (float(value["lng"]), float(value["lat"]))
        except (KeyError, TypeError, ValueError):
            continue
        if any(abs(xy[0] - other[0]) < 1e-9 and abs(xy[1] - other[1]) < 1e-9 for _, other in found):
            continue
        found.append((source, xy))
    return found


def _incompatible(store, attachments: list[Attachment]) -> bool:
    """两个候选是否落在互不相容的道路上：同一条物理道路，或投影点彼此靠近，才算相容。"""
    first = attachments[0]
    first_way = store.graph.edges[first.edge].get("osmid")
    for other in attachments[1:]:
        same_way = first_way is not None and store.graph.edges[other.edge].get("osmid") == first_way
        if same_way or first.projected.distance(other.projected) <= CANDIDATE_AGREEMENT_M:
            continue
        return True
    return False


def entrance_seeds(entrances: Iterable[Entrance], *, cutoff_m: float = SEARCH_CUTOFF_M) -> dict:
    """把有效入口合并成反向搜索的种子（§5.4）。

    同一条边上的多个入口合并为端点上的最小成本；接入成本本身就超过截止的入口不参与，
    但**不影响**同一次调用里的其他入口。这里不做任何"取平均"：多源最短路取的是最小值。
    """
    return {node: cost for node, (cost, _) in
            entrance_seeds_with_owner(entrances, cutoff_m=cutoff_m).items()}


def entrance_seeds_with_owner(entrances: Iterable[Entrance],
                              *, cutoff_m: float = SEARCH_CUTOFF_M) -> dict:
    """同上，但每个种子记住自己是哪个设施的入口（§6.2 要求每格报告最近已知设施）。

    同一条边上的多个入口合并时，保留成本最小的那个入口的设施 ID —— 与距离取最小值是
    同一个决定，不是两次独立的选择：先到的那家就是距离最近的那家。
    """
    seeds: dict = {}
    for entrance in entrances:
        if entrance.attachment is None:
            continue
        for node, cost in entrance.attachment.seeds.items():
            total = entrance.attachment.distance_m + cost
            if total > cutoff_m:
                continue
            current = seeds.get(node)
            if current is None or total < current[0]:
                seeds[node] = (total, entrance.facility_id)
    return seeds


@dataclass(frozen=True)
class ServiceViews:
    """两张只读视图（正反两个方向）与它们的分流统计；构建一次，整个任务期间复用。"""
    graph_version: str
    allowed: object
    possible: object
    reverses: dict
    counts: dict

    def view(self, name: str):
        return getattr(self, name)

    def reverse(self, name: str):
        """反向视图：反向搜索走的就是它，因为存取柜图的几何是正向存储的。"""
        return self.reverses[name]


_VIEW_CACHE: dict = {}
_VIEW_CACHE_LIMIT = 2


def build_views(graph, *, version: str) -> ServiceViews:
    """按通行判定切出两张子图视图（§5.2）。进程内按 (图, 版本) 缓存。

    视图是 ``edge_subgraph`` 的只读视图，不是副本：整城图按类别深复制一次就是几百 MB。
    反向视图同样只是视图 —— ``reverse(copy=False)`` 保持只读，且过滤器照旧生效
    （视图上的 ``adj`` 与 ``out_edges`` 只返回通过判定的边），这一点由服务图测试钉住。

    缓存**同时持有图本身的强引用**并用 ``is`` 复核：只按 ``id(graph)`` 缓存是错的，
    一个有向图被回收后新图会拿到同一个 ``id``，于是新任务收到上一份图的视图 ——
    症状是视图里查不到自己的节点。强引用把 ``id`` 复用这条路堵死，命中判断也就成立。
    """
    key = (id(graph), version)
    cached = _VIEW_CACHE.get(key)
    if cached is not None and cached[0] is graph:
        return cached[1]
    allowed_edges, possible_edges = [], []
    denied_nodes, conditional_nodes = set(), set()
    for node, data in graph.nodes(data=True):
        verdict = classify_barrier(data.get("barrier"))
        if verdict == DENIED:
            denied_nodes.add(node)
        elif verdict == POSSIBLE:
            conditional_nodes.add(node)
    for u, v, key_ in graph.edges(keys=True):
        verdict = classify_edge(graph.edges[u, v, key_])
        if verdict == DENIED:
            continue
        # 墙、栅栏两侧过不去：两个视图都不要这条边。
        if u in denied_nodes or v in denied_nodes:
            continue
        possible_edges.append((u, v, key_))
        # 门禁不明的门只进可能允许图：它可能开着，但不要假定它开着。
        if verdict == ALLOWED and u not in conditional_nodes and v not in conditional_nodes:
            allowed_edges.append((u, v, key_))
    allowed = graph.edge_subgraph(allowed_edges)
    possible = graph.edge_subgraph(possible_edges)
    views = ServiceViews(
        graph_version=version,
        allowed=allowed,
        possible=possible,
        reverses={ALLOWED: allowed.reverse(copy=False), POSSIBLE: possible.reverse(copy=False)},
        counts={"edges_allowed": len(allowed_edges), "edges_possible": len(possible_edges),
                "edges_total": graph.number_of_edges(), "nodes_denied": len(denied_nodes),
                "nodes_conditional": len(conditional_nodes)},
    )
    if len(_VIEW_CACHE) >= _VIEW_CACHE_LIMIT:
        _VIEW_CACHE.clear()
    _VIEW_CACHE[key] = (graph, views)
    return views


def reverse_field(views: ServiceViews, seeds: dict, *, cutoff_m: float = SEARCH_CUTOFF_M) -> dict:
    """反向多源最短路：每个节点到最近入口的模型距离（米），两个视图各一份（§5.4）。

    返回 ``{view: {node: distance_m}}``。没被搜到的节点不在结果里 —— 那是"没有模型
    路径"，不是"距离为零"：调用方必须把它与"超出截止"区分开。
    """
    if not seeds:
        return {name: {} for name in VIEWS}
    return {name: cutoff_dijkstra(views.reverse(name), seeds, cutoff_m, weight=LENGTH_WEIGHT)
            for name in VIEWS}


def reverse_field_owners(views: ServiceViews, seeds_with_owner: dict,
                         *, cutoff_m: float = SEARCH_CUTOFF_M) -> dict:
    """:func:`reverse_field` 的带设施归属版本：``{view: {node: (distance_m, facility_id)}}``。

    归属与距离来自同一次搜索，因此"最近"不会出现两个互相矛盾的答案。
    """
    if not seeds_with_owner:
        return {name: {} for name in VIEWS}
    return {name: cutoff_dijkstra_owners(views.reverse(name), seeds_with_owner, cutoff_m,
                                         weight=LENGTH_WEIGHT)
            for name in VIEWS}


def sample_distance(attachment: Attachment, field: dict) -> float | None:
    """评估点到类别设施的最短模型距离：接入成本 + 沿边到端点 + 端点到入口（§5.4）。

    取两个端点的最小值，因此落在边中部的评估点不必先绕到"最近的那个端点"再走回来，
    同一条边上的起终点直接路径也在其中。
    """
    best = _sample_choice(attachment, field)
    return None if best is None else best[0]


def sample_nearest_facility(attachment: Attachment, field: dict, owners: dict) -> str | None:
    """同一段路走出来的最近设施 ID：与 :func:`sample_distance` 用的是同一次比较。"""
    best = _sample_choice(attachment, field)
    return None if best is None else owners.get(best[1])


def _sample_choice(attachment: Attachment, field: dict) -> tuple[float, object] | None:
    best = None
    for node, along in attachment.seeds.items():
        reached = field.get(node)
        if reached is None:
            continue
        total = attachment.distance_m + along + reached
        if best is None or total < best[0]:
            best = (total, node)
    return best


def coverage_geometry(views: ServiceViews, seeds: dict, *, cutoff_m: float = SEARCH_CUTOFF_M,
                      weight: str = LENGTH_WEIGHT):
    """类别服务面的展示几何：反向可达边区间的并集（§5.4 的部分可达区间恢复）。

    这是**模型估计**，不是实测：它只回答"路网上 1100 米内能走到哪里"，面积结论一律
    来自网格（§6.1）。
    """
    if not seeds:
        return None, {name: {"full_edges": 0, "partial_edges": 0} for name in VIEWS}
    settled = {name: cutoff_dijkstra(views.reverse(name), seeds, cutoff_m, weight=weight)
               for name in VIEWS}
    return coverage_from_settled(views, settled, cutoff_m=cutoff_m, weight=weight)


def coverage_from_settled(views: ServiceViews, settled: dict, *,
                          cutoff_m: float = SEARCH_CUTOFF_M, weight: str = LENGTH_WEIGHT):
    """:func:`coverage_geometry` 用已经算好的距离场出图，不再跑第二次搜索。

    服务场判格子和画可达面要的是同一个东西，重跑一次 Dijkstra 既慢又可能因为种子构造
    不同而画出与结论不一致的面。``settled`` 就是 ``{view: {node: cost}}``，与
    :func:`reverse_field` 的返回同形。

    ``flipped=True`` 是因为反向搜索到达的是几何的尾段 —— 少了它，边界会画到起点那一侧，
    看起来像覆盖了错误的方向。
    """
    from ..algorithms.osm_offline.edge_intervals import reachable_intervals

    if not settled or not any(settled.values()):
        return None, {name: {"full_edges": 0, "partial_edges": 0} for name in VIEWS}
    reached = {}
    for name in VIEWS:
        reverse = views.reverse(name)
        reached[name] = reachable_intervals(reverse, settled.get(name, {}), cutoff_m,
                                            weight=weight, flipped=True)
    return reached[ALLOWED].geometry, {name: {"full_edges": network.full_edges,
                                              "partial_edges": network.partial_edges}
                                       for name, network in reached.items()}
