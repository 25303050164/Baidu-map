# 任务 B：测试与验收报告

日期：2026-09-14（北京时间）。交付分支：`feature/poi-data-v1`。

## 验收结论

**B1 工程完成，真实采集已执行，数据受限交付。** 固定范围内接受 88 个唯一 UID：菜市场 11、药店 57、小学 20；另有 9 项待核。96 个查询序列中 93 个完成、3 个分页不确定，`queryStatus=partial`，`catalogCompleteness=unverified`。这些数量不表示 15 分钟可达、营业中、可步行进入或完整社区目录。

自动化回归通过；人工独立身份/位置核验、分类正确率和事前固定已知设施清单的命中率尚未完成，不能据此宣布数据充分支持盲区判断。本次没有追加盲区、覆盖评分、数据库或部署工作。

## B0：现状与变更范围

- 开工目录的实际 Git 仓库为 `Baidu-map`；分支已有 B1 实现，初始提交 `aabcfe2`，工作区干净。没有覆盖或提交他人的未提交文件。
- 先拉取两个远程，`origin/main`、`upstream/main` 均更新到 `c3a0f10`；合并到当前分支的提交为 `1db4665`。保留此前本地同步提交与任务 A、前端成果。
- N02 使用 `/place/v3/around`（3.0），配置复用 `app.config.load_settings`，HTTPX 适配复用 `app.baidu.API_URL` 与日志静默策略；参考[百度官方地点检索 3.0 文档](https://lbsyun.baidu.com/docs/webapi?title=placev3/guide/webservice-placeapiV3/interfaceDocumentV3)。仅核对相关请求、坐标与返回字段，没有迁移接口版本。
- 复用 `app.place_protocol` 分页/错误码、`app.request_control` 请求名额、`app.analyses.RateGate`、`app.persistence` 原子账本与锁、算法包的 `LocalProjection`、`CancelToken`。POI 使用独立账本，不挪用任务 A 预算。
- 可用基线：后端 263 项通过；前端 152 项通过并构建成功。首次后端命令因测试临时父目录未创建产生 57 个 setup 错误，创建目录后基线全部通过；没有把环境错误计为产品缺陷或隐瞒失败重跑。
- B0 真实调用为 0。本轮修改限定在 `backend/app/poi/`、POI 测试及文档；公共预算、限流、分页模块、`life-circle-algorithm`、正式分析 API 和前端业务代码均无任务 B 新修改。

## 实现与修复

1. 分类标签按层级完整项匹配，避免“小学用品”“药店设备”等被子串误收；明确非目标标签冲突进入待核。
2. 同 UID 的名称、地址、父实体冲突也进入待核，保留全部观测和来源；单类别请求不会把另一类命中放入生产列表。
3. 请求经纬编码统一六位小数，原始投影精度和合法返回坐标保留。嵌套字段也经过白名单，避免无关对象/字段跨越 Provider 边界。
4. 每个序列追加请求页、成功页、报告总数历史及失败页原因；产物增加按类别的完成/受限统计。
5. 导出采用同一文件系统临时目录写入，全部文件成功后发布；磁盘故障时不暴露半份结果，也不覆盖已有目录。
6. 小样本暴露百度小学标签为“教育培训;小学”：旧规则误将上级分类中的“培训”视为培训机构。先用合成同结构数据复现，再修正为上级标签例外，真实采集使用 `poi-categories-v1.2`；编号校门同时明确排除。未为诊断追加网络请求。

新增 29 个测试：`test_poi_acceptance.py` 15、`test_poi_provider.py` 10、`test_poi_tool.py` 4。最初 11 个缺陷用例失败后修复通过；小样本后另有 2 个实际字段相关失败用例修复通过。

## 测验与回归结果

| 范围 | 实测结果 | 证据 |
| --- | --- | --- |
| 后端完整测试 | 292 通过，0 失败、0 跳过 | `backend-release.xml`，55.455 秒 |
| 独立等时圈算法 | 96 通过 | `algorithm-final.xml` |
| 前端单元测试 | 152 通过，16 个文件 | Vitest 输出 |
| 前端生产构建 | 通过 | TypeScript + Vite 输出 |
| 浏览器分析与路线流程 | 12 通过 | `npm.cmd run test:analysis-ui`；运行器正常退出（0） |
| 离线 plan / replay | 均通过，真实发送 0 | 本地 `plan/`、`replay/` |
| 真实 Provider 契约、预算和取消 | 模拟传输测试通过；真实小样本 3/3 成功 | 合成密钥，独立 MockTransport；小样本账本 |

合计 552 项自动化用例通过。后端有 2 条既有 Starlette/AnyIO 兼容性弃用提示，未影响结果。浏览器用例全部通过后，Windows 测试服务器收尾挂起；在同一操作中核实进程、父进程、仓库路径和 5179 端口后清理本次测试服务器，运行器返回 `12 passed`、退出码 0（6.7 分钟包含收尾等待）。测试中配置 `live=True` 的用例使用 MockTransport，不计为百度真实发送。

实际复测命令如下，从相应模块目录运行；先在仓库根目录创建 `.tmp/poi-b-20260914`，`--basetemp` 每次使用新的目录名。

```powershell
# backend
.\.venv\Scripts\python.exe -m pytest -q --tb=short -o cache_dir=../.tmp/poi-b-20260914/cache --basetemp=../.tmp/poi-b-20260914/backend-release --junitxml=../.tmp/poi-b-20260914/backend-release.xml
# life-circle-algorithm
..\backend\.venv\Scripts\python.exe -m pytest -q --tb=short -o cache_dir=../.tmp/poi-b-20260914/algorithm-cache --basetemp=../.tmp/poi-b-20260914/algorithm-final --junitxml=../.tmp/poi-b-20260914/algorithm-final.xml
# life-circle-demo
npm.cmd test -- --reporter=default
npm.cmd run build
npm.cmd run test:analysis-ui
```

### 任务书测试映射

| 编号 | 验证位置与结果 |
| --- | --- |
| T01–T02 | `test_poi_provider` 参数/版本；`test_poi_service` 非法请求；`test_poi_acceptance` 六位坐标 |
| T03–T05 | `test_place_safety` 短页、重复/重叠 UID、变化总数、提前空页、150 上限；查询覆盖保存原因 |
| T06–T08 | `test_place_safety` 业务错误/非 POI；`test_poi_service` 缺 UID、非法/十进制坐标；Provider 嵌套白名单 |
| T09–T13 | `test_poi_service` 跨格 UID、不同校区、疑似重复、分类冲突、格角及边界；新增名称/地址/父实体冲突回归 |
| T14–T15 | `test_poi_service` 类别预算耗尽保留其他类、空成功与失败语义；新增失败页定位 |
| T16–T18 | `test_poi_provider` 最后一次额度/超时重试、迟到响应/取消/截止、持久化失败阻止发送与重启；`test_poi_runtime` 一次性账本 |
| T19–T20 | `test_poi_provider` 缓存及页码变化；`test_poi_runtime` 授权/QPS/哈希/窗口闸门；`test_poi_tool` 两类在线入口未授权时不读密钥 |
| T21–T23 | 凭据和嵌套字段白名单、CSV 公式转义、导出失败、确定性回放；CLI plan/replay 禁止 HTTP 和密钥读取 |
| T24 | 后端分析合同、算法、前端单元及构建全部通过 |

## 真实执行记录

用户本次明确授权“qps=2，不限额度”。执行采用一次小样本最多 12 次、一次固定采集最多 300 次且每类最多 100 次的保护上限；没有因用户不限额而扩大空间范围或反复抓取。运行前本机未发现 Python 真实实验/服务进程；该检查不能证明其他机器未使用同一账号。

中心固定为 BD09LL `(121.513926, 31.313077)`，分析半边长 1600 米、额外边距 1000 米，16 格、每格 925 米圆检索、6 个关键词、96 序列，全部首页后轮转后续页。未使用矩形高级权限或逐点详情接口。

| 指标 | 小样本 | 固定采集 |
| --- | ---: | ---: |
| 时间（北京时间） | 01:18:00–01:18:02 | 01:20:03–01:21:09 |
| 真实预留/确认发送/成功响应 | 3 / 3 / 3 | 99 / 99 / 99 |
| 无法确认的保守占用 | 0 | 0 |
| 重试 | 0 | 0 |
| 滚动一秒最大预留 | 2 | 2 |
| 上次响应后最小间隔（约） | 515.694ms | 511.149ms |
| 端到端耗时 | 1.676s | 66.617s |
| 响应中位数 / P95 | 156 / 218ms | 125 / 234ms |
| 限流等待 | 1.016s | 49.797s |
| 接口成功率 | 100% | 100% |
| 查询状态 | completed | partial |

累计真实请求 **102 次**。间隔来自账本预留时间与毫秒四舍五入后的响应耗时，是本次本进程的观测值，不代表跨机器共享 QPS 保证。小样本用于接口契约，使用修正前规则的结果保留审计，不和正式结果相加。

### 正式数据及查询质量

| 类别 | 已接受唯一 UID | 完成/计划序列 | 受限序列 | 类别实际请求 |
| --- | ---: | ---: | ---: | ---: |
| 菜市场 | 11 | 48/48 | 0 | 48 |
| 药店 | 57 | 30/32 | 2 | 34 |
| 小学 | 20 | 15/16 | 1 | 17 |

收到 294 条候选，UID 合并 168 次；126 个唯一 UID 中，88 接受、9 待核、4 附属点排除、25 个全部观测位于检索窗口外而裁剪。非法必要字段 0；接受列表 UID 重复残留 0；跨 UID 疑似重复组 0，这不等于已消除现实实体重复。

受限序列均有两页成功响应，没有网络故障：

- `r1c0:pharmacy:药店`：总数报告 9，实际返回 8，下一页为空。
- `r1c0:pharmacy:药房`：总数报告 9，实际返回 8，下一页为空。
- `r2c1:primary_school:小学`：总数报告 3，实际返回 2，下一页为空。

这些序列保留 `pagination_uncertain`，没有为补齐总数追加抓取，不能推断缺失点的位置或直接判盲区。9 个待核项包含医院内部药房、市场内商铺、菜店口径不明确、办公室和不相关餐饮候选，均未进入接受数量。

### 人工质量验收的明确限制

抽样规则在代码中预先固定为各类别按 UID 哈希排序最多 15 个：正式结果待查菜市场 11、药店 15、小学 15，共 41 个，另复核 9 个待核项。`review.csv` 已生成，但独立人工核对未完成，不计算分类正确率。完整采集前未获得人工确认的已知设施清单，因此已知设施命中率记为未评估，不能事后选择命中对象补造分母。未进行现场入口、营业状态或地图位置独立核验。

## 产物、交接与安全

真实产物留在本地仓库的 Git 忽略目录 `.tmp/poi-b-20260914/live-collect/`，小样本在同级 `live-smoke/`。每套含：`config-sanitized.json`、`query-plan.json`、`poi-result.json`、`poi-list.csv`、`review.csv`、`query-coverage.json`、`metrics.json`、`ledger-final.json`、`security-check.json`、`验收报告.md`。

权威账本分别在 `backend/.poi-ledgers/poi-b-20260914-smoke-01/` 与 `backend/.poi-ledgers/poi-b-20260914-collect-01/`。禁止用导出的快照重置或续用旧额度，同一运行 ID 再次启动会被拒绝。真实输出从本地受保护配置读取 AK，导出精确凭据扫描通过，没有保存原始 URL、原始异常、电话或无关详情。

Git 交付仅包含实现、合成测试、本报告及[脱敏验收证据](任务B_脱敏验收证据.json)；证据记录真实产物 SHA256、统计和受限查询，不包含设施明细、真实 UID 或 AK。JSON 可通过 `PoiCollectionResult.model_validate_json()` 直接导入，后续模块复用 `pois`、`reviewCandidates`、`queryCoverage` 和范围信息即可，不必重新请求百度。

正式采集配置哈希：`101d3a502845e2cf3fbaac21dc9c23ec3e383147af7e8b9898679960d3c252f6`。规则：`poi-categories-v1.2`；接口：3.0；计划：`guodingyi-poi-v1`。

下一步是人工完成抽查与争议类别政策，然后按 B2/B3 的明确口径使用该数据。B1 的受限结果不会自动修改现有 `facilitiesStatus`，也不会自动生成设施可达、盲区面积或覆盖评分。
