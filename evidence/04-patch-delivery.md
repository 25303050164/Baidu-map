# A5｜可审查交付版本与 PR 草稿

**任务**：`scheme.md` A5
**执行时间**：2026-10-08
**授权边界**：**仅本地提交**。未 push、未 merge、未部署、未改线上配置、未调用真实百度服务。

---

## 1. 交付提交

| 项目 | 值 |
| --- | --- |
| 交付分支 | `delivery/facility-budget-cache`（从基线 HEAD 新建，**未推送**） |
| 交付提交 | `84db1b8` — `fix: keep cached pages out of the network budget and make extension retries idempotent` |
| 基线提交 | `decce8784e0505fb9601c1d493c522d919c1e348`（`distribution-boost` 本地与远端一致） |
| 提交文件数 | 37（28 个已跟踪文件修改 + 9 个新增，见 §2） |
| 变更规模 | +3571 / −268 |
| A5 记录提交 | 见 §8（本文件自身的提交，SHA 在文末） |

验证的代码内容与提交内容一致：提交后 `git status --porcelain -- <交付路径>` 为空（无未暂存改动），且 §3 的全部命令都在该内容上运行过。

> **关于"隔离工作树"**：本轮没有另建 `git worktree`。原因是本机只有主工作树里配好了 `backend/.venv` 与 `life-circle-demo/node_modules`，第二份检出无法运行测试，反而会让"验证过的内容"与"交付的内容"分开。改用**独立本地分支 + 路径白名单提交**达到同样目的：交付分支只含本任务文件，用户既有改动不进入提交；并逐文件核对提交后无残留差异。

## 2. 提交内容

**已跟踪文件（28，修改）**：`CURRENT_STATE.md`、`report.md`、`backend/.env.example`、`backend/README.md`、`backend/app/cache.py`、`backend/app/checkups/{facilities,manager,models,places,router,store}.py`、`backend/app/poi/{cache,online}.py`、`backend/tests/{test_cache,test_checkup_facilities,test_checkup_v2,test_facility_stage,test_poi_online}.py`、`life-circle-demo/README.md`、`life-circle-demo/src/checkup/{CheckupApp.tsx,capabilities.ts,capabilities.test.ts,categories.ts,categories.test.ts,checkup.css,contract.ts,fixtures.ts,validate.ts}`

**新增（9）**：`backend/app/poi/plan.py`、`life-circle-demo/src/checkup/extensions.ts`、`life-circle-demo/src/checkup/extensions.test.ts`、`backend/docs/设施检索预算与缓存修复-2026-10-08.md`、`backend/docs/设施分类规则约束报告-2026-10-05.md`（原 `report.md` 的保全副本）、`evidence/00-worktree-inventory.md`、`evidence/01-code-review.md`、`evidence/02-regression-evidence.md`、`evidence/03-validation.md`

**明确排除（用户既有改动，保持原状）**：`start.command`（仍为暂存状态，未进入提交）、`.gitattributes`、`Baidu-map/`、`intro-page/`、`ai-tone-issues.json`、`prompt.md`、`scheme.md`、`*:Zone.Identifier`、OSM 地区包相关工作（`backend/app/osm_package.py`、`backend/scripts/{configure,pack,setup}_osm_region.py`、`backend/docs/OSM_REGION_SETUP.md`、`backend/tests/test_osm_package.py`、`data/osm/*`）。

核对：`git show --name-only HEAD | grep -c start.command` → **0**；`git status --short | grep start.command` → 仍为 `M ` 暂存。

## 3. 数据库兼容与回滚

**迁移**：`facility_extensions` 追加三个**可空** TEXT 列

| 列 | 内容 | 旧行 |
| --- | --- | --- |
| `intent` | 客户端声明的请求身份（类别集合 + 是否显式给预算），JSON | `NULL` → 按旧规则判定 |
| `stop_reason` | 定稿时的停止原因 | `NULL` → 视图报 `null` |
| `initial_plan` | 执行前首轮估算 | `NULL` → 视图报 `null` |

- 机制沿用基线：`CREATE TABLE IF NOT EXISTS` + `PRAGMA table_info` 检查 + `ALTER TABLE ADD COLUMN`，幂等、非破坏、不改写既有行。
- 新代码读旧库：打开即补列，旧行读作"未记录"。测试：`test_an_old_extension_table_gains_the_new_columns_without_touching_rows`。
- 旧代码读新库：`_extension_record` 按列名取值、不引用新列，可正常读取；新增列可空，不影响旧查询。
- **回滚**：`git revert <commit>`（或把交付分支 reset 回基线）。数据库无需回退语句；页面缓存本来就是进程内存，回滚不涉及数据迁移。

**接口影响**：对外字段语义未变（`requests` = 页面处理次数，`networkRequests` = 实际派发次数）。新增均为可选/向后兼容：`facilities.initialPlan`、补查 `stopReason`/`initialPlan`、`capabilities.poiPlanning`、`capabilities.cache.processLocal`；`budgets.poiRequestsIsLowerBound` 语义修正为 `false`（该乘积历来是首轮估计）。**行为变化**：部分缓存 + 预算不足的补查由"422 拒绝"改为"受理并按缺页执行（partial）"——这是 scheme.md 不变量 5 要求的修复方向，已同步更新对应测试与说明。

## 4. 验证摘要（详见 `03-validation.md`）

```text
后端全量：34 failed / 1012 passed / 10 skipped   （基线 42 failed / 968 passed / 10 skipped）
          逐条比对：新增失败 0；基线失败修好 8 项
针对性：  test_checkup_facilities + test_checkup_v2 + test_cache + test_poi_online + test_quota
          → 126 passed
前端：    npm test → 41 files / 458 tests passed；npm run build → 通过
外连：    conftest 全会话防护 0 次尝试
E2E：     NOT RUN（缺 libasound.so.2；配置要求 msedge）→ 生产审批阻塞项
```

## 5. 遗留问题的分类（供审阅者签字）

| 类别 | 项 | 说明 |
| --- | --- | --- |
| `blocking`（生产审批） | `test_poi_tool.py::test_offline_cli_never_reads_credentials_or_sends_http`（2 项） | 离线 CLI 的"不读凭据/不发 HTTP"守卫在基线即失败；涉及安全承诺，需单独处理后才谈部署 |
| `blocking`（生产审批） | 浏览器 E2E 未运行 | 环境缺 Chromium 运行库；上线前必须在具备 Edge/Chromium 的环境中跑通 `test:checkup-ui` 与 `test:integration` |
| `nonblocking with evidence` | 类别词典重构遗留（23 项）、旧 POI 运行时（5 项）、生成物漂移（1 项）、其余既有偏差（3 项） | 全部 `baseline-confirmed`，不经过本轮的预算/缓存/补查路径 |
| `environment` | 无 | 依赖齐备；`shapely`/`pydantic_settings`/`life_circle` 均可用 |

## 6. PR 草稿

**标题**：`fix: keep cached pages out of the network budget and make extension retries idempotent`

**正文**：

```markdown
# 设施检索的预算、缓存与补查衔接修复

## 摘要
修复三处会让"有限额度浪费或误挡"的衔接问题，并补齐回归测试、契约与配置说明。
全部验证使用 mock/synthetic provider、临时 SQLite 与受控时钟；未使用真实 AK、未请求百度服务。

## 改了什么
1. 幂等先于预算，也先于"最新修订"：同一 clientRequestId 取回原补查（不排队、不发请求、
   不扣额度，日余额为 0 也一样）；参数不同 409 且优先于余额判断；身份只认客户端声明，
   冻结在 intent 列；旧行按旧规则；合法重试不再需要"最新修订的几何还能用"。
2. 预检看得见缓存，且只有一条规则：poi/plan.py 用规划器自己的第一轮与真实页键估算；
   admission_refusal() 由补查接口与设施阶段共用——全缓存放行、部分缓存按缺页放行、
   只有冷启动缺额具名拒绝。
3. 两把尺子：网络额度（ServicePool.attempt() 唯一原子扣减点）与本地处理上限（4096 步，
   独立停止原因）分开；额度用尽时继续读完缓存、缺页逐页记名并报 partial。
4. 计量按真实派发归属：PageResponse.manner（live/cache/shared/refused）；修掉"共享失败
   被等待方记成自己派发"和"纯缓存路径无法及时响应取消"。
5. 关停不再卡死：worker 循环由 closing 条件退出（检索器会把 CancelledError 当结果吸收）。
6. 诊断与文案：initialPlan / stopReason / poiPlanning / processLocal；isLowerBound=false；
   initialPlan 明确为"执行前估算，不是额度预留"。

## 迁移与兼容
- facility_extensions 追加三个可空列，沿用幂等迁移；旧行读作"未记录"，不补造值。
- 旧代码读新库不受影响；对外字段语义未变；新增诊断字段对旧响应兼容。
- 仅一处行为变化：部分缓存 + 预算不足由 422 改为受理并按缺页执行（见 §"为什么"）。

## 验证
- 后端全量 34 failed / 1012 passed（基线 42 / 968）→ 新增失败 0，修好 8 项基线失败
- 针对性 126 passed；前端 458 passed + build 通过；外连尝试 0
- E2E NOT RUN（缺 libasound.so.2 / 无 msedge）→ 生产审批阻塞

## 回滚
git revert <commit>；数据库新增列可空，回滚后旧行仍可读。
```

## 7. Gate A 自评

| Gate A 必须成立的条件 | 状态 | 依据 |
| --- | --- | --- |
| 预算与缓存核心回归通过 | ✅ | `02-regression-evidence.md`（13 类场景 + 计量记录） |
| 没有本轮新增且未解释的回归 | ✅ | 42 → 34 失败，逐条比对新增 0（同环境对照） |
| 旧调用方兼容 | ✅ | `legacy_limits()` 固定旧语义；旧库/旧行/旧响应均有测试 |
| 所有变更可审查/可回滚 | ✅ | 37 文件独立提交 `84db1b8`；迁移可空、回滚无数据迁移 |
| E2E 未通过必须明确列为生产审批阻塞 | ✅ | `03-validation.md` §4 + 本文件 §5 |

**建议的 Gate A 判定**：技术条件成立，可进入 C 阶段离线研究；**生产部署不通过**，直到 E2E 与 `test_poi_tool.py` 两项阻塞解决、且运营者完成 B 阶段。

## 8. 本文件自身的提交

`04-patch-delivery.md` 与 PR 草稿单独提交，便于审阅者按顺序阅读（代码提交在前，交付记录在后）。SHA 记录于 `git log --oneline delivery/facility-budget-cache`。
