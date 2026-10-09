# PR 草稿（A/C 阶段交付）

> **建议拆成两个 PR。** 当前分支 `delivery/facility-budget-cache` 同时包含"预算/缓存修复"
> 与"C 阶段离线研究工具"，两者的审阅关注点、回滚意义和风险都不同：
> 前者是要上生产的修复，后者是**不影响生产路径**的研究工具。下面给出两份草稿与拆分方法。

```bash
# 拆分建议（本地操作，均不推送）
git branch merge-plan-research 0efc8dc        # 含 C 工具与 C 证据
git rebase --onto 655e17d 1848253 delivery/facility-budget-cache   # 或按需摘取提交
```

---

## PR 1｜设施检索的预算、缓存与补查衔接修复

**标题**：`fix: keep cached pages out of the network budget and make extension retries idempotent`

### 摘要

修复三处会让"有限额度被浪费或被误挡"的衔接问题，并补齐回归测试、契约与配置说明。
另外把三个离线浏览器套件从"跑不起来"变成"真的在跑"，并修好一个过期示例配置。
全部验证使用 mock/synthetic provider、临时 SQLite、受控时钟与外连阻断；
**未使用真实 AK、未请求百度服务、未消耗线上额度**。

### 改了什么

1. **幂等先于预算，也先于"最新修订"**：同一 `clientRequestId` 取回原补查（不排队、不发请求、不扣额度，
   日余额为 0 也一样）；参数不同返回 409，且优先于余额判断；请求身份只认客户端声明，冻结在 `intent` 列；
   旧行按旧规则判定。原请求的合法重试不再需要"最新修订的几何还能用"。
2. **预检看得见缓存，且只有一条规则**：`poi/plan.py` 用规划器自己的首轮与**真实页键**估算；
   `poi_plan.admission_refusal()` 是 manager 与设施阶段共用的唯一准入规则——全缓存放行、
   部分缓存按缺页放行、只有冷启动缺额具名拒绝。
3. **两把尺子**：网络额度（`ServicePool.attempt()` 唯一原子扣减点）与本地处理上限（4096 步，
   独立停止原因）分开；额度用尽时继续读完缓存、缺页逐页记名并报 `partial`。
4. **计量按真实派发归属**：`PageResponse.manner`（live/cache/shared/refused）由真正解决该请求的那一层写出；
   修掉"共享 in-flight 失败时等待方被误记为已派发"与"纯缓存路径无法及时响应取消"。
5. **关停不再卡死**：worker 循环改为由 `closing` 条件退出。
6. **界面不再把失败说成"没有设施"**：上游失败/未完成时，空设施图层现在给出具名说明
   （停止原因复用补查面板同一张词表），而不是渲染"本次体检没有接收的设施"。
   该缺陷来自基线 `85ec80e4`，由本 PR 新增的浏览器套件**首次运行即发现**。
7. **让浏览器套件真的能跑**：默认改用 Playwright 自带 Chromium（原先把 `msedge` 写死，
   本机没有 Edge 就一个断言都跑不到）；缺的 `libasound.so.2` 由 `scripts/ensure-browser-libs.sh`
   解到仓库忽略目录（**不需要 root、不改系统**）；浏览器、产物目录、端口均可配置。
8. **修好过期示例配置**：`tools/poi-example.json` 只给了 3 个类的预算，而词典已是 31 类、
   `RuntimeConfig` 要求逐类给全，离线 CLI 会以一个笼统错误码失败；回放夹具同时缺一个新增额外关键词的首页。
9. **`start.command` 可在 Linux 直接运行**：原为 CRLF ＋ `100644`，shebang 会变成
   `#!/usr/bin/env bash\r` 而完全无法执行；改为 LF ＋ `100755`，并加 `.gitattributes`
   （`*.command`/`*.sh` 用 LF、`*.bat` 用 CRLF）防止检出时复发。

### 迁移与兼容

- `facility_extensions` 追加三个**可空**列（`intent`、`stop_reason`、`initial_plan`），
  沿用既有幂等迁移（`PRAGMA table_info` + `ALTER TABLE ADD COLUMN`）；旧行读作"未记录"，不补造值。
- 旧代码读取新库不受影响（按列名取值，不引用新列）。
- 对外字段语义未变：`requests` 仍是页面处理次数，`networkRequests` 仍是实际派发次数；
  新增诊断字段对旧响应缺失/null 兼容（前端已有向后兼容用例）。
- **唯一行为变化**：部分缓存 + 预算不足的补查由"422 拒绝"改为"受理并按缺页执行（`partial`）"，
  这是方案不变量 5 要求的方向。

### 验证

| 项目 | 结果 |
| --- | --- |
| 后端全量（当前） | **31 failed / 1016 passed / 10 skipped** |
| 后端全量（基线检出，同解释器） | 42 failed / 963 passed / 10 skipped |
| 逐节点集合比对 | **新增失败 0；修好 11**（31 + 11 = 42 精确闭合） |
| 前端 | `npm test` 41 files / **462 tests passed**；`npm run build` 通过 |
| 浏览器（真实运行） | `test:checkup-ui` **15**；`test:e2e` **10**；`test:integration` **7** 全部通过 |
| 外连尝试 | **0**（后端全会话守卫 ＋ 每个浏览器用例断言） |
| 启动脚本 | `bash -n` 通过；`./start.command --help` 通过 |

剩余 31 项失败全部 `baseline-confirmed`（类别词典口径 30 项 ＋ 生成物漂移 1 项），
已在隔离基线检出上逐项重放确认；改写它们等于宣布类别口径变更，属产品决定。

### 回滚

`git revert <commit>`（或把分支 reset 回基线）。数据库新增列可空、不参与旧查询；
页面缓存本来就是进程内存，回滚不涉及数据迁移。`start.command`/`.gitattributes` 的回滚只影响本地启动方式。

---

## PR 2｜C0–C2 离线查询计划研究工具与证据（不接入生产）

**标题**：`feat(tools): offline query-plan benchmark, and a feasibility audit of $ multi-keyword merge`

### 摘要

新增一套**离线**基准工具，用来回答"当前的设施检索计划到底花了多少次真实派发、拿到了哪些设施"，
以及"把关键词用 `$` 合并进一个查询会怎样"。工具只在 `backend/tools/` 下，
**生产规划器、缓存键与任何服务路径都没有改动**；候选分组计划声明独立 `planVersion`
（`merge-poc-v1`）与独立 transport 身份，因此它的页面缓存与生产互不相通。

### 内容

- `tools/poi_query_benchmark.py`：`--suite baseline`（基线计量）与 `--suite compare`（候选对照）；
  自建合成世界夹具、临时 SQLite 账本、受控时钟、外连阻断，并**断言自己的计量恒等式**
  （派发 == 任务桶增量 == 日账本增量 == 会话派发日志），不成立即以非零码退出。
- `tools/poi_benchmark_world.py`：确定性合成目录与 `around` 引擎，建模假设随每份报告输出。
- `tools/poi_benchmark_merge.py`：实验性分组计划与独立 runner（不接入应用）。
- `evidence/07`：基线实测；`evidence/08`：合并可行性审计；`evidence/09`：离线 PoC 结果。

### 主要发现

1. **官方文档确认** `$` 并集语法（≤10 词、并集语义），但**从未**承诺"并集 = 分别查询的集合等价"；
   且响应结构中**没有**任何字段能把单条 POI 归属回某个提交关键词。本仓库的响应白名单进一步使这类字段不可得。
2. **一个硬前置**：官方文档说明并集页为 `关键词数 × page_size` 条，而
   `place_protocol.response_error()` 把 `len(results) > 20` 判为 `invalid_response`——整页作废。
   **在决定这条规则之前，合并查询在本仓库中不可试验。**
3. **按小类合并同义词的收益取决于密度**（以"每词各查一次"为等覆盖对照）：
   稀疏 **−65.1%**、适中 **−18.3%**、密集 **＋10.3% 且未查完**。反转机制是文档的
   `total ≤ 150` 上限与既有细分规则的联动。
4. **生产计划本来就只发每小类主关键词**，因此同义词合并**不减少**当前计划的查询数；
   要再减少只能跨小类合并，而那会毁掉小类级完整性断言（`synonym` 是唯一跨类组数为 0 的粒度）。
5. 基准还测到一处**既有**成本问题（本轮未改，仅记录）：持续 `upstream_error` / `timeout`
   会以每序列 2 次的代价烧掉整轮预算，112 次派发换来零条证据。

### 验证

- 11 个场景 × 全部臂的计量恒等式逐条成立；外连尝试 **0**。
- 基线 13 个场景（稀疏/适中/密集/十类/紧预算/全缓存/无匹配/空页/限流/配额/超时）全部有整轮数字。
- **本 PR 不含任何真实 API 调用**，也不改变任何默认预算、类别数或评分定义。

### 边界

离线夹具只能支持"技术方案可试验 / 不可试验"。真实召回、真实接口的合并行为、真实配额与许可
分别属于 D 与 B 阶段，均未执行。
