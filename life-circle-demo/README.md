# 15 分钟生活圈前端

基于需求分析与技术方案实现的可交互前端演示。使用 React、TypeScript、Vite、Ant Design、CSS Modules、ECharts；状态由 Context + useReducer 管理。

默认入口已接入后端自适应网格算法，支持选点、任务进度、取消、质量提示，以及含孔洞和多分量的 BD09LL 几何。设施统计尚未接入。真实步行与社区实验尚未验证，当前验收使用合成 Provider 和离线地图 SDK 替身。

先按 [后端 README](../backend/README.md) 启动离线服务，再启动前端。完整接口、命令和验收见 [接入任务报告](../backend/docs/算法前后端接入任务报告.md)。下方“演示操作”及设施场景表仅适用于 `VITE_ANALYSIS_MODE=demo` 的原演示入口。

## 配置与模式

- `VITE_ANALYSIS_MODE=api`：默认，调用后端；`demo`：显式启用原设施模拟演示。
- `VITE_API_BASE_URL`：默认 `http://127.0.0.1:8000`。
- `VITE_BAIDU_MAP_AK`：浏览器地图 AK，与后端步行 AK 分开配置。API 模式中地图缺配置或加载失败会显示错误，坐标输入与结果摘要仍可使用。
- 可复制 `.env.example` 为本地 `.env.local` 并重启 Vite；不要在任何 `VITE_*` 中填写服务端 AK。
- API 入口支持 200/400/800 次预算，每秒轮询；没有模拟进度动画和预设设施结论。后台忙时稍后重试，任务过期时重新分析。

## 本地运行

已验证环境：Node.js 22.22.1、npm 10.9.4。依赖具体版本记录在 `package-lock.json`。

在文件夹中打开 cmd 或 PowerShell：

```powershell
npm ci --cache D:/CodexCaches/npm
npm run dev
```

打开 [本地 Demo](http://127.0.0.1:5173/)。已经安装依赖时只需 `npm run dev`。端口占用时不会自动切换，避免误打开其他项目；可先检查占用程序或指定其他端口启动。

已有 `node_modules` 时直接使用，构建输出仍为 `dist`；下载缓存默认使用上述 D 盘路径。浏览器测试截图、报告与临时文件位于 D 盘，使用本机 Microsoft Edge。首次安装依赖如需将 `node_modules` 也放在 D 盘，见接入任务报告中的目录联接命令。

```powershell
npm test
npm run build
npm run test:integration  # 真实 FastAPI + 合成 Provider + 模拟 SDK，禁止外部网络
npm run test:e2e          # 原演示入口回归
```

集成测试启动专用端口 5178/8018，不复用已有服务；默认 Python 为 `D:/CodexCaches/baidu-map-algorithm-venv/Scripts/python.exe`，可用 `ANALYSIS_TEST_PYTHON` 覆盖。产物默认在 `D:/CodexOutputs/isochrone-integration`；`INTEGRATION_OUTPUT_DIR`、`DEMO_OUTPUT_DIR` 可分别覆盖两套测试产物目录。

## 演示操作

1. 选择青禾街区 A、B、C 任一预设点，或在地图上点选 / 输入任意坐标。
2. 选择演示场景，然后点击「开始体检」。
3. 使用地图上方的类别筛选、左侧图层开关查看设施和区域；地图支持拖动、缩放及复位。
4. 点击设施点位或右侧问题卡片，在地图查看详情。
5. 点击「查看完整体检报告」，查看本次分析的全类别结果。

手机或窄屏下分析面板移到地图下方堆叠，右侧概览通过「查看结果」抽屉查看。坐标输入后需要先点击「应用坐标」。示意地图范围内任意中心点均可分析，A、B、C 为快捷预设；超出示意地图范围会提示重新选点，不会套用其他位置的结论。

### 固定场景

设施使用固定地理坐标，A、B、C 三个中心点位置不同，15 分钟圈内看到的设施组合与数量也不同：

| 演示点 | 正常结果（圈内设施） |
| --- | --- |
| A 点 | 7 处：菜市场 4、药店 2、小学 1 |
| B 点 | 6 处：菜市场 2、药店 2、小学 2 |
| C 点 | 6 处：菜市场 2、药店 3、小学 1 |

| 场景 | 内容 |
| --- | --- |
| 正常结果 | 按上表统计并显示 15 分钟圈内设施；圈外设施不在图中展示 |
| 设施缺失 | 东北住区两处菜市场从数据中移除，展示一个菜市场服务盲区 |
| 数据不足 | 药店数据不完整，数量显示「—」，问题区域显示「无法判断」，不计为盲区 |
| 分析失败 | 同一预设点首次失败，重试成功；刷新页面后重置模拟失败记录 |

设施地理坐标固定，不同演示点看到的设施集合与圈内数量不同；地图和列表仅显示 15 分钟圈内设施，圈外设施不展示、不计入数量。概览和报告始终为全类别结果，地图及列表响应类别筛选。

15 分钟范围与 1 公里盲区独立展示；距离口径和评估范围仍为演示条件，不在前端推导正式业务结论。单点结果不代表全社区覆盖率或服务质量。

## 工程结构

```text
src/
  App.tsx            工作台、分析表单、结果列表和报告
  Map.tsx            可替换的 SVG 地图渲染与交互
  Chart.tsx          ECharts 分类统计图表
  Store.tsx          状态容器与异步分析编排
  state.ts           状态转换及过期请求隔离
  service.ts         四类异步能力及可复现模拟任务
  data.ts            三个演示点与固定场景（设施走分类管线）
  domain.ts          坐标示意映射、分类筛选和统计
  types.ts           业务类型与类别定义
  taxonomy.ts        设施大类/小类、关键字字典与边界配置
  classify.ts        名称归一化与关键字匹配、误类排除
  dedup.ts           稳定 UID 去重与兜底键
  geofence.ts        圈内判定
  pipeline.ts        分类→去重→圈内判定，地图与统计的唯一结果源
  fixtures/          验收样例（每类≥5条、误类、重复 UID）
  domain.test.ts     业务数据与状态测试
  classify.test.ts   分类与误类排除测试
  dedup.test.ts      UID 去重测试
  pipeline.test.ts   管线与地图/统计一致性测试
tests/
  demo.spec.ts       Playwright 浏览器流程验收
docs/
  设施分类与UID去重说明.md   分类字典、去重、圈内判定与验收说明
```

页面通过服务层获取样例与结果，不直接读取模拟文件。后续可替换 `service.ts` 为后端调用，替换 `Map.tsx` 为百度地图实现；保留统一业务对象、图表和报告。SVG 示意坐标映射不是地理坐标转换算法。

## 检查与构建

```powershell
npm test
npm run build
npm run test:e2e
```

- Vitest：统计、未知状态、预设点匹配、任意点分析、盲区远近、过期结果隔离、服务重试，以及设施分类、误类排除、UID 去重与地图/统计一致性。
- Playwright：完整流程、图层与筛选、报告、三处预设点、自定义坐标、任意点分析、异常及窄屏操作。
- `npm run build`：TypeScript 检查与生产构建。
- 浏览器截图：`output/playwright/`；失败追踪：`test-results/`；测试报告：`playwright-report/`。

端到端测试会自动启动本地服务，或复用已运行的 5173 端口服务。执行前应确认该端口对应本工程。浏览器测试使用系统 Edge；其他环境需安装 Edge或调整 Playwright 的浏览器配置。

生产构建可通过 `npm run preview` 本地查看。运行服务时仅监听 `127.0.0.1`，没有发布到互联网。

## 本次范围

完成本地界面及模拟流程，不包括登录、历史记录、文件导出、综合评分、后端、数据库、真实地图或空间算法。验收证明的是 Demo 的展示和交互，不代表真实分析准确性。

