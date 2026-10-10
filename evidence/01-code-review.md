# A2｜预算—缓存—调度—补查全调用链审计

**任务**：`scheme.md` A2（审计预算—缓存—调度—补查全调用链）
**执行时间**：2026-10-08
**基准**：本地/远端 HEAD 均为 `decce8784e0505fb9601c1d493c522d919c1e348`，本轮改动未提交
**证据级别**：E0（代码审计）＋ E1（离线可复现探针与回归测试）

审计口径：对**每一个实际网络调用**解释它的一次扣费与归属，对**每一种缓存/拒绝**解释它报出的状态，并确认不存在重复计费或绕过配额的路径。

---

## 1. 调用链（唯一扣费入口）

```text
CheckupManager.submit / _stages                        （任务预算桶在此固定）
  └─ collect_facilities(payload, snapshot, quota, budget, cache, places_factory)
       ├─ poi_plan.initial_plan(domain, origin, categories, provider, api_version)
       │    └─ OnlinePlanner(budget=1) 只读第一轮 + poi.cache.page_key   ← 不发请求、不扣费
       ├─ poi_plan.estimate(...)  → KeyedCache.get()                     ← 只读，不刷新 obtainedAt
       ├─ poi_plan.admission_refusal(estimate)                           ← 冷启动缺额才拒绝
       ├─ CachedPages(cache, PlaceSession(quota.place, budget, deadline), provider, task_id)
       │    └─ KeyedCache.resolve(key, build)
       │         ├─ 命中：直接返回，不进配额池                  → manner=cache
       │         ├─ 在飞：await shield(同页请求)                 → manner=shared（成功或失败）
       │         └─ 未命中：build() → PlaceSession
       │              └─ ServicePool.attempt()  ← **唯一**检查并发、任务桶、日账本的原子点
       │                   ├─ check_allowances()（取消 / 任务桶 / 日额度）
       │                   ├─ gate.wait()（响应式限速；deadline 内）
       │                   ├─ ledger.reserve() + budget.consume()   ← 扣费发生在派发之前
       │                   └─ yield Attempt → transport.page() → attempt.outcome(reason)
       └─ OnlinePlanner.run
            └─ _attempt → PageResponse(payload, reason, manner)         ← 每页一个显式事实
                 manner=live   ：本运行真的派发了一次（失败/超时也算）
                 manner=cache  ：读了已取得的页面
                 manner=shared ：等的是别人的在飞请求（成功或失败都算别人的）
                 manner=refused：调度层在派发前拒绝，什么都没发出去
```

不变量核对：

| 不变量 | 结论 | 依据 |
| --- | --- | --- |
| 唯一派发与扣费入口 | ✅ 仍然只有 `ServicePool.attempt()`；预检只读 | `facilities.py:222-237`；`poi/plan.py` 只调 `KeyedCache.get` |
| 预检不发请求、不扣费、不预留 | ✅ | `test_a_fully_cached_retrieval_runs_with_an_exhausted_task_bucket`、`test_an_allowance_that_runs_out_after_the_precheck_still_bounds_every_dispatch` |
| 派发后失败/超时/重试不退款 | ✅ | `test_a_dispatched_but_failed_call_is_still_a_new_network_call` |
| 等待共享请求不重复计费 | ✅ **本轮修复** | `test_a_failed_shared_call_is_not_a_dispatch_for_the_waiter` |
| 部分缓存只按缺页扣费 | ✅ | `test_a_partly_cached_extension_spends_its_allowance_on_the_missing_pages` |
| 全缓存零额度可执行 | ✅ | 同上文件多处；`networkRequests=0` 且账本不变 |
| 处理步数有界且独立 | ✅ 4096，停止原因 `processing_limit_reached` | `test_the_local_processing_ceiling_has_its_own_stop_reason` |
| 取消/deadline 对纯缓存路径同样有效 | ✅ **本轮修复** | `test_a_pure_cache_run_observes_a_cancellation_that_already_arrived` |
| 旧 CLI 的页面次数预算语义不变 | ✅ `legacy_limits()` 固定旧语义 | `tests/test_poi_online.py` 全部通过；`legacy` 路径单独断言 |
| 缺页/错误不解释为零设施 | ✅ | `test_a_facility_run_that_fails_every_page_is_a_failed_query` 等 |

---

## 2. 发现的问题、严重程度与处置

| # | 问题（含复现证据） | 严重度 | 状态 | 处置 |
| --- | --- | --- | --- | --- |
| F1 | **共享 in-flight 失败时派发归属错误。** 派发方超时后，等待方虽然预算未动、账本只 +1，却被记为"已派发"；它的 `network_calls` 因此多算一次。探针：`{'probe':'shared_timeout','ownerDispatchedFlag':true,'waiterDispatchedFlag':true,'ownerBudgetSpent':{'poi':1},'waiterBudgetSpent':{},'ledgerDelta':1}` | 高（计量与账本不符） | **已修** | `KeyedCache.resolve` 对等待方把失败包成 `SharedBuildFailed`；`OnlinePlanner._attempt` 把它映射为 `manner=shared`（不派发），原因仍取自原异常。未知异常按原样重抛。 |
| F2 | **纯缓存运行不能及时响应取消。** 取消已排进事件循环，但全缓存路径没有任何 await 点，循环直到把页面全部处理完才"看到"取消：探针 `{'probe':'cancel_during_cache_drain','result':'completed','stopReason':null,'processed':4,'cancelProcessedAtFinish':false}` | 高（取消/deadline 失效） | **已修** | `run()` 每个调度步 `await asyncio.sleep(0)` 让出一次；让步有界（≤ 处理步数）。 |
| F3 | **关闭应用时正在跑的任务/补查会把关闭流程卡死。** `close()` 取消 token 与 worker 后 `await gather(worker)` 永不返回：检索器把 `CancelledError` 当成一种结果吸收掉，worker 回到 `await queue.get()` 再无唤醒。复现：无守卫版本退出 `TestClient` 15 秒后仍阻塞（`WATCHDOG: shutdown still blocked`），且用**默认预算**即可复现 → 与 F4/本轮改动无关，属基线既有缺陷 | 高（进程关不掉） | **已修** | `_serve`/`_serve_extensions` 的循环条件改为 `while not self.closing`；被吸收的取消随后由条件退出。 |
| F4 | **准入规则在 manager 与设施阶段不一致。** 设施阶段对"部分缓存"放行，`submit_extension` 仍按"缺页 > 预算"直接 422 —— 同一输入换一个入口结论不同，且违背"部分缓存允许取得可复用证据" | 中（接口行为不一致、挡掉可用证据） | **已修** | 抽出 `poi_plan.admission_refusal(estimate)` 作为唯一规则，两处共用。行为变化：部分缓存 + 预算不足现在受理并按缺页执行（partial），冷启动缺额仍具名拒绝。测试同步更新为验证真实证据与账本。 |
| F5 | **幂等查找晚于"最新修订/几何"读取。** 已有请求的重试本不需要当前圈面，但旧顺序先解析最新修订与几何：父任务发过一版没有几何的修订时，一次已受理的合法重试会拿到 409 | 中（合法重试失败） | **已修** | `find_extension` 提前到资源归属校验之后；只有新请求才解析圈面。等价判断的"当前解析预算"改成惰性读取，纯声明比较不触发圈面读取。 |
| F6 | **`initialPlan` 被文档写成"提交时冻结"。** 实际是设施阶段/补查**执行前**（派发第一页之前）算出的估算 | 低（说明不实） | **已修** | `models.py`、`README.md`、`.env.example`、`capabilities.ts`（含文案与其测试）、`extensions.ts`、`fixtures.ts` 统一改成"执行前估算，不是额度预留"。 |
| F7 | **预算等价规则依赖"当前解析出的默认值"。** 复现：显式 60 的行 + 省略重试，在默认值 60 时匹配、80 时冲突 | 低（**判定为既有兼容语义，非缺陷**） | 保持并加测试 | 旧版本按解析出的 `requested` 比较，行为一致；且"省略↔省略"永远匹配、与默认值变化无关（`test_an_omitted_budget_replay_survives_a_changed_default`）。新增测试固定两个方向，并固定"只在真正需要时才读当前解析值"。 |
| F8 | 数据库 schema 变更缺少迁移/回滚测试 | 低 | **已补** | `test_an_old_extension_table_gains_the_new_columns_without_touching_rows`：旧表打开即补三列、旧行原样读回、旧行仍按旧规则判定、新旧行共存。 |

未发现的问题（审计确认）：`poi/cache.py` 的页面键仍由 transport 自己声明身份；失败不写入成功缓存；`policy` 上没有第二条扣费入口；`_run_extension` 使用冻结的 `record.budget` 与 `record.base_revision`，不改父任务桶。

---

## 3. 并发与事务

| 场景 | 现状 | 证据 |
| --- | --- | --- |
| 同名同页并发共享（成功） | 只有派发方写缓存，等待方读同一结果 | `test_an_in_flight_page_shared_with_another_caller_is_not_a_new_call` |
| 同名同页并发共享（失败） | 只有派发方计费；等待方记为 shared；失败不写成功缓存 | `test_a_failed_shared_call_is_not_a_dispatch_for_the_waiter` 等 |
| 同 `clientRequestId` 并发创建补查 | `BEGIN IMMEDIATE` + 唯一约束，只有一方 `created=True`，只有创建者入队 | `test_two_concurrent_extension_submissions_create_exactly_one_row` |

---

## 4. 结论

- 每个真实派发都能追到 `ServicePool.attempt()` 的一次预留；等待共享、缓存命中、派发前拒绝都不计费，且**状态与计量一致**。
- 本轮共修 6 项（F1–F6）＋补 2 项测试缺口（F7/F8）；其中 F3 经对照确认是基线既有缺陷。
- 无重复计费路径；无绕过配额的路径；旧调用方预算语义保持不变。
