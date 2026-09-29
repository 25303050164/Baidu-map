"""§6.1 评估网格：全局对齐、细分是替换、叶格上限不静默粗化。"""
import pytest
from shapely.geometry import Point, box

from app.accessibility.grid import (COVERED, GRID_STEP_M, MAX_LEAF_CELLS, REFINE, REFINED_STEP_M,
                                    UNKNOWN, Cell, cells_covering, explore, support_points)


def test_cells_are_globally_aligned_not_centred_on_the_domain():
    """同一片区域上，两个不同的评估域得到同一批格 ID；格边界是 50 米的整数倍。"""
    first = cells_covering(box(0, 0, 90, 90))
    second = cells_covering(box(20, 20, 110, 110))
    assert {cell.id for cell in first} & {cell.id for cell in second}
    for cell in first + second:
        minx, miny, maxx, maxy = cell.bounds
        assert (minx, miny) == (cell.ix * GRID_STEP_M, cell.iy * GRID_STEP_M)
        assert (maxx - minx, maxy - miny) == (GRID_STEP_M, GRID_STEP_M)
    # 负坐标也走同一套 floor 语义：范围横跨两个格，索引取 floor 而不是向零取整。
    negative = cells_covering(box(-60, -60, -10, -10))
    assert {cell.id for cell in negative} == {"0:-2:-2", "0:-1:-2", "0:-2:-1", "0:-1:-1"}
    assert negative[0].bounds == (-100.0, -100.0, -50.0, -50.0)


def test_only_intersecting_cells_and_areas_add_up_exactly():
    """只评估与评估域相交的格；叶格面积之和等于评估域面积（C+G+U=A 的前提）。"""
    domain = box(10, 10, 130, 80)
    outcome = explore(domain, lambda cell, samples: (COVERED, None))
    assert outcome.area_m2 == pytest.approx(120 * 70, abs=1e-6)
    assert outcome.refined == 0 and not outcome.capped
    # 边界相切也算相交：贴着评估域外沿的格不能被漏掉。
    touching = cells_covering(box(0, 0, 50, 50))
    assert {cell.id for cell in touching} == {"0:0:0", "0:1:0", "0:0:1", "0:1:1"}


def test_refinement_replaces_the_parent_instead_of_adding_to_it():
    """细分后父格消失：四个 25 米子格的面积之和等于父格被评估域裁出的面积。"""
    domain = box(0.1, 0.1, 49.9, 49.9)
    calls: list[Cell] = []

    def evaluate(cell, samples):
        calls.append(cell)
        return (REFINE, None) if cell.level == 0 else (COVERED, None)

    outcome = explore(domain, evaluate)
    assert len(calls) == 5                     # 1 个父格 + 4 个子格
    assert [leaf.cell.level for leaf in outcome.leaves] == [1, 1, 1, 1]
    assert outcome.refined == 1
    assert outcome.area_m2 == pytest.approx(49.8 ** 2, abs=1e-9)
    # 输出按格索引排序，父格不再出现。
    assert [leaf.cell.id for leaf in outcome.leaves] == ["1:0:0", "1:0:1", "1:1:0", "1:1:1"]


def test_refinement_never_enqueues_children_outside_the_assessment_domain():
    """域只盖住半格时，域外那个子格面积为零：留着它只会在报告里写一条假条目。"""
    domain = box(0.1, 0.1, 49.9, 24.9)          # 只覆盖格子下半
    outcome = explore(domain, lambda cell, samples: (REFINE, None))
    assert [(leaf.cell.id, leaf.area_m2) for leaf in outcome.leaves] == [
        ("1:0:0", pytest.approx(49.8 * 24.8 / 2, abs=1e-9)),
        ("1:1:0", pytest.approx(49.8 * 24.8 / 2, abs=1e-9))]
    assert all(leaf.area_m2 > 0 for leaf in outcome.leaves)
    assert outcome.area_m2 == pytest.approx(49.8 * 24.8, abs=1e-9)


def test_leaf_cap_stops_refinement_and_leaves_the_cell_unknown():
    """到顶不再细化：未定论的格保持 unknown 并写明原因，不静默粗化成有结论的大格。"""
    domain = box(0.1, 0.1, 49.9, 49.9)
    # 一个父格换成四个子格要净增 3 个叶格：上限 3 时连第一次细分都换不起。
    outcome = explore(domain, lambda cell, samples: (REFINE, None), max_leaves=3)
    assert outcome.capped is True
    assert len(outcome.leaves) == 1
    assert outcome.leaves[0].verdict == UNKNOWN
    assert outcome.leaves[0].reason == "refinement_capped"
    assert outcome.leaves[0].area_m2 == pytest.approx(49.8 ** 2, abs=1e-9)


def test_failing_evaluation_is_unknown_not_a_silent_zero():
    domain = box(0, 0, 50, 50)

    def explode(cell, samples):
        raise RuntimeError("graph gone")

    outcome = explore(domain, explode)
    assert outcome.leaves[0].verdict == UNKNOWN
    assert outcome.leaves[0].reason == "assessment_failed"


def test_an_invalid_verdict_never_becomes_a_pass():
    outcome = explore(box(0, 0, 50, 50), lambda cell, samples: ("whatever", None))
    assert outcome.leaves[0].verdict == UNKNOWN


def test_domain_larger_than_the_cap_refuses_instead_of_pretending():
    domain = box(0, 0, 50 * 20, 50 * 20)
    with pytest.raises(ValueError, match="assessment_domain_exceeds_grid_limit"):
        explore(domain, lambda cell, samples: (COVERED, None), max_leaves=100)
    assert MAX_LEAF_CELLS == 5000


def test_support_points_are_inside_the_domain_and_include_a_boundary_support():
    cell = Cell(0, 0, 0)
    full = support_points(cell, cell.geometry())
    assert full[0].equals(cell.center) or cell.geometry().covers(full[0])
    assert len(full) == 5                       # 代表点 + 四条边中点
    # 评估域只覆盖格子的下半：代表点落在下半，上半的边中点被丢弃。
    half = support_points(cell, box(0, 0, 50, 20))
    assert all(half_point.y <= 20 for half_point in half)
    assert len(half) >= 1
    assert support_points(cell, box(100, 100, 150, 150)) == []


def test_cell_arithmetic_is_reversible():
    child = Cell(3, 5, 1)
    assert child.parent() == Cell(1, 2, 0)
    # 子格顺序：左下/右下/左上/右上 —— 右上才是 (3, 5)。
    assert Cell(1, 2, 0).children() == (Cell(2, 4, 1), Cell(3, 4, 1), Cell(2, 5, 1), Cell(3, 5, 1))
    assert Cell(1, 2, 0).step == GRID_STEP_M and child.step == REFINED_STEP_M
    assert child.bounds == (75.0, 125.0, 100.0, 150.0)
    assert child.contains(Point(90, 130))
    with pytest.raises(ValueError):
        child.children()
    with pytest.raises(ValueError):
        Cell(1, 2, 0).parent()
