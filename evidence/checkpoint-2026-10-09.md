# 检查点｜2026-10-09（当日最终）

**分支**：`delivery/facility-budget-cache`（本地提交，**未 push**）
**当日起点**：`b136ce7`　**当日终点**：`96b4dae`（4 个提交）

| 提交 | 内容 |
| --- | --- |
| `592c2ab` | 按控制台权益对齐配置（步行 16→3 QPS、地点 8/2→10、日预算→2000、单任务→240/1600）；`.env` 键名修正；测试账本隔离；新增只读核验工具；`evidence/05`、`06` 重写 |
| `fa484da` | `queryAreaCoverage`：各小类未完成区域的**并集**比例与 `met/unmet/unknown` 判决 |
| `46cd014` | 前端展示该比例，并按**真实停止原因**给出下一步（额度要到次日 / 超时可立即重试 / 权限不足重试没用） |
| `96b4dae` | 同一次体检的**重试**：`POST/GET /api/v2/checkups/{taskId}/retries`（+单条查询、取消），发布新修订、独立每轮预算、只花缺页的钱；`trace.retried` 与 `trace.recomputed` 分开 |

---

## 1. 当日已验证（可复现）

| 验证 | 结果 |
| --- | --- |
| 后端全量套件 | **31 failed / 1029 passed / 10 skipped**；失败集合与当日基线**逐项相同**（无新增、无修复）；1029 = 基线 1017 + 新增 12 个测试 |
| `tests/test_query_coverage.py` | 8 passed（重叠只算一次、圈外裁剪、圈面孔洞、恰好 80%、79.9%、单类整块未查、无面积=unknown、失败=unmet） |
| `tests/test_checkup_retry.py` | 4 passed（新修订 / 账本=派发数 / 旧修订按 hash 不变 / 幂等+409 / 已查完具名拒绝 / 改类别 422） |
| `tests/test_checkup_v2.py` | 19 passed（含生成契约逐字节一致） |
| 前端 | `tsc -b` 干净；`npm test` **474 passed**；`npm run build` 成功 |
| 浏览器（拦截 API） | `test:checkup-ui` **15 passed** |
| 真实百度调用 | **0 次** |
| 部署账本与体检存储 | **未被改动**（10-09 无 `daily_spend` 行；两个 SQLite 的 mtime 前后一致） |

## 2. 未做（按优先级）

1. **前端重试入口（按钮）** —— 后端接口已就绪，界面只有文字建议、还不能点。
   决策 1 的"统一提供重试入口"**只完成了一半**。
2. **会话与生命周期（决策 2 + 许可 L2/L4）** —— 完全未开始。现状仍是"关浏览器 / 后续三次
   体检"的期限**没有任何代码在执行**，与刚取得的授权不一致（`05` §3 已如实记录）。
3. **`docs/openapi.json` 落后** —— 未提交其再生成，原因见 `06` §6.1（混着无关的
   Starlette 措辞漂移与纯行尾噪声）。
4. **含明细响应的 `Cache-Control: no-store`** —— 未做。
5. **上海真实实验工具** —— 未开始；且仍被下面第 3 节第 1 条阻塞。

## 3. 阻塞项

1. **AK 归属错位（原因已查明，待运营者处理）** —— `05` §4.1 / §4.3。
   运营者说明：流量图查不到数据是因为查看控制台时**对应的是另一个账号的 AK**。
   由此"端点被误上传"的推测不再需要（本地证据当时也独立否证了它：30 个真实修订、
   776 个成功真实页、217 个全是 32 位十六进制 GUID 的设施）。
   **但更严重的新问题**：据以改配置的权益数字（地点 10 QPS / 步行 3 QPS / 5 万次每天）
   可能不属于应用实际使用的 AK，而 §2.2 的许可答复是对自己账号给的、那 776 次调用却记在
   别人账号上。**换 AK → 核对那个 AK 的权益与勾选 → 若数字不同再调一次配置**；
   在此之前不发起任何真实调用。（代码与离线验证**不需要**重跑。）
2. **许可缺出处**：L1/L2/L4 只有口头结论；L6（是否书面咨询平台）未答复。
3. **三个 `.env` 键静默失效**：`OSM_CACHE_PATH`、`OSM_CACHE_VERSION`、`OSM_WALK_SPEED_MPS`
   （由新工具报出，未修，待确认）。

## 3.1 已更正的一处读错（不再是偏离）

`06` §6.1：我原先把"若超额了、且已完成 ≥80% 就可以停止派发"读成了"一到 80% 就收工"，
并据此写了一节"偏离"。运营者 2026-10-09 澄清后，正确口径是：**停止派发的触发条件是额度
用尽，80% 是那一刻的验收线**。按这个口径，现有实现**已经符合**（额度用尽即停、被拒页面
按名字记账；≥80% 时 `queryAreaCoverage.status = met`、`queryStatus` 仍为 `partial`；
前端在 `met` 时不给重试建议）。已新增回归测试钉住这条口径。
**额度还剩时不提前收工**——那时没有超额，继续查完。

## 5. 一处仓库既有隐患（当日实测到）

`tools/export_contract` 的输出与仓库里那几份文件的**行尾不一致**：它写 LF，而
`api-contract.ts`、`analysis-response.schema.json`、`backend/mocks/*.json` 在仓库里是 CRLF。
所以任何人在 Linux 上跑一次导出，都会得到一堆**纯行尾**差异（本次已全部还原、未提交）。
新生成物 `checkup/contract.ts` 本身是 LF，与仓库一致，不受影响。

## 6. 未纳入提交的用户文件（保持原样）

`Baidu-map/`、`intro-page/`、`prompt.md`、`scheme.md`、`ai-tone-issues.json`、
`*:Zone.Identifier`、`backend/app/osm_package.py`、`backend/docs/OSM_REGION_SETUP.md`、
`backend/scripts/{configure_osm_region,pack_osm_bundle,setup_osm_region}.py`、
`backend/tests/test_osm_package.py`、`data/osm/*`。

`backend/.env` 的键名已修正，但该文件被 `.gitignore` 忽略，不在提交内。
