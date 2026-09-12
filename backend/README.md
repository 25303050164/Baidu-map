# N02 · FastAPI 与百度地图连接验证

本后端提供健康检查，以及手动执行一次百度地点检索的命令。前端 Demo 仍使用模拟数据。N02 通过不代表 FR-02 等时圈算法或 FR-03 完整设施业务已经实现。

## 1. 安装与启动（Windows PowerShell）

使用 Python 3.12，在仓库的 `backend` 目录执行。已创建 `.venv` 时跳过创建步骤，无需激活环境：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

打开 <http://127.0.0.1:8000/health>；接口文档在 <http://127.0.0.1:8000/docs>。用 Ctrl+C 停止服务。默认仅监听本机，不部署公网。

本次开发环境使用应用自带 Python 3.12.14，项目 `.venv` 已创建。若 `py` 不可用，可使用已安装 Python 3.12 的完整路径创建环境。依赖锁文件包含本次验证的直接和传递依赖；测试依赖也包含其中。

## 2. 配置服务端 AK

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
.\.venv\Scripts\python.exe -m app.smoke
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

允许 GET 和预检需要的 Content-Type，不允许凭据，不使用 `*`。非白名单来源无法通过浏览器读取响应；CORS 是浏览器策略，不是身份认证，普通 HTTP 客户端仍可访问健康检查。未来部署时替换为真实前端的完整来源（协议、域名及端口），重启服务；反向代理应禁用或脱敏可能带密钥的 URL 日志。

使用实际 Vite 前端来源验收：从 `backend` 执行以下命令复制测试页到已忽略的前端 `output` 目录，然后按前端 README 启动 Vite：

```powershell
New-Item -ItemType Directory -Force ..\life-circle-demo\output | Out-Null
Copy-Item tools\browser-health.html ..\life-circle-demo\output\n02-health.html
```

打开 <http://127.0.0.1:5173/output/n02-health.html>，分别点击“检查普通跨域请求”和“检查预检跨域请求”，两项应显示 `PASS`。也可将地址主机改成 `localhost` 复核另一白名单来源。测试页不接收密钥，不修改前端业务页面，不计入生产构建。

跨域依据：[FastAPI 官方文档](https://fastapi.tiangolo.com/tutorial/cors/)。

## 5. 自动检查与团队复核

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q
git check-ignore .env .venv/pyvenv.cfg logs/baidu-smoke.jsonl
```

自动测试使用虚拟密钥和模拟 HTTP 响应，不使用真实 AK、不消耗配额。覆盖健康检查、配置优先级、缺少密钥、跨域、响应验证、网络失败、禁止重试与日志脱敏。当前依赖会产生 Starlette 测试客户端的兼容性弃用提示，不影响测试结果。

复核人按 [N02 验收记录](docs/N02-验收记录.md) 检查结果。只有真实调用证据为 `success`，才能勾选“至少一次百度真实请求成功”；测试通过或健康检查成功不能替代此项。
