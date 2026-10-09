# A4｜完整验证与失败归因

**任务**：`scheme.md` A4
**执行时间**：2026-10-09（重写；上一版 2026-10-08 的两条结论已按 §3 撤回）
**环境**：同一台机器、同一虚拟环境（`backend/.venv`，Python 3.12.3，pytest 9.1.1）、同一依赖清单；
`conftest.py` 在导入任何应用模块之前就拒绝全部外连，测试结束打印尝试次数。
**被验代码**：`655e17dd69c5b6c128bc229ff3f764163cb8ceae`（分支 `delivery/facility-budget-cache`）；
冻结后在最终提交 `e515ad0` 上**再跑一次全量复核**，结果与失败节点集合完全一致（见表首两行）。
其余提交只新增 `backend/tools/poi_benchmark_*.py`（`pytest.ini` 的 `testpaths = tests`，不被收集）
与 `evidence/*`，不触及任何被测代码路径。
**基线对照**：`decce8784e0505fb9601c1d493c522d919c1e348`，在 `.tmp/baseline-tree` 的
`git worktree`（detached）中以**同一解释器**运行，`-p no:cacheprovider`。

> 关于隔离检出：`life_circle` 是指向主工作树的 editable 安装，因此必须确认它在本任务前后没有变化——
> `git log --oneline decce878..HEAD -- life-circle-algorithm` 输出为空，即该包与基线逐字节相同，
> 不会把新代码带进基线运行。`app` 包由 `backend/pytest.ini` 的 `pythonpath = .` 从检出根解析。

---

## 1. 实际执行的命令与结果

| 命令 | 结果 | 日志 |
| --- | --- | --- |
| `cd backend && .venv/bin/python -m pytest -q -rf` | **31 failed / 1016 passed / 10 skipped**（558.18s），exit 1 | `.tmp/verify/full-A-final.txt` |
| 同上，在**最终提交 `e515ad0`** 上再跑一次（冻结后复核） | **31 failed / 1016 passed / 10 skipped**（617.22s），exit 1；失败节点集合与上一次**完全相同**（集合差为空） | `.tmp/verify/full-A-final-tip.txt` |
| 同上，在 `.tmp/baseline-tree/backend`（基线检出） | **42 failed / 963 passed / 10 skipped**（482.23s），exit 1 | `.tmp/verify/baseline-full.txt` |
| 把本轮的 31 个失败节点逐个在基线检出重放 | **31 failed**（69.35s）——与当前失败集**逐项一致** | `.tmp/verify/baseline-repro.txt` |
| `cd life-circle-demo && npm test` | **41 files / 462 tests passed**，exit 0 | `.tmp/verify/frontend-unit.txt` |
| `cd life-circle-demo && npm run build` | 通过（`tsc -b` + `vite build`），exit 0 | `.tmp/verify/frontend-build.txt` |
| `npm run test:checkup-ui` | **15 passed**（36.9s），exit 0 | `.tmp/verify/browser-suites.txt` |
| `DEMO_PORT=5186 DEMO_OUTPUT_DIR=output/demo-regression npm run test:e2e` | **10 passed**（1.9m），exit 0 | 同上 |
| `npm run test:integration` | **7 passed**（1.4m），exit 0 | 同上 |
| `bash -n start.command && ./start.command --help` | 通过（打印启动器用法） | 见 `0665f60` |
| 外连尝试统计 | **0 次**：后端两次全量都没有 `external connection attempts refused` 行（`grep -c` = 0）；三个浏览器套件每个用例都断言 `external.attempts == 0` | — |

**未跟踪文件对计数的影响（必须说明）**：`backend/tests/test_osm_package.py`（用户 OSM 工作，未跟踪）
在整仓收集中贡献 **5** 个用例；它只有在整仓收集时才可导入（单独收集会 `ModuleNotFoundError: backend`，
因为需要另一模块先把仓库根加入 `sys.path`）。基线检出中没有该文件。因此可比口径为：
当前树 **1057** 项 = 31 + 1016 + 10；基线树 **1015** 项 = 42 + 963 + 10。31 项失败中没有一项来自该文件。

---

## 2. 失败归因（同环境、同依赖、逐项）

### 2.1 结论

```text
基线 42 failed  →  当前 31 failed
本轮引入（当前失败但基线通过）：0 项
本轮修好（基线失败但当前通过）：11 项
两边都失败（baseline-confirmed）：31 项
31 + 11 = 42   ← 与基线失败总数精确闭合
```

核对方式不是"看名字像不像"，而是**把当前 31 个失败节点逐个在基线检出上重放**，两次运行的
节点 id 集合做差集。当前失败集是基线失败集的**真子集**，所以"新增失败 0"是可复算的结论。

### 2.2 本轮修好的 11 项

| 用例 | 真实原因 |
| --- | --- |
| `test_poi_tool.py::test_offline_cli_never_reads_credentials_or_sends_http[plan]`、`[replay]` | **示例配置过期**：`tools/poi-example.json` 只给了 3 个类的预算，而 `RuntimeConfig` 要求逐类给全（词典已是 31 类），CLI 因此以一个笼统错误码退出。**守卫从未触发**（详见 §3 撤回） |
| `test_poi_service.py::test_total_budget_and_category_budget_preserve_other_categories` | 同一根因：用例构造的部分预算字典缺类 |
| `test_facilities.py`、`test_checkup_facilities.py` 共 7 项 | 期望值仍写死"三类 6 小类 × 4 块 = 24 页"等旧数字，与 3→10 类词典重构脱节；改为按词典推导期望值（断言未删、未放宽） |
| `test_facility_stage.py::test_the_daily_allowance_is_checked_before_the_request` | 夹具未固定档位切换时间，切换日之后生效的是 fallback 档；修的是夹具，断言原样 |

### 2.3 仍然失败的 31 项（全部 `baseline-confirmed`）

按用例文件分布（数据由失败节点统计得出）：

| 文件 | 项数 | 根因 |
| --- | ---: | --- |
| `test_facilities.py` | 10 | 类别词典/展示口径重构遗留 |
| `test_catalog.py` | 4 | 同上（旧三元组、旧共享词表、旧排除词表） |
| `test_poi_acceptance.py` | 4 | 同上（旧标签→类别映射期望） |
| `test_poi_service.py` | 4 | 3 项属旧自适应网格运行时的计划规模/状态期望；1 项属类别词典 |
| `test_business_api.py` | 3 | 旧类别数（`assert 13 == 5` 等） |
| `test_checkup_progress.py` | 2 | 展示组改名（`医疗` → `医疗健康`） |
| `test_analysis.py` | 1 | 旧展示组三元组 |
| `test_checkup_report.py` | 1 | 旧类别数（`assert 24 == 6`） |
| `test_place_safety.py` | 1 | 旧类别名（`school`） |
| `test_poi_evidence.py` | 1 | 生成物漂移：`api-contract.ts` 与当前序列化契约不一致（基线即已过期） |

判定：这些用例描述的是**类别词典口径、旧运行时语义与生成物**，都不经过本轮的
预算/缓存/补查路径，且在同一基线上以同样方式失败。**改写它们等于宣布类别口径变更**，
属于产品决定，不在本轮范围。其中 `test_poi_evidence.py` 的生成物漂移是唯一的非词典项，
重跑导出会一次性改写约 12k 行无关内容，因此本轮不动。

### 2.4 是否阻塞 Gate A

按 `scheme.md` §4 A4／§13：核心正确性（预算、缓存、幂等、取消、关停、准入）全部通过，
**新增失败 0**，旧调用方语义保持。31 项失败全部 `baseline-confirmed`，
不阻塞本地交付，也**不**构成新的生产阻塞项（它们与本轮改动无关，且在基线提交上同样存在）。

---

## 3. 对上一版 `03-validation.md` 的撤回与更正

上一版（2026-10-08）有两处**错误结论**，本版予以撤回：

**① 撤回："离线 CLI 在读凭据/发 HTTP，属既有安全风险"。**
这是从"守卫用例失败"反推"守卫触发"得来的，**没有读过守卫的实现，也没有读过 CLI 的真实失败原因**。
实际重跑后：`test_offline_cli_never_reads_credentials_or_sends_http` 里的
`monkeypatch.setattr(app.config, 'load_settings', forbidden)` 与 `httpx.AsyncClient.get` 替身
**从未被调用**；CLI 返回 1 的原因在更早一步——`CollectionConfig.model_validate_json` 拒绝了随附的
`tools/poi-example.json`（`runtime.categoryBudgets` 缺 28 个类）。第二个被掩盖的原因是回放夹具缺
`market.extraQueries` 新增的 `农副产品市场` 的首页，于是回放报 `fixture_missing` 并落 `partial`。
修法是**数据**（示例配置 + 夹具 + 两个用例的构造方式），不是放宽安全断言。
原先标为 `blocking（生产审批）` 的这一项**撤销**。

**② 更正："E2E NOT RUN（环境阻塞）"。**
当时的描述把"本机缺一个共享库"当成了被测功能的问题。实际有两层：
仓库四个 Playwright 配置都写死 `channel: 'msedge'`（本机没有 Edge，也没有任何系统 Chromium），
而 Playwright 自带的 Chromium 缺 `libasound.so.2`。现在：
- `life-circle-demo/scripts/ensure-browser-libs.sh` 把发行版运行库解到仓库忽略的
  `.tmp/playwright-libs/`（**不需要 root、不改系统**），`tests/browser.ts` 只在目录存在时接上
  `LD_LIBRARY_PATH`；浏览器、产物目录、端口都改为可配置，默认用自带 Chromium。
- 结果是三个套件**真的跑起来了并通过**（§1）。因此"E2E 未通过必须列为生产审批阻塞"这一条
  **已满足**：不是因为环境被豁免，而是因为 E2E 已经执行且通过。

**③ 上一版的失败计数（34 项、4 组分法）作废。** 上一版缺少可复核的基线日志（`/tmp/baidu-verify/*`
已不存在），分组是按行数估的。本版用隔离基线检出的**完整重跑**重新计得 42 → 31，并且 31 项逐个在
基线上重放确认。

---

## 4. 浏览器端到端：已运行（含一个真实缺陷）

三个套件共 **32** 个用例全部通过，且每个用例都断言 `external.attempts == 0`（即页面没有向
127.0.0.1 之外发出任何请求）。它们覆盖：普通体检、预算不足、全缓存零额度补查、部分结果与补查、
错误状态、实时诊断文案、幂等取回与冲突 409、限流有界停止、取消、父任务报告不变。

**这些套件第一次运行就抓到一个真实缺陷**：上游持续限流时，后端如实报
`queryStatus=failed` + `stopReason=rate_limit`，而界面仍渲染"本次体检没有接收的设施。"——
把一个**失败**说成了**没有**，正是 `scheme.md` G3／§4.4 要排除的那种零。
该行为来自基线提交 `85ec80e4`，**不是本轮引入**。修法：`nearestEmptyNote()` 按
`queryStatus`/`stopReason` 给出具名说明，停止原因复用补查面板同一张词表，未知原因原样回显；
新增 12 项单测（其中 4 项针对该函数）。旧行为在基线检出上可复现。

这条也说明：浏览器验收不是形式——它是本轮唯一发现该缺陷的环节。

---

## 5. 未验证范围（明确列出）

| 项目 | 状态 | 原因 |
| --- | --- | --- |
| 真实百度 Place API 行为与召回 | **NOT RUN** | 未获授权（D 阶段）；C 阶段只有离线证据 |
| 真实账号配额/计费与许可 | **NOT RUN** | 运营者核实（B 阶段） |
| v3 官方文档本身 | **NOT RUN（未能取得）** | 官方页面是 JS 单页应用，抓取只得标题壳；C1 已按官方 v2 原文 + 镜像交叉印证，并把差异登记为待确认项 |
| 多关键词 `$` 合并查询 | **NOT RUN（不可直接试验）** | 现有响应校验器把 `>20` 条判为 `invalid_response`，而官方文档说明并集页为 `关键词数 × page_size` 条；见 `08-query-merge-design.md` §5.1 |
| 多进程/多实例部署、跨日与跨重启续查 | **NOT RUN** | F 阶段范围 |
| 长时间稳定性、真实地理数据质量 | **NOT RUN** | 同上 |

---

## 6. 结论

- 后端全量：**31 failed / 1016 passed / 10 skipped**；基线同环境 **42 / 963 / 10**；
  逐项集合比对：**新增 0，修好 11**，`31 + 11 = 42` 精确闭合。
- 前端：**462 项单测**＋**构建**通过；浏览器三个套件 **32 项**通过，外连尝试 **0**。
- 上一版的"CLI 凭据风险"归因**错误且已撤回**；"E2E 环境阻塞"**已解除**，因为 E2E 现在真的运行并通过。
- 本阶段新发现、且**不属于**本轮授权的改动：C1 记录的并集页校验器冲突（C 阶段阻塞项）、
  以及基准测得的一处既有成本问题（持续非限流错误会以每序列 2 次的代价烧掉整轮预算，见 `07` §4(e)）。
  两者都按 `scheme.md` §12 记录并停止，未擅自"顺手修掉"。
