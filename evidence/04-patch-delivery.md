# A5｜可审查交付版本与 PR 草稿

**任务**：`scheme.md` A5
**执行时间**：2026-10-09（重写；上一版的阻塞项结论已按 §5 更新）
**授权边界**：**仅本地提交**。未 push、未 merge、未部署、未改线上配置、未调用真实百度服务。
**Gate A 判定**：**通过**（本地交付层面，见 §7）。生产部署仍不在授权范围内。

---

## 1. 交付提交

| 项目 | 值 |
| --- | --- |
| 交付分支 | `delivery/facility-budget-cache`（从基线新建，**未推送**） |
| 基线提交 | `decce8784e0505fb9601c1d493c522d919c1e348`（`distribution-boost` 本地与远端同点） |
| 交付范围 | 相对基线 **61 个文件**：+6757 / −309（22 新增、39 修改） |

提交序列（自基线起）：

| SHA | 内容 |
| --- | --- |
| `84db1b8` | **本轮修复本体**：缓存页面不占网络预算、补查幂等先于预算、两把尺子、计量按真实派发归属、关停退出 |
| `c384654` | A5 交付记录与 PR 草稿（上一版，已被本版取代） |
| `655e17d` | 让三个离线浏览器套件**真正运行**（自带 Chromium＋可配置端口/产物），并修好过期 CLI 示例与回放夹具 |
| `0665f60` | **并入用户改动**：`start.command` 改为 LF＋执行位，加 `.gitattributes` 固定脚本行尾 |
| `1848253` | C0/C2 离线基准工具与 C1 审计 |
| `0efc8dc` | A4 重测与撤回（CLI 归因、E2E 阻塞） |
| `77ab807` | C2 实测：加入**等覆盖率对照臂**（`allkw`），并修掉候选预检与夹具的两处缺陷 |
| 本文件 | A5 记录与 PR 草稿（本提交即分支末端，SHA 见 `git log --oneline delivery/facility-budget-cache`） |

**关于用户改动的并入**：用户明确指示把 `start.command` 的换行修复并入本次修改。原文件为 CRLF 且模式
`100644`，在 Linux 上 shebang 变成 `#!/usr/bin/env bash\r`，内核找不到解释器，文件**根本无法运行**；
缺执行位是第二个独立原因。核对：`git ls-files --eol '*.sh' '*.bat' '*.command'` 显示三个匹配文件的行尾
本就与 `.gitattributes` 一致，因此加入该属性文件**不改变任何既有文件的字节**；`bash -n` 与
`./start.command --help` 均通过。

**未被带入提交的用户改动**（保持原状）：`Baidu-map/`、`intro-page/`、`ai-tone-issues.json`、
`prompt.md`、`scheme.md`、`*:Zone.Identifier`，以及全部 OSM 地区包工作
（`backend/app/osm_package.py`、`backend/scripts/{configure,pack,setup}_osm_region.py`、
`backend/docs/OSM_REGION_SETUP.md`、`backend/tests/test_osm_package.py`、`data/osm/*`）。

---

## 2. 提交内容

| 范围 | 文件 | 内容 |
| --- | --- | --- |
| 配额/缓存/调度 | `backend/app/cache.py`、`backend/app/poi/{cache,online}.py` | `PageResponse.manner` 显式投递事实；两把尺子（网络额度 vs 4096 本地步数） |
| 首轮估算 | `backend/app/poi/plan.py`（新增） | 用规划器自己的首轮与真实页键做只读预检；`admission_refusal` 唯一准入规则 |
| 设施阶段 | `backend/app/checkups/facilities.py` | 缓存感知预检、`budget_refusal`、诊断计数 |
| 补查 | `backend/app/checkups/{manager,store}.py` | 幂等先于预算与"最新修订"；`intent`/`stop_reason`/`initial_plan` 三个可空列 |
| 契约 | `backend/app/checkups/{models,router}.py`、`life-circle-demo/src/checkup/{contract,capabilities,categories,validate,fixtures}.ts` | 新诊断字段与 capabilities 口径 |
| 传输身份 | `backend/app/checkups/places.py` | `declared_identity`（只读、不联网、不建客户端） |
| 后端测试 | `backend/tests/{test_cache,test_checkup_facilities,test_checkup_v2,test_facility_stage,test_poi_online,test_poi_service,test_poi_tool}.py`、`backend/tests/checkup_browser_app.py`（新增）、`backend/tests/fixtures/poi/collection.json` | 回归矩阵、离线集成后端、回放夹具 |
| 离线 CLI | `backend/tools/poi-example.json` | 逐类补齐预算（词典已是 31 类） |
| 前端 | `life-circle-demo/src/checkup/*`（含新增 `extensions.ts`）、`life-circle-demo/README.md` | 补查事实、诊断行、**失败不再渲染成"没有设施"** |
| 浏览器套件 | `life-circle-demo/{playwright*.config.ts,tests/browser.ts,tests/checkup-browser.spec.ts,tests/demo.spec.ts,scripts/ensure-browser-libs.sh}` | 三套件可在本机真实运行；自带 Chromium；外连断言 |
| C 阶段工具 | `backend/tools/{poi_query_benchmark,poi_benchmark_world,poi_benchmark_merge,poi_benchmark_guard}.py` | C0 基准与 C2 隔离 PoC |
| 证据 | `evidence/00`–`09`、`PR-DRAFT.md` | A 阶段记录（00–04）、B 阶段核实与决策表单（05–06）、C 阶段记录（07–09） |
| 启动脚本 | `start.command`、`.gitattributes` | 用户改动并入（§1） |

---

## 3. 数据库兼容与回滚

**迁移**：`facility_extensions` 追加三个**可空** TEXT 列。

| 列 | 内容 | 旧行 |
| --- | --- | --- |
| `intent` | 客户端声明的请求身份（类别集合 + 是否显式给预算），JSON | `NULL` → 按旧规则判定 |
| `stop_reason` | 定稿时的停止原因 | `NULL` → 视图报 `null` |
| `initial_plan` | 执行前首轮估算 | `NULL` → 视图报 `null` |

- 机制沿用基线：`CREATE TABLE IF NOT EXISTS` + `PRAGMA table_info` 检查 + `ALTER TABLE ADD COLUMN`，幂等、非破坏、不改写既有行。
- 新代码读旧库：打开即补列，旧行读作"未记录"；测试 `test_an_old_extension_table_gains_the_new_columns_without_touching_rows`。
- 旧代码读新库：`_extension_record` 按列名取值、不引用新列，可正常读取。
- **本轮 C 阶段新增的工具不触碰任何生产表**：基准只用自己的临时 SQLite 账本（`.tmp/` 下，仓库忽略）。
- **回滚**：`git revert <commit>`（或把交付分支 reset 回基线）。数据库无需回退语句；页面缓存是进程内存，
  回滚不涉及数据迁移。

**接口影响**：对外字段语义未变（`requests` = 页面处理次数，`networkRequests` = 实际派发次数）。
新增均为可选/向后兼容：`facilities.initialPlan`、补查 `stopReason`/`initialPlan`、
`capabilities.poiPlanning`、`capabilities.cache.processLocal`；`budgets.poiRequestsIsLowerBound`
语义修正为 `false`。**唯一行为变化**：部分缓存 + 预算不足的补查由"422 拒绝"改为"受理并按缺页执行
（`partial`）"——这是 `scheme.md` 不变量 5 要求的修复方向，对应测试与说明已同步。

---

## 4. 验证摘要（详见 `03-validation.md`）

```text
后端全量（当前）  ：31 failed / 1016 passed / 10 skipped     ← 655e17d
后端全量（基线检出）：42 failed / 963 passed / 10 skipped
逐节点集合比对    ：新增失败 0；修好 11；31 + 11 = 42 精确闭合
前端              ：npm test → 41 files / 462 tests passed；npm run build → 通过
浏览器（真实运行）  ：test:checkup-ui 15 passed；test:e2e 10 passed；test:integration 7 passed
外连尝试          ：0（后端全会话守卫 + 每个浏览器用例断言）
启动脚本          ：bash -n 通过；./start.command --help 通过
```

---

## 5. 遗留问题的分类（更新）

| 类别 | 项 | 状态 |
| --- | --- | --- |
| ~~`blocking`（生产审批）~~ | ~~`test_poi_tool.py` 离线 CLI 不读凭据/不发 HTTP（2 项）~~ | **已解决且归因撤回**：守卫从未触发；真实原因是示例配置过期 ＋ 回放夹具缺页。见 `03-validation.md` §3 |
| ~~`blocking`（生产审批）~~ | ~~浏览器 E2E 未运行~~ | **已解决**：三个套件在自带 Chromium 上真实运行并通过（32 项） |
| `nonblocking with evidence` | 31 项基线失败（类别词典口径 30 项 ＋ 生成物漂移 1 项） | 全部 `baseline-confirmed`；改写它们等于宣布类别口径变更，属产品决定 |
| `environment` | 无 | 依赖齐备；Playwright 自带 Chromium 可用（运行库解在仓库忽略目录，不需要 root） |
| **新发现（C 阶段）** | 并集页 `>20 条` 被 `response_error` 判为 `invalid_response` | 这是 C2/合并方案的**前置阻塞**，不是 A 的阻塞项；见 `08-query-merge-design.md` §5.1、§8 P1 |
| **新发现（既有成本）** | 持续非限流错误以每序列 2 次的代价烧掉整轮预算 | 既有行为，本轮未改；量级见 `07-baseline-benchmark.md` §4(e) |

---

## 6. PR 草稿

见 `evidence/PR-DRAFT.md`（与本文件同步更新）。

---

## 7. Gate A 判定

`scheme.md` §4 的 Gate A 必须同时成立的四项，逐项对照：

| Gate A 条件 | 结论 | 依据 |
| --- | --- | --- |
| 预算与缓存核心回归通过 | ✅ | `02-regression-evidence.md`（13 类场景＋计量记录）；`07-baseline-benchmark.md` 整轮量级复核（全缓存重放 203 页 / 0 派发 / 0 账本 / 结论与冷启动一致） |
| 没有本轮新增且未解释的回归 | ✅ | 42 → 31，31 项逐个在隔离基线检出重放确认，**新增 0**（集合差集可复算） |
| 旧调用方兼容 | ✅ | `legacy_limits()` 固定旧语义；旧库/旧行/旧响应均有测试；离线 CLI 现可完整回放 |
| 所有变更可审查/可回滚 | ✅ | 61 文件、6 个独立提交，按关注点分开；迁移可空、回滚无数据迁移 |
| E2E 未通过必须明确列为生产审批阻塞 | ✅（条件不再触发） | E2E **已运行并通过**（15＋10＋7） |

**判定：Gate A 通过（本地交付层面）。**

签发依据是上述可复算的事实，而不是"报告里说通过了"：上一轮的两条阻塞项，一条被证明是**错误归因**
（守卫用例失败被读成"读了凭据"，实际是示例配置过期），一条被**真正解决**（E2E 从"未运行"变为"运行且通过"）。
两者都不再需要"豁免"，因此不再阻塞。

**必须同时讲清楚的边界**：

1. Gate A 通过**不等于**生产部署通过。真实额度与许可（B 阶段）仍是运营者的事，本阶段没有任何真实调用。
2. C 阶段的离线研究可以开始；但其中"多关键词合并"在真实对照前有一个**硬前置**（并集页校验器冲突，
   §5 已列），且 `scheme.md` §6 C2 明确：离线夹具只能给出"技术方案可试验 / 不可试验"。
3. 本判定由代码代理按用户授权作出，覆盖的是 §4 的技术条件；D/E/F 与推送、合并、部署仍需各自授权。

---

## 8. 本文件自身的提交

`04-patch-delivery.md` 与 `PR-DRAFT.md` 单独提交，便于审阅者按顺序阅读（代码与工具在前，交付记录在后）。
SHA 记录于 `git log --oneline delivery/facility-budget-cache`。
