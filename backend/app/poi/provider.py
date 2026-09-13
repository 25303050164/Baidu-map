"""Place v3 adapter; only allowlisted fields cross the transport boundary."""
from copy import deepcopy
import json
from pathlib import Path
import re
import time
from typing import Protocol

from app.baidu import API_URL, silence_transport_logs
from app.place_protocol import response_error
from app.request_control import RequestStopped
from .planner import digest


class PoiProvider(Protocol):
    identity: str
    api_version: str
    network: bool

    async def page(self, params: dict, runtime) -> tuple[dict | None, str | None]: ...


def sanitize(value, secret=''):
    if isinstance(value, str):
        if secret:
            value = value.replace(secret, '[redacted]')
        value = re.sub(r'https?://\S+', '[url]', value)
        return re.sub(r'(?i)\b(?:ak|token|password)=[^\s&]+', '[redacted]', value)
    if isinstance(value, list):
        return [sanitize(v, secret) for v in value]
    if isinstance(value, dict):
        return {k: sanitize(v, secret) for k, v in value.items()}
    return value


def whitelist(payload, secret=''):
    result = {k: payload[k] if type(payload[k]) in (int, str, type(None)) else None
              for k in ('status', 'total', 'result_type') if k in payload}
    def point(value):
        if not isinstance(value, dict):
            return None
        return {k: value[k] if type(value[k]) in (int, float, str, bool) else None
                for k in ('lng', 'lat') if k in value}
    result['results'] = []
    for row in payload['results']:
        if not isinstance(row, dict):
            result['results'].append({'invalidType': type(row).__name__})
            continue
        item = {k: row[k] if isinstance(row[k], str) else None
                for k in ('uid', 'name', 'address') if k in row}
        if 'location' in row:
            item['location'] = point(row['location'])
        details = row.get('detail_info')
        if isinstance(details, dict):
            item['detail_info'] = {k: details[k] if isinstance(details[k], str) else None
                                  for k in ('classified_poi_tag', 'parent_id') if k in details}
            if 'navi_location' in details:
                item['detail_info']['navi_location'] = point(details['navi_location'])
        result['results'].append(item)
    return sanitize(result, secret)


class BaiduPoiProvider:
    identity, api_version, network = 'baidu_place', '3.0', True

    def __init__(self, client, settings):
        if not settings.ak_configured:
            raise RequestStopped('missing_ak')
        self.client, self._secret = client, settings.baidu_map_ak.get_secret_value()

    async def page(self, params, runtime):
        # Direct calls cannot bypass the runtime's durable reservation.
        runtime.authorize()
        if (not runtime.lock.locked() or not runtime.events
                or runtime.events[-1]['state'] != 'dispatch_started'
                or runtime.events[-1]['paramsHash'] != digest(params)):
            raise RequestStopped('dispatch_not_reserved')
        silence_transport_logs()
        response = await self.client.get(API_URL, params={**params, 'ak': self._secret},
            timeout=max(.01, min(8, runtime.deadline-time.monotonic())), follow_redirects=False)
        runtime.events[-1].update(state='response_received', httpStatus=response.status_code)
        payload = response.json() if response.status_code == 200 else None
        if isinstance(payload, dict) and type(payload.get('status')) is int:
            runtime.events[-1]['baiduStatus'] = payload['status']
        reason = response_error(response.status_code, payload)
        if reason:
            return None, reason
        if payload.get('result_type') != 'poi_type':
            return None, 'invalid_response'
        return whitelist(payload, self._secret), None


class ReplayProvider:
    identity, api_version, network = 'synthetic', '3.0', False

    def __init__(self, fixture):
        if fixture.get('source') != 'synthetic':
            raise ValueError('replay fixture must declare synthetic source')
        self.fixture = deepcopy(fixture)
        self.identity = 'synthetic:' + digest(fixture)

    @classmethod
    def from_path(cls, path):
        return cls(json.loads(Path(path).read_text(encoding='utf-8')))

    async def page(self, params, runtime):
        key = f"{params['query']}:{params['page_num']}"
        payload = self.fixture.get('pages', {}).get(key)
        if payload is None:
            return None, 'fixture_missing'
        reason = response_error(200, payload)
        return (whitelist(payload) if reason is None else None), reason
