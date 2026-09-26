/**
 * v2 体检地图：把一版修订的图层画上去。
 *
 * 与旧分析地图共用同一套覆盖物记账（`replaceGroup`）和同一份聚合逻辑，理由也一样：
 *
 * - **不用 `clearOverlays()`**：它会把热力图、别的图层连同用户正在看的东西一起摘掉；
 * - **视角只在"新结果"或"显式定位"时移动**：勾选图层、刷新数据都不该把用户正在看的地方拽走；
 * - **同格合并，绝不截断**：几百处设施逐点铺满会互相遮盖，只画前 N 条却是说谎 ——
 *   被丢掉的设施在图上完全看不出来。
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import type { Center } from '../types';
import { useBaiduMap } from '../map/useBaiduMap';
import type { BMapIcon, BMapMap, BMapOverlay, BMapViewEventType } from '../map/baiduMapTypes';
import { createDotIcon } from '../map/mapIcons';
import { drawGeometry } from '../analysis/geometry';
import { aggregateByCell, FACILITY_CLUSTER_CELL_PX } from '../map/layers/aggregate';
import { LAYER_STYLES, type LayerDrawable, type LayerPoint } from './layers';
import type { LayerId } from './validate';

export type CheckupLayerToggles = Partial<Record<LayerId, boolean>>;

/** 默认打开的是"结论"层；热力与核验是证据层，按需打开。 */
export const DEFAULT_CHECKUP_LAYERS: Record<LayerId, boolean> = {
  isochrone: true, accessibility: true, service_gaps: true, facilities: true,
  heatmap: false, verification: true, report: false,
};

type Group = 'vector' | 'point' | 'label';

const VIEW_EVENTS: BMapViewEventType[] = ['moveend', 'zoomend', 'resize'];
/** 点图层的绘制顺序：面积在前、点位在后，后画的压在上面。 */
const POINT_LAYERS: LayerId[] = ['heatmap', 'verification', 'facilities'];

function dominant(points: LayerPoint[]): LayerPoint {
  const counts = new Map<string, number>();
  for (const point of points) counts.set(point.color, (counts.get(point.color) ?? 0) + 1);
  let best = points[0];
  for (const point of points) if ((counts.get(point.color) ?? 0) > (counts.get(best.color) ?? 0)) best = point;
  return best;
}

export function CheckupMap({ center, onPick, resultCenter, layers, drawables, selectedId, onSelect }: {
  center: Center;
  onPick: (center: Center) => void;
  /** 已发布那一版修订的中心点；与选点分开，避免把"待分析选点"当成"结果中心"。 */
  resultCenter?: Center;
  layers: CheckupLayerToggles;
  drawables: Partial<Record<LayerId, LayerDrawable>>;
  selectedId?: string | null;
  onSelect?: (id: string) => void;
}) {
  const { api, mode, failureReason } = useBaiduMap();
  const container = useRef<HTMLDivElement>(null);
  const [map, setMap] = useState<BMapMap | null>(null);
  const [error, setError] = useState(false);
  const [viewTick, setViewTick] = useState(0);
  const pick = useRef(onPick);
  pick.current = onPick;
  const select = useRef(onSelect);
  select.current = onSelect;
  const initialCenter = useRef(center);
  initialCenter.current = center;
  const overlays = useRef<Record<Group, BMapOverlay[]>>({ vector: [], point: [], label: [] });

  const replaceGroup = (instance: BMapMap, group: Group, build: () => BMapOverlay[]) => {
    for (const overlay of overlays.current[group]) instance.removeOverlay?.(overlay);
    overlays.current[group] = build();
  };

  useEffect(() => {
    if (!api || !container.current) return;
    let instance: BMapMap | undefined;
    const el = container.current;
    const frame = requestAnimationFrame(() => {
      try {
        instance = new api.Map(el);
        instance.centerAndZoom(new api.Point(initialCenter.current.lng, initialCenter.current.lat), 15);
        instance.enableScrollWheelZoom(true);
        instance.addEventListener('click', event => pick.current({
          lng: +event.latlng.lng.toFixed(6), lat: +event.latlng.lat.toFixed(6) }));
        setMap(instance);
      } catch { setError(true); }
    });
    return () => { cancelAnimationFrame(frame);
      overlays.current = { vector: [], point: [], label: [] }; instance?.destroy?.(); };
  }, [api]);

  useEffect(() => {
    if (!map) return;
    let frame = 0;
    const onView = () => { cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => setViewTick(tick => tick + 1)); };
    for (const type of VIEW_EVENTS) map.addEventListener(type, onView);
    return () => { cancelAnimationFrame(frame);
      for (const type of VIEW_EVENTS) map.removeEventListener?.(type, onView); };
  }, [map]);

  // 面：等时圈、评估域与灰区。每次重绘只摘本组自己加进去的覆盖物。
  useEffect(() => {
    const instance = map;
    if (!instance || !api) return;
    try {
      replaceGroup(instance, 'vector', () => {
        const created: BMapOverlay[] = [];
        for (const id of ['isochrone', 'accessibility', 'service_gaps'] as LayerId[]) {
          const drawable = drawables[id];
          if (!layers[id] || !drawable) continue;
          for (const shape of drawable.shapes) {
            created.push(...drawGeometry(instance, api, shape.geometry, {
              strokeColor: shape.style.strokeColor, fillColor: shape.style.fillColor,
              fillOpacity: shape.style.fillOpacity, strokeWeight: shape.style.strokeWeight,
              ...(shape.style.strokeStyle ? { strokeStyle: shape.style.strokeStyle } : {}),
            }));
          }
        }
        return created;
      });
    } catch { setError(true); }
  }, [api, map, layers, drawables]);

  // 点：设施、热力采样、核验记录。按屏幕像素分格合并，数量写在标记上，一条也不丢。
  useEffect(() => {
    const instance = map;
    if (!instance || !api) return;
    try {
      replaceGroup(instance, 'point', () => {
        const created: BMapOverlay[] = [];
        for (const id of POINT_LAYERS) {
          const drawable = drawables[id];
          if (!layers[id] || !drawable || drawable.points.length === 0) continue;
          const aggregation = aggregateByCell(drawable.points.map(item => ({
            id: item.key, lng: item.lng, lat: item.lat, point: item,
          })), { cellPx: FACILITY_CLUSTER_CELL_PX,
            project: (lng, lat) => instance.pointToOverlayPixel(new api.Point(lng, lat)) });
          for (const cell of aggregation.cells) {
            const primary = cell.count > 1 ? dominant(cell.items.map(item => item.point))
              : cell.items[0].point;
            const selected = cell.items.some(item => item.id === selectedId);
            const icon = createDotIcon(api, {
              color: primary.color,
              text: cell.count > 1 ? String(cell.count) : undefined,
              filled: cell.count > 1 || selected,
            });
            const marker = new api.Marker(new api.Point(cell.lng, cell.lat), {
              title: cell.count > 1
                ? `${cell.count} 个点（${LAYER_STYLES[id].label}，点击查看其中一个）`
                : primary.title,
              ...(icon ? { icon } : {}),
            });
            marker.addEventListener('click', () => select.current?.(primary.key));
            instance.addOverlay(marker); created.push(marker);
          }
        }
        return created;
      });
    } catch { setError(true); }
  }, [api, map, layers, drawables, selectedId, viewTick]);

  // 中心标记独立成组：平移、勾选图层都不会让它跟着重建。
  useEffect(() => {
    const instance = map;
    if (!instance || !api) return;
    try {
      replaceGroup(instance, 'label', () => {
        const created: BMapOverlay[] = [];
        const marks: Array<[number, number, string]> = [];
        if (resultCenter) marks.push([resultCenter.lng, resultCenter.lat, '本次体检中心（与报告一致）']);
        if (!resultCenter || center.lng !== resultCenter.lng || center.lat !== resultCenter.lat) {
          marks.push([center.lng, center.lat, '待体检选点（BD09LL）']);
        }
        for (const [lng, lat, title] of marks) {
          const marker = new api.Marker(new api.Point(lng, lat), { title });
          instance.addOverlay(marker); created.push(marker);
        }
        return created;
      });
    } catch { setError(true); }
  }, [api, map, center, resultCenter]);

  // 视角只在"新结果"或"显式选中某处设施"时移动。建图时已经放在初值上，初值不算一次移动。
  const pannedTo = useRef<string | null>(`${initialCenter.current.lng},${initialCenter.current.lat}`);
  const pannedSelection = useRef<string | null>(null);
  useEffect(() => {
    const instance = map;
    if (!instance || !api) return;
    const target = resultCenter ?? center;
    const key = `${target.lng},${target.lat}`;
    const selectionKey = resultCenter ? selectedId ?? null : null;
    const movedToSelection = selectionKey !== null && selectionKey !== pannedSelection.current;
    pannedSelection.current = selectionKey;
    if (pannedTo.current === key && !movedToSelection) return;
    pannedTo.current = key;
    const focus = movedToSelection
      ? Object.values(drawables).flatMap(drawable => drawable?.points ?? [])
        .find(item => item.key === selectionKey) : undefined;
    try {
      instance.panTo(focus ? new api.Point(focus.lng, focus.lat)
        : new api.Point(target.lng, target.lat));
    } catch { setError(true); }
  }, [api, map, center, resultCenter, selectedId, drawables]);

  const legend = useMemo(() => POINT_LAYERS
    .flatMap(id => (drawables[id]?.points ?? []).map(point => ({ id, color: point.color,
      title: point.title })))
    .filter(item => layers[item.id]), [drawables, layers]);
  const unavailable = error || mode === 'fallback';
  return <div className="api-map-shell">
    <div ref={container} className="api-map" data-testid="checkup-map" aria-label="体检图层地图" />
    {(unavailable || mode === 'loading') && <div className="api-map-notice" role="status">
      <strong>{unavailable ? '地图不可用' : '正在加载百度地图'}</strong>
      <p>{unavailable ? (failureReason === 'missing-key'
        ? '尚未配置浏览器地图密钥，请联系项目管理员完成地图配置。仍可输入坐标、执行体检和查看报告。'
        : '百度地图脚本未能加载，请检查网络及浏览器地图密钥的权限和来源限制，修复后刷新页面。')
        : '地图就绪后可点击选择体检中心。'}</p>
    </div>}
    <div className="api-map-left">
      <div className="api-map-caption">百度坐标 BD09LL · 点击地图选点
        {resultCenter && <><br />图层与报告中心：{resultCenter.lng.toFixed(6)}, {resultCenter.lat.toFixed(6)}</>}
      </div>
    </div>
    {legend.length > 0 && <div className="api-map-legend" data-testid="checkup-legend" aria-label="体检图层图例">
      {Object.entries(LAYER_STYLES).filter(([id]) => layers[id as LayerId])
        .map(([id, style]) => <span key={id} className="api-legend-item">
          <i className="api-legend-dot" style={{ background: style.fillColor }} />{style.label}</span>)}
      {legend.length > 1 && <span className="api-legend-item">共 {legend.length} 个点，同格合并显示，数据不截断</span>}
    </div>}
  </div>;
}
