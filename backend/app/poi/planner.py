import hashlib
import json
import math
from pathlib import Path

from life_circle.coordinates import LocalProjection

RULES = json.loads(Path(__file__).with_name('categories.json').read_text(encoding='utf-8'))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def build_plan(request, config):
    origin = (request.center.lng, request.center.lat)
    projection = LocalProjection(origin)
    def extent(half):
        southwest = projection.to_geographic((-half, -half))
        northeast = projection.to_geographic((half, half))
        if not (-180 <= southwest[0] < northeast[0] <= 180 and -85 < southwest[1] < northeast[1] < 85):
            raise ValueError('window outside supported projection')
        return {'localMeters': [-half, -half, half, half], 'bd09ll': [*southwest, *northeast]}
    half = request.analysis_half_width_meters + request.search_margin_meters
    size = half / 2
    sequences = []
    for row in range(4):
        for col in range(4):
            x, y = -half + col*size, -half + row*size
            center = projection.to_geographic((x+size/2, y+size/2))
            bounds = [*projection.to_geographic((x,y)), *projection.to_geographic((x+size,y+size))]
            for category in request.categories:
                for query in RULES['queries'][category]:
                    sequences.append({'sequenceId': f'r{row}c{col}:{category}:{query}', 'tileId': f'r{row}c{col}',
                        'category': category, 'query': query, 'center': list(center),
                        'radius': math.ceil(size/math.sqrt(2))+5, 'localMeters': [x,y,x+size,y+size], 'bd09ll': bounds})
    if config.phase == 'smoke':
        sequences = [s for s in sequences if s['tileId'] == 'r0c0' and s['query'] == RULES['queries'][s['category']][0]]
    plan = {'provider': 'baidu_place', 'apiVersion': '3.0', 'coordinateSystem': 'bd09ll',
        'projectionVersion': 'local-equirectangular-6371008.8-v1', 'ruleVersion': RULES['version'],
        'analysisExtent': extent(request.analysis_half_width_meters), 'searchExtent': extent(half),
        'pageSize': 20, 'maxPages': 8, 'sequences': sequences, 'order': 'first-pages-then-round-robin',
        'request': request.model_dump(mode='json', by_alias=True), 'rulesHash': digest(RULES)}
    runtime = config.model_dump(mode='json', by_alias=True, exclude={'authorized', 'approved_config_hash'})
    plan['configHash'] = digest({'plan': plan, 'runtime': runtime})
    plan['requestUpperBound'] = min(len(sequences)*8*2, config.total_budget,
                                     sum(config.category_budgets[c] for c in request.categories))
    return plan


def parameters(sequence, page):
    lng, lat = sequence['center']
    return {'query': sequence['query'], 'location': f'{lat:.6f},{lng:.6f}', 'radius': sequence['radius'],
            'coord_type': 3, 'radius_limit': 'true', 'scope': 2, 'page_size': 20, 'page_num': page, 'output': 'json'}
