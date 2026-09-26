"""The one category dictionary: request types, classification vocabulary, aliases.

§4.3 requires one authoritative backend dictionary, and §10 forbids hand
maintaining several inconsistent category and request-type lists. Every consumer
reads this module and nothing else:

* the contract vocabulary (``major``/``minor`` categories in ``contracts.py``,
  used by ``/api/analyses``, ``places.py`` and the v2 checkup snapshot), and
* the POI runtime's vocabulary, which spells the education category
  ``primary_school`` for historical reasons.

``primary_school`` is declared here as that runtime's own spelling of ``school``:
one category with a name per consumer, never two categories. The alias is applied
by :func:`poi_rules`, so no POI module keeps a private list.

Each category carries three word lists, and they are not interchangeable:

``query`` / ``extraQueries``
    Request types sent to the place service. The legacy endpoint sends only
    ``query``; the POI runtime sends the whole list in this order.
``nameHints``
    Words that identify the category from a facility's name.
``tagHints``
    Words that identify it from the provider's own classification tag.

The vocabulary is data (``categories.json``) so it can be reviewed and versioned
on its own; this module is the only reader.
"""
import json
from dataclasses import dataclass
from pathlib import Path

CATALOG_PATH = Path(__file__).with_name("categories.json")


@dataclass(frozen=True)
class Category:
    """One minor category, named once, however many vocabularies spell it."""
    key: str
    major: str
    label: str
    query: str
    poi_runtime: bool = False
    poi_name: str | None = None
    extra_queries: tuple[str, ...] = ()
    name_hints: tuple[str, ...] = ()
    tag_hints: tuple[str, ...] = ()

    def queries(self) -> tuple[str, ...]:
        """Every request type for this category, primary one first."""
        return (self.query, *self.extra_queries)


def _read() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


DATA = _read()
VERSION: str = DATA["version"]

CATEGORIES: tuple[Category, ...] = tuple(
    Category(
        key=item["key"], major=item["major"], label=item["label"], query=item["query"],
        poi_runtime=item.get("poiRuntime", False), poi_name=item.get("poiName"),
        extra_queries=tuple(item.get("extraQueries", ())),
        name_hints=tuple(item["nameHints"]), tag_hints=tuple(item["tagHints"]),
    ) for item in DATA["categories"])

BY_KEY: dict[str, Category] = {category.key: category for category in CATEGORIES}
# The POI runtime's own spelling for a category, where it differs.
ALIASES: dict[str, str] = {c.key: c.poi_name for c in CATEGORIES if c.poi_name}
# Which categories that runtime collects at all.
POI_RUNTIME: tuple[str, ...] = tuple(c.key for c in CATEGORIES if c.poi_runtime)

EXCLUSIONS: tuple[str, ...] = tuple(DATA["excluded"])
NON_BUSINESS_PARENT_TAGS: frozenset[str] = frozenset(DATA["nonBusinessParentTags"])
CONFLICTING_TAGS: frozenset[str] = frozenset(DATA["conflictingTags"])
REVIEW_TAGS: tuple[str, ...] = tuple(DATA["review"])
POSSIBLE_DUPLICATE_METERS: float = DATA["possibleDuplicateMeters"]


def keys() -> tuple[str, ...]:
    return tuple(category.key for category in CATEGORIES)


def queries(key: str) -> tuple[str, ...]:
    return BY_KEY[key].queries()


def majors() -> tuple[str, ...]:
    """Major categories in the order their first minor category appears."""
    return tuple(dict.fromkeys(category.major for category in CATEGORIES))


def minors_of(major: str) -> tuple[str, ...]:
    return tuple(category.key for category in CATEGORIES if category.major == major)


def poi_key(key: str) -> str:
    """The POI runtime's spelling of a category; every other name is unchanged."""
    return ALIASES.get(key, key)


def _mapping(selector) -> dict:
    return {poi_key(category.key): selector(category) for category in CATEGORIES
            if category.poi_runtime}


# The POI runtime's view: only the categories it collects, under its own names,
# in the shape its planner and normalizer already read.
POI_RULES: dict = {
    "version": VERSION,
    "queries": _mapping(lambda c: list(c.queries())),
    "supportedTags": _mapping(lambda c: list(c.tag_hints)),
    "nameHints": _mapping(lambda c: list(c.name_hints)),
    "excluded": list(EXCLUSIONS),
    "nonBusinessParentTags": sorted(NON_BUSINESS_PARENT_TAGS),
    "conflictingTags": sorted(CONFLICTING_TAGS),
    "review": list(REVIEW_TAGS),
    "possibleDuplicateMeters": POSSIBLE_DUPLICATE_METERS,
}


def poi_rules() -> dict:
    return POI_RULES
