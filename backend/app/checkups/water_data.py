"""水系数据证据：这一版的水体障碍来自哪里、复核过哪一片、哪里仍然冲突。

热力与灰区用 OSM 水体当障碍；百度底图上画的水面只是底图，并不参与计算。两者对不上
的地方（国定一社区的虬江就是一例：百度把河道画在实际河道以北几十到近两百米）如果
不说清楚，读者会把底图水面下的覆盖读成"水面上有服务"，或把真实河道上的未知读成
"算错了"。这里把三件事一起写进修订：

* 计算用的是哪一份数据（OSM 提取版本与 PBF 摘要）以及哪一份复核（``id@version``）；
* 评估域里多少面积被复核覆盖、多少仍是 OSM 原样；
* 复核认定的内容（实测河宽、补录水体、来源冲突、底图画错的水面、已核实的桥梁），
  几何转成 bd09ll，供地图按原样绘制。
"""
from shapely import make_valid
from shapely.geometry import shape
from shapely.ops import transform

from ..geo.coordinates import wgs84_to_bd09
from .models import WaterDataEvidence

#: 复核文件里原样带到修订中的说明字段。
REVIEW_META = ("reviewId", "version", "title", "reviewedAt", "scope", "appliesTo", "sources",
               "method", "limitations")


def _metric(projection, geometry):
    return transform(projection.forward.transform, shape(geometry))


def _display(projection, geometry):
    """米制几何 → bd09ll；空几何或转换失败都不画，而不是画一个错的形状。"""
    if geometry is None or geometry.is_empty:
        return None
    try:
        return projection.public_geometry(make_valid(geometry), repair_roundoff=True)
    except ValueError:
        return None


def _anchor(lnglat):
    """复核文件的标注点（WGS84）→ bd09ll。"""
    lng, lat = wgs84_to_bd09(*lnglat)
    return {"lng": lng, "lat": lat}


def _items(projection, rows, extent, keep):
    """复核条目 → 修订里的条目：元数据原样，几何裁到载入范围并转成 bd09ll。"""
    items = []
    for geometry, item in rows:
        clipped = geometry.intersection(extent) if extent is not None else geometry
        if clipped.is_empty:
            continue
        items.append({**{k: item[k] for k in keep if k in item},
                      "anchor": _anchor(item["anchor"]) if item.get("anchor") else None,
                      "geometry": _display(projection, clipped)})
    return items


def _review_payload(review, local, projection, extent):
    payload = review.payload
    reaches = []
    for label, osm_id, width, surface in local.reach_items:
        if label != review.label:
            continue
        meta = next((r for r in payload.get("reaches", []) if int(r.get("osmId", -1)) == osm_id), {})
        reaches.append({"osmId": osm_id, "name": meta.get("name"), "widthM": width,
                        "osmWidthTag": meta.get("osmWidthTag"), "status": meta.get("status"),
                        "measurement": meta.get("measurement"), "baiduOffset": meta.get("baiduOffset"),
                        "note": meta.get("note"), "geometry": _display(projection, surface)})
    crossings = []
    for item in payload.get("crossings", []):
        geometry = _metric(projection, item["geometry"]) if item.get("geometry") else None
        if geometry is not None and extent is not None and not geometry.intersects(extent):
            continue
        crossings.append({**{k: item[k] for k in ("osmId", "highway", "river", "lengthM", "status", "note")
                             if k in item},
                          "anchor": _anchor(item["anchor"]) if item.get("anchor") else None,
                          "geometry": _display(projection, geometry)})
    return {**{k: payload[k] for k in REVIEW_META if k in payload}, "label": review.label,
            "extent": _display(projection, review.extent),
            "reaches": reaches, "crossings": crossings,
            "supplements": _items(projection, review.supplements, extent,
                                  ("id", "status", "areaM2", "sources", "note")),
            "conflicts": _items(projection, review.conflicts, extent,
                                ("id", "status", "areaM2", "sources", "note")),
            "basemapMisdrawn": _items(projection, review.misdrawn, extent,
                                      ("id", "kind", "verifiedAs", "areaM2", "note"))}


def _hectares(area_m2: float) -> str:
    return f"{area_m2 / 10_000:.2f} 公顷"


def water_evidence(local, domain, projection) -> WaterDataEvidence | None:
    """一版修订的水系证据。``local`` 为 None（没有载入障碍层）时不出这一栏。"""
    if local is None:
        return None
    source = local.source or {}
    available = local.water is not None and not local.water.is_empty
    extent = local.extent
    domain_area = domain.area if domain is not None else None
    reviewed = local.reviewed.intersection(domain).area if domain is not None else 0.0
    conflict = local.conflicts.intersection(domain).area if domain is not None else 0.0
    reviews = [_review_payload(review, local, projection, extent) for review in local.reviews]
    rejected = list(source.get("water_reviews_rejected") or [])
    osm = source.get("osm_data_version")
    statements = []
    if not available:
        statements.append("水体障碍层不可用：这一版的格没有按水体判定，跨河两侧可能被当成连通。")
    else:
        statements.append(f"水体障碍来自 OpenStreetMap（{osm}）；百度底图上的水面只作底图显示，"
                          "不参与计算，两者位置可能不一致。")
    for review in reviews:
        share = reviewed / domain_area if domain_area else 0.0
        statements.append(f"评估域 {share:.0%} 位于水系复核「{review.get('title')}」（{review['label']}）"
                          "范围内：河道位置、河宽、补录水体与跨河桥梁按独立影像与第二家地图核对后计算。")
        misdrawn = review["basemapMisdrawn"]
        if misdrawn:
            statements.append(f"复核范围内百度底图有 {len(misdrawn)} 处水面经核实为陆地（河道错位、场地误绘），"
                              "按陆地计算；这些地方底图显示的水面不代表计算结果。")
    if domain_area is not None and reviews and domain_area - reviewed > 1:
        statements.append(f"评估域其余 {_hectares(domain_area - reviewed)} 未经复核，水系按 OSM 原样计算。")
    if conflict > 0:
        names = "、".join(item["id"] for review in reviews for item in review["conflicts"])
        statements.append(f"评估域内约 {round(conflict)} 平方米来源互相矛盾且未能裁决（{names}），"
                          "这些格显示为“数据冲突／未知”，不计为覆盖，也不计为灰区。")
    for name in rejected:
        statements.append(f"水系复核文件 {name} 与当前 OSM 数据版本不符，未采用；它覆盖的范围按 OSM 原样计算。")
    return WaterDataEvidence(
        obstacle_layer_available=available, osm_data_version=osm,
        source_pbf_sha256=source.get("source_pbf_sha256"), reviews=reviews, rejected_reviews=rejected,
        domain_area_m2=domain_area, reviewed_area_m2=reviewed,
        unreviewed_area_m2=None if domain_area is None else max(0.0, domain_area - reviewed),
        conflict_area_m2=conflict, statements=statements)
