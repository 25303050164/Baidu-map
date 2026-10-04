# 设施分类规则约束报告

## 1. 规则版本

- 当前版本：`poi-categories-v1.4`
- 规则唯一来源：`backend/app/categories.json`
- 分类规则发生变化时必须升级版本号，并同步重新生成接口文档、模拟响应和前端契约。
- 历史报告保留生成时的规则版本，不回写为当前版本。

## 2. 展示分组

用户界面统一展示五组，后台仍保留细分类用于检索和审计：

| 展示组 | 合并的后台大类 |
| --- | --- |
| 健康照护 `healthcare` | `medical`、`care` |
| 教育成长 `education` | `education` |
| 生活消费 `daily_life` | `shopping`、`dining`、`finance`、`life` |
| 公共出行 `public_mobility` | `public`、`transport` |
| 文体休闲 `leisure` | `leisure` |

展示组只负责用户筛选、图例、热力层和汇总展示，不替代后台细分类，也不能改变检索关键词。

## 3. 当前细分类

当前字典实际启用的细分类为：

- `market`：菜市场，归入 `daily_life`
- `supermarket`：超市，归入 `daily_life`
- `pharmacy`：药店，归入 `healthcare`
- `hospital_pharmacy`：医院药房，归入 `healthcare`
- `school`：小学，归入 `education`

其他展示组已经在接口中预留，可在补充细分类和检索词后启用。未配置细分类前，不得把展示组数量解释为已经检索到该类设施。

## 4. 归属和交叉约束

每个设施必须有且只有一个主类：

- `category`、`minor_category` 和 `major_category` 必须描述同一个主类。
- 主类用于去重、圈内计数、服务评估和统计。
- 同一个设施不得因为多个关键词命中而重复计数。

交叉项目使用次类和能力表达：

- `secondaryCategories`：设施同时具备的其他细分类。
- `capabilities`：可供用户理解或后续服务分析使用的能力标签。
- 例如医院药房：主类为 `hospital_pharmacy`，次类为 `pharmacy`，能力为 `pharmacy`。

只有在字典中明确声明的交叉关系才允许自动归属。未声明且互相冲突的命中必须进入待审核或排除状态，不能通过优先级强行选出一个主类。

## 5. 接口约束

### 分类目录

```text
GET /api/facility-catalog
```

响应必须包含：

- `version`
- `displayGroups`
- `categories`

### 分析结果

`data.facilityDisplayGroups` 返回展示组元数据。每条设施记录可以包含：

- `displayGroup`
- `secondaryCategories`
- `capabilities`

这三个字段对旧客户端保持可选兼容；新服务生成的设施记录必须补齐它们。

## 6. 检索和统计约束

- 检索关键词、名称词和标签词必须来自分类字典，业务模块不得维护私有分类表。
- 名称命中和标签命中发生非声明交叉时，结果不能自动接受为主类。
- 展示组统计只能通过设施的主类映射计算，不能把次类再次加入设施总数。
- 地图聚合可以按展示组着色，但聚合只改变绘制方式，不删除或合并业务记录。
- `in_circle` 为 `null` 的设施不能计入圈内设施数或热力密度。

## 7. 变更检查清单

修改分类时必须同时检查：

1. `backend/app/categories.json` 的版本、主类、展示组、优先级和交叉声明。
2. 旧版 `classify` 调用是否仍返回主类或 `None` 的兼容结果。
3. `Facility`、`CategoryResult`、POI 标准化结果的字段别名是否一致。
4. `/api/facility-catalog`、OpenAPI、JSON Schema、模拟响应和前端契约是否重新生成。
5. 主类冲突、次类归属、目录接口和重复统计是否有回归测试。
6. 历史报告是否保留原规则版本，不被批量改写。
