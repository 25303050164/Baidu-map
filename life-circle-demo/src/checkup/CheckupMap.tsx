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
import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import type { Center } from '../types';
import { useBaiduMap } from '../map/useBaiduMap';
import type { BMapMap, BMapOverlay, BMapViewEventType } from '../map/baiduMapTypes';
import { createDotIcon } from '../map/mapIcons';
import { drawGeometry } from '../analysis/geometry';
import { aggregateByCell, FACILITY_CLUSTER_CELL_PX } from '../map/layers/aggregate';
import { createDensityOverlay, type DensityOverlay } from '../map/layers/heatmapOverlay';
import {
  DENSITY_SCALE_MAX, DENSITY_UNIT, HEAT_KERNEL_RADIUS_M, SINGLE_FACILITY_PEAK, densityLegendCss,
} from '../map/layers/density';
import { createServiceOverlay, type ServiceOverlay } from '../map/layers/serviceOverlay';
import {
  SERVICE_COMPOSITE, SERVICE_DISTANCE_MAX_M, SERVICE_GAP_RGB, SERVICE_SCORE_MAX, SERVICE_UNKNOWN_RGB,
  serviceRampCss,
} from '../map/layers/serviceField';
import {
  DENSITY_ALL, LAYER_STYLES, POINT_COLORS, densityFacilities, type LayerDrawable, type LayerPoint, type ServiceSamples,
} from './layers';
import { createWaterOverlay, type WaterAnnotation, type WaterOverlay } from '../map/layers/waterOverlay';
import { CATEGORY_COLORS, CATEGORY_ORDER, categoryLabel } from './report';
import type { LayerId } from './validate';
import { CROSSING_COLOR, WATER_KINDS, WATER_STYLES, type WaterKind, type WaterStyle, type WaterView } from './water';

/** 两种热力（§8.1）：服务覆盖与设施密度互斥，同一时刻只开一种。 */
export type HeatLayer = 'service' | 'density';
/** 水系标注不是后端的图层，是修订里水系证据的翻译；与热力并存，不互斥。 */
export type CheckupLayerToggles = Partial<Record<LayerId | HeatLayer | 'water', boolean>>;

/**
 * 默认显示圈面、服务覆盖热力和核验；模型网格采样按需打开。
 * 服务覆盖是体检的结论本身；设施密度只说"设施扎不扎堆"，不说覆盖，所以不默认打开。
 */
export const DEFAULT_CHECKUP_LAYERS: Record<LayerId | HeatLayer | 'water', boolean> = {
  isochrone: true, accessibility: true, service_gaps: true, facilities: true,
  heatmap: false, verification: true, report: false, density: false, service: true, water: true,
};

/** 面内标字：只标会被读错的那几类。河道本身有底图注记，范围只是虚线框，不再加字。 */
const WATER_MAP_LABELS: Partial<Record<WaterKind, string>> = {
  conflict: '数据冲突／未知', misdrawn: '底图水面有误·实为陆地', supplement: '补录水体',
};

/** 图例色块与画布同一套样式：斜线、底色、虚实边都一致。 */
function waterSwatch(style: WaterStyle) {
  return {
    display: 'inline-block', width: 18, height: 12, boxSizing: 'border-box' as const,
    border: `2px ${style.dash.length > 0 ? 'dashed' : 'solid'} ${style.stroke}`,
    background: style.hatch
      ? `repeating-linear-gradient(135deg, ${style.hatch} 0 1.5px, ${style.fill ?? 'transparent'} 1.5px 5px)`
      : style.fill ?? 'transparent',
  };
}

const rgbCss = ([r, g, b]: readonly number[]) => `rgb(${r}, ${g}, ${b})`;

/** 有面积的图层画成方块（填色与描边同地图），点图层画成圆点。 */
const AREA_LAYERS = new Set<LayerId>(['isochrone', 'accessibility', 'service_gaps']);

function alpha(hex: string, opacity: number): string {
  const value = Number.parseInt(hex.slice(1), 16);
  return `rgba(${(value >> 16) & 255}, ${(value >> 8) & 255}, ${value & 255}, ${opacity})`;
}

/**
 * 图层色块：面板开关与地图图例共用。点图层按属性着色，所以色块画的是它实际会用到的
 * 几种颜色，而不是样式表里那个兜底灰。
 */
export function layerSwatch(id: LayerId): CSSProperties {
  const style = LAYER_STYLES[id];
  if (AREA_LAYERS.has(id)) {
    return { background: alpha(style.fillColor, Math.max(style.fillOpacity, 0)),
      boxShadow: 'none', outline: `1.5px ${style.strokeStyle === 'dashed' ? 'dashed' : 'solid'} ${style.strokeColor}`,
      outlineOffset: -1.5, borderRadius: 1 };
  }
  const colors = id === 'facilities' ? CATEGORY_ORDER.map(category => CATEGORY_COLORS[category])
    : id === 'heatmap' ? [POINT_COLORS.covered, POINT_COLORS.gap, POINT_COLORS.unknown]
      : id === 'verification' ? [POINT_COLORS.verified_reachable, POINT_COLORS.verified_unreachable]
        : [style.fillColor];
  const step = 100 / colors.length;
  return { background: colors.length === 1 ? colors[0] : `conic-gradient(${colors
    .map((color, index) => `${color} ${index * step}% ${(index + 1) * step}%`).join(', ')})` };
}

type Group = 'vector' | 'point' | 'label' | 'water';

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

export function CheckupMap({ center, onPick, resultCenter, layers, drawables, coverage = null,
  serviceMode = SERVICE_COMPOSITE, densityCategory = DENSITY_ALL, water = null, selectedId, onSelect }: {
  center: Center;
  onPick: (center: Center) => void;
  /** 已发布那一版修订的中心点；与选点分开，避免把"待分析选点"当成"结果中心"。 */
  resultCenter?: Center;
  layers: CheckupLayerToggles;
  drawables: Partial<Record<LayerId, LayerDrawable>>;
  /** 这一版的模型网格（服务覆盖热力的输入）；null 表示还没取到。 */
  coverage?: ServiceSamples | null;
  /** 服务覆盖热力看哪一类；综合要求三类都已知。 */
  serviceMode?: string;
  /** 设施密度看哪一类；默认三类合算。 */
  densityCategory?: string;
  /** 这一版的水系标注；null 表示这一版没有水系证据。 */
  water?: WaterView | null;
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
  const overlays = useRef<Record<Group, BMapOverlay[]>>({ vector: [], point: [], label: [], water: [] });
  const density = useRef<DensityOverlay | null>(null);
  const serviceHeat = useRef<ServiceOverlay | null>(null);
  const waterMarks = useRef<WaterOverlay | null>(null);

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
      overlays.current = { vector: [], point: [], label: [], water: [] }; instance?.destroy?.(); };
  }, [api]);

  useEffect(() => {
    if (!api || !map) return;
    const overlay = createDensityOverlay(api, {
      viewport: () => ({ width: container.current?.clientWidth ?? 0,
        height: container.current?.clientHeight ?? 0 }),
    });
    density.current = overlay;
    return () => { overlay?.destroy(); density.current = null; };
  }, [api, map]);

  // v2 facilities 仅包含按计算圈面接收的设施；隔离项与圈外记录不在该图层。
  // 采用原始设施而非聚合标记或模型网格；疑似重复组合并、类别筛选在 densityFacilities。
  const densityInput = useMemo(() => densityFacilities(drawables.facilities, densityCategory),
    [drawables.facilities, densityCategory]);

  useEffect(() => {
    const overlay = density.current;
    if (!overlay || !map) return;
    overlay.setFacilities(densityInput.points);
    overlay.setBoundary(drawables.isochrone?.shapes[0]?.geometry ?? null);
    if (layers.density) overlay.attach(map); else overlay.detach();
  }, [api, map, layers.density, densityInput, drawables.isochrone]);

  useEffect(() => {
    if (!api || !map) return;
    const overlay = createServiceOverlay(api, {
      categories: CATEGORY_ORDER,
      viewport: () => ({ width: container.current?.clientWidth ?? 0,
        height: container.current?.clientHeight ?? 0 }),
    });
    serviceHeat.current = overlay;
    return () => { overlay?.destroy(); serviceHeat.current = null; };
  }, [api, map]);

  useEffect(() => {
    const overlay = serviceHeat.current;
    if (!overlay || !map) return;
    // 画的是后端的评估格本身（结论与模型距离），不是设施点的核叠加；
    // 评估域给出"域内无格即未知"，计算圈给出裁剪。
    overlay.setSamples(coverage?.samples ?? [], coverage?.domain ?? null);
    overlay.setMode(serviceMode);
    overlay.setBoundary(drawables.isochrone?.shapes[0]?.geometry ?? null);
    if (layers.service) overlay.attach(map); else overlay.detach();
  }, [api, map, layers.service, coverage, serviceMode, drawables.isochrone]);

  useEffect(() => {
    if (!api || !map) return;
    const overlay = createWaterOverlay(api, {
      viewport: () => ({ width: container.current?.clientWidth ?? 0,
        height: container.current?.clientHeight ?? 0 }),
    });
    waterMarks.current = overlay;
    return () => { overlay?.destroy(); waterMarks.current = null; };
  }, [api, map]);

  // 水系标注按后端给的几何原样画：不平移、不按底图对齐（偏离沿河变化，统一平移只会在
  // 别处造出新的错位）。桥梁是点，走 SDK 标记，和设施点一样能悬停看说明。
  const waterAnnotations = useMemo<WaterAnnotation[]>(() => (water?.shapes ?? []).map(shape => ({
    key: shape.key, geometry: shape.geometry, style: WATER_STYLES[shape.kind],
    label: shape.anchor && WATER_MAP_LABELS[shape.kind]
      ? { text: WATER_MAP_LABELS[shape.kind] as string, ...shape.anchor } : null,
  })), [water]);
  useEffect(() => {
    const overlay = waterMarks.current;
    if (!overlay || !map) return;
    overlay.setAnnotations(waterAnnotations);
    if (layers.water && waterAnnotations.length > 0) overlay.attach(map); else overlay.detach();
  }, [api, map, layers.water, waterAnnotations]);

  useEffect(() => {
    const instance = map;
    if (!instance || !api) return;
    try {
      replaceGroup(instance, 'water', () => {
        if (!layers.water) return [];
        return (water?.crossings ?? []).map(mark => {
          const icon = createDotIcon(api, { color: CROSSING_COLOR, text: '桥', filled: true });
          const marker = new api.Marker(new api.Point(mark.lng, mark.lat), {
            title: mark.title, ...(icon ? { icon } : {}) });
          instance.addOverlay(marker);
          return marker;
        });
      });
    } catch { setError(true); }
  }, [api, map, layers.water, water]);

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
  //
  // 开着热力时圈面只描边：BMapGL 的面画在 WebGL 底层，热力画布在其上的 DOM 容器里，
  // 面填色不会盖住热力，却会透过半透明热力把整圈染成一片青绿，渐变就看不出来了。
  // 服务覆盖热力自己就把缺口画成灰，所以灰区此时也只留边线和编号对应的轮廓。
  const heatOn = !!(layers.service || layers.density);
  useEffect(() => {
    const instance = map;
    if (!instance || !api) return;
    const outlineOnly = new Set<LayerId>(heatOn ? ['isochrone'] : []);
    if (layers.service) outlineOnly.add('service_gaps');
    try {
      replaceGroup(instance, 'vector', () => {
        const created: BMapOverlay[] = [];
        for (const id of ['isochrone', 'accessibility', 'service_gaps'] as LayerId[]) {
          const drawable = drawables[id];
          if (!layers[id] || !drawable) continue;
          for (const shape of drawable.shapes) {
            created.push(...drawGeometry(instance, api, shape.geometry, {
              strokeColor: shape.style.strokeColor, fillColor: shape.style.fillColor,
              fillOpacity: outlineOnly.has(id) ? 0 : shape.style.fillOpacity,
              strokeWeight: shape.style.strokeWeight,
              ...(shape.style.strokeStyle ? { strokeStyle: shape.style.strokeStyle } : {}),
            }));
          }
        }
        return created;
      });
    } catch { setError(true); }
  }, [api, map, layers.isochrone, layers.accessibility, layers.service_gaps, heatOn, layers.service,
    drawables.isochrone, drawables.accessibility, drawables.service_gaps]);

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
  }, [api, map, layers.heatmap, layers.verification, layers.facilities,
    drawables.heatmap, drawables.verification, drawables.facilities, selectedId, viewTick]);

  // 中心标记独立成组：平移、勾选图层都不会让它跟着重建。
  useEffect(() => {
    const instance = map;
    if (!instance || !api) return;
    try {
      replaceGroup(instance, 'label', () => {
        const created: BMapOverlay[] = [];
        // 结果中心用墨色，尚未体检的新选点用印章红：两者同时出现时一眼分得开。
        const marks: Array<[number, number, string, string]> = [];
        if (resultCenter) marks.push([resultCenter.lng, resultCenter.lat, '本次体检中心（与报告一致）', '#1d2327']);
        if (!resultCenter || center.lng !== resultCenter.lng || center.lat !== resultCenter.lat) {
          marks.push([center.lng, center.lat, '待体检选点（BD09LL）', '#b3372c']);
        }
        for (const [lng, lat, title, color] of marks) {
          const icon = createDotIcon(api, { color, layered: true });
          const marker = new api.Marker(new api.Point(lng, lat), { title, ...(icon ? { icon } : {}) });
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
  const waterOn = !!(layers.water && water?.available
    && (water.shapes.length > 0 || water.crossings.length > 0));
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
      <div className="api-map-caption">点击地图选点 · BD09LL
        {resultCenter && <><br />图层与报告中心 <b>{resultCenter.lng.toFixed(6)}, {resultCenter.lat.toFixed(6)}</b></>}
      </div>
    </div>
    {/* 还没有任何一层画上去时不出图例：空图上的一串色块只会让人以为已经有结论。 */}
    {Object.values(drawables).some(Boolean) && (legend.length > 0 || heatOn || waterOn)
      && <div className="api-map-legend" data-testid="checkup-legend" aria-label="体检图层图例">
      {layers.service && <span className="api-legend-item api-legend-block" data-testid="service-legend">
        {api && !api.Overlay ? '当前地图不支持服务覆盖热力' : <>
          <span className="api-legend-line">
            <i className="api-legend-ramp" style={{ background: serviceRampCss(serviceMode) }} />
            {serviceMode === SERVICE_COMPOSITE
              ? `三类均已知处覆盖类别占比 0–${SERVICE_SCORE_MAX}%`
              : `${categoryLabel(serviceMode)}：已覆盖处最近设施步行 0–${SERVICE_DISTANCE_MAX_M} 米`}</span>
          <span className="api-legend-line">
            <i className="api-legend-dot" style={{ background: rgbCss(SERVICE_GAP_RGB) }} />服务不足
            <i className="api-legend-dot" style={{ background: rgbCss(SERVICE_UNKNOWN_RGB) }} />数据未知
            <span className="api-legend-note">· 模型估计，不是实测
              {coverage === null && ' · 等待模型网格'}
              {coverage !== null && coverage.samples.length === 0 && ' · 本次没有可绘制的网格'}
              {coverage !== null && coverage.dropped > 0 && ` · ${coverage.dropped} 个格数据不全未绘制`}</span>
          </span>
        </>}
      </span>}
      {layers.density && <span className="api-legend-item" data-testid="density-legend"
        data-points={densityInput.points.length} style={{ whiteSpace: 'normal', flexWrap: 'wrap' }}>
        {api && !api.Overlay ? '当前地图不支持设施密度热力' : <>
          {/* 色带连同不透明度与地图同源；竖线标出单个设施中心的读数。 */}
          <span className="density-ramp" style={{ background: densityLegendCss() }}>
            <i data-testid="density-single-tick"
              style={{ left: `${(SINGLE_FACILITY_PEAK / DENSITY_SCALE_MAX) * 100}%` }} /></span>
          {densityCategory === DENSITY_ALL ? '设施' : categoryLabel(densityCategory)}密度
          0–{DENSITY_SCALE_MAX} {DENSITY_UNIT}（竖线：单个设施中心 {SINGLE_FACILITY_PEAK.toFixed(2)}）
          · 核半径 {HEAT_KERNEL_RADIUS_M} 米 · 不代表服务覆盖率
          {!drawables.facilities && ' · 等待设施结果'}
          {drawables.facilities?.state === 'empty' && ' · 本次没有可绘制设施'}
          {drawables.facilities?.state === 'ready' && densityInput.records === 0
            && ` · 本次没有${categoryLabel(densityCategory)}设施`}
          {densityInput.points.length > 0 && ` · ${densityInput.points.length} 处设施参与`}
          {densityInput.merged > 0 && `（${densityInput.merged} 条疑似重复已合并）`}
          {densityInput.points.length > 0 && !drawables.isochrone?.shapes.length && ' · 等待计算圈面'}
        </>}
      </span>}
      {waterOn && water && <span className="api-legend-item" data-testid="water-legend"
        data-shapes={water.shapes.length} data-crossings={water.crossings.length}
        style={{ whiteSpace: 'normal', flexWrap: 'wrap' }}>
        {api && !api.Overlay ? '当前地图不支持水系标注' : <>
          {WATER_KINDS.filter(kind => water.shapes.some(shape => shape.kind === kind)).map(kind =>
            <span key={kind} data-water-kind={kind} title={WATER_STYLES[kind].note}
              style={{ display: 'inline-flex', alignItems: 'center', gap: 4, marginRight: 6 }}>
              <i style={waterSwatch(WATER_STYLES[kind])} />
              {kind === 'reach' && water.reachWidthM
                ? `${WATER_STYLES.reach.label}（OSM，宽 ${water.reachWidthM.min === water.reachWidthM.max
                  ? water.reachWidthM.min : `${water.reachWidthM.min}–${water.reachWidthM.max}`} m）`
                : WATER_STYLES[kind].label}
            </span>)}
          {water.crossings.length > 0 && <span data-water-kind="crossing"
            style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
            <i className="api-legend-dot" style={{ background: CROSSING_COLOR }} />已核实桥梁 {water.crossings.length} 座</span>}
          · 以复核后的水系计算，底图水面仅作参照
        </>}
      </span>}
      {Object.entries(LAYER_STYLES).filter(([id]) => layers[id as LayerId])
        .map(([id, style]) => <span key={id} className="api-legend-item">
          <i className={AREA_LAYERS.has(id as LayerId) ? 'api-legend-area' : 'api-legend-dot'}
            style={layerSwatch(id as LayerId)} />{style.label}</span>)}
      {layers.facilities && (drawables.facilities?.points.length ?? 0) > 0
        && <span className="api-legend-item api-legend-cats">设施类别
          {CATEGORY_ORDER.map(category => <span key={category} className="api-legend-item">
            <i className="api-legend-dot" style={{ background: CATEGORY_COLORS[category] }} />
            {categoryLabel(category)}</span>)}</span>}
      {legend.length > 1 && <span className="api-legend-item api-legend-count">共 {legend.length} 个点，同格合并显示，数据不截断</span>}
    </div>}
  </div>;
}
