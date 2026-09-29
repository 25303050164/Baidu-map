"""步行路线传输（§6.3、§9.2）：一次预留换一次真实路线。

核验阶段与"点击设施看路线"问的是同一个问题 —— 从这里走到这家设施要多少米、走不走得通
—— 所以两者共用一条传输、一个服务池和一本账。差别只在从哪个桶里扣：核验扣任务的
``route`` 桶（标准 120 次），点击详情扣 ``detail`` 桶（标准 20 次），两个桶都不会被对方
撑大。

这里的计量点就是配额模块的服务池：一次尝试在同一处检查服务上限、阶段桶、日额度和截止
时间，并在发送前做一次事务性预留。**不再另外走路线闸门** —— 池子已经持有该服务的唯一在途
位置并按上限节流，再叠一层闸门会节流两次并把自己锁死（``quota`` 模块开头写明了这一条）。

直线距离不在这里出现：它只用于候选排序与保守筛选，任何"在服务范围内"的结论都必须由
这条返回的路线距离支撑（§6.3）。
"""
import time
from typing import Protocol

import httpx
from life_circle.providers import BaiduProvider

from ..baidu import silence_transport_logs
from ..quota import AttemptCancelled, BudgetExhausted, DeadlineReached

#: 任务自己的池名，与 ``quota.TaskBudget`` 的拼写一致。
ROUTE_POOL = 'route'
DETAIL_POOL = 'detail'
#: 单次响应的上限，永远让位于任务截止时间。
MAX_TIMEOUT_SECONDS = 8.0
#: 一次候选最多试两次：重试也是尝试，照样从桶里扣。
RETRY_LIMIT = 2
#: 只有这两种失败值得重试：其余（额度、鉴权、参数、无结果）重试一次就是烧一次预算。
RETRY_REASONS = ('temporary', 'timeout')
#: 撞到这些原因就停掉整个阶段 —— 继续问下去只会把同一份失败重复一遍。
STOP_REASONS = frozenset(('auth', 'configuration', 'permission', 'quota', 'invalid_parameter'))


class RoutesUnavailable(Exception):
    """这个部署建不出路线传输（比如没有 AK）。"""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class RouteTransport(Protocol):
    identity: str
    network: bool

    def session(self, pool, *, budget, deadline): ...


async def open_online(settings, stack) -> "OnlineRouteTransport":
    """生产传输：一个阶段一个 HTTP 客户端，或者一个带名字的拒绝。"""
    if not settings.ak_configured:
        raise RoutesUnavailable('missing_ak')
    silence_transport_logs()
    client = await stack.enter_async_context(
        httpx.AsyncClient(trust_env=False, follow_redirects=False))
    return OnlineRouteTransport(client, settings.baidu_map_ak.get_secret_value())


class OnlineRouteTransport:
    """directionlite 步行路线：与旧设施阶段同一个适配器和同一套解析。"""

    identity, api_version, network = 'baidu_direction', 'directionlite/v1/walking', True

    def __init__(self, client, secret):
        self.client, self.secret = client, secret

    def session(self, pool, *, budget, deadline):
        return RouteSession(self, pool, budget=budget, deadline=deadline)

    async def route(self, facility_id, origin, destination, timeout):
        """timeout 是剩余秒数；Provider 接收单调时钟的绝对截止时间。"""
        silence_transport_logs()
        uid = facility_id.removeprefix('baidu_place:') if facility_id.startswith('baidu_place:') else None
        provider = BaiduProvider(self.secret, client=self.client,
                                 destination_uid=uid or None, route_metric='distance')
        return await provider.query_walking_time(origin, destination, time.monotonic() + timeout)


class RouteSession:
    """一个任务的路线尝试，绑在它自己的池、桶和截止时间上。

    调用一次就是"把这个设施核验掉"：拿到一条可用观测就返回，值得重试的失败在原位重试，
    每次都单独预留。撞上停止类原因或预算耗尽时，后续候选会拿到一个带原因的拒绝而不是
    静默的空结果。
    """

    def __init__(self, transport, pool, *, budget, deadline, token=None):
        self.transport, self.pool, self.budget, self.deadline = transport, pool, budget, deadline
        # The task's cancel token, checked by the pool before every attempt is sent.
        self.token = token
        self.attempts = 0
        self.stop_reason: str | None = None

    @property
    def identity(self) -> str:
        """写进核验证据的提供方：阶段只认这个名字，不认传输怎么实现。"""
        return self.transport.identity

    @property
    def network(self) -> bool:
        return bool(getattr(self.transport, 'network', True))

    async def __call__(self, facility_id, origin, destination, *, pool: str = ROUTE_POOL):
        value = None
        for _ in range(RETRY_LIMIT):
            if self.stop_reason is not None:
                return None
            timeout = self.deadline - time.monotonic()
            if timeout <= 0:
                self.stop_reason = 'deadline'
                return None
            try:
                async with self.pool.attempt(self.deadline, budget=self.budget,
                                             pool=pool, token=self.token) as attempt:
                    timeout = self.deadline - time.monotonic()
                    if timeout <= 0:
                        attempt.outcome('deadline')
                        raise DeadlineReached()
                    self.attempts += 1
                    value = await self.transport.route(
                        facility_id, origin, destination,
                        min(MAX_TIMEOUT_SECONDS, timeout))
                    attempt.outcome(value.reason)
            # 桶用尽和到点都不是"这条路走不通"，而是"这次没有结论"：停下来，把原因
            # 带到证据里去，而不是让异常从阶段里冒出来变成一次失败。
            except BudgetExhausted:
                self.stop_reason = 'task_budget_exhausted'
                return None
            except DeadlineReached:
                self.stop_reason = 'deadline'
                return None
            except AttemptCancelled:
                # 取消之后不再发出新的请求；已发出的那一次收不回，但不会有下一次。
                self.stop_reason = 'cancelled'
                return None
            if value.reason in STOP_REASONS:
                self.stop_reason = value.reason
                break
            if value.reason not in RETRY_REASONS:
                break
        return value
