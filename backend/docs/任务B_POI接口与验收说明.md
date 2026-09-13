# 任务 B1：百度 POI 数据层接口与验收说明

更新：2026-09-14。B1 工程回归通过，并已按用户批准的 QPS=2 完成真实小样本及一次固定范围采集。采集接受菜市场 11、药店 57、小学 20 个唯一 UID，9 项待核，93/96 个序列完成；3 个序列分页总数不一致，结果为 `partial`，目录完整性仍为 `unverified`。详细测试、真实调用及限制以[任务 B 测试与验收报告](reviews/2026-09-14/任务B_测试与验收报告.md)为准。

## 可复用接口

```python
from app.poi.service import collect_pois

result = await collect_pois(request, provider, runtime)
```

`PoiCollectRequest` 只允许 BD09LL、固定的分析窗口参数和 `market`、`pharmacy`、`primary_school` 三类。`RuntimeConfig` 保存本次运行的独立预算、类别额度、截止时间、QPS 和在线授权信息；密钥不属于请求或结果。

结果的 `queryStatus` 表示计划执行状态，`catalogCompleteness` 永远为 `unverified`。`pois` 只放合法 UID、名称、坐标且确定性分类为 accepted 的条目；`reviewCandidates` 保存冲突、类别政策存疑和证据不足项；`quarantine` 保存缺少必要字段、非法坐标和窗口外项。每条记录都保留检索格、关键词和页码来源。

## 离线命令

从 `backend` 目录运行：

```powershell
& .\.venv\Scripts\python.exe -m tools.poi_collect --mode plan `
  --config tools/poi-example.json --output .tmp/poi-plan

& .\.venv\Scripts\python.exe -m tools.poi_collect --mode replay `
  --config tools/poi-example.json `
  --fixtures tests/fixtures/poi/collection.json `
  --output .tmp/poi-replay-<run-id>
```

`plan` 只生成固定范围、4×4 检索格、关键词、页数和配置哈希，网络请求为 0。`replay` 只读取带 `source: synthetic` 的人工夹具，生成标准 JSON、CSV、覆盖、指标、脱敏检查和验收说明。输出先写入同级临时目录，全部写入且安全检查通过后再发布，避免把半份产物交给下游。

当前合成夹具包含跨检索词重复 UID、分页记录、缺失 UID、生鲜超市、北门和未知类别证据，用于验证去重、分类待核、排除和隔离逻辑。夹具不含真实百度 UID 或社区数据。

## 请求与分页规则

每个 1300 米检索格使用 BD09LL 圆形检索，半径为 925 米；16 个格与六个固定关键词（菜市场、农贸市场、菜场、药店、药房、小学）生成 96 个序列。每个序列最多 8 页、每页最多 20 条。首页全部完成后按固定顺序轮转后续页；重复页、变化总数、提前空页和百度保护上限会写入查询覆盖并降低状态。

同一供应方 UID 只保留一个实体，合并所有来源；位置、名称、地址、父实体或分类冲突进入待核，不随机覆盖。不同 UID 不自动合并，只按规范化名称、地址和 20 米候选规则标记疑似重复。

当前规则为 `poi-categories-v1.2`：分类标签按层级分隔后匹配，不把“小学用品”当小学；“教育培训”作为百度上级标签不触发培训机构排除，下级培训标签及名称仍按排除规则处理。编号校门进入附属点排除。未请求的已识别类别移入排除记录。

每个 `queryCoverage` 记录 `requestedPages`、`successfulPages`、`reportedTotals`、`pageErrors`，与 `sequenceId`、`tileId`、`category`、`query` 联合定位异常。来源同时保留类别和序列。请求坐标编码为纬度、经度，各六位小数；计划保留投影计算精度，返回合法坐标保留原值，未进行坐标系转换。`metrics.json.coverageByCategory` 给出各类别计划、完成、受限和失败数量。

## 在线入口的安全闸门

`live-smoke` 和 `live-collect` 要求显式授权、配置哈希匹配、正数 POI QPS、有效时区运行窗口、独立持久化账本和未使用的运行 ID。账本在每次网络发送前记录保守预算占用；超时或无法确认的发送仍占额度。配额、权限、限流、账本失败、取消和截止会停止后续发送。真实入口不会从现有等时圈实验账本借用额度。

本轮在线入口已验证：小样本 3 次、正式采集 99 次，均为真实百度响应。示例配置仍保持未授权；每次新的在线运行仍需独立运行 ID、已批准配置哈希及有效运行窗口。

## 验收结果

当前离线 B1 测试覆盖：

- 计划边界、4×4 格角覆盖、BD09LL 米制范围裁剪；
- 短页继续分页、重复页、总数变化、空页、保护性上限；
- 合法和非法坐标、业务错误、非 POI 响应、取消、预算最后一次和重复运行；
- 分类接受／待核／排除、同 UID 冲突、跨 UID 疑似重复和附属入口；
- 结果固定字段、CSV 公式转义、凭据扫描、原子产物和不可覆盖的输出目录。

本次真实百度调用总数为 102。B1 工程完成、真实数据受限；独立人工质量核验和事前固定的已知设施命中率未完成。真实数据及权威账本仅保存在本地，不随 Git 推送。当前仓库已经有有限设施业务实现，本次没有改动它的状态语义，也没有用 B1 数量替代步行核验、盲区或完整体检结论。
