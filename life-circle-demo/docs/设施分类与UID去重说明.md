# 设施分类、UID 去重与圈内判定说明

- 任务对应：`15分钟生活圈_任务明细.csv` 的 **N13**（阶段2 / P0 / 数据）
- 需求对应：FR-03、FR-04；AC-04
- 实现范围：`life-circle-demo/src`（前端 TypeScript 模块）
- 验收口径：每类至少 5 条样例；排除培训机构等明显误类；优先按稳定 UID 去重；圈内统计与地图使用同一结果

> 本文描述的是演示阶段的数据清洗与判定逻辑。分类边界中的待确认项（生鲜超市、医院药房、九年一贯制等）以配置项形式提供，默认包含，可关闭，不代表业务方已最终确认。

---

## 1. 目标与总体流程

设施数据从「百度 POI 原始记录」到「可上图、可统计的设施」需要四步：

```text
原始 POI 记录
  → ① 分类 classifyPoi      名称/标签归一化 + 大类/小类关键字匹配 + 否定词剔除
  → ② 去重 dedupe           稳定 UID 优先，缺失时用兜底键
  → ③ 圈内判定 inCircle     射线法判断点是否落在等时圈多边形内
  → ④ 统一结果 buildFacilities
        facilities : 供地图与统计共用的同一数组（唯一结果源）
        classified : 全部已分类去重记录（含非首期三类，供扩展）
        rejected   : 被排除/无法分类/非三类的记录及原因
        duplicates : 重复分组清单
```

对应源码：

| 文件 | 职责 |
| --- | --- |
| `src/taxonomy.ts` | 大/小类定义、关键字字典、否定词、边界配置、到既有 `Category` 的映射 |
| `src/classify.ts` | 归一化与关键字匹配，输出 `Classification` |
| `src/dedup.ts` | 稳定 UID 去重与兜底键 |
| `src/geofence.ts` | 圈内判定（复用 `domain.pointInPolygon`） |
| `src/pipeline.ts` | 编排唯一结果源 `buildFacilities()` |
| `src/fixtures/facilities.sample.ts` | 验收样例数据（含误类与重复 UID） |
| `src/data.ts` | 演示数据接入 pipeline，地图/统计共用 `result.facilities` |

---

## 2. 大类粗划分

采用「大类（粗）→ 小类（细）」两级。前四类为本次重点，其余为结构预留，字典同样可扩展。

| 大类 key | 大类标签 | 小类 |
| --- | --- | --- |
| `medical` | 医疗健康 | 药店药房、医院/门诊药房、医院、社区卫生服务/诊所、体检/健康管理 |
| `shopping` | 购物消费 | 农贸市场、生鲜门店、超市/便利店 |
| `education` | 教育 | 小学、九年一贯制/完小/教学点、中学、高等院校、幼儿园/学前、培训机构 |
| `care` | 疗养康养 | 养老院/敬老院、康复/护理 |
| `dining` | 餐饮 | 餐厅/快餐、咖啡/饮品 |
| `finance` | 金融 | 银行、保险/证券 |
| `public` | 政务与公共服务 | 政务/社区服务、邮政/快递、文化/图书 |
| `leisure` | 文体休闲 | 公园/绿地、体育健身、休闲娱乐 |
| `transport` | 交通出行 | 公共交通、停车场、能源站 |
| `life` | 生活服务 | 维修、美容美发/洗衣 |

### 与首期三类（`market` / `pharmacy` / `school`）的映射

地图、图表和报告目前只渲染菜市场、药店、小学三类。为保证「圈内统计与地图同一结果」，小类到既有 `Category` 的映射如下，其余大类仍会完成分类，但 `category` 为空、不进入地图与数量统计：

| 小类 | 映射 `category` | 说明 |
| --- | --- | --- |
| 农贸市场 | `market` | 菜市场核心口径 |
| 生鲜门店 | `market` | 边界项 `freshMarket`，默认计入 |
| 药店药房 | `pharmacy` | 药店核心口径 |
| 医院/门诊药房 | `pharmacy` | 边界项 `hospitalPharmacy`，默认计入 |
| 小学 | `school` | 小学核心口径 |
| 九年一贯制/完小/教学点 | `school` | 边界项 `combinedSchool`，默认计入 |
| 其余小类 | 无 | 仅分类，不进入首期地图统计 |

---

## 3. 小类关键字字典

匹配文本 = `名称 + 标签(tags)`，**不使用地址**（避免「XX小学对面」之类的地址串造成误判）。所有关键字先做归一化。

### 3.1 医疗健康 `medical`

| 小类 key | 标签 | include（正向词） | exclude（否定词） |
| --- | --- | --- | --- |
| `pharmacy` | 药店药房 | 药店、药房、大药房、医药商店、医药连锁、国药、药局、药行、药品、医药 | 兽药、宠物药、药膳、药材种植、药业、制药、药厂、医药公司、医药代表、药品包装 |
| `hospital-pharmacy` | 医院/门诊药房 | 医院药房、门诊药房、住院药房、便民药房 | — |
| `hospital` | 医院 | 医院、人民医院、中医院、附属医院、中心医院、卫生院、妇幼保健、儿童医院、口腔医院、眼科医院、肿瘤医院 | 宠物医院、动物医院 |
| `clinic` | 社区卫生服务/诊所 | 社区卫生服务、社区卫生服务中心、社区医院、诊所、门诊部、卫生室、卫生服务站、医务室 | — |
| `checkup` | 体检/健康管理 | 体检中心、体检、健康管理中心、健康管理 | — |

### 3.2 购物消费 `shopping`

| 小类 key | 标签 | include | exclude |
| --- | --- | --- | --- |
| `wet-market` | 农贸市场 | 菜市场、农贸市场、集贸市场、便民市场、生鲜市场、综合市场、菜篮子、菜场、菜市、市集、鲜市、菜店、副食店、社区菜店 | 花鸟市场、花卉市场、鲜花市场、花鸟、花卉、鲜花、家具、建材、五金、汽配、服装、农资、宠物、批发市场、电器、手机、数码、古玩、茶叶市场 |
| `fresh-store` | 生鲜门店 | 生鲜超市、生鲜、果蔬、水果店、水果、蔬菜、水产、海鲜、肉铺、鲜肉、肉店、蛋品、粮油 | 花卉、宠物 |
| `supermarket` | 超市/便利店 | 超级市场、生活超市、超市、便利店、百货、商场、购物中心 | — |

### 3.3 教育 `education`

下表 `exclude` 共用同一组**教育误类否定词**：

```text
培训、培训班、辅导、辅导班、补习、补习班、教育科技、教育集团、教育咨询、教育服务、
课外、兴趣班、书院、私塾、网校、在线教育、美术、音乐、舞蹈、书法、编程、机器人、
考研、公考、留学、职业技能、技能培训、驾驶员培训、驾校、体能、跆拳道
```

| 小类 key | 标签 | include |
| --- | --- | --- |
| `primary-school` | 小学 | 中心小学、实验小学、附属小学、第一～第六小学、小学 |
| `combined-school` | 九年一贯制/完小/教学点 | 九年一贯制、一贯制、完小、教学点、小学部、初中部 |
| `middle-school` | 中学 | 高级中学、初级中学、实验中学、职业中学、中学、初中、高中 |
| `college` | 高等院校 | 职业技术学院、职业学院、高等专科、研究生院、大学、学院（exclude：培训学院、教育学院、继续教育） |
| `preschool` | 幼儿园/学前 | 幼儿园、学前教育、学前、幼教、托育、保育、早教中心 |
| `training` | 培训机构 | 与上表否定词组相同（作为负向小类，`priority` 最高） |

**误类排除机制**：`training` 小类 `priority = 90` 且 `negative = true`；小学、完小、中学等小类自身也带上述 `exclude`。因此「实验小学培训中心」「XX 美术兴趣班」等无论命中哪个正向词，都会被 training 覆盖或直接剔除，输出 `category = undefined`，绝不进入小学统计。

### 3.4 疗养康养 `care`

| 小类 key | 标签 | include | exclude |
| --- | --- | --- | --- |
| `nursing-home` | 养老院/敬老院 | 养老院、敬老院、老年公寓、颐养院、养老服务中心、托老所、日间照料、长者照护、养老照料、福利院 | 养老地产、养老咨询、养老投资、养老保险 |
| `rehab` | 康复/护理 | 护理院、康复中心、康复医院、疗养院、康复理疗、护理中心、康复医疗 | — |

### 3.5 其余扩展大类（字典见 `taxonomy.ts`）

| 大类 | 小类（include 摘要） |
| --- | --- |
| `dining` 餐饮 | 餐厅/快餐（餐厅、饭店、餐馆、酒楼、快餐、小吃、面馆、火锅、食堂…）；咖啡/饮品（咖啡、奶茶、饮品、茶楼、茶馆、果汁） |
| `finance` 金融 | 银行（银行、信用社、农商行、储蓄所…）；保险/证券（保险、证券、基金、理财、金融、投资） |
| `public` 政务与公共服务 | 政务/社区服务、邮政/快递、文化/图书 |
| `leisure` 文体休闲 | 公园/绿地、体育健身、休闲娱乐 |
| `transport` 交通出行 | 公共交通、停车场、能源站 |
| `life` 生活服务 | 维修、美容美发/洗衣 |

---

## 4. 判定规则与优先级

`classifyPoi(poi, flags)` 的流程：

```text
1. text = normalize(名称 + 标签)
2. 若 text 为空 → 无法分类（negative）
3. 遍历所有小类：
     a. 若该小类绑定的边界开关为 false → 跳过
     b. 若 text 命中该小类的任一 exclude → 跳过
     c. 取该小类中命中的最长 include 关键字作为匹配词
4. 无候选 → 未命中小类（major 为空）
5. 有候选 → 按 priority 降序、匹配词长度降序、key 升序排序，取第一个
6. 命中 training（negative）→ category 置空
```

- `normalize`：`NFKC` 归一化（全角→半角）→ 转小写 → 去除非字母/数字/汉字字符。示例：`ＡＢＣ　小学` → `abc小学`。
- 优先级的用途：解决同一名称命中多个小类的情况。例如「人民医院药房」同时命中 `hospital-pharmacy`（priority 36）与 `pharmacy`（30），取前者；关闭 `hospitalPharmacy` 后，则落到 `pharmacy`。
- 否定词优先级高于正向词：见 3.3。

### 边界配置 `BoundaryFlags`

| 开关 | 默认 | 影响的小类 | 含义 |
| --- | --- | --- | --- |
| `freshMarket` | `true` | `fresh-store` | 生鲜超市/果蔬店是否计入菜市场 |
| `hospitalPharmacy` | `true` | `hospital-pharmacy` | 医院/门诊药房是否计入药店 |
| `combinedSchool` | `true` | `combined-school` | 完小/教学点/九年一贯制是否计入小学 |

三个默认值对应需求文档中的「待确认」项，正式接入前需业务确认；`taxonomy.ts` 的 `boundaryMeta` 提供中文说明。

---

## 5. UID 去重

### 5.1 稳定 UID 优先级

按以下顺序取设施的唯一标识（`dedup.ts` 的 `stableUid`）：

1. `uid`（百度 POI 稳定 UID，最优先）
2. `source + sourceId`（组合键，形如 `baidu:123`）
3. `poiId`

### 5.2 兜底键

当三条稳定标识都缺失时，使用：

```text
兜底键 = 归一化名称 | 坐标(5 位小数)
```

- 有经纬度用 `lng,lat` 各保留 5 位小数。
- 只有示意坐标时用四舍五入的整数 `x,y`。
- 示例：`无名生鲜|2,2`。

### 5.3 合并策略

- 同一 key 的记录归为一组；组内按「字段完整度」降序（有 `uid` > `sourceId` > `address` > `tags` > 经纬度），完整度相同再按 UID、名称排序，取第一条为保留记录。
- 输出 `DedupResult`：`unique`（去重后记录）、`duplicates`（重复分组，含 `key / kind / count / kept / removed / names`）、`removedCount`。
- 排序确定性：`unique` 按稳定 UID 升序输出，保证同一输入结果可复现。

---

## 6. 圈内判定

- 复用 `domain.pointInPolygon` 的射线法，判断设施点是否落在 15 分钟等时圈多边形内。
- `geofence.inCircle(point, polygon)` 对空多边形返回 `false`。
- `geofence.poiPoint(poi)` 支持两种坐标：演示用的 `x/y` 与真实的 `lng/lat`（后者经 `centerToPoint` 映射）。缺坐标的记录进入 `rejected`，原因「缺少坐标，无法进行圈内判定」。
- 等时圈与 1 公里盲区是两套独立口径；本模块只负责等时圈内的设施归属，不改变盲区规则。

---

## 7. 与地图 / 统计的集成（同一结果）

`buildFacilities()` 返回的 `facilities` 是**唯一结果源**，被写入 `AnalysisResult.facilities`：

- 地图：`Map.tsx` 通过 `filterFacilities(result, filter)` 渲染，该函数只保留 `inCircle && category`。
- 统计：`summarize(result)` 对 `result.facilities.filter(f => f.inCircle)` 按 `id` 去重后计数。
- 报告与图表：同样读取 `AnalysisResult`。

因为两者消费同一个数组，且 pipeline 已保证 `id`（= 稳定 UID 或兜底键）唯一，所以「地图可见设施数」与「圈内统计数」天然一致。`pipeline.test.ts` 对此有断言：

```ts
summary.total === filterFacilities(result, 'all').length
summary[category] === filterFacilities(result, category).length
```

演示数据 `src/data.ts` 的 19 条 fixture 已带稳定 `uid` 并统一走 `buildFacilities()`，地图数量、图表和报告数量均未改变。

---

## 8. 验收样例（每类 ≥ 5 条）

样例文件：`src/fixtures/facilities.sample.ts`（`sampleFacilities`）。

### 8.1 首期三类（各 6 条）

| 类别 | 样例名称 |
| --- | --- |
| 菜市场 `market` | 青禾菜市场、邻里鲜市、南苑生鲜市集、东里菜场、河畔生鲜超市、西里果蔬店 |
| 药店 `pharmacy` | 青禾药房、康宁药店、邻家药房、文景大药房、中心街药局、人民医院药房 |
| 小学 `school` | 青禾实验小学、东里小学、文景中心小学、南桥小学、南苑完小、西里教学点 |

### 8.2 误类样例（应被排除出小学/三类）

| 名称 | 期望 | 命中 |
| --- | --- | --- |
| 新东方英语培训中心 | 不计入小学 | `training`（培训） |
| 学而思辅导班 | 不计入小学 | `training`（辅导） |
| 青禾美术兴趣班 | 不计入小学 | `training`（美术 / 兴趣班） |
| 青禾花鸟市场 | 不计入菜市场 | `wet-market` 否定词（花鸟） |
| 宠物药店 | 不计入药店 | `pharmacy` 否定词（宠物药） |
| 阳光幼儿园 | 不计入小学 | `preschool`（幼儿园） |

### 8.3 其他大类样例

市第一人民医院（医疗）、青禾社区卫生服务中心（医疗）、幸福养老院（疗养）、康复护理中心（疗养）、老街面馆（餐饮）、街角咖啡馆（餐饮）、工商银行（金融）、青禾公园（休闲）、公交车站（交通）、社区理发店（生活服务）。

### 8.4 重复样例

| 场景 | 数据 | 结果 |
| --- | --- | --- |
| 稳定 UID 重复 | 两条 `uid = BM-9100` 的「重复菜市场」 | 合并为 1 条，保留字段更全的一条 |
| 兜底键重复 | 两条无 UID 的「无名生鲜」，坐标相同 | 合并为 1 条 |

`sampleFacilities` 共 38 条，去重后移除 2 条。测试命令与结果：

```powershell
npm test
# 27 passed（classify 5 / dedup 5 / pipeline 4 / 既有 domain 13）
npm run build
# tsc 类型检查 + 生产构建通过
```

---

## 9. 验收口径对照

| 验收标准（N13） | 实现 | 证据 |
| --- | --- | --- |
| 每类至少准备 5 条样例 | `facilities.sample.ts` 三类各 6 条 | `pipeline.test.ts`、本文 8.1 |
| 排除培训机构等明显误类 | `training` 负向小类 + 各小类 `exclude` 否定词 | `classify.test.ts`「excludes training institutions」 |
| 优先按稳定 UID 去重 | `stableUid` 优先级 + 兜底键 + 合并策略 | `dedup.test.ts` 四个用例 |
| 圈内统计与地图使用同一结果 | `buildFacilities` 唯一数组；`filterFacilities` / `summarize` 同源 | `pipeline.test.ts`「feeds the map and the statistics…」 |
| 三类清洗规则 | 分类 + 去重 + 圈内三段式管线 | 全部源码与测试 |

---

## 10. 待确认事项与限制

1. 生鲜超市、医院/门诊药房、完小/九年一贯制的归类仍为待确认项，当前以 `BoundaryFlags` 默认包含；正式验收前需业务确认并固定。
2. 关键字字典为规则匹配，无法覆盖所有名称变体；建议后续用真实 POI 抽样回归，持续补充 include/exclude。
3. `training` 负向小类采用高优先级覆盖；若出现「学校内设培训部」等本应保留的情况，需要单独规则处理。
4. 名称中不含明确设施词（如部分品牌名）的记录会落到「未分类」，不进入首期三类，属预期行为。
5. 兜底键依赖名称与坐标；名称写法差异（如「青禾菜市场」与「青禾农贸市场」）且无 UID 时可能无法识别为同一设施。
6. 当前去重为完全一致 UID / 兜底键匹配，未做「近似重复」（同名、坐标极近但名称略有差异）合并；如需可后续增加距离阈值。
