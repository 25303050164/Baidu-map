"""§5 B2 决策 1：普通体检"最多允许残余 20% 地段"的口径。

这个文件测的是一句话：**残余是各小类未完成区域的并集，不是各小类覆盖率的平均。**

两者的差别不是精度问题。假设十个小类里九个整块查完、一个整块没查：
按平均算是 90%，按并集算是 0%。而报告要回答的问题是"读者能不能把这份结果当作
覆盖了这片区域" —— 答案是"不能"，因为那一个小类的地段里，设施一个都没查过。
所以这里逐条钉住并集口径，包括重叠只算一次、圈外不算、圈面孔洞不算残余。
"""
from shapely.geometry import box

from app.checkups.facilities import SHARED_COMPLETION_TARGET, shared_completion


def test_a_fully_queried_boundary_is_met_with_no_residual():
    report = shared_completion({'pharmacy': [], 'school': []}, box(0, 0, 100, 100))
    assert report['status'] == 'met'
    assert report['target'] == SHARED_COMPLETION_TARGET == 0.80
    assert report['sharedCompletionRatio'] == 1.0
    assert report['residualRatio'] == 0.0
    assert report['boundaryAreaM2'] == 10_000.0
    assert report['sharedCompletedAreaM2'] == 10_000.0
    # Every category that was asked about is named, so a reader can see the union
    # really did cover all of them rather than a subset that happens to be listed.
    assert report['categories'] == ['pharmacy', 'school']


def test_the_target_itself_counts_as_met_and_a_hair_under_does_not():
    # 10000 m² 的圈面：正好 2000 m² 未完成 = 80.0%。门槛是"至少 80%"。
    exact = shared_completion({'a': [(0, 0, 100, 20)]}, box(0, 0, 100, 100))
    assert exact['residualRatio'] == 0.2 and exact['status'] == 'met'
    under = shared_completion({'a': [(0, 0, 100, 20.1)]}, box(0, 0, 100, 100))
    assert under['residualRatio'] == 0.201 and under['status'] == 'unmet'


def test_overlapping_gaps_are_counted_once_not_once_per_category():
    """两个小类各自空出 25%，重叠 6.25% 时残余是 43.75%，不是 50%。"""
    report = shared_completion({'a': [(0, 0, 50, 50)], 'b': [(25, 25, 75, 75)]},
                               box(0, 0, 100, 100))
    assert report['residualRatio'] == 0.4375
    assert report['sharedCompletionRatio'] == 0.5625
    assert report['status'] == 'unmet'


def test_one_unqueried_category_cannot_hide_behind_the_others():
    """这条就是"不许用平均数掩盖某一个类没查"本身。"""
    report = shared_completion({'nine_finished': [], 'one_never_asked': [(0, 0, 100, 100)]},
                               box(0, 0, 100, 100))
    assert report['sharedCompletionRatio'] == 0.0
    assert report['residualRatio'] == 1.0
    assert report['status'] == 'unmet'


def test_a_gap_outside_the_boundary_is_not_a_residual_of_the_boundary():
    """查询缓冲区超出圈面的那部分不属于"本次体检的地段"，不能算成没查完。"""
    report = shared_completion({'a': [(-50, -50, 50, 50)]}, box(0, 0, 100, 100))
    assert report['residualRatio'] == 0.25
    assert report['status'] == 'unmet'


def test_a_hole_in_the_boundary_is_not_a_residual():
    """圈面自己的孔洞不是"没查完"：分母与残余都用圈面本身算。"""
    boundary = box(0, 0, 100, 100).difference(box(40, 40, 60, 60))
    report = shared_completion({'a': [(40, 40, 60, 60)]}, boundary)
    assert report['boundaryAreaM2'] == 9_600.0
    assert report['residualRatio'] == 0.0
    assert report['status'] == 'met'


def test_no_measureable_area_is_unknown_rather_than_unmet():
    """``unknown`` 只留给"量不出来"。没有面积就没有比例，不能编一个 0 出来。"""
    report = shared_completion({'a': []}, box(10, 10, 10, 10))
    assert report['status'] == 'unknown'
    assert report['reason'] == 'empty_boundary_area'
    assert report['sharedCompletionRatio'] is None and report['residualRatio'] is None


def test_missing_evidence_is_a_residual_not_an_absence_of_evidence():
    """一台从没跑过的检索报 ``unmet``：没查完就是没查完，不能写成"未知"来显得不确定。

    ``unknown`` 与 ``unmet`` 的区别是刻意的：前者说"这个指标算不出来"，
    后者说"算出来了，没达标"。把失败读成前者，等于用一个模糊的说法替一次失败开脱。
    """
    report = shared_completion({'a': [(0, 0, 100, 100)]}, box(0, 0, 100, 100))
    assert report['status'] == 'unmet'
