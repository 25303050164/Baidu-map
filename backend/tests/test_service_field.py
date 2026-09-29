"""§5.2/§5.4 的服务场在 §6.1 网格上的三态判定。

这里的每一条断言都盯着一种"看起来很合理"的误判：

* 1000±100 米**不是**放宽半径：带内必须细化，细化后仍无定论就是未知，不能算覆盖；
* "没有模型路径"有两种成因 —— 距离超过截止（可以判灰区）与图不连通（缺图，只能未知）；
* 一个类别的入口全部未解决时，整个类别是未知，不是"到处都没有服务"；
* 附近有入口未解决的同类设施时，不许判灰区。
"""
import networkx as nx
import pytest
from shapely.geometry import LineString, Point, box

from app.accessibility.field import ServiceField
from app.accessibility.grid import COVERED, GAP, REFINE, UNKNOWN, Cell, explore
from app.accessibility.service_graph import Entrance, attach_point, build_views
from app.algorithms.osm_offline.graph_store import PEDESTRIAN_ATTRS, GraphStore
from app.facilities import RULE

CRS = "EPSG:32651"
SPEED = 1.3
CELL_AREA = 49.8 ** 2          # 一格被薄评估域裁出的面积

#: 一条 0–2400 米、每 400 米一个节点的东西走廊，全部双向可步行。
CORRIDOR = {"e": (0.0, 0.0), "a": (400.0, 0.0), "b": (800.0, 0.0), "c": (1200.0, 0.0),
            "d": (1600.0, 0.0), "f": (2000.0, 0.0), "g": (2400.0, 0.0)}


def build_store(nodes, ways, *, barriers=None):
    graph = nx.MultiDiGraph(crs=CRS, osm_data_version="service-field-test", walking_speed_mps=SPEED)
    for node, (x, y) in nodes.items():
        attributes = {"x": float(x), "y": float(y)}
        if barriers and node in barriers:
            attributes["barrier"] = barriers[node]
        graph.add_node(node, **attributes)
    for index, (u, v, tags, both) in enumerate(ways):
        for tail, head in ((u, v), (v, u)) if both else ((u, v),):
            line = LineString([nodes[tail], nodes[head]])
            data = dict.fromkeys(PEDESTRIAN_ATTRS)
            data.update(geometry=line, length=line.length, travel_time_s=line.length / SPEED,
                        osmid=index + 1, **tags)
            graph.add_edge(tail, head, **data)
    return GraphStore(graph, speed=SPEED, crs=CRS)


def corridor_ways():
    order = ["e", "a", "b", "c", "d", "f", "g"]
    return [(order[i], order[i + 1], {}, True) for i in range(len(order) - 1)]


def entrance_at(store, xy, *, facility_id="f1", category="medical"):
    attachment, reason = attach_point(store, xy)
    return Entrance(facility_id, category, "point", Point(xy), attachment,
                    "valid" if attachment else "unresolved", reason)


def views_of(store, version="field-test"):
    return build_views(store.graph, version=version)


def test_attachment_cache_is_exact_bounded_and_cannot_cross_graphs():
    from app.accessibility.service_graph import PointAttachments
    store = build_store(CORRIDOR, corridor_ways())
    cached = PointAttachments(store, max_entries=1)
    at_limit = cached.resolve((100, 50))
    assert at_limit[0] is not None
    assert cached.resolve((100, 50)) is at_limit
    assert cached.resolve((100, 50 + 1e-8))[0] is None
    assert len(cached._items) == 1
    other = build_store(CORRIDOR, corridor_ways())
    with pytest.raises(ValueError, match='attachment_cache_store_mismatch'):
        field_for(other, [], attachments=cached)


def field_for(store, entrances, *, query_complete=True, **kwargs):
    return ServiceField(category="medical", rule=RULE, store=store, views=views_of(store),
                        entrances=entrances, query_complete=query_complete, **kwargs)


def one_cell_domain(x0: float, x1: float):
    """只覆盖一格、且让支持点落在路旁 50 米内的评估域。"""
    return box(x0 + 0.1, 0.1, x1 - 0.1, 49.9)


def test_a_cell_well_inside_the_threshold_is_covered_without_refinement():
    store = build_store(CORRIDOR, corridor_ways())
    field = field_for(store, [entrance_at(store, (0.0, 0.0))])
    outcome = explore(one_cell_domain(300, 350), field.evaluate)
    assert [(leaf.cell.id, leaf.verdict) for leaf in outcome.leaves] == [("0:6:0", COVERED)]
    assert outcome.leaves[0].area_m2 == pytest.approx(CELL_AREA, abs=1e-6)
    assert outcome.refined == 0 and not outcome.capped
    # 证据是可复核的：支持点全部为真、没有视图冲突、模型距离含两侧吸附成本。
    assessment = field.assess(Cell(6, 0), [Point(325.0, 25.0)])
    assert [sample.value for sample in assessment.samples] == [True]
    assert assessment.samples[0].attachment_m == pytest.approx(25.0)
    assert assessment.min_distance_m == pytest.approx(350.0)      # 25 米吸附 + 325 米沿路
    assert assessment.samples[0].conflict is False


def test_a_cell_inside_the_tolerance_band_refines_and_stays_unknown_not_covered():
    """1000±100 米是**不确定**：既不能算覆盖，也不到"明确超过"，细化后只能是未知。

    评估域取在 900–1000 米之间：格内**任何**支持点的模型距离都严格落在误差带里，
    因此这条断言不依赖支持点具体落在格内的哪个位置（代表点恰好落在 1000 米上会让
    ``distance_within`` 返回 True，那是规则本身的端点包含，不是这一段要证明的事）。
    """
    store = build_store(CORRIDOR, corridor_ways())
    field = field_for(store, [entrance_at(store, (0.0, 0.0))])
    outcome = explore(one_cell_domain(900, 950), field.evaluate)
    assert outcome.refined == 1 and not outcome.capped
    # 四个 25 米子格（1 米级索引翻倍，所以 0/1 行都在格内）都还在带里。
    assert [(leaf.cell.level, leaf.verdict) for leaf in outcome.leaves] == [(1, UNKNOWN)] * 4
    assert all(leaf.reason == "distance_in_tolerance_band" for leaf in outcome.leaves)
    assert outcome.by_verdict(COVERED) == [] and outcome.by_verdict(GAP) == []
    assert outcome.area_m2 == pytest.approx(CELL_AREA, abs=1e-9)     # 四个 25 米子格之和


def test_a_cell_beyond_the_cutoff_in_the_same_component_is_a_gap():
    store = build_store(CORRIDOR, corridor_ways())
    field = field_for(store, [entrance_at(store, (0.0, 0.0))])
    outcome = explore(one_cell_domain(1250, 1300), field.evaluate)
    assert [(leaf.cell.id, leaf.verdict) for leaf in outcome.leaves] == [("0:25:0", GAP)]
    assert outcome.leaves[0].reason is None
    assert outcome.refined == 0


def test_a_cell_on_a_disconnected_piece_of_road_is_unknown_not_a_gap():
    """缺图不是 0 分：路网在这里断开，模型路径不存在，但那不等于"没有服务"。"""
    nodes = {"e": (0.0, 0.0), "a": (400.0, 0.0), "i0": (1200.0, 0.0), "i1": (1600.0, 0.0)}
    store = build_store(nodes, [("e", "a", {}, True), ("i0", "i1", {}, True)])
    field = field_for(store, [entrance_at(store, (0.0, 0.0))])
    outcome = explore(one_cell_domain(1200, 1250), field.evaluate)
    assert [(leaf.cell.id, leaf.verdict) for leaf in outcome.leaves] == [("0:24:0", UNKNOWN)]
    assert outcome.leaves[0].reason == "graph_disconnected"
    assert field.counts["graph_disconnected"] >= 1


def test_a_category_without_a_single_valid_entrance_is_entirely_unknown():
    """设施位置不明的类别：整类别未知，而且不为此细化上百个格。"""
    store = build_store(CORRIDOR, corridor_ways())
    unresolved = Entrance("lost", "medical", "point", Point(2500.0, 300.0), None, "unresolved",
                         "no_legal_attachment")
    field = field_for(store, [unresolved])
    outcome = explore(one_cell_domain(300, 350), field.evaluate)
    assert [(leaf.verdict, leaf.reason) for leaf in outcome.leaves] == [(UNKNOWN, "no_valid_entrance")]
    assert outcome.refined == 0
    assert field.summary()["valid_entrances"] == 0
    assert field.summary()["unresolved_entrances"] == 1


def test_a_cell_without_legal_attachment_is_unknown_with_the_reason():
    store = build_store(CORRIDOR, corridor_ways())
    field = field_for(store, [entrance_at(store, (0.0, 0.0))])
    from shapely.geometry import box
    far_from_road = box(300.1, 400.0, 349.9, 449.9)          # 离走廊 400 米
    outcome = explore(far_from_road, field.evaluate)
    assert [(leaf.verdict, leaf.reason) for leaf in outcome.leaves] == [
        (UNKNOWN, "no_legal_attachment")]


def test_the_two_views_disagreeing_on_a_conditional_gate_is_unknown():
    """严格视图走不到、乐观视图走得到：结论取决于一处条件通行，只能是未知。"""
    nodes = {"p0": (0.0, 0.0), "p1": (300.0, 0.0), "p2": (600.0, 0.0), "p3": (900.0, 0.0)}
    store = build_store(nodes, [("p0", "p1", {}, True),
                                ("p1", "p2", {"access": "customers"}, True),
                                ("p2", "p3", {}, True)])
    field = field_for(store, [entrance_at(store, (0.0, 0.0))])
    outcome = explore(one_cell_domain(600, 650), field.evaluate)
    assert [(leaf.verdict, leaf.reason) for leaf in outcome.leaves] == [(UNKNOWN, "views_disagree")]
    assert outcome.refined == 0
    assessment = field.assess(Cell(12, 0), [Point(625.0, 25.0)])
    assert assessment.samples[0].conflict is True
    assert assessment.samples[0].value is None
    assert field.counts["views_conflict"] >= 1


def test_an_unresolved_entrance_nearby_suppresses_the_gap_verdict():
    """§5.3：未解决的入口会影响附近的缺口判断，必须记下影响范围而不是照判灰区。"""
    store = build_store(CORRIDOR, corridor_ways())
    unresolved = Entrance("lost", "medical", "point", Point(2500.0, 300.0), None, "unresolved",
                         "no_legal_attachment")
    field = field_for(store, [entrance_at(store, (0.0, 0.0)), unresolved])
    near = explore(one_cell_domain(2400, 2450), field.evaluate)
    assert [(leaf.verdict, leaf.reason) for leaf in near.leaves] == [
        (UNKNOWN, "entrance_unresolved_nearby")]
    # 直线距离超过影响半径时灰区照判：保守筛选不能把整张图都变成未知。
    far = explore(one_cell_domain(1250, 1300), field.evaluate)
    assert [(leaf.cell.id, leaf.verdict, leaf.reason) for leaf in far.leaves] == [("0:25:0", GAP, None)]


def test_incomplete_queries_never_produce_a_gap():
    """必要查询没有完成时不许判灰区（§6.2）：不可达只能是未知。"""
    store = build_store(CORRIDOR, corridor_ways())
    entrance = entrance_at(store, (0.0, 0.0))
    complete = field_for(store, [entrance])
    assert explore(one_cell_domain(1250, 1300), complete.evaluate).leaves[0].verdict == GAP
    partial = field_for(store, [entrance], query_complete=False)
    assert explore(one_cell_domain(1250, 1300), partial.evaluate).leaves[0].verdict == UNKNOWN
    assert partial.summary()["query_complete"] is False


def test_refinement_triggers_come_from_the_boundary_obstacle_and_verification():
    store = build_store(CORRIDOR, corridor_ways())
    entrance = entrance_at(store, (0.0, 0.0))
    domain = one_cell_domain(300, 350)
    covering = LineString([(320.0, 0.0), (320.0, 50.0)])       # 穿过这一格的圈边界
    field = field_for(store, [entrance], boundary=covering)
    assert explore(domain, field.evaluate).refined == 1
    # 边界挪开之后这一格不再被细化：触发器必须真的由边界引起。
    elsewhere = field_for(store, [entrance], boundary=LineString([(900.0, 0.0), (900.0, 50.0)]))
    assert explore(domain, elsewhere.evaluate).refined == 0
    obstacle = field_for(store, [entrance], obstacle_intersects=lambda geometry: geometry.area > 0)
    assert explore(domain, obstacle.evaluate).refined == 1
    verified = field_for(store, [entrance], verification_conflict=lambda geometry: True)
    assert explore(domain, verified.evaluate).refined == 1


def test_every_cell_reports_only_the_four_documented_verdicts():
    """网格只接受 covered/gap/unknown/refine —— 判定不许漏出第五种状态。"""
    store = build_store(CORRIDOR, corridor_ways())
    field = field_for(store, [entrance_at(store, (0.0, 0.0))])
    seen = set()
    for start in range(50, 2400, 50):
        for point in [Point(start + 25.0, 25.0), Point(start + 1.0, 1.0)]:
            seen.add(field.evaluate(Cell(start // 50, 0), [point])[0])
    assert seen <= {COVERED, GAP, UNKNOWN, REFINE}
    assert REFINE in seen and COVERED in seen and GAP in seen
