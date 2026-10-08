# 设施分类规则约束报告

## 1. 规则版本

- 当前版本：`poi-categories-v2.2`
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

名称和业务标签使用同一套主类选择规则。目录的 `nameShadows` 只允许具体词在相同文本位置遮蔽其包含的泛化词；其他位置的独立命中仍须检查冲突。无声明关系的名称和标签冲突返回 `needs_review`，不进入正式数量、覆盖或密度统计，旧名称接口也不接纳该记录。

严格 POI 流程要求业务标签证据，旧接口仍允许名称判定。`教育培训` 等父标签不作为业务证据；独立培训证据仍排除。`美术馆` 内包含的 `美术` 不作为独立培训命中。

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
- 总分要求十个后台大类都有可用空间支持，每类权重为 `1/10`；三类数据只能形成分类结果。

## 6. 变更检查清单

1. 更新 `backend/app/categories.json` 的版本、展示组和交叉声明。
2. 检查 `classify`、POI 标准化和 UID 去重是否仍只产生一个主类。
3. 重新生成 `backend/docs`、`backend/mocks`、`life-circle-demo/src/api-contract.ts` 和 `life-circle-demo/src/checkup/contract.ts`。
4. 检查 `/api/facility-catalog`、主类冲突、次类字段和重复统计。
5. 历史报告保留原规则版本，不进行批量改写。
