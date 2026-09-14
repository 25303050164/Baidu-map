# OSM 离线模式交付报告

日期：2026-09-14

## 1. 交付结论

已完成单城市、受控范围的 OSM 离线链路：

- 运行时后端只读取本地缓存，不下载 PBF、不调用在线路网服务。
- 上海受控范围缓存已生成并通过真实任务 smoke 验收。
- 未覆盖的分析中心会被后端拒绝，不会静默切换到其他数据源。
- `hybrid` 保留百度和 OSM 两套结果，不直接合并几何。

当前缓存覆盖的是上海中心区域，不代表整个上海市行政范围：

```text
BD09LL bbox: [121.461022, 31.103866, 121.660733, 31.404437]
```

## 2. OSM 数据

来源：Geofabrik 上海 extract

```text
https://download.geofabrik.de/asia/china/shanghai-latest.osm.pbf
```

实际快照：

```text
dataVersion: geofabrik-shanghai-260913
dataDate: 2026-09-13
downloadedAt: 2026-09-14
MD5: c6ca96c0327e0a4cfeebfacec9f8381e
```

PBF 已通过 MD5 校验。原始 PBF 位于：

```text
backend/data/osm-source/shanghai-latest.osm.pbf
```

该文件由 Git 忽略，不应提交仓库。

## 3. 生成的缓存

输出目录：

```text
backend/data/osm-cache/
```

生成文件：

- `metadata.json`：城市、覆盖范围、数据版本、日期、坐标系、速度和许可信息。
- `graph.json`：步行路网节点与边。

数据规模：

```text
nodes: 159,868
edges: 179,376
graph.json: 约 41 MB
```

转换规则：

- 输入 PBF 坐标：WGS84。
- 缓存坐标：BD09LL。
- 固定步行速度：1.3 m/s。
- 最大起终点吸附距离：250 m。

JSON 缓存也由 Git 忽略，不应提交仓库。

## 4. 虚拟环境

没有物理合并虚拟环境。采用独立准备环境：

```text
backend/venv-osm-prep/
```

安装：

```powershell
py -3.14 -m venv backend/venv-osm-prep
& backend/venv-osm-prep/Scripts/python.exe -m pip install -r backend/requirements-osm-prep.txt
```

准备脚本：

```text
backend/tools/prepare_osm_cache.py
```

它只处理本地 PBF，并生成后端当前缓存格式。后端运行环境仍保持独立。

## 5. 后端配置

`backend/.env` 已配置：

```dotenv
ANALYSIS_PROVIDER=synthetic
OSM_CACHE_PATH=data/osm-cache
OSM_WALK_SPEED_MPS=1.3
```

本地未配置百度 AK/QPS，因此使用 `synthetic` 作为百度在线模式的本地联调替身；生产真实百度模式仍需单独配置 AK 和 `ANALYSIS_QPS`。

相对路径从 `backend` 目录解析。后端启动时能力状态为：

```text
osm_offline: available=true, availability=ready
```

本地前端 `.env.local` 已开启：

```dotenv
VITE_ENABLE_DEVELOPER_MODE=true
```

重启 Vite 后，开发者模式中的 OSM 和 hybrid 选项会根据上述 capabilities 状态启用。

## 6. 真实离线验收

验收中心：

```text
lng=121.514
lat=31.313
budget=200
analysisMode=osm_offline
```

结果：

```text
任务状态：completed
耗时：约 39.6 秒
dataSource：osm_offline
businessStatus：partial
isochrone.quality：partial
task.requests：0
task.networkRequests：0
algorithm.statistics.requests：200
algorithm.statistics.network_requests：0
```

任务过程中未调用百度或其他在线路网服务。部分结果来自边界未完成和少量无法吸附/不可达节点，符合当前 OSM 数据和算法限制。

## 7. 代码改动摘要

后端：

- 新增 `AnalysisMode`、capabilities API、OSM provenance 和 hybrid 结果契约。
- 新增本地 `OsmCache`、`OsmOfflineProvider` 和 `OsmOfflineEngine`。
- `analysisMode` 纳入任务幂等指纹。
- OSM 模式任务状态的百度请求计数固定为 0。
- 新增 `POST /api/v1/analysis/osm_offline` 离线诊断接口。
- 新增 `backend/tools/prepare_osm_cache.py` 和准备环境依赖。

前端：

- 增加开发者模式开关和三种分析模式选择。
- 增加 capabilities 查询和不可用原因展示。
- 请求、状态机、结果校验和报告均保留 `analysisMode` 与 OSM provenance。
- OSM 不再被误显示为百度数据。
- API 页面新增基于真实任务阶段的全屏加载动画和取消入口，不使用模拟百分比。

## 8. 已知限制与后续工作

- 当前是上海受控 bbox，不是全上海或全国缓存。
- 当前路由图全量加载到 Python 内存；最近节点搜索和 Dijkstra 仍是验证级实现。
- 真实生产规模建议增加空间索引，或迁移到 OSRM、Valhalla、GraphHopper 等专用路由引擎。
- OSM 数据不是道路真值；门禁、临时通行和部分步行限制未建模。
- PBF 下载和缓存准备属于离线运维流程，分析请求阶段必须保持无外部网络。

## 9. 验证记录

已完成：

- 准备脚本 `--help` 和 Python 编译检查通过。
- OSM cache 准备脚本单元测试通过。
- 后端相关测试：`70 passed`。
- 前端 `npm run build` 通过。
- 上海真实 PBF MD5 校验通过。
- 上海真实 OSM 离线任务完成。
