"""A controlled synthetic place directory, and the ``around`` engine over it.

The benchmark asks one narrow question — for the *same* world, what does a query
plan really dispatch and what does it really find — and it can only answer that
against a world it owns. This module is that world: deterministic facilities with
stable UIDs, and a search engine that answers them the way the place service is
modelled to.

It is a **model, not a specification**, and the difference decides what a result may
be used to claim. An offline fixture can support "the implementation and its
accounting are self-consistent"; it can never support "the real service behaves this
way" or "recall is unchanged" (scheme.md §6 C2). Every assumption about what the
service would return is therefore listed in :data:`MODEL_ASSUMPTIONS` and repeated
in each emitted report, rather than left implicit in this code.

What is *not* modelled here, on purpose: the quota, rate-limit, retry, truncation
and pagination rules. Those stay in the production code the benchmark exercises
(``place_protocol.Pagination``, ``quota.ServicePool``), so a fixture cannot quietly
disagree with the behaviour it is supposed to measure.
"""
import math
import random
from dataclasses import dataclass

from app.poi.normalize import classify
from app.poi.planner import RULES
from life_circle.coordinates import LocalProjection

#: Everything this fixture assumes about the service. Reported verbatim next to
#: every measurement, because a reader cannot judge the numbers without them.
MODEL_ASSUMPTIONS = (
    '每家设施只匹配一个检索关键词：本小类的主关键词，或本小类的一个额外关键词。',
    '一次查询返回"关键词落在提交集合内、且到查询圆心距离不超过所给半径"的设施，'
    '按（距离, UID）稳定排序。',
    'total 是这批设施的真实条数；分页按同一顺序切片，页大小 20，与规划器实际发送的参数一致。',
    '结果里没有"这条命中哪个关键词"的字段：官方响应结构中不存在这样的字段，'
    '所以一个并集页无法由客户端拆回各关键词。',
    '名称与分类标签取自仓库自己的类别词典，分类因此走真实分类器，而不是夹具查表。',
    '夹具不复制配额、限流、重试、截断与分页上限规则；这些仍由被测的生产代码决定。',
)

#: Page size the planner sends (``planner.parameters``); the engine slices by it.
PAGE_SIZE = 20

#: The name/word pairs a category's facilities can wear. Built once per category;
#: the dictionary is the only source, and an unusable vocabulary is a fixture error
#: rather than a silently empty category.
_KINDS: dict[tuple, tuple] = {}
_NAME_SUFFIXES = ('一', '二', '三', '四')


@dataclass(frozen=True)
class Kind:
    """One name/tag pair a facility of a given (category, keyword) can wear.

    ``verdict`` is what the repository's own classifier concludes about this pair,
    recorded at fixture build time so the report can say whether a category that
    came back empty was empty in the world or excluded on the way in.
    """

    category: str
    keyword: str
    name: str
    tag: str
    verdict: str = 'accepted'
    evidence: tuple = ()


@dataclass(frozen=True)
class Place:
    """One synthetic facility: a stable UID, a kind, and a place in the plane."""

    uid: str
    kind: Kind
    x: float
    y: float


@dataclass(frozen=True)
class Fault:
    """How the modelled service misbehaves, if it does.

    ``reason`` is the conclusion the *transport* would reach (``rate_limit`` for a
    429 body, ``upstream_error`` for a 5xx, ``quota`` for an exhausted account), not
    an HTTP status: the real session reaches it inside ``provider.read_page``, and
    the fixture supplies it directly so the planner sees exactly the same value it
    would see live.

    ``results_mode`` is the other axis — a service that answers *successfully* with
    nothing to report. ``no_match`` is a true empty directory for this query, while
    ``empty_with_total`` is a page that reports a positive ``total`` and still
    returns no rows: the two must never be read as each other, so both are fixture
    modes rather than one.
    """

    reason: str | None = None
    from_call: int = 1
    results_mode: str = 'normal'

    def __post_init__(self):
        if self.results_mode not in ('normal', 'no_match', 'empty_with_total'):
            raise ValueError(f'unknown results_mode: {self.results_mode}')
        if self.reason is not None and self.reason not in (
                'rate_limit', 'upstream_error', 'quota', 'permission', 'timeout', 'network_error'):
            raise ValueError(f'unknown fault reason: {self.reason}')
        if self.from_call < 1:
            raise ValueError('from_call counts from 1')


def _kinds_for(category: str, keyword: str) -> tuple:
    """Name/tag pairs this category can wear, each validated by the real classifier.

    The dictionary decides both the words and the tags, so the fixture cannot invent
    a facility the real pipeline would read differently. Two outcomes are legitimate,
    and they are kept apart rather than smoothed over:

    * The category's vocabulary classifies into the category — the ordinary case.
    * It does not. ``training`` is the standing example: the dictionary marks it
      ``negative``, so every row the ``培训`` query returns is *excluded* from
      counting by design. Those facilities are still placed, because the service
      really would return them; what gets recorded is their verdict, so a category
      that measures empty can be told apart from one whose rows were never
      retrievable at all.

    A pair that classifies into some *other* category is refused instead: it would
    move evidence between categories inside the fixture, which is a fixture error
    rather than a property of the dictionary.
    """
    cached = _KINDS.get((category, keyword))
    if cached is not None:
        return cached
    names, tags = RULES['nameHints'][category], sorted(RULES['supportedTags'][category])
    accepted, unaccepted = [], []
    for word in names:
        if len(accepted) >= len(_NAME_SUFFIXES):
            break
        for suffix in _NAME_SUFFIXES:
            name = f'{word}示例{suffix}'
            for tag in tags:
                classified, status, evidence = classify(name, [tag])
                if classified == category and status == 'accepted':
                    accepted.append(Kind(category, keyword, name, tag, 'accepted', ()))
                    break
                # A pair this dictionary will not accept is kept as the fallback only;
                # the scan continues, because another tag may still classify into the
                # category ('一贯制' is unwelcome with the '九年一贯制' tag, which the
                # review list matches, and welcome with '小学部').
                if classified is None and status in ('excluded', 'needs_review') and not unaccepted:
                    unaccepted.append(Kind(category, keyword, name, tag, status, tuple(evidence)))
    kinds = accepted or unaccepted
    if not kinds:
        raise ValueError(f'no dictionary vocabulary classifies into {category!r}')
    _KINDS[(category, keyword)] = tuple(kinds)
    return _KINDS[(category, keyword)]


class World:
    """The directory itself: what exists, where, and what a query returns."""

    def __init__(self, *, places, origin, half_meters):
        self.places = tuple(places)
        self.origin = (float(origin[0]), float(origin[1]))
        self.half_meters = float(half_meters)
        self.projection = LocalProjection(self.origin)
        by_keyword: dict[str, list] = {}
        for place in self.places:
            by_keyword.setdefault(place.kind.keyword, []).append(place)
        self.by_keyword = {keyword: tuple(values) for keyword, values in by_keyword.items()}

    def __len__(self):
        return len(self.places)

    def row(self, place: Place) -> dict:
        """One provider row, in the documented response shape and nothing more.

        No keyword field is added. The service does not return one, and inventing it
        would hand the candidate plan an attribution the real caller never gets.
        """
        lng, lat = self.projection.to_geographic((place.x, place.y))
        return {'uid': place.uid, 'name': place.kind.name,
                'address': f'合成地址 {place.uid}',
                'location': {'lng': lng, 'lat': lat},
                'detail_info': {'classified_poi_tag': place.kind.tag}}

    def query(self, keywords, center, radius, page) -> dict:
        """One page of one ``around`` search over this world."""
        matched = []
        for keyword in keywords:
            for place in self.by_keyword.get(keyword, ()):
                distance = math.hypot(place.x - center[0], place.y - center[1])
                if distance <= radius:
                    matched.append((distance, place.uid, place))
        matched.sort(key=lambda item: (item[0], item[1]))
        start = page * PAGE_SIZE
        return {'status': 0, 'total': len(matched), 'result_type': 'poi_type',
                'results': [self.row(place) for _, _, place in matched[start:start + PAGE_SIZE]]}

    def composition(self) -> dict:
        """How many facilities exist per keyword, category and classifier verdict."""
        per_keyword: dict[str, int] = {}
        per_category: dict[str, int] = {}
        per_verdict: dict[str, int] = {}
        for place in self.places:
            per_keyword[place.kind.keyword] = per_keyword.get(place.kind.keyword, 0) + 1
            per_category[place.kind.category] = per_category.get(place.kind.category, 0) + 1
            per_verdict[place.kind.verdict] = per_verdict.get(place.kind.verdict, 0) + 1
        return {'places': len(self.places), 'keywords': len(per_keyword),
                'perKeyword': dict(sorted(per_keyword.items())),
                'perCategory': dict(sorted(per_category.items())),
                'perClassifierVerdict': dict(sorted(per_verdict.items())),
                'unacceptedCategories': self.unaccepted_categories()}

    def unaccepted_categories(self) -> dict:
        """Categories whose own vocabulary the classifier will not accept, and why.

        This is a property of the repository's dictionary, not of the fixture: it
        belongs in the report so that a zero in a category's coverage column can be
        read correctly instead of being attributed to the retrieval.
        """
        notes: dict[str, dict] = {}
        for place in self.places:
            if place.kind.verdict != 'accepted':
                notes.setdefault(place.kind.category,
                                 {'verdict': place.kind.verdict,
                                  'evidence': list(place.kind.evidence)})
        return dict(sorted(notes.items()))

    def intended_uids(self) -> dict:
        """Per category, the UIDs the fixture really holds.

        This is *fixture ground truth for the evaluator only*. It is never handed to
        a plan under test — a plan that could read the answer would measure nothing —
        and a coverage number derived from it is a fixture property, not a real-world
        recall figure.
        """
        grouped: dict[str, set] = {}
        for place in self.places:
            grouped.setdefault(place.kind.category, set()).add(place.uid)
        return {category: frozenset(uids) for category, uids in sorted(grouped.items())}


def build_world(*, half_meters, origin, categories, density_per_km2, seed,
                extra_keyword_share=0.0) -> World:
    """Lay out a directory of the requested density over the requested square.

    ``extra_keyword_share`` is the share of facilities that are reachable only
    through a category's *additional* keyword rather than its primary one. It is
    zero by default because the first-round plan sends primary keywords only, and a
    non-zero share is how a scenario shows that the baseline's coverage of a
    category depends on which of its keywords it sends.
    """
    categories = tuple(categories)
    if not categories:
        raise ValueError('at least one category required')
    half = float(half_meters)
    area_km2 = (2 * half / 1000.0) ** 2
    count = max(1, round(area_km2 * density_per_km2))
    rng = random.Random(seed)
    places = []
    for index in range(count):
        category = categories[index % len(categories)]
        queries = RULES['queries'][category]
        keyword = queries[0]
        if len(queries) > 1 and rng.random() < extra_keyword_share:
            keyword = queries[rng.randrange(1, len(queries))]
        variants = _kinds_for(category, keyword)
        places.append(Place(uid=f'w{index:06d}', kind=variants[index % len(variants)],
                            x=rng.uniform(-half, half), y=rng.uniform(-half, half)))
    return World(places=places, origin=origin, half_meters=half)


class SyntheticService:
    """The world behind one session's calls, with call accounting and faults.

    ``calls`` is the one record of what a run really asked the service for, counted
    where the request is made rather than inferred from a planner counter.
    """

    identity, api_version, network = 'synthetic:poi-benchmark', '3.0', False

    def __init__(self, world: World, *, fault: Fault | None = None, delay: float = 0.0):
        self.world, self.fault, self.delay = world, fault, delay
        self.calls = 0

    def respond(self, sequence, page):
        """One answer for one requested page: ``(payload, reason)``, as a session returns."""
        self.calls += 1
        fault = self.fault
        if fault is not None and fault.reason and self.calls >= fault.from_call:
            return None, fault.reason
        keywords = tuple(part for part in sequence['query'].split('$') if part)
        x0, y0, x1, y1 = sequence['localMeters']
        payload = self.world.query(keywords, ((x0 + x1) / 2, (y0 + y1) / 2),
                                   sequence['radius'], page)
        if fault is not None and fault.results_mode == 'no_match':
            payload = {**payload, 'total': 0, 'results': []}
        elif fault is not None and fault.results_mode == 'empty_with_total':
            payload = {**payload, 'results': []}
        return payload, None
