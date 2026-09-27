"""§6.3 百度重点核验：把模型最不确定的那几处拿去问一条真实路线。

这一阶段不重算覆盖、不改面积、不给分。它做的事只有一件：在标准 120 次路线预算里，按
类别轮转挑出最值得问的设施，向百度要一条真实步行路线，把回答按证据层级记下来。
当前路线起点是体检中心，与模型网格到最近设施的距离不可直接比较，暂不判网格冲突。

四条口径决定了它长成这样：

* **直线只用来排队。** 候选排序和保守筛选可以用直线距离（走不完 1200 米直线的设施，
  步行也不可能落在 1000 米内），但任何"在服务范围内"的结论都必须由返回的路线距离支撑。
* **严格映射不放宽。** 原始 POI 的严格核验继续用现有 :func:`poi_evidence` 语义：端点必须
  几乎完全重合才算数。路线端点与原始目标有偏移时，那条证据留在"道路端点路线证据"这一层，
  绝不提升为严格核验 —— 否则一次端点偏移就能把整片灰区读成"已实测有服务"。
* **单点不覆盖整格。** 一条路线只说明这条路线的两端。未来只有建立同起终点的模型证据后，
  才能标记待细化的冲突，不把整格改判，也不取平均。
* **失败不等于盲区。** 最近几家都问不到路线，说明的是"这几家没有证据"，不是"这里没有
  设施"。没有可解释的候选排除依据时，结论保留未知。
"""
import math
from dataclasses import dataclass, field as dataclass_field

from life_circle.coordinates import LocalProjection

from ..catalog import major_of
from ..contracts import Issue
from ..poi_evidence import poi_evidence
from .models import DISTANCE_RULE, VerificationEvidence

#: 保守筛选：直线这么远的设施，步行不可能落在 1000 米规则之内。
CANDIDATE_STRAIGHT_LINE_M = 1200.0

NO_TRANSPORT = 'route_transport_unavailable'
NO_FACILITIES = 'facility_stage_unavailable'


def usable_route(observation, requested) -> bool:
    """这条观测能不能参与判定：端点核实过、有结果，且不是"零距离的别处"。

    口径与设施阶段（``facilities.py``）逐字一致：一次零距离路线只有在它确实就是被问的
    那一点时才算数 —— 否则那是上游把"没走"当成了结论。
    """
    if observation is None or not observation.endpoint_verified or observation.duration is None:
        return False
    return not (observation.duration == 0
                and requested != (observation.destination[0], observation.destination[1]))


def within_rule(distance) -> bool | None:
    """这条路线距离是否落在规则之内；不能判定时是 None，而不是 False。

    判定误差带（1000 ± 100 米）里的距离既不支持"已覆盖"也不支持"缺口"：把它算成
    缺口会让误差带变成一条假的服务边界，算成覆盖则相反。详情证据用的是同一个函数，
    所以点击一条设施不会得到与核验阶段不同的判定口径。
    """
    if distance is None or not math.isfinite(distance) or distance < 0:
        return None
    if abs(distance - DISTANCE_RULE.threshold_m) <= DISTANCE_RULE.tolerance_m \
            and distance != DISTANCE_RULE.threshold_m:
        return None
    return distance <= DISTANCE_RULE.threshold_m


@dataclass
class VerificationOutcome:
    """核验阶段的产出：证据，以及不能核验时带名字的原因。

    ``conflicts`` 就写在证据里（它要被冻结进修订），这里不再另存一份 —— 同一个结论有两
    个来源时，早晚会出现一边改了另一边没改。
    """
    evidence: VerificationEvidence | None
    status: str
    issues: list = dataclass_field(default_factory=list)
    network_requests: int = 0


def refusal(reason: str, *, message: str | None = None) -> VerificationOutcome:
    """没有可用的路线传输：带着名字拒绝，而不是报一份空核验。"""
    text = message or {
        NO_TRANSPORT: '本次体检没有可用的步行路线服务，灰区只有模型证据。',
        NO_FACILITIES: '设施阶段没有建立结果，核验阶段没有可核验的对象。',
        'missing_ak': '未配置百度地图 AK，本次体检没有可用的步行路线服务，灰区只有模型证据。',
        'task_budget_exhausted': '本任务的路线预算已用尽，核验阶段未开始。',
        # 上游给上来的原因照原样报告，而不是当成一条缺失的说明。
    }.get(reason, f'核验阶段未运行：{reason}')
    return VerificationOutcome(
        evidence=VerificationEvidence(status='not_integrated', reason=reason, notes=[text]),
        status='not_integrated',
        issues=[Issue(code='VERIFICATION_UNAVAILABLE', message=text, scope='verification')])


def candidate_order(facilities, *, majors, zones, entrances, origin, projection=None):
    """要问哪几家设施，按什么顺序问（§6.3）。

    排序依据全部来自已经冻结的证据，不引入新的判断。档位决定**类别内**谁先被问：

    * **疑似灰区**：某一处灰区自己报出来的"最近已知设施"是第一档 —— 它就是模型说
      "太远"的那个旁边最近的设施。此处仅提升核验优先级，中心到设施路线不能直接检验灰区距离。
    * **未知门禁**：入口解析不出合法接入的设施排在第二档：它的距离结论本来就带着未决项。
    * **决定边缘**：其余按 |直线距离 − 规则阈值| 升序 —— 最接近 1000 米的那些设施，一次
      路线最可能把结论从"覆盖"翻成"缺口"或反过来。同档内按设施 ID 稳定排序。

    类别之间轮转交错（§6.3 要求的"按类别轮转"）：两类设施数量差十倍时，预算不该按数量
    分配。所以一个类别里的"决定边缘"可能排在另一个类别里的"疑似灰区"之前 —— 档位管的是
    同一类别内部的次序，轮转管的是类别之间。
    """
    projection = LocalProjection(origin) if projection is None else projection
    ranked: dict[str, list] = {major: [] for major in majors}
    suspected = set()
    for zone in zones or ():
        nearest = zone.get('nearestFacility')
        if nearest:
            suspected.add(nearest)
    for item in facilities or ():
        major = major_of(item.get('category'))
        if major not in ranked:
            continue
        location = item.get('location')
        if not isinstance(location, dict):
            continue
        x, y = projection.to_local((location['lng'], location['lat']))
        straight = (x * x + y * y) ** .5
        if straight > CANDIDATE_STRAIGHT_LINE_M:
            continue
        entrance = (entrances or {}).get(str(item.get('id')))
        unresolved = entrance is not None and getattr(entrance, 'status', None) == 'unresolved'
        rank = 0 if str(item.get('id')) in suspected else 1 if unresolved else 2
        ranked[major].append({
            'facilityId': str(item.get('id')), 'category': item.get('category'), 'major': major,
            'location': (float(location['lng']), float(location['lat'])),
            'straightLineM': straight, 'priority': ('suspected_zone', 'unresolved_entrance',
                                                    'decision_edge')[rank],
            'entranceStatus': None if entrance is None else getattr(entrance, 'status', None),
            'entranceOffsetM': None if entrance is None or entrance.attachment is None
                               else entrance.attachment.distance_m,
            'rank': (rank, abs(straight - DISTANCE_RULE.threshold_m), str(item.get('id'))),
        })
    for items in ranked.values():
        items.sort(key=lambda item: item['rank'])
    ordered, queues = [], [ranked[major] for major in majors]
    while any(queues):
        for queue in queues:
            if queue:
                ordered.append(queue.pop(0))
    return ordered


async def verify_facilities(*, facilities, majors, zones, heatmap, entrances, session, origin,
                            projection=None) -> VerificationOutcome:
    """跑完核验阶段。``session`` 为 None 表示这个部署没有路线服务。

    ``session`` 一次调用就是一个候选的全部尝试（含重试），每条尝试各扣一次 ``route``
    桶；这里不自己数次数，只读它报回来的尝试量，避免两处计数对不上。
    """
    if session is None:
        return refusal(NO_TRANSPORT)
    if facilities is None:
        return refusal(NO_FACILITIES)
    candidates = candidate_order(facilities, majors=majors, zones=zones, entrances=entrances,
                                 origin=origin, projection=projection)
    if not candidates:
        return VerificationOutcome(
            evidence=VerificationEvidence(
                status='partial', provider=session.identity, checked=0,
                queries={'routeAttempts': session.attempts},
                notes=['本次检索没有落在直线 1200 米内的设施，核验阶段没有可核验的对象。']),
            status='partial')
    records, flags, failures, unresolved = [], [], 0, 0
    for item in candidates:
        observation = await session(item['facilityId'], origin, item['location'])
        if observation is None:
            # 池子用尽或撞上停止类原因：剩下的候选没有结论，不是"走不通"。
            break
        record = _record(item, observation, origin)
        records.append(record)
        if record['conflict']:
            flags.append({'facilityId': record['facilityId'], 'category': record['category'],
                          'cell': record['modelCell'], 'modelStatus': record['modelStatus'],
                          'routeDistanceM': record['routeDistanceM']})
        if record['poiStatus'] == 'pending':
            failures += 1
        if record['entranceStatus'] == 'unresolved':
            unresolved += 1
    checked = len(records)
    unverified = len(candidates) - checked
    stopped = session.stop_reason
    notes = ['核验只对本次检索到的设施成立，不构成目录完整性证明。',
             '单条路线只证明这条路线的两端：它不把整格改判为已实测，也不把未知变成覆盖。',
             '直线距离仅用于候选排序与保守筛选，所有"在服务范围内"的结论都来自返回路线距离。',
             '本次中心到设施路线与模型网格到最近同类设施的距离不可直接比较，未进行网格冲突判定。']
    if unverified:
        notes.append(f'{unverified} 处候选未取到路线'
                     f'（{stopped or "budget_exhausted"}）：未核验不等于走不通。')
    if unresolved:
        notes.append(f'{unresolved} 处设施的入口在模型里接入不了，路线证据也无法代替入口证据。')
    if flags:
        notes.append(f'{len(flags)} 处设施的路线证据与模型结论不一致：这些格标为待细化，'
                     f'不取平均、也不由单条路线改判。')
    # 只有"每一家都问过、每一家都拿到可用结论、没有冲突"才算 complete。有人问过却只
    # 拿到 pending 时是 partial：核验跑完了，但那几家没有任何严格证据 —— "跑完了"和
    # "核验到了"不是同一句话。
    status = ('complete' if checked == len(candidates) and not unverified and not flags
              and not failures else 'partial')
    queries = {'routeAttempts': session.attempts, 'candidates': len(candidates),
               'checked': checked, 'unverified': unverified, 'stopReason': stopped}
    return VerificationOutcome(
        evidence=VerificationEvidence(
            status=status, provider=session.identity, checked=checked,
            failed=failures, unresolved=unresolved, facilities=records, conflicts=flags,
            queries=queries, notes=notes),
        status=status, network_requests=session.attempts)


def _record(item, observation, origin) -> dict:
    """一条路线观测 → 一份带层级的证据。

    ``poiEvidence`` 是严格那一层：端点不重合就是 ``pending``，绝不上浮成"可达"。判定用的
    是返回的路线距离，连接段估算只作为单独的模型值报出来。
    """
    destination = item['location']
    strict = poi_evidence(observation, origin, destination, item['facilityId'])
    # 「可用」= 端点核实过、路线有结果，**并且**严格映射成立。端点对不上的那一条
    # （偏移、只核到道路端点）留在道路端点证据层，绝不产生"在服务范围内"的判定：
    # 一条停在 80 米外的路线，距离天然偏短，用它判覆盖就是把灰区读成有服务。
    usable = usable_route(observation, destination) and strict.status != 'pending'
    distance = observation.distance_m if usable else None
    within = within_rule(distance)
    # 返回的距离不管能不能判定都留着 —— 它是端点证据层里的那个数值，只是不带结论。
    returned = observation.distance_m if usable_route(observation, destination) else None
    # 中心→某设施的路线与设施所在网格→最近同类设施不是同一个问题。
    # 当前热力没有同起点、同目的设施的距离证据，不能据此生成模型冲突。
    # 更不能把中心局部投影的格号当作 EPSG:32651 全局格号。
    model_status, conflict = None, False
    return {
        'facilityId': item['facilityId'], 'category': item['category'],
        # 设施自己的位置：图层画的是它，不是被吸附过的路线端点。
        'location': {'lng': destination[0], 'lat': destination[1]},
        'straightLineM': round(item['straightLineM'], 3), 'priority': item['priority'],
        'routeDistanceM': None if returned is None else round(returned, 3),
        'durationS': observation.observed_duration,
        'withinRule': within,
        # 严格那一层要端点和原目标几乎完全重合；道路端点层留着实际起终点和偏移。
        'poiStatus': strict.status, 'poiReason': strict.reason,
        'endpointVerified': strict.endpoint_verified,
        'routeOrigin': observation.route_origin, 'routeDestination': observation.route_destination,
        'originOffsetM': observation.origin_offset_m,
        'destinationOffsetM': observation.destination_offset_m,
        'evidenceGrade': 'verified' if strict.status != 'pending' else 'model',
        'modelCell': None, 'modelStatus': model_status, 'conflict': conflict,
        'modelComparison': 'not_comparable_origin_and_destination',
        'entranceStatus': item['entranceStatus'], 'entranceOffsetM': item['entranceOffsetM'],
        'reason': observation.reason,
    }
