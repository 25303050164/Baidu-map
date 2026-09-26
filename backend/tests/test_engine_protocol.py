"""成圈快照的两个计数必须跟着结果走。

一次真实体检暴露过这里：``build`` 收下了 ``requests_used`` / ``network_requests`` 却
没往构造器里传，于是两个引擎的成圈请求都变成 0 —— 任务计数因此在下一阶段发布时从 226
掉回 28，``trace.budgets.isochrone.spent`` 也永远是 0，而报告读起来像"这一版没花过额度"。
计数的意义就是"付过的都留着"，所以它不该被任何一次组装悄悄吃掉。
"""
from app.engines.protocol import IsochroneSnapshot


def snapshot(**overrides) -> IsochroneSnapshot:
    fields = dict(
        engine_id="baidu_e82", engine_version="local-multicross-e82",
        algorithm="local-multicross-e82", parameters={"threshold": 900},
        geometry={"type": "MultiPolygon", "coordinates": []}, display_geometry=None,
        unknown_region=None, uncertain_region=None, computation_extent=None,
        quality="partial", stop_reason="budget", warnings=[],
        statistics={"requests": 400, "network_requests": 400},
        requests_used=400, network_requests=400)
    fields.update(overrides)
    return IsochroneSnapshot.build(**fields)


def test_the_counters_survive_assembly():
    result = snapshot()
    assert result.requests_used == 400
    assert result.network_requests == 400
    # 留在快照里的原始统计不受影响：报告要能自己核对这两个数从哪来。
    assert result.statistics["requests"] == 400


def test_an_engine_that_never_left_the_process_reports_no_network_spend():
    # 合成与离线路径会算边界采样，却一次网络请求也不发。两个计数不是同一件事，
    # 预算池读的是后面那个。
    result = snapshot(statistics={"requests": 144, "network_requests": 0},
                      requests_used=144, network_requests=0)
    assert result.requests_used == 144
    assert result.network_requests == 0


def test_the_boundary_digest_ignores_the_counters():
    # isochroneHash 认的是边界本身；换一个计数不该换掉这份指纹，否则同一圈会被报成两版。
    assert snapshot().isochrone_hash == snapshot(
        requests_used=1, network_requests=1).isochrone_hash
