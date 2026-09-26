"""§6.5 灰区合并：共享边邻接、孔洞保留、综合灰区口径、标签与建议措辞。"""
import pytest
from shapely.geometry import LineString, box

from app.accessibility.grid import COVERED, GAP, UNKNOWN, Cell
from app.accessibility.zones import (COMPOSITE, MIN_LABEL_AREA_M2, SUGGESTIONS, CellRecord,
                                    merge_zones)

DOMAIN = box(0, 0, 200, 200)


def rec(ix, iy, category, verdict=GAP, geometry=None, **kw):
    cell = Cell(ix, iy)
    return CellRecord(cell=cell, category=category, verdict=verdict,
                      geometry=cell.geometry() if geometry is None else geometry, **kw)


def test_shared_edge_merges_but_point_contact_does_not():
    """对角相邻的两个格只共一个点：合并它们会把两个被隔开的角落写成一片。"""
    edge = merge_zones([rec(0, 0, "clinic"), rec(1, 0, "clinic")], domain=DOMAIN)
    assert len(edge) == 1 and edge[0].area_m2 == pytest.approx(100 * 50)
    corner = merge_zones([rec(0, 0, "clinic"), rec(1, 1, "clinic")], domain=DOMAIN)
    assert len(corner) == 2
    assert all(zone.area_m2 == pytest.approx(2500) for zone in corner)


def test_the_obstacle_predicate_sees_the_shared_boundary_not_the_cells():
    """障碍判据收到的是共享边本身：给它整块几何会连"格内部有没有障碍"都算成不连通。"""
    seen = []

    def obstacle(shared):
        seen.append(shared)
        return False

    zones = merge_zones([rec(0, 0, "clinic"), rec(1, 0, "clinic")], domain=DOMAIN,
                        obstacle_between=obstacle)
    assert len(zones) == 1
    assert [geometry.geom_type for geometry in seen] == ["LineString"]
    assert seen[0].length == pytest.approx(50)


def test_an_obstacle_on_the_shared_edge_keeps_the_two_sides_apart():
    """河的两岸共享一条边，但这条边被障碍穿过时不能合并成一片灰区。"""
    river = LineString([(50, 0), (50, 50)])
    zones = merge_zones([rec(0, 0, "clinic"), rec(1, 0, "clinic")], domain=DOMAIN,
                        obstacle_between=lambda shared: shared.intersects(river))
    assert len(zones) == 2
    assert sorted(zone.cell_ids for zone in zones) == [("0:0:0",), ("0:1:0",)]


def test_the_union_keeps_the_hole_in_the_middle_of_a_ring():
    """中间那格有服务：合并外圈不能把它填掉，孔洞是"这一圈缺、中间不缺"的证据。"""
    ring = [rec(ix, iy, "clinic") for ix, iy in
            ((0, 0), (1, 0), (2, 0), (0, 1), (2, 1), (0, 2), (1, 2), (2, 2))]
    zones = merge_zones(ring, domain=DOMAIN)
    assert len(zones) == 1
    zone = zones[0]
    assert zone.parts == 1 and len(zone.geometry.interiors) == 1
    assert zone.area_m2 == pytest.approx(8 * 2500)


def test_no_closing_operation_fills_a_narrow_channel():
    """一条 1 米宽的通道不能因为显示方便就被闭运算抹掉：两侧是两片独立的灰区。"""
    left = box(0, 0, 49.5, 50)
    right = box(50.5, 0, 100, 50)
    zones = merge_zones([rec(0, 0, "clinic", geometry=left),
                         rec(1, 0, "clinic", geometry=right)], domain=box(0, 0, 100, 50))
    assert len(zones) == 2
    assert all(zone.area_m2 == pytest.approx(49.5 * 50) for zone in zones)
    assert sum(zone.area_m2 for zone in zones) < 100 * 50


def test_area_is_the_union_not_the_sum_of_the_cells():
    """同一个格同时缺两类设施：综合灰区仍然只算这一格的面积，相加就是重复计数。"""
    zones = merge_zones([rec(0, 0, "clinic"), rec(0, 0, "school")], domain=DOMAIN,
                        majors=("clinic", "school", "market"))
    composite = [zone for zone in zones if zone.kind == COMPOSITE]
    assert len(composite) == 1
    assert composite[0].area_m2 == pytest.approx(2500)
    assert composite[0].cell_ids == ("0:0:0",)      # 一格两类，格数仍是一
    assert composite[0].categories == ("clinic", "school")


def test_composite_needs_two_of_the_three_majors_and_unknown_is_not_missing():
    """三类里一类是 gap、一类是 unknown 的格不是综合灰区：unknown 不是"缺"。"""
    records = [rec(0, 0, "clinic"), rec(0, 0, "school"),
               rec(1, 0, "clinic"), rec(1, 0, "school", verdict=UNKNOWN),
               rec(2, 0, "market", verdict=UNKNOWN), rec(2, 0, "school", verdict=UNKNOWN)]
    zones = merge_zones(records, domain=DOMAIN, majors=("clinic", "school", "market"))
    composite = [zone for zone in zones if zone.kind == COMPOSITE]
    assert len(composite) == 1 and composite[0].cell_ids == ("0:0:0",)
    # 每类灰区照旧各自成区：(0,0) 与 (1,0) 的 clinic 共享边所以是一片，
    # unknown 的两格不落在任何区里。
    assert sorted(zone.cell_ids for zone in zones if zone.kind == "single") == \
        [("0:0:0",), ("0:0:0", "0:1:0")]
    assert all("0:2:0" not in zone.cell_ids for zone in zones)


def test_a_category_outside_the_majors_never_joins_a_composite_zone():
    """非三大类之间再怎么缺也不构成综合灰区，只在各自类别里成区。"""
    zones = merge_zones([rec(0, 0, "bank"), rec(0, 0, "park")], domain=DOMAIN,
                        majors=("clinic", "school", "market"))
    assert [zone.kind for zone in zones] == ["single", "single"]


def test_zones_are_clipped_back_to_the_assessment_domain():
    """并集之后重新裁剪：评估域外面的部分不属于这份报告的面积。"""
    zones = merge_zones([rec(0, 0, "clinic"), rec(1, 0, "clinic"), rec(2, 0, "clinic")],
                        domain=box(0, 0, 120, 50))
    assert len(zones) == 1
    assert zones[0].parts == 1
    assert zones[0].area_m2 == pytest.approx(120 * 50)


def test_a_small_zone_keeps_its_geometry_and_area_but_not_its_label():
    """小于 2500 m² 不默认显示标签：面积结论不因为标签规则而改变。"""
    zones = merge_zones([rec(0, 0, "clinic")], domain=box(0, 0, 20, 50))
    zone = zones[0]
    assert zone.area_m2 == pytest.approx(1000)
    assert zone.area_m2 < MIN_LABEL_AREA_M2
    assert zone.label_visible is False
    assert not zone.geometry.is_empty


def test_the_zone_record_carries_the_evidence_the_report_needs():
    """一个区要能独立回答：缺什么、多大、多可信、最近的一家在哪、为什么、下一步做什么。"""
    zones = merge_zones([rec(0, 0, "clinic", distance_m=800, nearest_facility="B"),
                         rec(1, 0, "clinic", distance_m=400, nearest_facility="A")],
                        domain=DOMAIN)
    zone = zones[0]
    assert zone.id == "single:clinic:0:0:0"
    assert zone.categories == ("clinic",) and zone.kind == "single"
    assert zone.cell_ids == ("0:0:0", "0:1:0")
    assert zone.nearest_facility == "A"           # 取最近的，不是列表里第一个
    assert set(zone.as_dict()) >= {"id", "index", "categories", "kind", "areaM2", "parts",
                                   "cellIds", "labelVisible", "suspected", "evidenceGrade",
                                   "queryStatus", "nearestFacility", "reason", "suggestion",
                                   "geometry"}


def test_the_reason_is_the_most_specific_one_among_the_members():
    """具体原因优先于笼统的"证据不足"：可复核的理由才能被核查。"""
    zones = merge_zones([rec(0, 0, "clinic", reason="distance_in_tolerance_band"),
                         rec(1, 0, "clinic", reason="entrance_unresolved_nearby")],
                        domain=DOMAIN)
    assert zones[0].reason == "entrance_unresolved_nearby"
    assert zones[0].suggestion == SUGGESTIONS["entrance_unresolved_nearby"]


def test_suggestions_are_follow_up_checks_never_construction_conclusions():
    """建议只能指向补采、通道核查、设施核查，不能把模型结果写成确定的建设选址结论。"""
    # §6.5：建议只能指向补采、通道核查、设施核查这三类动作。
    actions = ("核查", "采集", "补全")
    forbidden = ("建设", "应建", "选址", "必须新增", "确定没有", "确实缺失")
    for text in SUGGESTIONS.values():
        assert any(action in text for action in actions)
        assert not any(word in text for word in forbidden)
    corridor = merge_zones([rec(0, 0, "clinic"), rec(1, 0, "clinic")], domain=DOMAIN,
                           obstacle_between=lambda shared: False)
    assert corridor[0].suggestion == SUGGESTIONS["corridor"]


def test_the_evidence_grade_is_the_weakest_member():
    """一片区域里只有一部分核验过，整体就不能标成核验过。"""
    mixed = merge_zones([rec(0, 0, "clinic", evidence_grade="verified"),
                         rec(1, 0, "clinic", evidence_grade="model")], domain=DOMAIN)
    assert mixed[0].evidence_grade == "model" and mixed[0].suspected is True
    verified = merge_zones([rec(0, 0, "clinic", evidence_grade="verified")], domain=DOMAIN)
    assert verified[0].evidence_grade == "verified" and verified[0].suspected is False


def test_a_partial_query_propagates_and_becomes_the_reason_when_nothing_else_explains_it():
    """检索没跑完的区标为部分检索：它的面积是"已查到的部分"，不能当成完整结论。"""
    zones = merge_zones([rec(0, 0, "clinic"), rec(1, 0, "clinic", query_complete=False)],
                        domain=DOMAIN)
    assert zones[0].query_status == "partial"
    assert zones[0].reason == "query_status"
    assert zones[0].suggestion == SUGGESTIONS["query_status"]
    complete = merge_zones([rec(0, 0, "clinic")], domain=DOMAIN)
    assert complete[0].query_status == "complete"


def test_only_gap_cells_ever_produce_zones():
    """covered 与 unknown 都不成区：把未知合并进灰区等于把没结论说成没服务。"""
    zones = merge_zones([rec(0, 0, "clinic", verdict=COVERED),
                         rec(1, 0, "clinic", verdict=UNKNOWN),
                         rec(2, 0, "clinic")], domain=DOMAIN)
    assert [zone.cell_ids for zone in zones] == [("0:2:0",)]
    assert merge_zones([], domain=DOMAIN) == []
    assert merge_zones([rec(0, 0, "clinic", verdict=UNKNOWN)], domain=DOMAIN) == []


def test_a_gap_cell_outside_the_domain_does_not_become_a_zero_area_zone():
    """与评估域只剩相切的格不是一片灰区：面积为零的条目会让灰区数量虚增。"""
    touching = merge_zones([rec(4, 0, "clinic")], domain=DOMAIN)   # 格子从 x=200 起
    assert touching == []


def test_zone_ids_are_stable_and_the_numbering_follows_the_area():
    """同一个输入两次合并得到同样的 ID 与编号；编号按面积降序，给人看的顺序才有意义。"""
    records = [rec(0, 0, "clinic"), rec(1, 0, "clinic"), rec(0, 3, "school")]
    first = merge_zones(records, domain=DOMAIN)
    second = merge_zones(list(reversed(records)), domain=DOMAIN)
    assert [(zone.id, zone.index) for zone in first] == \
        [(zone.id, zone.index) for zone in second]
    assert [zone.index for zone in first] == [1, 2]
    assert first[0].area_m2 == pytest.approx(5000)
    assert first[1].area_m2 == pytest.approx(2500)
    assert {zone.categories for zone in first} == {("clinic",), ("school",)}
