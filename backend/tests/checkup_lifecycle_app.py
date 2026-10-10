"""可控的体检后端：真实的任务生命周期与进度发布，零外部请求、零百度额度。

浏览器验收（``life-circle-demo/tests/checkup-lifecycle.spec.ts``）用它把任务停在指定的地方，
再放行：创建之后、回答之前；第一次采样之前；载入路网；等时圈采样中；设施检索中；服务覆盖
评估中；路线核验中。于是"首次请求前""同一阶段长时间运行""阶段切换""断网重连""刷新与切页恢复""取消中
恢复"都能在真实底图上按需复现，而不必等一次真实体检恰好走到那里。

它只替换**出进程的那一层**，其余全是生产代码：

- 成圈用解析解（E8.2）与本地步行时间（OSM＋百度），设施检索与路线核验走
  ``test_checkup_facilities`` 的合成传输 —— 它们照样经过生产的额度池、预算与截止时间，
  只是不出进程。任务、修订、进度、取消、恢复、图层与报告都是生产的那一套。
- 步行路网、障碍层、水系复核用本机的真实数据（按 ``.env`` 的 OSM 配置），所以评估阶段
  的计数是真算出来的。启动时在后台线程预载路网，``/control/ready`` 在载完之前回 503。
- 进程里装了套接字守卫：任何非回环地址的解析与连接一律拒绝并记数。验收以
  ``/control/state`` 里的 ``external.attempts == 0`` 证明这一轮没有花任何额度。

启动（在 backend 目录，浏览器 AK 与服务端 AK 都不需要）::

    BAIDU_MAP_AK= LIFECYCLE_DIR=../.tmp/lifecycle LIFECYCLE_ORIGIN=http://127.0.0.1:5182 \\
      .venv/Scripts/python.exe -m uvicorn checkup_lifecycle_app:app --app-dir tests \\
      --host 127.0.0.1 --port 8021 --no-access-log

控制接口只在这个应用上::

    POST /control/arm      {"point": "places", "after": 2}   放过 2 次之后停住（一次性）
    POST /control/release  {"point": "places"}               放行并解除
    POST /control/pace     {"point": "sampling", "seconds": 0.3}  每次经过都等这么久
    POST /control/reset                                       解除全部停点与节奏
    POST /control/cold                                        丢掉已缓存的障碍层与风险层
    GET  /control/state                                       停点、计数、任务、外部连接
    GET  /control/ready                                       路网载完才 200；载入中带上载到哪一步
"""
import asyncio
import gc
import ipaddress
import json
import os
import shutil
import socket
import sqlite3
import sys
import threading
import time
import traceback
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from pydantic import SecretStr

# -- 套接字守卫：先装上，再导入任何可能联网的东西 -------------------------------

EXTERNAL = {"attempts": 0, "targets": []}
_GUARD_LOCK = threading.Lock()


def _refuse(target) -> None:
    with _GUARD_LOCK:
        EXTERNAL["attempts"] += 1
        EXTERNAL["targets"].append(str(target)[:80])
    raise ConnectionRefusedError("lifecycle harness: external connections are refused")


def _loopback(host) -> bool:
    if host in (None, "", "localhost"):
        return True
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


def _address_host(address):
    return address[0] if isinstance(address, tuple) else address


_getaddrinfo = socket.getaddrinfo


def _guarded_getaddrinfo(host, *args, **kwargs):
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not _loopback(host):
        _refuse(host)
    return _getaddrinfo(host, *args, **kwargs)


def _guard_connect(original):
    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _loopback(_address_host(address)):
            _refuse(_address_host(address))
        return original(self, address)
    return connect


def _guard_sock_connect(original):
    async def sock_connect(self, sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not _loopback(_address_host(address)):
            _refuse(_address_host(address))
        return await original(self, sock, address)
    return sock_connect


socket.getaddrinfo = _guarded_getaddrinfo
socket.socket.connect = _guard_connect(socket.socket.connect)
socket.socket.connect_ex = _guard_connect(socket.socket.connect_ex)
# asyncio 在 Windows 上用 ConnectEx 直接连，不经过 socket.connect：两种事件循环都守住。
from asyncio import proactor_events, selector_events  # noqa: E402

for _loop in (proactor_events.BaseProactorEventLoop, selector_events.BaseSelectorEventLoop):
    _loop.sock_connect = _guard_sock_connect(_loop.sock_connect)

# -- 配置：本机的 OSM 数据，假的服务端 AK，临时的任务目录 ------------------------

from fastapi import Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from app.config import load_settings  # noqa: E402

if os.environ.get("BAIDU_MAP_AK"):
    raise SystemExit("lifecycle harness: start it with an empty BAIDU_MAP_AK")

ROOT = Path(os.environ.get("LIFECYCLE_DIR", "../.tmp/lifecycle")).resolve()
ROOT.mkdir(parents=True, exist_ok=True)
# 每次从空的任务库与账本开始：计数（创建了几个任务）才只属于这一轮。只删自己建的那几样。
for _name in ("checkups", "ledgers"):
    shutil.rmtree(ROOT / _name, ignore_errors=True)
(ROOT / "quota.sqlite3").unlink(missing_ok=True)
#: 合成传输要一个非空的 AK 才会被调用；它从不离开进程（守卫在上面）。
OFFLINE_KEY = "lifecycle-offline-no-network"

settings = load_settings().model_copy(update=dict(
    baidu_map_ak=SecretStr(OFFLINE_KEY), analysis_provider="synthetic",
    baidu_place_qps=10000, baidu_direction_qps=10000,
    # 档位不随日历切换：这个受控后端的节奏只由 /control 决定。
    baidu_quota_fallback_at=datetime(2099, 1, 1, tzinfo=timezone(timedelta(hours=8))),
    checkup_dir=ROOT / "checkups", hybrid_ledger_dir=ROOT / "ledgers",
    quota_ledger_path=ROOT / "quota.sqlite3",
    cors_origins=[os.environ.get("LIFECYCLE_ORIGIN", "http://127.0.0.1:5182")]))

# main 在导入时建一个默认应用：别让它去读任何别的配置。
with patch("app.config.load_settings", return_value=settings):
    from app.main import create_app  # noqa: E402

from app.algorithms.baidu_e82 import EndpointAnalyticProvider  # noqa: E402
from app.checkups import manager as manager_module  # noqa: E402

from test_checkup_facilities import (  # noqa: E402
    FastGate, OfflineHybrid, SyntheticPlaceSession, SyntheticPlaces, SyntheticRouteSession,
    SyntheticRoutes, row, straight_routes)

import math  # noqa: E402


# -- 停点 ----------------------------------------------------------------------

class Controls:
    """具名停点。``arm(point, after)`` 放过 ``after`` 次之后停住，直到 ``release``。

    停点既在事件循环里（采样、检索、核验、回答创建）也在工作线程里（载入路网、评估），
    所以用线程事件；异步一侧轮询它，不占住事件循环 —— 状态接口在停住期间照常回答。
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.armed: dict[str, int] = {}
        self.gates: dict[str, threading.Event] = {}
        self.passes: Counter = Counter()
        self.holding: Counter = Counter()
        self.pace: dict[str, float] = {}

    def arm(self, point: str, after: int = 0) -> None:
        with self.lock:
            self.armed[point] = self.passes[point] + max(0, after)
            self.gates[point] = threading.Event()

    def release(self, point: str) -> None:
        with self.lock:
            self.armed.pop(point, None)
            gate = self.gates.pop(point, None)
        if gate is not None:
            gate.set()

    def reset(self) -> None:
        for point in list(self.gates):
            self.release(point)
        with self.lock:
            self.pace.clear()

    def _enter(self, point: str):
        with self.lock:
            self.passes[point] += 1
            limit = self.armed.get(point)
            if limit is None or self.passes[point] <= limit:
                return None
            gate = self.gates[point]
            self.holding[point] += 1
            return gate

    def _leave(self, point: str) -> None:
        with self.lock:
            self.holding[point] -= 1

    async def passing(self, point: str) -> None:
        gate = self._enter(point)
        if gate is not None:
            try:
                while not gate.is_set():
                    await asyncio.sleep(0.05)
            finally:
                self._leave(point)
        if self.pace.get(point):
            await asyncio.sleep(self.pace[point])

    def passing_sync(self, point: str) -> None:
        gate = self._enter(point)
        if gate is not None:
            try:
                gate.wait()
            finally:
                self._leave(point)
        if self.pace.get(point):
            time.sleep(self.pace[point])

    def state(self) -> dict:
        with self.lock:
            return {"armed": sorted(self.armed), "holding": sorted(p for p, n in self.holding.items() if n > 0),
                    "passes": dict(self.passes), "pace": dict(self.pace)}


controls = Controls()


# -- 受控的替身 ------------------------------------------------------------------

class ControlledAnalytic(EndpointAnalyticProvider):
    """E8.2 的解析解：成圈开始先过 ``isochrone`` 停点，每次采样再过 ``sampling`` 停点。

    "首次请求前"停在 ``isochrone``：任务已在运行，一次采样也还没预留。停在 ``sampling`` 的是
    已经发出、还没回答的一次采样 —— E8.2 等它 8 秒就算超时并重试一次，这本身就是进展。
    """

    async def __aenter__(self):
        await controls.passing("isochrone")
        return self

    async def __aexit__(self, *exc):
        return False

    async def query_walking_time(self, origin, destination, deadline):
        await controls.passing("sampling")
        return await super().query_walking_time(origin, destination, deadline)


def analytic(origin):
    return ControlledAnalytic(origin, lambda x, y: math.hypot(x, y) / 1.2)


class ControlledHybrid(OfflineHybrid):
    async def query_walking_time(self, origin, destination, deadline):
        await controls.passing("sampling")
        return await super().query_walking_time(origin, destination, deadline)


class ControlledPlaceSession(SyntheticPlaceSession):
    async def __call__(self, sequence, page):
        # 停在发出之前：停住期间的计数就是真的已经发出的页数。
        await controls.passing("places")
        return await super().__call__(sequence, page)


class ControlledPlaces(SyntheticPlaces):
    def session(self, pool, *, budget, deadline):
        return ControlledPlaceSession(self, pool, budget=budget, deadline=deadline)


class ControlledRouteSession(SyntheticRouteSession):
    async def __call__(self, facility_id, origin, destination, **kwargs):
        await controls.passing("routes")
        return await super().__call__(facility_id, origin, destination, **kwargs)


class ControlledRoutes(SyntheticRoutes):
    def session(self, pool, *, budget, deadline):
        return ControlledRouteSession(self, pool, budget=budget, deadline=deadline)


def around_each_block(sequence, page):
    """每个检索块的中心放一家本类设施：设施跟着任务的中心走，两个中心的结果不会相同。"""
    at = tuple(sequence["center"])
    results = [row(sequence, 0, at=at, uid=f"{sequence['sequenceId']}#0")]
    return {"status": 0, "total": len(results), "result_type": "poi_type", "results": results}, None


_assess = manager_module.assess_accessibility


def controlled_assessment(*args, progress=None, **kwargs):
    """评估在工作线程里跑：每报一次判定格数就过一次 ``assessment`` 停点。"""
    if progress is None:
        return _assess(*args, **kwargs)

    def relay(step, **detail):
        progress(step, **detail)
        if step == "category" and detail.get("cells"):
            controls.passing_sync("assessment")
    return _assess(*args, progress=relay, **kwargs)


manager_module.assess_accessibility = controlled_assessment

places, routes = ControlledPlaces(around_each_block), ControlledRoutes(straight_routes())
app = create_app(settings, provider_factory=analytic,
                 hybrid_provider_factory=lambda projection, config: ControlledHybrid(projection),
                 place_factory=lambda _settings: places, route_factory=lambda _settings: routes)
for _engine in app.state.checkups.registry.engines.values():
    _engine.gate = FastGate()

offline = app.state.osm_offline
_load = offline.get


def _controlled_get():
    controls.passing_sync("graph")
    return _load()


offline.get = _controlled_get
threading.Thread(target=_load, name="lifecycle-graph-warmup", daemon=True).start()


# -- 事件循环看门狗 ----------------------------------------------------------------

class LoopWatch:
    """事件循环多久没有转一圈：状态接口能不能及时回答，取决于它。

    循环里一个协程每 0.1 秒记一次心跳，两次心跳相隔超过 1 秒就算一次卡顿，时长以它量的为准。
    旁边一个线程每 0.25 秒看一眼，心跳过期就把事件循环线程此刻的调用栈记下来（每次卡顿最多
    记 3 份），连同其他线程各自停在哪 —— 卡在哪一行一目了然。这个线程自己也要抢 GIL：别的线程
    在一次 C 调用里一直占着 GIL 时它根本醒不来，所以时长不能靠它量。

    卡顿时事件循环线程往往停在 ``select`` 上 —— 不是它自己忙，是别的线程占着 GIL。最常见的
    是一次完整的循环垃圾回收要走遍常驻的整张路网，所以这里也记下每次第 2 代回收的耗时。
    """

    THRESHOLD = 1.0
    COLLECTION = 0.2

    def __init__(self):
        self.beat = time.monotonic()
        self.started = False
        self.worst = 0.0
        self.stalls: list[dict] = []
        self.collections: list[dict] = []
        self._collecting = None
        self.lock = threading.Lock()
        gc.callbacks.append(self._collection)

    def _collection(self, phase, info):
        if info.get("generation") != 2:
            return
        if phase == "start":
            self._collecting = time.perf_counter()
        elif self._collecting is not None:
            seconds = time.perf_counter() - self._collecting
            self._collecting = None
            if seconds >= self.COLLECTION:
                self.collections.append({"at": time.time(), "seconds": round(seconds, 2),
                                         "collected": info.get("collected"), "frozen": gc.get_freeze_count()})

    def start(self):
        if self.started:
            return
        self.started = True
        # 从第一个请求起算：进程启动到这里的这段不是事件循环卡住。
        with self.lock:
            self.beat = time.monotonic()
        loop_thread = threading.get_ident()
        asyncio.get_running_loop().create_task(self._beat())
        threading.Thread(target=self._watch, args=(loop_thread,), name="lifecycle-loop-watch",
                         daemon=True).start()

    async def _beat(self):
        while True:
            now = time.monotonic()
            with self.lock:
                gap = now - self.beat
                if gap > self.THRESHOLD:
                    stall = next((s for s in reversed(self.stalls) if s["beat"] == self.beat), None)
                    if stall is None:
                        stall = {"beat": self.beat, "at": time.time() - gap, "seconds": gap, "stacks": [],
                                 "others": []}
                        self.stalls.append(stall)
                    stall["seconds"] = max(stall["seconds"], gap)
                    self.worst = max(self.worst, gap)
                self.beat = now
            await asyncio.sleep(0.1)

    def _watch(self, loop_thread):
        current = None
        while True:
            time.sleep(0.25)
            lag = time.monotonic() - self.beat
            with self.lock:
                if lag <= self.THRESHOLD:
                    current = None
                    continue
                self.worst = max(self.worst, lag)
                if current is None or current["beat"] != self.beat:
                    current = {"beat": self.beat, "at": time.time() - lag, "seconds": lag, "stacks": [],
                               "others": []}
                    self.stalls.append(current)
                current["seconds"] = max(current["seconds"], lag)
                if len(current["stacks"]) < 3 and lag >= self.THRESHOLD * (1 + 4 * len(current["stacks"])):
                    frames = sys._current_frames()
                    frame = frames.get(loop_thread)
                    if frame is not None:
                        current["stacks"].append([line.strip() for line in traceback.format_stack(frame)[-12:]])
                    names = {t.ident: t.name for t in threading.enumerate()}
                    current["others"].append({
                        names.get(ident, str(ident)): [line.strip() for line in traceback.format_stack(f)[-3:]]
                        for ident, f in frames.items()
                        if ident not in (loop_thread, threading.get_ident())})

    def state(self):
        with self.lock:
            return {"worstSeconds": round(self.worst, 2), "lagNowSeconds": round(time.monotonic() - self.beat, 2),
                    "stalls": [{"at": s["at"], "seconds": round(s["seconds"], 2), "stacks": s["stacks"],
                                "others": s["others"]} for s in self.stalls[-20:]],
                    "stallCount": len(self.stalls),
                    "fullCollections": self.collections[-40:], "frozen": gc.get_freeze_count()}


loop_watch = LoopWatch()


# -- 记账与控制接口 --------------------------------------------------------------

REQUESTS: Counter = Counter()


@app.middleware("http")
async def account(request: Request, call_next):
    loop_watch.start()
    path, method = request.url.path, request.method
    creating = method == "POST" and path.rstrip("/") == "/api/v2/checkups"
    if creating:
        REQUESTS["create"] += 1
    elif method == "POST" and path.endswith("/cancel"):
        REQUESTS["cancel"] += 1
    elif method == "POST" and "/routes/" in path:
        REQUESTS["route"] += 1
    elif method == "GET" and path.startswith("/api/v2/checkups"):
        REQUESTS["get"] += 1
    response = await call_next(request)
    if creating and response.status_code < 300:
        REQUESTS["created"] += 1
    if creating:
        # 任务已经建好、开始排队；回答压在这里：页面此时刷新，就是"创建请求没有回音"。
        await controls.passing("create")
    return response


def _tasks() -> list[dict]:
    path = app.state.checkups.store.path
    if not Path(path).exists():
        return []
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT task_id, client_request_id, engine, status, stage, revision, payload"
            " FROM tasks ORDER BY created_at").fetchall()
    tasks = []
    for task_id, request_id, engine, status, stage, revision, payload in rows:
        center = json.loads(payload).get("center")
        tasks.append({"taskId": task_id, "clientRequestId": request_id, "engine": engine,
                      "status": status, "stage": stage, "revision": revision, "center": center})
    return tasks


@app.get("/control/ready")
def ready():
    status = 200 if offline.state == "ready" else 503
    loading = offline.loading
    return JSONResponse({"graph": offline.state, "loading": loading and list(loading)},
                        status_code=status)


@app.get("/control/state")
def state():
    with _GUARD_LOCK:
        external = {"attempts": EXTERNAL["attempts"], "targets": list(EXTERNAL["targets"])}
    return {"graph": offline.state, "requests": dict(REQUESTS), "external": external,
            "loop": loop_watch.state(), "tasks": _tasks(), **controls.state()}


@app.post("/control/arm")
async def arm(request: Request):
    body = await request.json()
    controls.arm(body["point"], int(body.get("after", 0)))
    return controls.state()


@app.post("/control/release")
async def release(request: Request):
    controls.release((await request.json())["point"])
    return controls.state()


@app.post("/control/pace")
async def pace(request: Request):
    body = await request.json()
    with controls.lock:
        controls.pace[body["point"]] = float(body["seconds"])
    return controls.state()


@app.post("/control/reset")
def reset():
    controls.reset()
    return controls.state()


@app.post("/control/cold")
def cold():
    """丢掉进程里缓存的障碍层和 OSM 风险层：下一个任务像服务刚起来那样重新载入它们，
    用来复现"首个任务的载入期间，状态接口还答不答"。路网本身不丢（重载要十来分钟）。"""
    from app.algorithms.hybrid_isochrone import hard_obstacles, osm_guidance
    hard_obstacles._cached_index.cache_clear()
    osm_guidance.risk_index.cache_clear()
    return {"cold": ["obstacles", "risks"]}
