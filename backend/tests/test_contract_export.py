"""生成物必须等于这段代码现在会产生的东西。

`backend/docs/openapi.json`、`backend/docs/analysis-response.schema.json`、四份 mock 和
两份前端契约都是 ``tools/export_contract`` 的产物，而它们此前**落后于代码**：文档里缺少
v2 的补查与重试端点，并且还在广告一个重构前就换掉的默认类别顺序 —— 客户端照着那份文档
发请求，会得到一个代码已经不用的默认值。

这组用例钉住三件事，缺一条都会让同一类问题再来一次：

1. **仓库里的九份生成物与本代码逐字节一致。** 改了路由却忘了导出，这里直接红，而不是
   等到某天有人发现文档里少了一个端点。
2. **检查模式（``--check``）不写文件，且没有差异时退出码为 0。** 一个会顺手改写三棵目录树的
   "检查"不能用来判断"要不要提交"，而它以前正是这样把必要的更新一起带走的（`evidence/06` §6.1）。
3. **行尾与解释器都不影响产物。** 生成物有的 CRLF 有的 LF，且 FastAPI 的 422 描述取自
   ``http.client.responses`` —— CPython 3.13 改了那个词。两者都会让"只改了内容"的提交
   看起来像整文件重写，也都会让同一段代码产出两份不同的文档。
"""
import json
from pathlib import Path

import pytest

from tools import export_contract
from tools.export_contract import REPO, build, differences, encode, main, targets, write


@pytest.fixture(scope="module")
def documents() -> dict:
    """这一份产物算一次就够：``build`` 是纯函数，不读也不写任何部署数据。"""
    return build(REPO)


def test_the_committed_generated_files_are_exactly_what_this_code_produces(documents):
    assert differences(REPO, documents) == []


def test_every_target_is_built(documents):
    # 少建一份文件时，差异检查会安静地跳过它 —— 那正是它最该报出来的情况。
    assert set(targets(REPO)) == set(documents)
    assert len(documents) == 9


def test_the_openapi_document_describes_the_running_application(documents):
    spec = json.loads(documents["openapi"])
    # v2 的补查与重试端点曾经只存在于代码里：文档说没有，客户端就没有理由去调。
    assert {"/api/v2/checkups/{task_id}/facility-extensions",
            "/api/v2/checkups/{task_id}/facility-extensions/{extension_id}",
            "/api/v2/checkups/{task_id}/facility-extensions/{extension_id}/result",
            "/api/v2/checkups/{task_id}/facility-extensions/{extension_id}/cancel",
            "/api/v2/checkups/{task_id}/retries",
            "/api/v2/checkups/{task_id}/retries/{retry_id}",
            "/api/v2/checkups/{task_id}/retries/{retry_id}/cancel"} <= set(spec["paths"])
    assert {"FacilityExtensionRequest", "FacilityExtensionView", "FacilityExtensionDocument",
            "FacilityRetryRequest", "FacilityRetryView"} <= set(spec["components"]["schemas"])
    # 文档广告的默认类别顺序必须是代码现在用的那一个。
    from app.catalog import default_analysis_majors
    assert spec["components"]["schemas"]["AnalysisInput"]["properties"]["facilityCategories"][
        "default"] == list(default_analysis_majors())


def test_check_mode_writes_nothing_and_reports_what_differs(tmp_path, documents):
    empty = tmp_path / "empty"
    empty.mkdir()
    # 一份产物都没有：检查模式报差异，并且**一个文件都不写**。
    assert main(["--check", "--out", str(empty)]) == 1
    assert list(empty.rglob("*")) == []
    write(empty, documents)
    assert main(["--check", "--out", str(empty)]) == 0


def test_each_file_is_written_in_its_pinned_line_ending(tmp_path, documents):
    where = tmp_path / "tree"
    write(where, documents)
    crlf = targets(where)["openapi"]
    lf = targets(where)["v2-contract"]
    assert crlf.read_bytes() == encode(documents["openapi"], "\r\n")
    assert lf.read_bytes() == encode(documents["v2-contract"], "\n")
    assert differences(where, documents) == []


def test_a_drifted_line_ending_is_a_difference_until_it_is_resolved(tmp_path, documents):
    """行尾漂移必须能报出来，也必须能修回去 —— 否则这类提交永远说不清。"""
    where = tmp_path / "exact"
    write(where, documents)
    stale = targets(where)["v2-contract"]
    stale.write_bytes(stale.read_bytes().replace(b"\n", b"\r\n"))
    assert differences(where, documents) == [f"{stale}: line endings differ (expected LF)"]
    write(where, documents)
    assert differences(where, documents) == []


def test_build_is_deterministic(documents):
    assert build(REPO) == documents


def test_status_phrases_are_frozen_against_the_interpreter():
    spec = {"paths": {"/one": {"post": {"responses": {
        "422": {"description": "Unprocessable Entity"},   # 3.12 的说法
        "413": {"description": "Content Too Large"},      # 3.13 的说法
        "404": {"description": "没有这个任务"},            # 路由自己写的，不动
    }}}}}
    export_contract.freeze_status_phrases(spec)
    responses = spec["paths"]["/one"]["post"]["responses"]
    assert responses["422"]["description"] == "Unprocessable Content"
    assert responses["413"]["description"] == "Content Too Large"
    assert responses["404"]["description"] == "没有这个任务"
