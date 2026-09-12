# FastAPI · 等时圈任务服务

本后端已接入自适应网格算法，提供创建分析、查询进度、读取结果和取消任务的接口。前端默认使用该服务；设施统计尚未接入。真实步行接口及社区实验尚未验证。完整接口和验收结果见 [接入任务报告](docs/算法前后端接入任务报告.md)。原 N02 健康检查和地点检索验证命令继续保留。

## 1. 安装与启动（Windows PowerShell）

使用已安装的 Python 3.11，在仓库的 `backend` 目录执行。环境、下载和测试缓存使用 D 盘：

```powershell
New-Item -ItemType Directory -Force D:/CodexTemp,D:/CodexCaches | Out-Null
$env:TEMP = 'D:/CodexTemp'
$env:TMP = 'D:/CodexTemp'
$env:PYTHONPYCACHEPREFIX = 'D:/CodexCaches/baidu-pycache'
$env:UV_CACHE_DIR = 'D:/CodexCaches/uv'
# 已有该环境时跳过创建；也可以用已安装 Python 的完整路径代替 py -3.11。
if (-not (Test-Path D:/CodexCaches/baidu-map-algorithm-venv/Scripts/python.exe)) { py -3.11 -m venv D:/CodexCaches/baidu-map-algorithm-venv }
$analysisPython = 'D:/CodexCaches/baidu-map-algorithm-venv/Scripts/python.exe'
uv pip install --python $analysisPython -r requirements.lock.txt -e ../life-circle-algorithm
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
$env:ANALYSIS_PROVIDER = 'synthetic'  # 首次联调使用离线合成场景
& $analysisPython -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

打开 <http://127.0.0.1:8000/health>；接口文档在 <http://127.0.0.1:8000/docs>。用 Ctrl+C 停止服务。默认仅监听本机，不部署公网。

本轮已验证 Python 3.11.15 和锁定依赖兼容性。必须安装本地算法包，不能仅安装后端 requirements。任务仅保存在当前进程，最多一个任务运行；终态保留 30 分钟、最多 20 条，重启后清空。不要启用多个 worker 或开发热重载来运行正式实验。

`ANALYSIS_PROVIDER` 默认 `baidu`；真实模式必须显式配置 `BAIDU_MAP_AK` 和正数 `ANALYSIS_QPS`。空 QPS 表示未配置，创建任务返回 503；离线模式忽略 QPS，使用匀速平面。真实模式不会自动回退合成数据。请在下一阶段核验步行权限后再启用真实调用。

## 2. 原 N02 地点检索验证配置

1. 登录[百度地图控制台](https://lbsyun.baidu.com/apiconsole/key)，在“应用管理 / 我的应用”找到现有 AK。确认应用类型为“服务端”，请求校验方式为“IP 白名单”。
2. 确认该应用开通“地点检索”能力，包含地点检索 3.0，且配额可用。权限名称以控制台当前页面为准。
3. 将运行本后端电脑的**出口公网 IP**加入白名单；不是 `127.0.0.1`，也不是局域网地址。可由团队网络管理员提供；若使用外部 IP 查询工具，应确保与本程序走同一出口。网络、VPN 或出口变化后需重新核对。
4. 用本地编辑器打开 `backend/.env`，只在 `BAIDU_MAP_AK=` 后填写真实 AK，不要填入 `.env.example`。不需要在聊天中发送 AK。
5. 保存后重启 FastAPI。手动验证命令每次运行都会重新读取配置。

系统环境变量优先于 `.env`；`.env` 路径固定为后端目录，不依赖启动时的工作目录。健康接口仅返回 `baidu_ak_configured` 布尔值；`true` 只说明非空，不能证明权限、白名单或真实请求成功。

`.env`、Python 环境和本地日志均由 Git 忽略。不要将服务端 AK 放入 `VITE_*`、前端源码、截图、命令参数、文档或完整请求 URL。以后接入 JS 地图应另用浏览器类型 AK，不复用本次服务端密钥。

官方参考：[获取 AK](https://lbsyun.baidu.com/index.php?title=FAQ-obtainAK)、[地点检索 3.0](https://lbs.baidu.com/docs/webapi?title=placev3%2Fguide%2Fwebservice-placeapiV3%2FinterfaceDocumentV3)。

## 3. 一次真实请求和脱敏记录

在 `backend` 中运行；不必先启动 FastAPI：

```powershell
& $analysisPython -m app.smoke
$LASTEXITCODE
```

程序通过 HTTPS 请求 `/place/v3/around`，固定查询 BD-09 坐标 `39.915,116.404`（纬度、经度）周边 1000 米内的药店，严格半径、第一页、最多 10 条。它只用于连接验证，不推导步行可达性或正式业务统计。

一次执行至多发出一次请求，不重试、不跟随重定向；连接、读取等网络阶段超时为 10 秒。程序不继承环境代理变量，使用直接网络出口；不要通过关闭 TLS 校验解决网络问题。

终端输出一行 JSON，同时追加到 `logs/baidu-smoke.jsonl`。记录仅包含 UTC 时间、请求 ID、接口路径、耗时、HTTP 状态、百度业务状态码、结果数量及固定结果标识。成功必须同时满足 HTTP 200、百度整数 `status=0`、设施数组及所含设施的必要字段有效。合法空数组代表调用成功，但不是“附近没有药店”的业务结论。

| outcome | 含义与处理 |
| --- | --- |
| `success` | 请求成功；退出码 0，可将本行作为脱敏调用证据 |
| `missing_ak` | 未填写 AK，未发出请求；检查 `.env` 或环境变量 |
| `config_error` | 配置格式错误；核对 JSON 来源列表和环境变量 |
| `baidu_error` | 百度业务失败；依据 `baidu_status` 和官方状态码说明核对 AK 类型、权限、IP 白名单与配额 |
| `http_error` | HTTP 非 200；核对网络或服务状态，不自动重试 |
| `timeout` / `network_error` | 请求超时或网络失败；排查网络后再手动执行 |
| `invalid_response` | 非 JSON、状态字段异常或设施结构无效，不算通过 |
| `internal_error` | 其他本地调用错误；复核代码与运行环境，不输出原始异常 |

调用或配置失败退出码为 1，记录保存失败为 2。HTTPX/HTTPCore 请求日志被禁用，异常不直接输出；启动命令关闭访问日志，避免有人把敏感参数拼入健康检查 URL 后被记录。不要启用包含请求 URL 的底层调试日志。

## 4. 浏览器跨域策略

默认 `CORS_ORIGINS` 为以下 JSON 数组，来源不能带路径或末尾斜杠：

```dotenv
CORS_ORIGINS=["http://127.0.0.1:5173","http://localhost:5173"]
```

允许 GET、POST 和预检需要的 Content-Type，不允许凭据，不使用 `*`。非白名单来源无法通过浏览器读取响应；CORS 是浏览器策略，不是身份认证。当前服务仅面向本机联调；未来部署时需另行设计身份认证和共享任务管理。

使用实际 Vite 前端来源验收：从 `backend` 执行以下命令复制测试页到已忽略的前端 `output` 目录，然后按前端 README 启动 Vite：

```powershell
New-Item -ItemType Directory -Force ..\life-circle-demo\output | Out-Null
Copy-Item tools\browser-health.html ..\life-circle-demo\output\n02-health.html
```

打开 <http://127.0.0.1:5173/output/n02-health.html>，分别点击“检查普通跨域请求”和“检查预检跨域请求”，两项应显示 `PASS`。也可将地址主机改成 `localhost` 复核另一白名单来源。测试页不接收密钥，不修改前端业务页面，不计入生产构建。

跨域依据：[FastAPI 官方文档](https://fastapi.tiangolo.com/tutorial/cors/)。

## 5. 自动检查与团队复核

```powershell
uv pip check --python $analysisPython
& $analysisPython -m pytest -q -o cache_dir=D:/CodexCaches/backend-pytest --basetemp=D:/CodexCaches/backend-tests
git check-ignore .env .venv/pyvenv.cfg logs/baidu-smoke.jsonl
```

自动测试使用虚拟密钥和模拟 HTTP 响应，不使用真实 AK、不消耗配额。覆盖健康检查、配置优先级、缺少密钥、跨域、响应验证、网络失败、禁止重试与日志脱敏。当前依赖会产生 Starlette 测试客户端的兼容性弃用提示，不影响测试结果。

复核人按 [N02 验收记录](docs/N02-验收记录.md) 检查结果。只有真实调用证据为 `success`，才能勾选“至少一次百度真实请求成功”；测试通过或健康检查成功不能替代此项。
