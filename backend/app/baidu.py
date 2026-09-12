import logging
from datetime import datetime, timezone
from time import perf_counter
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import Settings

API_PATH = "/place/v3/around"
API_URL = "https://api.map.baidu.com" + API_PATH
TIMEOUT_SECONDS = 10.0


class Location(BaseModel):
    model_config = ConfigDict(strict=True)
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lng: float = Field(ge=-180, le=180, allow_inf_nan=False)


class Place(BaseModel):
    model_config = ConfigDict(strict=True)
    uid: str = Field(min_length=1)
    name: str = Field(min_length=1)
    location: Location


class SearchResponse(BaseModel):
    model_config = ConfigDict(strict=True)
    status: int
    results: list[Place]


def silence_transport_logs() -> None:
    # HTTPX INFO contains the entire URL, including AK. Block the namespaces,
    # including propagation to root handlers even when the application uses DEBUG.
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        logger.setLevel(logging.CRITICAL + 1)


def new_record() -> dict:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request_id": str(uuid4()),
        "api_path": API_PATH,
        "elapsed_ms": 0,
        "http_status": None,
        "baidu_status": None,
        "result_count": None,
        "outcome": "not_run",
    }


def verify_baidu(settings: Settings, *, transport=None) -> dict:
    """One request, no retries or redirects. Return an allowlisted audit summary."""
    silence_transport_logs()
    record = new_record()
    if not settings.ak_configured:
        record["outcome"] = "missing_ak"
        return record

    start = perf_counter()
    try:
        with httpx.Client(
            timeout=TIMEOUT_SECONDS,
            follow_redirects=False,
            transport=transport,
            trust_env=False,
        ) as client:
            response = client.get(
                API_URL,
                params={
                    "ak": settings.baidu_map_ak.get_secret_value(),
                    "query": "药店",
                    "location": "39.915,116.404",
                    "coord_type": 3,
                    "radius": 1000,
                    "radius_limit": "true",
                    "scope": 1,
                    "page_num": 0,
                    "page_size": 10,
                    "output": "json",
                },
            )
            record["http_status"] = response.status_code
            if response.status_code != 200:
                record["outcome"] = "http_error"
            else:
                payload = response.json()
                if not isinstance(payload, dict) or type(payload.get("status")) is not int:
                    record["outcome"] = "invalid_response"
                else:
                    record["baidu_status"] = payload["status"]
                    if payload["status"] != 0:
                        record["outcome"] = "baidu_error"
                    else:
                        parsed = SearchResponse.model_validate(payload)
                        record["result_count"] = len(parsed.results)
                        record["outcome"] = "success"
    except httpx.TimeoutException:
        record["outcome"] = "timeout"
    except httpx.RequestError:
        record["outcome"] = "network_error"
    except (ValueError, ValidationError):
        record["outcome"] = "invalid_response"
    except Exception:
        # A transport or parser exception can embed credentials; no raw traceback.
        record["outcome"] = "internal_error"
    finally:
        record["elapsed_ms"] = round((perf_counter() - start) * 1000)
    return record
