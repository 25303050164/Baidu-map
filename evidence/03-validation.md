# A4｜完整验证与失败归因

**任务**：`scheme.md` A4
**执行时间**：2026-10-08
**环境**：同一台机器、同一虚拟环境（`backend/.venv`，Python 3.12.3）、同一依赖锁定；`conftest.py` 全会话拒绝外连。

---

## 1. 实际执行的命令与结果

| 命令 | 结果 | 备注 |
| --- | --- | --- |
| `cd backend && .venv/bin/python -m pytest -q` | **34 failed / 1011 passed / 10 skipped**（668s） | 见 §2 逐项归因；基线同环境为 **42 failed / 968 passed / 10 skipped** |
| `cd backend && .venv/bin/python -m pytest -q tests/test_checkup_facilities.py tests/test_checkup_v2.py tests/test_cache.py tests/test_poi_online.py tests/test_quota.py` | **126 passed** | 本轮改动涉及的全部套件 |
| `cd backend && .venv/bin/python -m pytest -q tests/test_checkup_v2.py tests/test_cache.py tests/test_poi_online.py` | **71 passed** | 修复后的定向复跑 |
| `cd life-circle-demo && npm test` | **41 files / 458 tests passed** | 前端未改行为，只改诊断文案与其断言 |
| `cd life-circle-demo && npm run build`（`tsc -b && vite build`） | 通过 | 类型检查与构建均过 |
| `cd backend && .venv/bin/python -m tools.export_contract` | 运行过；只保留 v2 契约产物 | 旧生成物在基准版本即已漂移，整批重写会把无关变更混入，已回退（见 §3 第③类） |
| Playwright E2E（`test:e2e` / `test:checkup-ui` / `test:integration`） | **NOT RUN（环境阻塞）** | 见 §4 |
| 外连尝试统计 | **0 次** | 两次全量运行均无 `/tmp/baidu-verify/*.txt` 中的 `external connection attempts refused` 行 |

退出码处理：所有命令都保留真实退出码（不再用管道尾部命令掩盖失败）；`git diff --check` 需用 `core.whitespace=cr-at-eol`（仓库工作区为 CRLF），结果干净。

---

## 2. 失败归因（同环境、同依赖，逐项）

对照文件：基线 `/tmp/baidu-verify/baseline-full.txt`（改动前）与本轮 `/tmp/baidu-verify/A-after-fixes.txt`。逐条比对（名称规范化后）：

```text
baseline=42  now=34
新增失败（本轮引入或新暴露）：无
本轮修好：8 项
```

**本次引入：0 项。** 8 项修好的是基线里本就失败、且与本轮改动直接相关或时间相关的用例：

| 修好的用例 | 原因 |
| --- | --- |
| `test_checkup_facilities.py::test_both_engines_close_the_loop_over_their_own_boundary` 等 7 项 | 基线里仍在用"三类 6 小类 × 4 块 = 24 页"的旧数字，与 3→10 类别重构脱节；本轮按词典推导期望值（断言未删未放宽） |
| `test_facility_stage.py::test_the_daily_allowance_is_checked_before_the_request` | 夹具未固定档位切换时间：切换日之后生效的是 fallback 档（80），于是用例真的发了请求。修的是夹具（固定切换时间），断言原样 |

**基线已存在、与本轮无关：34 项**（下面按根因分组，附本轮捕获的断言行）：

### ① 3→10 类别词典重构遗留（23 项）

旧三元组 `(market, pharmacy, school)`／旧展示组名／旧类别数仍写死在用例里：

| 文件 | 断言行（本轮捕获） |
| --- | --- |
| `test_catalog.py` | `assert ('pharmacy', ...) == ('market', 'pharmacy', 'school')`；`assert [{'hospital_...'}] == [{'hospital_...', 'pharmacy'}]`；`assert '培训' in ('出入口', '门口', ...)`；`assert 'hospital' == 'clinic'` |
| `test_facilities.py` | `assert 'fresh_store' == 'supermarket'`；`assert 'preschool' == None`；`assert 'hospital' == None`；`assert 32 == 6`（×7，每类期望数） |
| `test_place_safety.py` | `assert 'school' is None` |
| `test_poi_acceptance.py` | `assert 'accepted' != 'accepted'`（×3）；`assert 'excluded' == 'accepted'` |
| `test_poi_service.py` | `assert 'accepted' == 'needs_review'` |
| `test_analysis.py` | `assert 'care' in ('shopping', 'medical', 'education')` |
| `test_business_api.py` | `assert (13 == 5)`（×3） |
| `test_checkup_report.py` | `assert 24 == 6` |
| `test_checkup_progress.py` | `assert '评估服务覆盖 · 医疗健康（第 1/2 类）' == '评估服务覆盖 · 医疗（第 1/2 类）'`（×2，展示组改名） |

判定：这些用例描述的是**类别词典与展示口径**，不经过本轮的预算/缓存/补查路径；它们在基线提交上以同样方式失败。分类：`baseline-confirmed`（保留原样，不在本轮范围内改写——改写它们等于宣布类别口径变更，需要产品决定）。

### ② 旧 POI 运行时与离线 CLI（7 项）

| 文件 | 断言行 | 说明 |
| --- | --- | --- |
| `test_poi_service.py` | `ValidationError: RuntimeConfig`；`assert 'partial' == 'completed'`（×2）；`assert (1520 == 96)` | 旧自适应网格运行时的计划规模与状态期望 |
| `test_poi_tool.py` | `assert 1 == 0`（×2） | 离线 CLI 的"不读凭据/不发 HTTP"守卫在基线即失败 |

判定：`baseline-confirmed`。**其中 `test_poi_tool.py` 的两项需要在 PR 描述里显式列出**：它检测到 CLI 路径读了凭据/发了请求，属于既有风险，本轮未触碰该路径，也不应用"与本轮无关"掩盖。

### ③ 生成物漂移（1 项）

`test_poi_evidence.py::test_typescript_matches_current_serialization_contract`：`api-contract.ts` 与当前序列化契约不一致。该生成物在基准提交上即已过期；重跑导出会一次性改写约 12k 行无关内容，因此本轮只保留 v2 契约（`life-circle-demo/src/checkup/contract.ts`）的变更。判定：`baseline-confirmed`。

### ④ 其余既有偏差（3 项）

`test_analysis.py::test_four_mocks_consistent`、`test_business_api.py` 的 3 项 HTTP 链路断言、`test_checkup_report.py::...` 报告字段数：均为基线既有、与本轮无关。判定：`baseline-confirmed`。

### 是否阻塞 Gate A

按 scheme.md §4 A4/§12：核心正确性（预算/缓存/幂等/取消/关停）**全部通过**，无未解释的新增回归，旧调用方语义保持。34 项失败全部为 `baseline-confirmed`（类别词典、旧运行时、生成物），**不阻塞 Gate A 的本地交付**；其中 `test_poi_tool.py` 的凭据/HTTP 守卫失败标记为 **`blocking`（生产审批）**，因为它涉及"离线 CLI 是否会读凭据"的安全承诺，需要单独处理后再谈部署。

---

## 3. 断言完整性声明

- 本轮**没有删除、跳过或弱化**任何断言；没有新增 `skip`/`xfail`。
- 唯一被改写期望的用例是 `test_a_partly_cached_extension_spends_its_allowance_on_the_missing_pages`：原期望"部分缓存 + 预算不足 → 422"被准入规则修复推翻（scheme.md 不变量 5：部分缓存允许取得可复用证据）。改写后的用例断言**更强的证据**：受理、只按缺页派发、账本精确增量、随后只差 1 页即完成。
- 行尾处理：仓库工作区混用 CRLF/LF。早期编辑曾把两个文件改成 LF，造成约 1300 行纯行尾差异；已按"内容未变的行恢复原字节"修复（`test_checkup_facilities.py` +1384/−686 → **+815/−45**，`backend/README.md` +71/−46 → **+25/−0**）。校验：`git diff --numstat` 与 `git diff --ignore-space-at-eol --numstat` 一致。

---

## 4. 浏览器 E2E：NOT RUN（环境阻塞，非"通过"）

按 scheme.md A4 要求"尝试在可用 Chromium/Playwright 环境运行 E2E"，本轮实际尝试并留证：

```text
$ node -e '...chromium.executablePath()...'
{"node":"v24.15.0","chromiumPath":"~/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome","chromiumInstalled":true}

$ chromium-1243/chrome-linux64/chrome --version
chrome: error while loading shared libraries: libasound.so.2: cannot open shared object file

$ chromium_headless_shell-1243/.../chrome-headless-shell --version
chrome-headless-shell: error while loading shared libraries: libasound.so.2: cannot open shared object file

$ ldd chromium-1243/chrome-linux64/chrome | grep -c "not found"
1
```

- 仓库的四个 Playwright 配置都指定 `channel: 'msedge'`；本机没有 Edge，也没有任何系统 Chromium（`which chromium chromium-browser microsoft-edge google-chrome` 全部为空）。
- 缺失的是一个系统级共享库（`libasound.so.2`），系统里找不到该文件；安装它需要 root 与软件源访问，属于环境变更，未经批准不做。
- 因此 **E2E 记为 NOT RUN**，并按 scheme.md 要求列为**生产部署的待解阻塞项**：在具备 Edge 或补齐 Chromium 运行库的环境中必须先跑通 `test:checkup-ui` 与 `test:integration` 才谈上线。

已用带外方式部分覆盖 UI 行为：前端 458 项单测通过（含"旧后端缺字段"的向后兼容用例），但这**不能替代**浏览器端到端验证。

---

## 5. 未验证范围（明确列出）

| 项目 | 状态 | 原因 |
| --- | --- | --- |
| 真实百度 Place API 行为与召回 | NOT RUN | 未获授权（scheme.md D 阶段） |
| 真实账号配额/计费 | NOT RUN | 运营者核实（B 阶段） |
| 浏览器 E2E | NOT RUN | §4 环境阻塞 |
| 多进程/多实例部署、持久化续查 | NOT RUN | 本轮范围之外（F 阶段） |
| 长时间稳定性、真实地理数据质量 | NOT RUN | 同上 |

---

## 6. 结论

- 核心套件 126 项全通过；全量 34 项失败**全部**可归因到基线既有原因，且都在同一环境做过前后对照；新增失败 0。
- 8 项基线失败被修好（7 项旧类别数字 + 1 项时间相关夹具）。
- E2E 未运行，已记录可复现的阻塞证据，并作为生产审批阻塞项。
