# OSM 本地缓存准备

## 目标

当前 OSM 模式只支持单城市或受控覆盖范围。后端运行时不下载 PBF、不连接在线路网服务，只读取 `OSM_CACHE_PATH` 指向的本地 `metadata.json` 和 `graph.json`。

PBF 下载和图构建属于离线准备阶段，不应与 FastAPI 运行时混在同一个生产依赖环境中。

## 推荐环境

虚拟环境不能安全物理合并。推荐保留：

- `backend/venv`：FastAPI、任务 API 和分析运行时。
- `backend/venv-osm-prep`：Pyrosm 和 PBF 处理依赖。

两个环境共享仓库文件，但不共享 Python site-packages。当前 Pyrosm 0.13.x 支持 CPython 3.10-3.14；如果只在本机准备数据，也可以把 `requirements-osm-prep.txt` 安装到现有 `backend/venv`，但这会增大运行环境，不推荐部署时这样做。

如果选择本机单环境模式，不需要“合并”两个 venv，直接向现有环境追加可选依赖即可：

```powershell
& backend/venv/Scripts/python.exe -m pip install -r backend/requirements-osm-prep.txt
```

安装完成后，使用同一个解释器运行下面的准备脚本；FastAPI 运行时仍然不会导入 Pyrosm。

## 准备流程

1. 从 Geofabrik、BBBike 或组织认可的 OSM extract 来源下载目标城市 `.osm.pbf`。
2. 将 PBF 放在 `backend/data/osm-source/`，该目录中的 PBF 不提交 Git。
3. 创建准备环境并安装依赖：

```powershell
py -3.14 -m venv backend/venv-osm-prep
& backend/venv-osm-prep/Scripts/python.exe -m pip install -r backend/requirements-osm-prep.txt
```

4. 运行准备脚本：

```powershell
& backend/venv-osm-prep/Scripts/python.exe backend/tools/prepare_osm_cache.py `
  --pbf backend/data/osm-source/shanghai-latest.osm.pbf `
  --output backend/data/osm-cache `
  --city 上海市 `
  --data-version geofabrik-shanghai-20260914 `
  --data-date 2026-09-14 `
  --coverage-bbox 121.45 31.10 121.65 31.40 `
  --overwrite
```

`--coverage-bbox` 使用 PBF 的原始 WGS84 经度、纬度顺序。它应覆盖实际分析点和算法最大扩展范围，不要只圈住一个起点。

## 输出契约

脚本生成的 `metadata.json` 至少包括：

```json
{
  "coordinateSystem": "bd09ll",
  "coverageCity": "上海市",
  "coverage": [121.45, 31.10, 121.65, 31.40],
  "dataVersion": "geofabrik-shanghai-20260914",
  "dataDate": "2026-09-14",
  "preparedAt": "2026-09-14T00:00:00+00:00",
  "attribution": "© OpenStreetMap contributors",
  "license": "ODbL"
}
```

`graph.json` 包含真实 OSM walking network 的节点和边。节点坐标会被转换为 BD09LL；边包含米制长度、固定 1.3 m/s 速度计算出的耗时和双向步行标记。

空的 `metadata.json`、`graph.json` 或只有 `{}` 的文件不会使能力变为 ready。准备完成后重启后端，检查：

```text
GET /api/analyses/capabilities
```

只有 `osm_offline.available=true` 且 `availability=ready` 时，前端开发者模式才会启用 OSM 与 hybrid。

## 约束

- OSM 原始数据不是道路真值，门禁、临时通行和部分步行限制可能缺失。
- 不要将 PBF、`.poly`、生成的 JSON cache 或准备环境提交 Git。
- 当前后端不支持运行时自动下载；未在 coverage 内的坐标会被拒绝。
- `generatedAt` 是分析时间，不能替代 OSM 的 `dataDate`。
