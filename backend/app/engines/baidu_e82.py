"""E8.2 compute-only adapter: the boundary core without its facility stage.

The legacy manager runs ``analyze_facilities`` right after ``compute_e82``. That
full pipeline is deliberately not reused here; a checkup retrieves facilities
once, in its own stage, under its own budget.
"""
import math
from contextlib import AsyncExitStack

import httpx
from life_circle.models import IsochroneRequest
from life_circle.providers import BaiduProvider

from ..algorithms.baidu_e82 import ALGORITHM, EndpointAnalyticProvider, compute_e82
from ..analyses import LimitedProvider, effective_qps
from ..baidu import silence_transport_logs
from .protocol import EngineCapabilities, EngineContext, IsochroneAsk, IsochroneSnapshot

# The boundary core is tuned for this request shape; a checkup never overrides it.
MAX_EXTENT_M = 1600
BUDGET_TIERS = (200, 400, 800)


def e82_request(origin, budget: int, *, qps: float | None = None) -> IsochroneRequest:
    """The one request shape production uses; offline benchmarks build it here too."""
    return IsochroneRequest(
        origin, "bd09ll", budget=budget, max_extent=MAX_EXTENT_M, expand=False,
        time_bands=(15,), config_version=ALGORITHM, qps=qps)


class BaiduE82Engine:
    engine_id = "baidu_e82"

    def __init__(self, settings, gate, provider_factory=None):
        self.settings, self.gate, self.provider_factory = settings, gate, provider_factory

    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            engine_id=self.engine_id, label="百度边界搜索（E8.2）", engine_version=ALGORITHM,
            budgets=BUDGET_TIERS, default_budget=400, requires_osm_graph=False,
            notes=["端点证据来自百度实际返回，不来自合成端点",
                   "半径与网格策略由服务端控制，请求不能覆盖",
                   "内部未独立核验，质量不会高于 partial"])

    def unavailable_reason(self) -> str | None:
        """Why a task could not run at all, known before it is admitted."""
        if self.provider_factory is not None or self.settings.analysis_provider == "synthetic":
            return None
        return None if self.settings.ak_configured else "walking_ak_not_configured"

    async def _provider(self, stack, origin):
        if self.provider_factory:
            provider = self.provider_factory(origin)
            if hasattr(provider, "__aenter__"):
                provider = await stack.enter_async_context(provider)
            return provider
        if self.settings.analysis_provider == "synthetic":
            # Synthetic points have exact endpoints and never claim real ones.
            return EndpointAnalyticProvider(origin, lambda x, y: math.hypot(x, y) / 1.2)
        if not self.settings.ak_configured:
            raise ValueError("walking_ak_not_configured")
        silence_transport_logs()
        client = await stack.enter_async_context(
            httpx.AsyncClient(trust_env=False, follow_redirects=False))
        # The shared gate keeps every attempt, including retries, paced against
        # the same account-level budget as the legacy endpoints.
        return LimitedProvider(
            BaiduProvider(self.settings.baidu_map_ak.get_secret_value(), client=client), self.gate)

    async def compute(self, ask: IsochroneAsk, context: EngineContext) -> IsochroneSnapshot:
        async with AsyncExitStack() as stack:
            provider = await self._provider(stack, ask.origin)
            request = e82_request(ask.origin, ask.budget,
                                 qps=effective_qps(self.settings, self.gate) if provider.network else None)
            result = await compute_e82(request, provider, context.token,
                                       on_progress=context.progress)
        payload = result.to_dict()
        # E8.2 exposes no separate display shell; the computed boundary is drawn.
        return IsochroneSnapshot.build(
            engine_id=self.engine_id, engine_version=ALGORITHM, algorithm=ALGORITHM,
            parameters=payload["config"], geometry=payload["geometry"],
            display_geometry=payload["geometry"], unknown_region=payload["unknownRegion"],
            uncertain_region=payload["uncertainRegion"],
            computation_extent=payload["computationExtent"], quality=result.quality,
            stop_reason=result.stop_reason, warnings=list(result.warnings),
            statistics=payload["statistics"],
            requests_used=payload["statistics"]["requests"],
            network_requests=payload["statistics"]["network_requests"])
