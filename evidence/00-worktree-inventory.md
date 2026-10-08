# A1｜工作区清单与保全

**任务**：`scheme.md` A1（保全工作区并建立变更清单）
**执行时间**：2026-10-08
**证据级别**：E0（静态记录）＋ E1（可重复的本地命令输出）

---

## 1. 版本与工作区

| 项目 | 值 | 获取方式 |
| --- | --- | --- |
| 分支 | `distribution-boost` | `git branch --show-current` |
| 本地 HEAD | `decce8784e0505fb9601c1d493c522d919c1e348` | `git rev-parse HEAD` |
| 远端 HEAD | `decce8784e0505fb9601c1d493c522d919c1e348` | `git ls-remote https://github.com/PennEwan/Baidu-map.git refs/heads/distribution-boost` |
| 是否已提交 | **未提交**。本地 28 个已跟踪文件被修改、1 个文件已暂存、若干未跟踪文件 | `git status --short` |
| 是否已推送/合并/部署 | **否**。本轮未执行任何 push / merge / deploy | 执行记录 |

本地 HEAD 与远端一致；本轮全部改动都在工作区，不在远端。因此**不能把远端代码当成已修复版本**，也不能把上一轮 `report.md` 的结论当作复验结论。

## 2. 变更清单与归属

### 2.1 本任务（prompt.md 修复 + 同位置验证）的改动 — 交付范围

已跟踪文件修改（28，按目录）：

| 范围 | 文件 | 内容 |
| --- | --- | --- |
| 配额/缓存/调度 | `backend/app/cache.py`、`backend/app/poi/cache.py`、`backend/app/poi/online.py` | 显式投递事实、两把尺子、`PageResponse.manner` |
| 设施阶段 | `backend/app/checkups/facilities.py` | 缓存感知预检、`budget_refusal`、诊断计数 |
| 补查 | `backend/app/checkups/manager.py`、`backend/app/checkups/store.py` | 幂等先于预算、`intent`/`stop_reason`/`initial_plan` |
| 契约 | `backend/app/checkups/models.py`、`backend/app/checkups/router.py`、`life-circle-demo/src/checkup/contract.ts` | 新诊断字段、capabilities 口径 |
| 传输身份 | `backend/app/checkups/places.py` | `declared_identity`（只读、不联网） |
| 测试 | `backend/tests/test_cache.py`、`test_checkup_facilities.py`、`test_checkup_v2.py`、`test_facility_stage.py`、`test_poi_online.py` | 回归矩阵 |
| 说明 | `CURRENT_STATE.md`、`backend/README.md`、`backend/.env.example`、`life-circle-demo/README.md` | 额度/缓存/补查口径 |
| 前端 | `life-circle-demo/src/checkup/{CheckupApp.tsx,capabilities.ts,capabilities.test.ts,categories.ts,categories.test.ts,checkup.css,fixtures.ts,validate.ts}` | 文案、诊断行、补查事实 |
| 报告 | `report.md` | 本轮递交报告（替换 2026-10-05 同名文件，原文已保全，见 §4） |

新增文件（5）：

| 文件 | 状态 | 说明 |
| --- | --- | --- |
| `backend/app/poi/plan.py` | 未跟踪，已核对存在（134 行） | 首轮计划与成本估算 |
| `life-circle-demo/src/checkup/extensions.ts` | 未跟踪，已核对存在（106 行） | 补查面板渲染辅助 |
| `life-circle-demo/src/checkup/extensions.test.ts` | 未跟踪，已核对存在 | 上述辅助的单测 |
| `backend/docs/设施检索预算与缓存修复-2026-10-08.md` | 未跟踪，已核对存在（230 行） | 完整证据文档 |
| `evidence/` 目录 | 本轮创建 | A/C 阶段证据（本文件为第一篇） |

上一轮报告中声称存在的三个文件（`backend/app/poi/plan.py`、前端补查模块、测试与文档）**已逐一核对存在且为未提交状态**，见上表；没有把"报告中说有"当作"存在"。

### 2.2 用户既有改动 — **不在交付范围，必须保全**

| 项目 | 状态 | 处理方式 |
| --- | --- | --- |
| `start.command` | 已暂存（+16/−16） | 保持暂存不动；交付提交按路径指定文件，不会带上它 |
| `.gitattributes` | 未跟踪 | 保持不动（声明 `*.command`/`*.sh` 用 LF、`*.bat` 用 CRLF） |
| `Baidu-map/`、`intro-page/`、`ai-tone-issues.json` | 未跟踪 | 保持不动 |
| `backend/app/osm_package.py`、`backend/docs/OSM_REGION_SETUP.md`、`backend/scripts/{configure,pack,setup}_osm_region.py`、`backend/tests/test_osm_package.py`、`data/osm/*` | 未跟踪（OSM 地区包工作） | 保持不动 |
| `prompt.md`、`scheme.md`、`*:Zone.Identifier` | 未跟踪 | 保持不动（任务输入） |

**保全方式**：不执行 `reset`、`checkout`、`stash`、`clean`，不删除任何文件；交付提交使用路径白名单；未跟踪文件一律不加入提交。

### 2.3 与已提交历史的关系

`report.md` 在本轮被替换为本次递交报告。原《设施分类规则约束报告》（2026-10-05，109 行）已：

1. 逐字节复制为 `backend/docs/设施分类规则约束报告-2026-10-05.md`（未跟踪，6524 字节）；
2. 保留在 git 历史中（提交 `c030bca`），可用 `git show c030bca:report.md` 复核。

## 3. 数据库 schema 变更与迁移策略

本轮唯一 schema 变更是**追加三个可空列**到 `facility_extensions`：

| 列 | 类型 | 含义 | 旧行读取 |
| --- | --- | --- | --- |
| `intent` | TEXT（JSON） | 客户端当时声明的请求身份（类别集合 + 是否显式给预算） | `NULL` → `intent=None`，按旧版解析规则判定 |
| `stop_reason` | TEXT | 定稿时的停止原因 | `NULL` → 视图报 `null`，不猜原因 |
| `initial_plan` | TEXT（JSON） | 执行前首轮估算 | `NULL` → 视图报 `null` |

要点：

- **非破坏性、幂等**：`create_schema()` 先 `CREATE TABLE IF NOT EXISTS`，再对 `PRAGMA table_info` 缺失的列执行 `ALTER TABLE ... ADD COLUMN`（`EXTENSION_ADDED_COLUMNS`）。基准版本已有同机制（此前只有 `facilities_status`），本轮只是把三个新列加进同一个元组。
- **旧库升级**：旧版数据库打开即补齐列，既有行保持原样（不改写任何行、不重算预算）。
- **回滚**：旧代码读取新版数据库时，`_extension_record` 按列名取值且不引用新列，因此可正常读取；新增列可空，不影响旧查询。
- `tasks` 表本轮未改（其 `ADDED_COLUMNS` 为基准版本已有）。

## 4. 行尾与格式保全

仓库工作区同时存在 CRLF 与 LF 文件。本轮编辑中出现过一次副作用：两处文件被改写为 LF，导致 **约 1300 行纯行尾差异**混入 diff。已修复：逐行比对基准与工作区，**内容未变的行恢复其原始字节（含行尾）**，仅真正新增的行按该文件周围惯例写入。结果：

| 文件 | 修复前 numstat | 修复后 numstat（含行尾 / 忽略行尾） |
| --- | --- | --- |
| `backend/tests/test_checkup_facilities.py` | +1384 / −686 | **+743 / −45**（二者一致） |
| `backend/README.md` | +71 / −46 | **+25 / −0** |
| 全树 | +2700 / −935 | **+2013 / −248**（忽略行尾 +2012 / −247） |

校验命令与结果：

```text
$ git -c core.whitespace=cr-at-eol diff --check      # 无输出，退出码 0
$ git diff --numstat          # 与 git diff --ignore-space-at-eol --numstat 基本一致（差 1 行）
```

说明：`git diff --check` 默认把 CRLF 的 `\r` 当作行尾空白，必须用 `core.whitespace=cr-at-eol` 才有意义。本轮不统一行尾（会制造大范围无关 diff）；新增文件按 LF 写入，不触碰既有文件的行尾。

## 5. 完成判据对照

| 判据 | 结论 | 依据 |
| --- | --- | --- |
| 任何一个改动都能找到归属 | ✅ | §2.1 与本任务逐项对应；§2.2 为用户既有改动并排除 |
| 没有损失原有修改 | ✅ | 未执行 reset/checkout/stash/clean；`start.command` 暂存状态保留；`report.md` 原文双重保全（§2.3） |
| 没有先验假设"报告中说有的文件就一定已存在" | ✅ | 三个新增文件逐个 `ls` 核对（§2.1） |
| 数据库变更可迁移、可回滚 | ✅ | §3（追加可空列 + 幂等迁移 + 旧代码可读） |

## 6. 遗留事项（移交 A2/A3）

A1 只做盘点与保全，不改行为。进入 A2 时需要复核并在 A3 修复的已掌握线索（详见 `01-code-review.md`）：

1. 共享 in-flight 失败时的派发归属（等待方被记为已派发）；
2. 全缓存循环对取消/截止时间的响应（已排队的取消在返回后才生效）；
3. 补查默认预算等价规则对当前默认值的依赖（需判定是缺陷还是既有兼容语义）；
4. manager 与设施阶段对"部分缓存 + 预算不足"的准入不一致；
5. 补查幂等查找仍晚于"最新圈面/几何"读取；
6. `initialPlan` 被文档写成"提交时冻结"，实际是执行前估算。

## 7. 复现命令

```bash
git branch --show-current
git rev-parse HEAD
git ls-remote https://github.com/PennEwan/Baidu-map.git refs/heads/distribution-boost
git status --short
git diff --stat
git diff --cached --stat
git ls-files --others --exclude-standard
git show HEAD:backend/app/checkups/store.py | grep -n EXTENSION_ADDED_COLUMNS
git -c core.whitespace=cr-at-eol diff --check
```
