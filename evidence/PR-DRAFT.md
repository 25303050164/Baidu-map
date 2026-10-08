# 设施检索的预算、缓存与补查衔接修复（A 阶段交付）

## 摘要

修复三处会让"有限额度浪费或误挡"的衔接问题，并补齐回归测试、契约与配置说明。所有验证使用 mock/synthetic provider、临时 SQLite 与受控时钟；未使用真实 AK、未请求百度服务、未消耗线上额度。

## 改了什么

1. **幂等先于预算，也先于"最新修订"**：同一 `clientRequestId` 取回原补查（不排队、不发请求、不扣额度，日余额为 0 也一样）；参数不同返回 409，且优先于余额判断；请求身份只认客户端声明（类别集合 + 是否显式给预算），冻结在 `intent` 列；旧行按旧规则判定。原请求的合法重试不再需要"最新修订的几何还能用"。
2. **预检看得见缓存，且只有一条规则**：`poi/plan.py` 用规划器自己的第一轮与真实页键算出首轮页数/可复用页数/预计新增调用数；`poi_plan.admission_refusal()` 是 manager 与设施阶段共用的唯一准入规则——全缓存放行、部分缓存按缺页放行、只有冷启动缺额具名拒绝。
3. **两把尺子**：网络额度（`ServicePool.attempt()` 唯一原子扣减点）与本地处理上限（4096 步，独立停止原因）分开；额度用尽时继续读完缓存、缺页逐页记名并报 `partial`。
4. **计量按真实派发归属**：`PageResponse.manner`（live/cache/shared/refused）由真正解决该请求的那一层写出。本轮修掉两个计量/调度缺陷：共享 in-flight 失败时等待方被误记为"已派发"；纯缓存路径因无 await 点而无法及时响应取消。
5. **关停不再卡死**：worker 循环改为由 `closing` 条件退出——检索器会把 `CancelledError` 当结果吸收，只依赖异常会让 `close()` 等一个永不唤醒的 worker（基线既有缺陷，已复现并修复）。
6. **诊断与文案**：`facilities.initialPlan`、补查 `stopReason`/`initialPlan`、capabilities 的 `poiPlanning`/`cache.processLocal`；`budgets.poiRequestsIsLowerBound` 改为 false；`initialPlan` 明确为"执行前估算，不是额度预留"。

## 迁移与兼容

- `facility_extensions` 追加三个**可空**列（`intent`、`stop_reason`、`initial_plan`），沿用既有幂等迁移（`PRAGMA table_info` + `ALTER TABLE ADD COLUMN`）；旧行读作"未记录"，不补造值。
- 旧代码读取新库不受影响（按列名取值，不引用新列）。
- 对外字段语义未变：`requests` 仍是页面处理次数，`networkRequests` 仍是实际派发次数；新增诊断字段对旧响应缺失/null 兼容（前端已有向后兼容用例）。

## 验证

- 后端全量：34 failed / 1011 passed / 10 skipped（基线 42 failed / 968 passed）——**新增失败 0**，另修好 8 项基线失败。
- 针对性：`test_checkup_facilities.py`、`test_checkup_v2.py`、`test_cache.py`、`test_poi_online.py`、`test_quota.py` 共 126 passed。
- 前端：41 files / 458 tests passed；`npm run build` 通过。
- 外连尝试 0；E2E **NOT RUN**（环境缺 `libasound.so.2`，且配置要求 msedge）→ 生产审批阻塞项。

## 仍未解决 / 不在本 PR 范围

- 34 项基线失败全部已归因（类别词典重构遗留、旧 POI 运行时、生成物漂移）；其中 `test_poi_tool.py` 的"离线 CLI 不读凭据/不发 HTTP"守卫失败标记为**生产阻塞**，需单独处理。
- 真实百度 API 实验、真实配额核验、生产部署、持久化断点续查均不在本轮授权范围内。

## 回滚

`git revert <commit>`（或在交付分支上 reset）即可；数据库新增列可空、不参与旧查询，回滚到旧代码后既有行仍可读。页面缓存本来就是进程内存，回滚不涉及数据迁移。
