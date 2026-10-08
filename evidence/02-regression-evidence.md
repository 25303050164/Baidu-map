# A3｜针对性复现与端到端数据流回归

**任务**：`scheme.md` A3
**执行时间**：2026-10-08
**口径**：全部使用 mock/synthetic provider、临时 SQLite、受控时钟、外连阻断（`conftest.py` 的全会话防护记录为 **0 次尝试**）。测试穿过真实链路 `manager → store → facilities → poi/plan → OnlinePlanner → poi/cache → KeyedCache → ServicePool`，不只断言新 helper。

---

## 1. 计量记录（scheme.md §10.2 格式）

来源：`/tmp/baidu-verify/a3_evidence.py`（可重复执行的离线探针，不读密钥、不联网）。字段原样保留，下面为可读汇总。

| 场景 | 页面处理 | 缓存/共享 | 新增网络 | 任务/日账本增量 | 成功页 | 状态 / stopReason |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 冷查询（核心三类、4 分块、预算 60） | 112 | 0 / 0 | 60 | +60 | 60 | `partial` / `network_budget_exhausted` |
| 全缓存（首轮 56 页全在缓存，任务桶 0、日余额 0） | 56 | 56 / 0 | **0** | **+0** | 56 | `complete` / — |
| 部分缓存（可复用 8/20，预算 11＝缺页−1） | 20 | 8 / 0 | 11 | +11 | 19 | `partial` / `network_budget_exhausted` |
| 部分缓存（上一轮已缓存 19/20，预算 12） | 20 | 19 / 0 | **1** | +1 | 20 | `completed` / — |
| 幂等重试（补查成功、日余额清零，同 ID） | 0 | — | **0** | **+0** | — | 取回原补查（同 `extensionId`、原 `baseRevision=5`），未入队 |
| partial 后新标识补查（整轮需 112 页） | 112 | 56 / 0 | 52 | +52 | **60 → 112** | `completed` / — |
| 共享 in-flight 失败（派发方超时） | 2 | 0 / 1 | 1 | +1 | 0 | `failed` / `timeout`；派发方 `manner=live(dispatched=True)`，等待方 `manner=shared(dispatched=False)` |
| 纯缓存运行中途取消 | 0 | — | 0 | +0 | 0 | `cancelled` / `cancelled`（第一个调度步即响应） |

原始行（节选）：

```json
{"Task ID / scenario": "cold-core-3-categories", "Pages processed": 112, "Network calls dispatched": 60, "Task budget delta / Daily ledger delta": 60, "Status / stopReason": {"status": "partial", "stopReason": "network_budget_exhausted"}, "Facility evidence (successful pages)": 60, ...}
{"Task ID / scenario": "all-cached-zero-allowance", "Pages processed": 56, "Network calls dispatched": 0, "Task budget delta / Daily ledger delta": 0, "Status / stopReason": {"status": "complete", "stopReason": null}, "Facility evidence (successful pages)": 56, ...}
{"Task ID / scenario": "partly-cached-short-budget", "Pages processed": 20, "Network calls dispatched": 11, "Task budget delta / Daily ledger delta": 11, "Facility evidence (successful pages)": 19, ...}
{"Task ID / scenario": "partly-cached-exact-budget", "Pages processed": 20, "Network calls dispatched": 1, "Task budget delta / Daily ledger delta": 1, "Facility evidence (successful pages)": 20, ...}
{"Task ID / scenario": "partial-then-new-extension", "Pages processed": 112, "Network calls dispatched": 52, "Task budget delta / Daily ledger delta": 52, "Facility evidence (successful pages)": 112, ...}
{"Task ID / scenario": "shared-in-flight-failure", "Network calls dispatched": 1, "Task budget delta / Daily ledger delta": 1, ...}
{"Task ID / scenario": "cache-only-run-cancelled", "Pages processed": 0, "Network calls dispatched": 0, "Status / stopReason": {"status": "cancelled", "stopReason": "cancelled"}, ...}
{"external connection attempts refused": 0}
```

**核对**：每一行的"新增网络"与"账本增量"相等；页面处理数可以远大于新增网络（重放），这正是本轮修复点。

---

## 2. 场景覆盖（scheme.md A3 表 → 用例）

| scheme 场景 | 用例 | 关键断言 |
| --- | --- | --- |
| 冷查询、4 分块、核心三类首轮 | `test_both_engines_close_the_loop_over_their_own_boundary`、探针 §1 | 首轮 56 页派出、不虚报整轮完成 |
| 全缓存、任务与日余额都为 0 | `test_a_fully_cached_retrieval_runs_with_an_exhausted_task_bucket`、`test_a_fully_cached_retrieval_at_the_same_budget_costs_no_new_calls` | 网络 0、账本 +0、状态准确 |
| 部分缓存、新增额度恰好覆盖缺页 | `test_a_partly_cached_extension_spends_its_allowance_on_the_missing_pages` | 20 页处理 / 11 次新增 / partial；随后只差 1 页 |
| 先 60 页 partial，新标识补查 | 同上 `partial-then-new-extension`、`test_each_extension_advances_only_as_far_as_the_days_balance_allows` | 成功页 60 → 112，额度只花在缺页 |
| 原请求同 ID 重试、余额为 0 | `test_a_replay_after_the_days_allowance_is_gone_returns_the_original` | 同 `extensionId`/`baseRevision`，不发请求、不扣额度 |
| 同 ID 改参数 | `test_a_conflict_beats_an_exhausted_allowance` | 409 优先于余额判断 |
| 父任务新修订 / 默认预算变化 | `test_a_replay_keeps_its_original_revision_after_the_parent_publishes_another`、`test_an_omitted_budget_replay_survives_a_changed_default`、`test_a_replay_survives_a_latest_revision_whose_geometry_is_unusable` | 重试绑原修订；新标识才绑新修订；圈面不可用不影响重试 |
| 并发相同 ID / 相同页面共享 | `test_two_concurrent_extension_submissions_create_exactly_one_row`、`test_an_in_flight_page_shared_with_another_caller_is_not_a_new_call`、`test_a_failed_shared_call_is_not_a_dispatch_for_the_waiter` | 只创建一次；只有派发方计费；失败不写成功缓存 |
| 缓存过期 / 预检后额度被他人消耗 | `test_an_allowance_that_runs_out_after_the_precheck_still_bounds_every_dispatch` | 派发前最终校验、不超额、不伪成功 |
| 首轮已缓存但下一页缺失且预算 0 | `test_a_missing_page_after_a_fully_cached_first_round_is_partial_not_completed` | 保留证据、`partial`、缺页具名 |
| 持续 rate-limit / quota / 超时 | `test_a_dispatched_but_failed_call_is_still_a_new_network_call`、`test_a_refusal_before_dispatch_costs_nothing`、`test_a_facility_run_that_fails_every_page_is_a_failed_query` | 有界停止、已派发计费、不重试风暴、不写成零设施 |
| 取消 / deadline / 本地步数上限 | `test_a_cache_only_run_still_checks_cancellation_and_the_deadline`、`test_a_pure_cache_run_observes_a_cancellation_that_already_arrived`、`test_the_local_processing_ceiling_has_its_own_stop_reason` | 有限退出、原因分开 |
| 补查成功/失败/取消后父任务不变 | `test_a_facility_extension_reuses_the_boundary_and_leaves_the_report_alone`、`test_a_cancelled_extension_keeps_what_it_retrieved_and_never_touches_the_report`、`test_shutting_down_while_an_extension_runs_still_returns` | 修订数、报告逐字不变；关停不再卡死 |
| 北京时间跨日 / SQLite 重启 | `test_each_extension_advances_only_as_far_as_the_days_balance_allows`、`test_an_old_extension_table_gains_the_new_columns_without_touching_rows` | 跨日账本按新日期；旧库升级后旧行照读 |
| 60 个以上缓存页 + 少量新网络页 | `test_more_than_sixty_cached_pages_do_not_stop_the_run_before_the_missing_ones` | 处理 112 页、新增 3，旧口径只重放 60 页 |
| 前端/契约向后兼容 | `capabilities.test.ts`（旧后端缺 `poiPlanning`/`cache`）、`extensions.test.ts`（旧记录缺 `initialPlan`/`stopReason`） | 缺项省略、不写 0、不编造 |

---

## 3. 复现命令

```bash
cd backend
.venv/bin/python -m pytest -q tests/test_checkup_facilities.py tests/test_checkup_v2.py tests/test_cache.py \
    tests/test_poi_online.py tests/test_quota.py          # 126 passed
.venv/bin/python /tmp/baidu-verify/a3_evidence.py          # §1 的计量记录（离线探针）
```

被修复缺陷的"先复现、后修复"证据（同一环境，改动前后对照）：

| 缺陷 | 复现证据（改动前） | 修复后 |
| --- | --- | --- |
| 共享失败归属 | 探针 `shared_timeout`：等待方 `dispatched=true` 但预算未动、账本仅 +1 | 等待方 `manner=shared`，`dispatched=false` |
| 纯缓存取消 | 探针 `cancel_during_cache_drain`：`status=completed, stopReason=null, processed=4` | `status=cancelled`，第一个调度步即停 |
| 关停卡死 | 去掉循环守卫后 `TestClient.__exit__` 阻塞（15 秒看门狗），默认预算亦可复现 | 退出立即返回 |
| 准入不一致 | 部分缓存 + 预算不足：manager `422`，设施阶段放行 | 两处都放行并按缺页执行 |
| 幂等晚于修订读取 | 最新修订几何不可用时，合法重试得 409 | 重试正常取回（新增用例固定） |

新增/更新的回归用例共 9 个（5 个本轮新增：共享失败归属、纯缓存取消、关停返回×2、几何不可用的重试；另更新 1 个准入期望、新增 2 个迁移/惰性读取用例）。

---

## 4. 结论

- 13 类 A3 场景全部有穿过真实链路的用例，且都带页数/派发/账本三类数字。
- 三个高严重度缺陷（共享失败归属、纯缓存取消、关停卡死）都先复现再修，并留下失败→通过的对照。
- 所有运行的外连尝试为 0。
