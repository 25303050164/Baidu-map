# 检查点｜2026-10-09 本批次（配置对齐真实权益 + B 阶段证据）

**停止原因**：用户电脑即将断电，按指示在检查点暂存并停止。
**分支**：`delivery/facility-budget-cache`（本地提交，**未 push**）
**本批次起点**：`b136ce7`

---

## 1. 本批次做了什么

### 1.1 配置对齐控制台核实到的真实权益

运营者于 2026-10-09 从百度控制台直接复制权益文本，据此修正了应用自身配置
（此前应用额度远**低于**账号，但同时有一处**高于**账号）：

| 项 | 原值 | 现值 | 依据 |
| --- | --- | --- | --- |
| 地点 QPS（两档） | 8 / 2 | **10** | 账号 10 QPS |
| 步行路线 QPS（两档） | **16** / 2 | **3** | 账号 3 QPS（原值超出 5 倍，潜伏 429） |
| 应用日预算（两档） | 1600 / 80 | **2000** | 运营授权；占账号 5 万/天的 4% |
| 单任务预算默认 | 60 | **240** | 覆盖合成夹具"适中"整轮 203 次 |
| 单任务预算上限 | 160 | **1600** | 覆盖"密集"整轮 1379 次 |
| `.env` 键名 | `ANALYSIS_PROVIDE` | `ANALYSIS_PROVIDER` | 拼错被静默忽略 |
| 档位切换 | 20 倍落差 | **保留切换点，两档参数一致** | 降级理由（权益失效）已被控制台否定 |

`app/config.py` 新增 `VERIFIED_ENTITLEMENT`，把核实到的四个数字记进代码，供测试锁定。

### 1.2 测试卫生

- `tests/conftest.py` 新增会话级 autouse 夹具：所有显式构造的 `Settings(_env_file=None, …)`
  若未自带路径，则把 `quota_ledger_path`/`checkup_dir`/`hybrid_ledger_dir` 重定向到临时目录。
  **即测试套件不再初始化也不会花掉部署自己的账本与体检存储**（已用 mtime 前后对比验证）。
  `load_settings()` 不受影响。
- `tests/integration_app.py` 补上自己的临时路径（原先指向真实账本与存储）。
- 新增 `tools/verify_deployment_config.py`：只读、外连阻断、不读 AK 值、`mode=ro` 打开账本，
  并报出**无人读取的 `.env` 键**。它发现了另外三个静默失效的键：
  `OSM_CACHE_PATH`、`OSM_CACHE_VERSION`、`OSM_WALK_SPEED_MPS`（**未修**，待确认）。
- `tools/poi_query_benchmark.py`：把 `-60` 命名场景的预算**钉死为 60**（新增
  `RECORDED_DEFAULT_BUDGET`），否则 C0/C2 的既有记录会被"看起来一样、实际不同"地重跑。

### 1.3 证据文件

- `evidence/05-operations-and-license.md`：**重写**。并入控制台权益（A1–A7）、
  许可答复（L1–L6）、配置口径（C1–C4），新增 §4.1 流量图差异、§4.2 实验授权边界。
- `evidence/06-service-level-decisions.md`：**重写**。五问答复 + 四项补充口径 + 决策记录 v2 +
  由决策推导的实现要求清单。

---

## 2. 验证状态（**重要：不完整**）

| 项 | 状态 |
| --- | --- |
| `tests/test_quota.py` + `test_checkup_v2.py` + `test_checkup_facilities.py` | ✅ **78 passed**（改额度后重跑，用 `-p no:randomly`） |
| 后端**全量**套件（改额度后） | ⚠️ **未完成**：中途被停止。上一次完整运行显示 **40 failed / 1008 passed**，我逐个归因并修掉了其中 **9 个新失败**（详见下），但**没有重跑全量确认回到 31 个已知失败** |
| 前端单测 / 构建 / 浏览器套件 | ❌ 本批次**未运行**（本批次未改前端） |
| 真实百度调用 | **0 次**（全程未见真实请求；`verify_deployment_config` 报告外连尝试 0） |
| 部署账本与体检存储被改动 | **无**：10-09 仍无 `daily_spend` 行，两个 SQLite 的 mtime 前后一致 |

### 2.1 已归因并修掉的 9 个新失败

全部根因相同：断言写死了旧的 60 基线。修法是**把每个测试自己的场景钉住**，不削弱断言：

| 测试 | 修法 |
| --- | --- |
| `test_quota.py::test_the_configured_qps_cap_is_the_tier_ceiling` | 3/10；并断言切换档位**不再**改变额度上限 |
| `test_quota.py::test_both_tiers_carry_the_same_ceiling_and_stay_inside_the_account` | 新增：锁定"两档一致且不超账号权益" |
| `test_checkup_facilities.py`（2 处） | `make_app` 固定日额度 1600（避免随部署默认漂移）；`poiRequests` 60→240 |
| `test_a_facility_run_that_fails_every_page_is_a_failed_query` | 显式钉 `maxPoiRequests=60`，保住"额度中途用尽、被拒页面按名字记录"这一分支 |
| `test_an_omitted_budget_replay_survives_a_changed_default` | 默认 60→240；新默认由 `DEFAULT_POI_REQUESTS*2` 推出 |
| `test_a_row_written_before_the_identity_column_is_still_replayable` | 冻结预算改为**实际解析值**（不再写字面量） |
| "跨日推进"一节 | `REQUEST_LIMIT` 由 `DEFAULT_POI_REQUESTS` 改为显式 **60**（该节文档写的就是 60/80 的场景） |
| `test_checkup_v2.py` | `poiRequests` 240、`maxPoiRequests` 1600、QPS 3/10、trace `poi.limit` 240 |

---

## 3. 未完成的阻塞项（按优先级）

1. **流量图与账本对不上（真实实验的硬阻塞）** —— `05` §4.1。
   运营者推测 `/place/v3/around` 可能是误上传；但本地已落盘证据**反证**了这一点：
   30 个 `dataSource=baidu_place` 修订、**776 个成功真实页**、
   **217 个设施全部是 32 位十六进制 GUID**，且修订日期与账本逐日对应。
   需要控制台补四个数字：10-08 与 09-29 的当日调用数、页面所属 AK 是否同一个、流量图粒度。
   **在拿到之前不发起任何真实调用，也不改生产请求路径。**
2. **许可缺出处**：L1/L2/L4 只有口头结论；L6（是否书面咨询平台）**未答复**。
3. **L2/L4 的期限尚无代码执行**：现状是长期落盘（264 文件 / 461.9 MB），与刚取得的授权不一致。
4. **三个 `.env` 键静默失效**（`OSM_*`）—— 未修。
5. **全量套件未复跑**（见 §2）。

---

## 4. 下一步（已批准计划中尚未执行的部分）

计划见上一轮已批准的方案，剩余顺序建议：

1. 复跑后端全量套件，确认 **31 failed**（已知失败）无新增；
2. **覆盖率指标 `queryAreaCoverage`**（各小类未完成区域取并集 / 圈面 ≥ 80%）+ 测试；
3. **同一次体检的重试**（新修订、独立本轮预算、冻结圈面、幂等、409、禁止并行发布）；
4. **会话与生命周期**（会话续租/离开、来源期限传递、到期清理、历史迁移清单）；
5. **前端**（覆盖率展示、按真实原因提示、重试入口、`no-store`、过期状态）；
6. **上海受控实验工具**（上限 = min(40% × 账号日权益, 可用日余额)；**32 次上限已取消**）；
7. 更新 `evidence/07`–`09` 的口径说明（默认预算已从 60 变为 240），
   并新增实验与生命周期证据文件。

---

## 5. 提交时未纳入的用户文件（保持原样，未被覆盖）

`Baidu-map/`、`intro-page/`、`prompt.md`、`scheme.md`、`ai-tone-issues.json`、
`*:Zone.Identifier`、`backend/app/osm_package.py`、`backend/docs/OSM_REGION_SETUP.md`、
`backend/scripts/{configure_osm_region,pack_osm_bundle,setup_osm_region}.py`、
`backend/tests/test_osm_package.py`、`data/osm/*`。

**另外**：`backend/.env` 的键名已修正，但该文件被 `.gitignore` 忽略，因此不在提交内。
