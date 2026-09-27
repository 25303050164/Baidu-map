"""热力图障碍验收的数据侧：存档任务评估域里的硬障碍（水体），以及压在水面上的评估格。

只读存档、不联网、不花额度。输出：

- 水面与评估域的交集（bd09ll），供前端验收在真实底图上找判读点；
- 压在水面上的格按（类别, 格边长, 结论）的计数 —— 后端把这些格加密并判"未知"，前端只负责
  把"未知"画出来、不让相邻格的覆盖色渗过去；
- 评估域周围的 OSM 水体原样（bd09ll，带 osm_id 与名称），供前端把它叠到百度底图上，对照
  两边的水系是否一致（`HEAT_OSM_WATER`）。
"""
import argparse
import collections
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shapely.geometry import box
from app.algorithms.hybrid_isochrone.hard_obstacles import ObstacleIndex, yes
from app.checkups.accessibility_stage import OBSTACLE_MARGIN_M, metric_region, public_geometry
from app.config import load_settings
from app.geo.projection import MetricProjection


def nearby_water(index: ObstacleIndex, projection, domain, window) -> list[dict]:
    """窗口里的地表水体，逐个要素：几何裁到窗口，另记进入评估域的长度（线）或面积（面）。"""
    out = []
    for i in sorted(map(int, index.index.query(window, predicate="intersects"))):
        geometry, props = index.rows[i]
        underground = yes(props.get("tunnel")) or props.get("location") == "underground" or yes(props.get("covered"))
        if props.get("kind") != "water" or underground:
            continue
        line = geometry.geom_type in ("LineString", "MultiLineString")
        inside = geometry.intersection(domain)
        out.append({
            "osmId": props.get("osm_id"), "name": props.get("name"), "width": props.get("width"),
            "inDomain": round(inside.length if line else inside.area, 1),
            "inDomainUnit": "m" if line else "m2",
            "distanceToDomainM": round(geometry.distance(domain), 1),
            "geometry": public_geometry(projection, geometry.intersection(window)),
        })
    return out


def scan(revision: dict, settings, projection, index: ObstacleIndex) -> dict:
    heat = revision["heatmap"]
    domain = metric_region(heat["domain"], projection)
    window = domain.envelope.buffer(OBSTACLE_MARGIN_M)
    # 与体检流水线同一个入口（load_obstacles 只多一层缓存与异常降级）。
    local = index.local(window)
    water = local.water.intersection(domain)
    parts = list(getattr(water, "geoms", [water])) if not water.is_empty else []
    cells = collections.Counter()
    for category, items in heat["categories"].items():
        for item in items:
            level = int(item["cell"].split(":")[0])
            size = heat["stepM"] / 2 ** level
            x, y = projection.origin((item["lng"], item["lat"]))
            if box(x - size / 2, y - size / 2, x + size / 2, y + size / 2).intersects(local.water):
                cells[f"{category}/{size:g}m/{item['status']}"] += 1
    return {
        "osmDataVersion": settings.osm_data_version,
        "warnings": list(local.warnings),
        "unresolvedWaterLines": len(local.unresolved_lines),
        "waterInDomainM2": round(water.area, 1),
        "waterParts": sorted((round(part.area, 1) for part in parts), reverse=True),
        "cellsOnWater": dict(sorted(cells.items())),
        "water": public_geometry(projection, water) if not water.is_empty else None,
        "nearbyWater": nearby_water(index, projection, domain, window),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkup-dir", type=Path, default=Path("../.tmp/accept-0928/checkups"))
    parser.add_argument("--task", action="append", metavar="SLUG=TASK_ID", required=True,
                        help="例如 e82=f6128318-...；可重复")
    parser.add_argument("--revision", default="revision-0005-reporting.json")
    parser.add_argument("--output", type=Path, default=Path("../.tmp/heat-obstacle-scan.json"))
    args = parser.parse_args()
    settings = load_settings()
    projection = MetricProjection(settings.osm_metric_crs)
    index = ObstacleIndex(json.loads(Path(settings.hybrid_obstacle_path).read_text(encoding="utf-8")),
                          projection, settings.osm_data_version)
    result = {}
    for pair in args.task:
        slug, task_id = pair.split("=", 1)
        path = args.checkup_dir / "tasks" / task_id / args.revision
        result[slug] = {"taskId": task_id, "revision": args.revision,
                        **scan(json.loads(path.read_text(encoding="utf-8")), settings, projection, index)}
        summary = {k: v for k, v in result[slug].items() if k not in ("water", "nearbyWater")}
        summary["nearbyWater"] = [{k: v for k, v in w.items() if k != "geometry"}
                                  for w in result[slug]["nearbyWater"] if w["name"] or w["inDomain"] > 0]
        print(slug, json.dumps(summary, ensure_ascii=False))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
