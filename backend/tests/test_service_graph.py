"""§5.2 两张只读视图、§5.3 入口接入、§5.4 反向多源最短路。

这些测试的价值全在"证据方向"上，所以每条断言都尽量钉在一个会被相反实现破坏的位置：

* 视图必须是**只读视图**且过滤器在正反两个方向都生效 —— 否则被禁止的边会在反向搜索里
  悄悄变成可通行，缺口凭空消失；
* 条件通行只出现在可能允许图里，于是两个视图会给出不同结论（§5.2 要求这种不同落成未知，
  那是 field.py 的事，但视图本身必须真的不同）；
* 接入只有 50 米，且候选不相容、连接段穿障碍时不猜；
* 反向搜索的距离从入口量起、截止 1100 米，且**超出截止是"没有值"而不是 0**。
"""
import gc

import networkx as nx
import pytest
from shapely.geometry import LineString, Point

from app.accessibility.service_graph import (ALLOWED, DENIED, POSSIBLE, SEARCH_CUTOFF_M,
                                             SUPPORT_LIMIT_M, Entrance, attach_point,
                                             build_views, classify_barrier, classify_edge,
                                             coverage_geometry, entrance_seeds, resolve_entrances,
                                             reverse_field, sample_distance)
from app.algorithms.osm_offline.graph_store import PEDESTRIAN_ATTRS, GraphStore
from app.facilities import RULE
from app.rules import distance_within

CRS = "EPSG:32651"
SPEED = 1.3


def build_store(nodes, ways, *, barriers=None):
    """一个真实的 :class:`GraphStore`（冻结、带 STRtree）。

    每条 way 是 ``(u, v, tags, both_directions)``；``osmid`` 缺省按顺序生成，因为
    "两个候选是不是同一条路"要靠它判断。
    """
    graph = nx.MultiDiGraph(crs=CRS, osm_data_version="service-graph-test", walking_speed_mps=SPEED)
    for node, (x, y) in nodes.items():
        attributes = {"x": float(x), "y": float(y)}
        if barriers and node in barriers:
            attributes["barrier"] = barriers[node]
        graph.add_node(node, **attributes)
    for index, (u, v, tags, both) in enumerate(ways):
        osmid = tags.get("osmid", index + 1)
        for tail, head in ((u, v), (v, u)) if both else ((u, v),):
            line = LineString([nodes[tail], nodes[head]])
            data = dict.fromkeys(PEDESTRIAN_ATTRS)
            data.update(geometry=line, length=line.length, travel_time_s=line.length / SPEED,
                        osmid=osmid, **{k: val for k, val in tags.items() if k != "osmid"})
            graph.add_edge(tail, head, **data)
    return GraphStore(graph, speed=SPEED, crs=CRS)


#: 650 米一段的东西走廊（末尾一段 1600 米，用来试"沿边到端点超过截止"的情形），
#: 加上 w1 处向北 400 米的支路。全部双向。
CORRIDOR = {"w0": (0.0, 0.0), "w1": (650.0, 0.0), "w2": (1300.0, 0.0), "w3": (1950.0, 0.0),
            "w4": (2600.0, 0.0), "w5": (4200.0, 0.0), "n1": (650.0, 200.0), "n2": (650.0, 400.0)}
CORRIDOR_WAYS = [("w0", "w1", {}, True), ("w1", "w2", {}, True), ("w2", "w3", {}, True),
                 ("w3", "w4", {}, True), ("w4", "w5", {}, True), ("w1", "n1", {}, True),
                 ("n1", "n2", {}, True)]
#: 入口就在 w1 节点上（接入距离 0），因此"从入口量起的距离"就是"从 w1 量起的距离"。
ENTRANCE_XY = (650.0, 0.0)


def corridor():
    return build_store(CORRIDOR, CORRIDOR_WAYS)


def edges_of(view):
    """视图的有向边集合：视图对不在其中的节点会抛 KeyError，用集合判断才没有歧义。"""
    return set(view.edges(keys=True))


def entrance_at(store, xy, *, limit_m=SUPPORT_LIMIT_M, facility_id="f1", category="medical"):
    attachment, reason = attach_point(store, xy, limit_m=limit_m)
    return Entrance(facility_id, category, "point", Point(xy), attachment,
                    "valid" if attachment else "unresolved", reason)


@pytest.mark.parametrize("tags,verdict", [
    ({}, ALLOWED),
    ({"highway": "footway"}, ALLOWED),
    ({"highway": "residential", "oneway:foot": "yes"}, ALLOWED),   # 方向由有向边的存在表达
    ({"foot": "no"}, DENIED),
    ({"foot": "use_sidepath"}, DENIED),
    ({"access": "private"}, DENIED),
    ({"access": "private", "foot": "yes"}, ALLOWED),               # 私人通行权不排除行人
    ({"highway": "motorway"}, DENIED),
    ({"highway": "motorway_link"}, DENIED),
    ({"highway": "motorway", "foot": "yes"}, DENIED),              # 法定禁行优先于映射标签
    ({"highway": "construction"}, DENIED),
    ({"access": "customers"}, POSSIBLE),
    ({"access": "residents"}, POSSIBLE),
    ({"access": "unknown"}, POSSIBLE),
    ({"foot": "unknown"}, POSSIBLE),
])
def test_edge_permission_order_puts_legal_prohibitions_first(tags, verdict):
    assert classify_edge(tags) == verdict


@pytest.mark.parametrize("barrier,verdict", [
    (None, None), ("no", None), ("none", None),
    ("turnstile", ALLOWED), ("bollard", ALLOWED),
    ("gate", POSSIBLE), ("lift_gate", POSSIBLE), ("yes", POSSIBLE),
    ("wall", DENIED), ("fence", DENIED),
])
def test_barrier_classification_keeps_unknown_gates_out_of_the_allowed_view(barrier, verdict):
    assert classify_barrier(barrier) == verdict


def test_views_partition_edges_and_stay_read_only():
    store = build_store({"a": (0, 0), "b": (300, 0), "c": (600, 0), "d": (900, 0)},
                        [("a", "b", {}, True), ("b", "c", {"foot": "no"}, True),
                         ("c", "d", {"access": "customers"}, True)])
    views = build_views(store.graph, version="test")
    assert views.counts["edges_total"] == 6
    assert views.counts["edges_allowed"] == 2          # 只有 a-b 两个方向
    assert views.counts["edges_possible"] == 4         # 加上 c-d 两个方向
    assert all(edge not in edges_of(views.allowed) for edge in [("b", "c", 0), ("c", "d", 0)])
    assert all(edge not in edges_of(views.possible) for edge in [("b", "c", 0), ("c", "b", 0)])
    assert ("c", "d", 0) in edges_of(views.possible) and ("d", "c", 0) in edges_of(views.possible)
    # 只读：视图上不能加边（视图是共享内存的，改它就是改整城图）。
    with pytest.raises(nx.NetworkXError):
        views.allowed.add_edge("a", "z")
    # 复用同一份图，不复制。
    assert build_views(store.graph, version="test") is views
    assert build_views(store.graph, version="other").counts == views.counts


def test_views_are_never_carried_over_to_another_graph():
    """只按 ``id(graph)`` 缓存会出错：图被回收后新图拿到同一个 id，就会收到上一份视图。"""
    first = build_store({"a": (0, 0), "b": (300, 0)}, [("a", "b", {}, True)])
    views = build_views(first.graph, version="test")
    assert views.counts["edges_possible"] == 2
    del first, views
    gc.collect()
    second = build_store({"a": (0, 0), "b": (300, 0), "c": (600, 0)},
                         [("a", "b", {}, True), ("b", "c", {}, True)])
    fresh = build_views(second.graph, version="test")
    # 新图必须拿到自己的视图：节点与边数都对得上，而不是上一份只剩 a-b 的视图。
    assert fresh.counts["edges_possible"] == 4 and fresh.counts["edges_total"] == 4
    assert set(fresh.possible.nodes()) == {"a", "b", "c"}


def test_a_blocking_barrier_node_removes_its_edges_from_both_views():
    store = build_store({"a": (0, 0), "b": (300, 0), "c": (600, 0), "g": (0, 200)},
                        [("a", "b", {}, True), ("b", "c", {}, True), ("a", "g", {}, True)],
                        barriers={"b": "wall", "g": "gate"})
    views = build_views(store.graph, version="test")
    assert views.counts["nodes_denied"] == 1 and views.counts["nodes_conditional"] == 1
    # 墙节点两侧的边在两个视图里都没有了：过不去就是过不去。
    for edge in [("a", "b", 0), ("b", "a", 0), ("b", "c", 0), ("c", "b", 0)]:
        assert edge not in edges_of(views.possible) and edge not in edges_of(views.allowed)
    # 条件不明的门只从明确允许图里消失，可能允许图仍然保留它。
    assert ("a", "g", 0) in edges_of(views.possible)
    assert ("a", "g", 0) not in edges_of(views.allowed)
    assert views.counts["edges_allowed"] == 0 and views.counts["edges_possible"] == 2


def test_the_two_views_really_disagree_about_conditional_access():
    """§5.2 的核心：条件通行让两个视图给出**不同**的结论，所以必须分成两次搜索。"""
    store = build_store({"a": (0, 0), "b": (300, 0), "c": (600, 0), "d": (900, 0)},
                        [("a", "b", {}, True), ("b", "c", {"access": "customers"}, True),
                         ("c", "d", {}, True)])
    views = build_views(store.graph, version="test")
    seeds = entrance_seeds([entrance_at(store, (0.0, 0.0))])
    fields = reverse_field(views, seeds)
    assert fields[ALLOWED] == {"a": 0.0, "b": 300.0}
    assert fields[POSSIBLE] == {"a": 0.0, "b": 300.0, "c": 600.0, "d": 900.0}
    # 两个视图的差集不是空的：这正是"必须落成未知"的那部分面积。
    assert set(fields[POSSIBLE]) - set(fields[ALLOWED]) == {"c", "d"}


def test_reverse_search_never_walks_a_denied_edge_backwards():
    """反向搜索最危险的一种错法：把禁止的边当成可通行。"""
    store = build_store({"a": (0, 0), "b": (300, 0), "c": (600, 0)},
                        [("a", "b", {}, True), ("b", "c", {"foot": "no"}, True)])
    views = build_views(store.graph, version="test")
    fields = reverse_field(views, entrance_seeds([entrance_at(store, (0.0, 0.0))]))
    for name in (ALLOWED, POSSIBLE):
        assert fields[name] == {"a": 0.0, "b": 300.0}
        assert "c" not in fields[name]


def test_an_entrance_on_a_road_nobody_may_walk_is_refused_not_attached():
    """§5.3：入口必须与实际道路层级一致 —— 只有禁行道路可接时，入口是未解决的。"""
    store = build_store({"a": (0, 0), "b": (300, 0), "c": (600, 0)},
                        [("a", "b", {"foot": "no"}, True), ("b", "c", {}, True)],
                        barriers={"c": "wall"})
    denied_road = entrance_at(store, (150.0, 0.0))
    assert denied_road.status == "unresolved" and denied_road.reason == "no_usable_attachment"
    # 墙节点上的入口同样不可用：挂上去也只是"过不去"。
    assert entrance_at(store, (600.0, 0.0)).reason == "no_usable_attachment"
    # 半径内根本没有路时是另一种原因，核验队列要分得清这两件事。
    assert entrance_at(store, (150.0, 400.0)).reason == "no_legal_attachment"
    assert entrance_seeds([denied_road]) == {}


def test_attachment_measures_the_perpendicular_gap_and_both_legs_along_the_edge():
    store = corridor()
    attachment, reason = attach_point(store, (200.0, 5.0))
    assert reason is None
    assert attachment.distance_m == pytest.approx(5.0)
    # 投影在 w0-w1 上距 w0 200 米处：到 w0 还有 200 米，到 w1 还有 450 米。
    assert attachment.seeds == {"w0": pytest.approx(200.0), "w1": pytest.approx(450.0)}
    assert attachment.length_m == pytest.approx(650.0)


def test_no_attachment_beyond_the_fifty_metre_limit():
    store = corridor()
    attachment, reason = attach_point(store, (200.0, 60.0))
    assert attachment is None and reason == "no_legal_attachment"
    # 同一点放宽到 100 米就能接入 —— 上限是参数，不是隐含的 200 米放宽。
    assert attach_point(store, (200.0, 60.0), limit_m=100.0)[0] is not None
    assert SUPPORT_LIMIT_M == 50.0
    empty = build_store({"only": (0, 0)}, [])
    assert attach_point(empty, (0.0, 0.0)) == (None, "nearest_edge_not_found")


def test_attachment_choice_is_deterministic_when_two_edges_are_equidistant():
    """等距时按 edge_ids 的顺序取最小者的规则必须稳定，否则同一点两次评估会不同。"""
    nodes = {"a": (0, 0), "b": (300, 0), "c": (0, 100), "d": (300, 100)}
    store = build_store(nodes, [("c", "d", {}, True), ("a", "b", {}, True)])
    first = attach_point(store, (150.0, 50.0))[0]
    second = attach_point(store, (150.0, 50.0))[0]
    assert first.edge == second.edge
    assert first.edge == store.edge_ids[min(
        store.index.query_nearest(Point(150.0, 50.0), all_matches=True))]
    assert first.distance_m == pytest.approx(50.0)


def test_entrances_fall_back_to_the_navigation_point():
    store = build_store({"a": (0, 0), "b": (500, 0)}, [("a", "b", {}, True)])
    facilities = [{"id": "far", "location": {"lng": 200.0, "lat": 80.0},
                   "navigationLocation": {"lng": 200.0, "lat": 10.0}}]
    resolved = resolve_entrances(facilities, store=store, category_of=lambda f: "medical")
    assert len(resolved) == 1
    assert (resolved[0].status, resolved[0].source) == ("valid", "navigation")
    assert resolved[0].attachment.distance_m == pytest.approx(10.0)


def test_an_unresolved_entrance_records_why_instead_of_guessing():
    store = build_store({"a": (0, 0), "b": (500, 0)}, [("a", "b", {}, True)])
    facilities = [{"id": "no-coords", "location": {"lng": None, "lat": None}},
                  {"id": "off-road", "location": {"lng": 200.0, "lat": 300.0}}]
    resolved = {item.facility_id: item for item in
                resolve_entrances(facilities, store=store, category_of=lambda f: "medical")}
    assert resolved["no-coords"].reason == "no_location"
    assert resolved["no-coords"].point is None          # 不编造占位坐标
    assert resolved["off-road"].reason == "no_legal_attachment"
    assert all(item.attachment is None for item in resolved.values())


def test_incompatible_candidates_stay_unresolved_instead_of_picking_one():
    """POI 原始点与导航点落在两条互不相容的路上：无法判断哪个是入口，进核验队列。"""
    nodes = {"p0": (-100, 0), "p1": (500, 0), "q0": (-100, 80), "q1": (500, 80)}
    store = build_store(nodes, [("p0", "p1", {}, True), ("q0", "q1", {}, True)])
    facilities = [{"id": "ambiguous", "location": {"lng": 200.0, "lat": 5.0},
                   "navigationLocation": {"lng": 200.0, "lat": 75.0}}]
    resolved = resolve_entrances(facilities, store=store, category_of=lambda f: "medical")[0]
    assert resolved.status == "unresolved"
    assert resolved.reason == "multiple_incompatible_road_candidates"


def test_compatible_candidates_resolve_to_the_nearer_one():
    nodes = {"p0": (-100, 0), "p1": (500, 0), "q0": (-100, 80), "q1": (500, 80)}
    store = build_store(nodes, [("p0", "p1", {}, True), ("q0", "q1", {}, True)])
    facilities = [{"id": "fine", "location": {"lng": 200.0, "lat": 5.0},
                   "navigationLocation": {"lng": 210.0, "lat": 6.0}}]
    resolved = resolve_entrances(facilities, store=store, category_of=lambda f: "medical")[0]
    assert (resolved.status, resolved.source) == ("valid", "point")
    assert resolved.attachment.distance_m == pytest.approx(5.0)


def test_the_obstacle_guard_rejects_only_the_candidate_that_crosses():
    store = build_store({"a": (0, 0), "b": (500, 0)}, [("a", "b", {}, True)])
    facilities = [{"id": "f", "location": {"lng": 200.0, "lat": 40.0},
                   "navigationLocation": {"lng": 300.0, "lat": 10.0}}]
    # 连接段穿水体：原始点被否决，导航点仍然可用 —— 一个候选失败不拖累另一个。
    cross_water = lambda point, projected: point.y > 20
    resolved = resolve_entrances(facilities, store=store, category_of=lambda f: "medical",
                                guard=cross_water)[0]
    assert (resolved.status, resolved.source) == ("valid", "navigation")
    rejected = resolve_entrances(facilities, store=store, category_of=lambda f: "medical",
                                 guard=lambda point, projected: True)[0]
    assert rejected.reason == "connector_crosses_obstacle"


def test_entrance_seeds_take_the_minimum_and_drop_what_the_cutoff_excludes():
    store = corridor()
    near = entrance_at(store, (200.0, 5.0), facility_id="near")
    same_edge = entrance_at(store, (100.0, 5.0), facility_id="same-edge")
    merged = entrance_seeds([near, same_edge])
    # 同一条边上的两个入口在**两个端点上都取最小值**，不是取平均、也不是取某一个入口。
    assert merged["w0"] == pytest.approx(105.0)         # min(200 + 5, 100 + 5)
    assert merged["w1"] == pytest.approx(455.0)         # min(450 + 5, 550 + 5)
    # 长边上的入口：沿边到远端要 1400 米（再加 5 米接入即 1405 米），超过 1100 米的那一端
    # 不作种子，近端照常参与。
    far = entrance_at(store, (4000.0, 5.0), facility_id="far", limit_m=100.0)
    assert far.attachment.seeds == {"w4": pytest.approx(1400.0), "w5": pytest.approx(200.0)}
    assert entrance_seeds([far]) == {"w5": pytest.approx(205.0)}
    assert max(entrance_seeds([far]).values()) < SEARCH_CUTOFF_M
    assert entrance_seeds([Entrance("x", "medical", "point", None, None, "unresolved",
                                    "no_location")]) == {}


def test_reverse_field_measures_from_the_entrance_and_stops_at_the_cutoff():
    store = corridor()
    views = build_views(store.graph, version="test")
    fields = reverse_field(views, entrance_seeds([entrance_at(store, ENTRANCE_XY)]))
    for name in (ALLOWED, POSSIBLE):
        field = fields[name]
        assert field["w1"] == pytest.approx(0.0)
        assert field["w0"] == pytest.approx(650.0)
        assert field["w2"] == pytest.approx(650.0)
        assert field["n2"] == pytest.approx(400.0)
        # 超出截止的节点**不在结果里**：那是"没有值"，不是距离 0，也不是"不可达"。
        assert "w3" not in field
    assert SEARCH_CUTOFF_M == 1100.0


def test_sample_distance_adds_both_legs_and_the_attachment_cost():
    store = corridor()
    views = build_views(store.graph, version="test")
    field = reverse_field(views, entrance_seeds([entrance_at(store, ENTRANCE_XY)]))[ALLOWED]

    def distance(xy, **kw):
        attachment, reason = attach_point(store, xy, **kw)
        assert reason is None
        return sample_distance(attachment, field)

    assert distance((650.0, 0.0)) == pytest.approx(0.0)          # 就在入口上
    assert distance((200.0, 5.0)) == pytest.approx(455.0)        # 5 米接入 + 450 米沿边
    assert distance((1500.0, 0.0)) == pytest.approx(850.0)       # 经过 w2 走 200 + 650
    assert distance((1650.0, 0.0)) == pytest.approx(1000.0)      # 恰好 1000 米
    assert distance((1700.0, 0.0)) == pytest.approx(1050.0)      # 落在 ±100 米误差带里
    # 2400 米处：两个端点都不在 1100 米截止内，因此没有值 —— 而不是 0 或 1100。
    assert distance((2400.0, 0.0)) is None


def test_the_threshold_and_its_band_come_from_the_rule_not_from_the_search_cutoff():
    """误差带是**不确定**，不是放宽半径：900–1100 米之间没有结论（恰好 1000 米除外）。

    这条决定了 field.py 的取向 —— 落在带内的格必须细化，细化后仍无定论就是未知，
    而不是"按 1100 米算覆盖"。正是它让 §7.1 的"最高覆盖率 (C+U)/A"有意义。
    """
    assert RULE.threshold_m == 1000 and RULE.tolerance_m == 100 and RULE.inclusive is True
    assert SEARCH_CUTOFF_M == RULE.threshold_m + RULE.tolerance_m
    assert distance_within(899.9, "walking_route", RULE) is True
    assert distance_within(900.0, "walking_route", RULE) is None       # 带的下沿
    assert distance_within(999.0, "walking_route", RULE) is None
    assert distance_within(1000.0, "walking_route", RULE) is True      # 含端点
    assert distance_within(1050.0, "walking_route", RULE) is None
    assert distance_within(1100.0, "walking_route", RULE) is None      # 带的上沿
    assert distance_within(1100.1, "walking_route", RULE) is False
    # 超出搜索截止的值观察不到，但由"带已在上沿结束"可知它一定是 False —— 不会漏判。
    assert distance_within(5000.0, "walking_route", RULE) is False


def test_coverage_geometry_recovers_the_tail_on_the_far_side_of_the_entrance():
    store = corridor()
    views = build_views(store.graph, version="test")
    geometry, counts = coverage_geometry(views, entrance_seeds([entrance_at(store, ENTRANCE_XY)]))
    # 西 650 米整段 + 东 1100 米（650 到 w2，再往东 450 米）+ 北 400 米支路。
    assert geometry.length == pytest.approx(2150.0, abs=1e-6)
    assert geometry.covers(Point(0.0, 0.0)) and geometry.covers(Point(650.0, 400.0))
    assert geometry.covers(Point(1749.0, 0.0))
    # 反了方向就会画到 w3 那侧（1500–1950），既越过截止又多算 450 米。
    assert not geometry.covers(Point(1751.0, 0.0))
    assert not geometry.covers(Point(1900.0, 0.0))
    assert counts[ALLOWED]["partial_edges"] >= 1
    assert coverage_geometry(views, {})[0] is None
