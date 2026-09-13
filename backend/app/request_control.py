"""Response-paced slots; existing algorithm limiter stays unchanged."""
from contextlib import asynccontextmanager
import time


class RequestStopped(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


@asynccontextmanager
async def request_slot(gate, token, deadline):
    async with gate.attempt_lock:
        if token.cancelled:
            raise RequestStopped("cancelled")
        if not await gate.wait(deadline):
            raise RequestStopped("deadline")
        if token.cancelled:
            raise RequestStopped("cancelled")
        if time.monotonic() >= deadline:
            raise RequestStopped("deadline")
        outcome = {"reason": "interrupted"}
        try:
            yield outcome
        finally:
            gate.completed(outcome["reason"])
