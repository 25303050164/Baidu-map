"""§5.4：把最短路径权重从时间参数化为距离，两处必须同时改。

体检的服务标准是步行距离 1000 米，而等时圈是 900 秒，两者在 OSM 图上是同一个搜索的
两个权重（``length`` 米与 ``travel_time_s`` 秒，后者恰好等于前者除以步速）。这里用
距离阶梯（899/900/999/1000/1001/1100/1101 米）钉住边界包含关系，并证明：

* 距离权重下 1000 米含端点，1001 米不含；
* 时间权重仍然是缺省行为，且在同一条边上给出**不同**的可达长度（否则两套权重只是
  数字上碰巧相同，测试通不过也发现不了）；
* 反向搜索（设施入口 → 网格点）走的是几何的尾段，不能按近端截取。
"""
import networkx as nx
import pytest
from shapely.geometry import LineString, Point

from app.algorithms.osm_offline.edge_intervals import reachable_intervals
from app.algorithms.osm_offline.graph_store import PEDESTRIAN_ATTRS
from app.algorithms.osm_offline.routing import LENGTH_WEIGHT, TIME_WEIGHT, cutoff_dijkstra

SPEED = 1.3
DISTANCE_BUDGET_M = 1000.0
TIME_BUDGET_S = 900.0


def graph():
    return nx.MultiDiGraph(crs="EPSG:32651", osm_data_version="test", walking_speed_mps=SPEED)


def edge(g, u, v, length_m, *, reverse=False):
    """一条从 (0,0) 到 (length_m,0) 的直边；时间权重严格等于 长度 / 步速。"""
    line = LineString([(0, 0), (length_m, 0)])
    g.add_node(u, x=0, y=0)
    g.add_node(v, x=length_m, y=0)
    data = dict.fromkeys(PEDESTRIAN_ATTRS)
    data.update(geometry=line, length=length_m, travel_time_s=length_m / SPEED, osmid=1)
    g.add_edge(u, v, **data)
    if reverse:
        g.add_edge(v, u, **{**data, "geometry": LineString([(length_m, 0), (0, 0)])})
    return (u, v, 0)


@pytest.mark.parametrize("length_m,reached", [
    (899, True), (900, True), (999, True),
    (1000, True),           # 1000 米含端点：服务标准是"不超过 1000 米"
    (1001, False), (1100, False), (1101, False),
])
def test_distance_ladder_is_inclusive_at_one_thousand_metres(length_m, reached):
    g = graph()
    edge(g, "origin", f"node-{length_m}", length_m)
    settled = cutoff_dijkstra(g, {"origin": 0.0}, DISTANCE_BUDGET_M, weight=LENGTH_WEIGHT)
    assert (f"node-{length_m}" in settled) is reached
    assert settled.get("origin") == 0.0


@pytest.mark.parametrize("length_m,reached", [(1169, True), (1170, True), (1171, False)])
def test_time_weight_is_still_the_default_behaviour(length_m, reached):
    """缺省权重仍是时间：900 秒 × 1.3 米/秒 ≈ 1170 米，与 1000 米完全不同。"""
    g = graph()
    edge(g, "origin", f"node-{length_m}", length_m)
    assert (f"node-{length_m}" in cutoff_dijkstra(g, {"origin": 0.0}, TIME_BUDGET_S)) is reached


def test_the_two_weights_really_differ_on_the_same_edge():
    """同一条 1100 米的边：距离 1000 米到不了，时间 900 秒到得了。"""
    g = graph()
    edge(g, "origin", "far", 1100)
    assert cutoff_dijkstra(g, {"origin": 0.0}, DISTANCE_BUDGET_M, weight=LENGTH_WEIGHT) == {"origin": 0.0}
    assert "far" in cutoff_dijkstra(g, {"origin": 0.0}, TIME_BUDGET_S)


def test_edge_interval_recovery_uses_the_same_weight_as_the_search():
    """1 个 2000 米的边：距离权重截 1000 米，时间权重截 1300 米，两者都从近端量起。"""
    g = graph()
    edge(g, "origin", "far", 2000)
    line = g.edges[("origin", "far", 0)]["geometry"]
    by_distance = reachable_intervals(g, {"origin": 0.0}, DISTANCE_BUDGET_M, weight=LENGTH_WEIGHT)
    assert by_distance.geometry.length == pytest.approx(1000.0, abs=1e-6)
    assert by_distance.geometry.covers(Point(1000, 0))
    assert not by_distance.geometry.covers(Point(1001, 0))
    by_time = reachable_intervals(g, {"origin": 0.0}, TIME_BUDGET_S)
    assert by_time.geometry.length == pytest.approx(TIME_BUDGET_S * SPEED, abs=1e-6)
    # 缺省权重下同一份 geometry 只在近端截一段，不是尾段。
    assert by_time.geometry.covers(line.interpolate(TIME_BUDGET_S * SPEED - 1))
    assert not by_time.geometry.covers(line.interpolate(TIME_BUDGET_S * SPEED + 1))


def test_reverse_recovery_measures_from_the_far_end_of_the_stored_geometry():
    """反向搜索（入口 → 网格点）到达的是尾段；按近端截取会画到相反的一侧。"""
    g = graph()
    edge(g, "a", "b", 2000)
    reverse = g.reverse(copy=False)
    settled = cutoff_dijkstra(reverse, {"b": 0.0}, DISTANCE_BUDGET_M, weight=LENGTH_WEIGHT)
    assert settled == {"b": 0.0}          # a 在 2000 米外，超出 1000 米预算
    tail = reachable_intervals(reverse, settled, DISTANCE_BUDGET_M, weight=LENGTH_WEIGHT, flipped=True)
    assert tail.geometry.covers(Point(1500, 0))
    assert tail.geometry.covers(Point(1000, 0))
    assert not tail.geometry.covers(Point(999, 0))


def test_one_way_edge_is_reachable_one_way_only():
    """单向步行边：从起点到终点可达，反方向不可达 —— 反向搜索不能把它当成双向。"""
    g = graph()
    edge(g, "a", "b", 300)               # 只有 a → b，没有反向边
    reverse = g.reverse(copy=False)
    assert cutoff_dijkstra(reverse, {"b": 0.0}, DISTANCE_BUDGET_M, weight=LENGTH_WEIGHT) == {"b": 0.0, "a": 300.0}
    assert cutoff_dijkstra(reverse, {"a": 0.0}, DISTANCE_BUDGET_M, weight=LENGTH_WEIGHT) == {"a": 0.0}


def test_multi_source_reverse_search_takes_the_nearest_entrance():
    """多源反向搜索：网格点由最近的入口覆盖，取的是最小值而不是累加。"""
    g = graph()
    edge(g, "near", "cell", 200, reverse=True)
    edge(g, "cell", "far", 900, reverse=True)
    reverse = g.reverse(copy=False)
    settled = cutoff_dijkstra(reverse, {"near": 0.0, "far": 0.0}, DISTANCE_BUDGET_M, weight=LENGTH_WEIGHT)
    assert settled["cell"] == 200.0
    assert settled["near"] == 0.0


def test_entrance_seeds_costlier_than_the_budget_are_dropped_without_stopping_the_search():
    """入口自身的接入代价就超过 1000 米时不参与；同一次调用里其他入口照常覆盖网格点。"""
    g = graph()
    edge(g, "too-far", "x", 1500)
    edge(g, "ok", "cell", 400, reverse=True)
    settled = cutoff_dijkstra(g, {"too-far": 1500.0, "ok": 0.0}, DISTANCE_BUDGET_M, weight=LENGTH_WEIGHT)
    assert settled == {"ok": 0.0, "cell": 400.0}


def test_weights_are_declared_once_and_named_the_same_everywhere():
    assert TIME_WEIGHT == "travel_time_s"
    assert LENGTH_WEIGHT == "length"
