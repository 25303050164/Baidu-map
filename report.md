# 设施分类规则约束报告

## 1. 规则版本

- 当前版本：`poi-categories-v2.1`
- 唯一分类来源：`backend/app/categories.json`
- 后台保留 31 个细分类，用于检索、归类、审计和接口兼容。
- 用户界面统一展示 5 个展示组，展示组变化必须同步目录接口、前端契约和文档。
- 分类规则变化必须升级版本号，不得覆盖历史版本的报告。

## 2. 五个展示组

| 展示组 | 展示键 | 后台大类 |
| --- | --- | --- |
| 健康照护 | `healthcare` | `medical`、`care` |
| 教育成长 | `education` | `education` |
| 生活消费 | `daily_life` | `shopping`、`dining`、`finance`、`life` |
| 公共出行 | `public_mobility` | `public`、`transport` |
| 文体休闲 | `leisure` | `leisure` |

展示组只负责用户筛选、图例、热力层和汇总展示，不替代后台细分类，也不改变检索关键词。后台十个大类仍作为接口和历史客户端的兼容字段保留。

## 3. 交叉归属

每条设施记录只有一个主类。`category`、`minor_category` 和 `major_category` 必须描述同一个主类；主类用于去重、圈内计数、服务评估和统计。

只有设施确实同时提供另一类服务时，才声明次类或能力：

- `hospital_pharmacy` 的 `secondaryCategories` 包含 `pharmacy`，能力为 `pharmacy`。
- `combined_school` 的 `secondaryCategories` 包含 `school`、`middle_school`，能力为 `primary_education`、`middle_education`。

生鲜店、康复护理、养老等相近但服务边界不同的设施继续保留独立细分类，不因为展示组相同而重复归属。未声明的多重命中必须进入审核或排除状态，不能仅凭优先级自动生成多个主类。

## 4. 接口约束

分类目录可通过以下接口直接读取：

```text
GET /api/facility-catalog
```

响应包含 `version`、`displayGroups` 和 `categories`。分析结果的 `data.facilityDisplayGroups` 返回五个展示组元数据；设施记录可以返回 `displayGroup`、`secondaryCategories` 和 `capabilities`。这些字段对旧客户端保持可选兼容，新生成的设施记录会按目录自动补齐。

## 5. 统计约束

- 展示组统计只按设施主类映射计算，次类不能再次加入总数。
- 地图可以按展示组着色，但聚合只改变绘制方式，不删除或合并业务记录。
- `in_circle` 为 `null` 的设施不能计入圈内数量或热力密度。
- 检索词、名称词和标签词必须来自分类字典，业务模块不得维护私有分类表。

## 6. 变更检查清单

1. 更新 `backend/app/categories.json` 的版本、展示组和交叉声明。
2. 检查 `classify`、POI 标准化和 UID 去重是否仍只产生一个主类。
3. 重新生成 `backend/docs`、`backend/mocks`、`life-circle-demo/src/api-contract.ts` 和 `life-circle-demo/src/checkup/contract.ts`。
4. 检查 `/api/facility-catalog`、主类冲突、次类字段和重复统计。
5. 历史报告保留原规则版本，不进行批量改写。

## 7. 路网下载与构建改动

本轮同步更新 `intro-page/Baidu-map-site/help/操作说明.md` 与 `help/index.html`：移除不存在的 `python dev.py osm` 自动下载说明，改为统一的地区运行包下载、校验、选择和自定义 PBF 构建流程。

## 当前下载与构建流程

新增 `backend/scripts/setup_osm_region.py` 作为单一入口：

```bash
python backend/scripts/setup_osm_region.py list
python backend/scripts/setup_osm_region.py download --url <runtime-package-url> --region <region-id> --sha256 <archive-sha256>
python backend/scripts/setup_osm_region.py select --region <region-id>
python backend/scripts/setup_osm_region.py clear
```

下载命令要求运行包 ZIP 包含 `data/osm/manifest.json`，会安全解压、校验 manifest 中声明的文件哈希，然后写入 `backend/.env` 并激活地区。已经解压的包可以直接用 `select` 激活。

自定义地区或重建数据时，构图依赖只需安装一次，之后由一个命令生成图缓存、风险层、障碍层和 manifest，并完成激活：

```bash
python -m pip install -r backend/requirements-osm-build.txt
python backend/scripts/setup_osm_region.py build --region <region-id> --pbf <path-to-pbf> --coverage <path-to-poly> --version <snapshot-id> --metric-crs <metric-crs> --source <pbf-url> --downloaded-at <yyyy-mm-dd>
```

生成包保存在 `data/osm/regions/<region-id>/`。`--no-simplify` 用于排查构图问题，替换已有地区时使用 `--force`。`dev.py` 仍只负责依赖、环境和服务启动。

## 路网地区配置

- 后端不再默认指向上海 PBF、图缓存、风险层、障碍层或水系复核目录；未选择 OSM 数据包时，路网状态为 `unconfigured`，百度模式仍可独立运行。
- 默认米制投影改为通用的 `EPSG:3857`。具体 OSM 数据包必须在 manifest 或其 `graph_metadata.json` 中声明自己的 CRS 和数据版本。
- 构图脚本在未配置 PBF 或图缓存目标时返回明确错误，不再依赖隐含的上海输出路径。
- `backend/app/osm_package.py` 负责发现、解析和写入地区数据包配置。
- `backend/scripts/configure_osm_region.py` 保留为兼容入口；新流程统一使用 `setup_osm_region.py`。
- `backend/docs/OSM_REGION_SETUP.md` 记录下载、构建、激活和清除配置的操作流程。
- 数据包至少需要图缓存和覆盖边界；风险层、障碍层和复核文件按数据版本绑定，不能复用上海文件到其他地区。

## 验证

- `backend/tests/test_osm_package.py` 覆盖地区 manifest 选择、清除配置、现有运行时包发现，以及临时 ZIP 下载和哈希校验。
- `python -m py_compile backend/scripts/setup_osm_region.py` 已通过。
- `python -m pytest backend/tests/test_osm_package.py -q` 已通过（4 passed）。
- `python backend/scripts/setup_osm_region.py list` 已识别本地上海包并报告 `ready`。
- 后端完整 pytest 在当前环境收集阶段受缺少 `shapely`、`pydantic_settings`、`life_circle`、`networkx` 等依赖影响，未能执行；该环境阻塞与本次脚本无关。

日期：2026-10-05

## 8. 使用说明同步

- `intro-page/Baidu-map-site/help/操作说明.md` 与 `help/index.html` 已同步统一的地区包下载、选择、清除和 PBF 构建命令。
- 已运行站点构建，将同样内容生成到 `intro-page/Baidu-map-site/dist/help/`；源码与构建产物哈希一致。
- `npm.cmd run check` 已通过。
