"""Place v3 response and pagination rules shared by bounded collectors."""
from dataclasses import dataclass, field
import hashlib
import json

STOP_ERRORS = frozenset({"rate_limit", "permission", "quota", "parameter_error"})
RETRY_ERRORS = frozenset({"upstream_error", "timeout", "network_error"})


def response_error(http_status, payload):
    if http_status == 429:
        return "rate_limit"
    if http_status in (401, 403):
        return "permission"
    if http_status >= 500:
        return "upstream_error"
    if http_status != 200:
        return "http_error"
    if not isinstance(payload, dict) or type(payload.get("status")) is not int:
        return "invalid_response"
    code = payload["status"]
    if code in (401, 402):
        return "rate_limit"
    if code in (4, 302):
        return "quota"
    if code in (3, 5) or 200 <= code < 300:
        return "permission"
    if code == 2:
        return "parameter_error"
    if code:
        return "upstream_error" if code == 1 else "baidu_error"
    if payload.get("result_type", "poi_type") != "poi_type":
        return "non_poi_response"
    if not isinstance(payload.get("results"), list) or len(payload["results"]) > 20:
        return "invalid_response"
    return None


@dataclass
class Pagination:
    pages: int = 0
    returned: int = 0
    total: int | None = None
    fingerprints: set = field(default_factory=set)
    warnings: list = field(default_factory=list)

    def consume(self, payload, max_pages):
        """Return a stop reason, or None to schedule the next page."""
        rows = payload["results"]
        total = payload.get("total")
        total = total if type(total) is int and total >= 0 else None
        fingerprint = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.pages += 1
        self.returned += len(rows)
        if rows and fingerprint in self.fingerprints:
            return "pagination_anomaly"
        self.fingerprints.add(fingerprint)
        if self.pages > 1 and self.total != total:
            self.warnings.append("pagination_uncertain")
        self.total = total
        if total is not None and total >= 150:
            self.warnings.append("possible_truncation")
        if total is not None and (self.returned > total or (not rows and self.returned < total)):
            self.warnings.append("pagination_uncertain")
        if not rows or (total is not None and total < 150 and self.returned >= total):
            return self.warnings[-1] if self.warnings else "completed"
        if self.pages >= max_pages:
            return "possible_truncation" if "possible_truncation" in self.warnings else "page_limit"
        return None
