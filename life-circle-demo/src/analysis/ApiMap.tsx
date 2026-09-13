import { useEffect, useRef, useState } from 'react';
import type { Center } from '../types';
import { useBaiduMap } from '../map/useBaiduMap';
import type { BMapMap } from '../map/baiduMapTypes';
import type { Isochrone } from './types';
import { drawGeometry } from './geometry';
import type { Facility, AssessmentPoint } from '../api-contract';

export type Layers = { reachable: boolean; unknown: boolean; uncertain: boolean; extent: boolean; serviceBlind: boolean };
export function ApiMap({ center, result, resultCenter, layers, onPick, minutes = 15, facilities = [], assessments = [], blindRegions = {}, route = [], onFacility }: { center: Center; result?: Isochrone; resultCenter?: Center; layers: Layers; onPick: (center: Center) => void; minutes?: number; facilities?: Facility[]; assessments?: AssessmentPoint[]; blindRegions?: Record<string, unknown>; route?: [number, number][]; onFacility?: (id: string) => void }) {
  const { api, mode, failureReason } = useBaiduMap();
  const container = useRef<HTMLDivElement>(null);
  const [map, setMap] = useState<BMapMap | null>(null);
  const pick = useRef(onPick);
  pick.current = onPick;
  const initialCenter = useRef(center);
  initialCenter.current = center;
  const [error, setError] = useState(false);
  useEffect(() => {
    if (!api || !container.current) return;
    let instance: BMapMap | undefined;
    const el = container.current;
    // Match BaiduMapView: cancel StrictMode's trial setup before the SDK starts async work.
    const frame = requestAnimationFrame(() => {
      try {
        instance = new api.Map(el);
        instance.centerAndZoom(new api.Point(initialCenter.current.lng, initialCenter.current.lat), 15);
        instance.enableScrollWheelZoom(true);
        instance.addEventListener('click', event => pick.current({ lng: +event.latlng.lng.toFixed(6), lat: +event.latlng.lat.toFixed(6) }));
        setMap(instance);
      } catch { setError(true); }
    });
    return () => { cancelAnimationFrame(frame); instance?.destroy?.(); };
  }, [api]);
  useEffect(() => {
    const instance = map;
    if (!instance || !api) return;
    try {
      instance.clearOverlays();
      instance.panTo(new api.Point(center.lng, center.lat));
      if (result) {
        if (layers.extent) drawGeometry(instance, api, result.computationExtent, { strokeColor: '#64748b', fillOpacity: 0, strokeStyle: 'dashed', strokeWeight: 1 });
        const band = result.timeBands?.find(b=>b.minutes===minutes);
        if (layers.reachable) drawGeometry(instance, api, band ? band.geometry : result.geometry, { strokeColor: '#147d70', fillColor: '#2da990', fillOpacity: .28, strokeWeight: 2 });
        if (layers.serviceBlind) Object.values(blindRegions).forEach(region => drawGeometry(instance, api, region as never, { strokeColor: '#4b5563', fillColor: '#6b7280', fillOpacity: .38, strokeWeight: 1 }));
        if (layers.unknown) drawGeometry(instance, api, result.unreachableRegion ?? null, { strokeColor: '#374151', fillColor: '#6b7280', fillOpacity: .28, strokeStyle: 'dashed' });
        if (layers.unknown) drawGeometry(instance, api, result.unknownRegion, { strokeColor: '#64748b', fillColor: '#64748b', fillOpacity: .24, strokeStyle: 'dashed' });
        if (layers.uncertain) drawGeometry(instance, api, result.uncertainRegion, { strokeColor: '#ca8a04', fillColor: '#facc15', fillOpacity: .15, strokeWeight: 1 });
      }
      for (const facility of facilities.slice(0,100)) {
        const marker = new api.Marker(new api.Point(facility.location.lng,facility.location.lat), {title:facility.name});
        marker.addEventListener('click', ()=>onFacility?.(facility.id));
        instance.addOverlay(marker);
      }
      for (const p of assessments) {
        const state = p.categories.some(c=>c.status==='unknown') ? 'unknown' : p.categories.some(c=>c.status==='blind') ? 'blind' : 'covered';
        const labels={covered:'有设施',blind:'查询内缺失',unknown:'无法判断'};
        const label = new api.Label(labels[state],{position:new api.Point(p.location.lng,p.location.lat)});
        label.setStyle({color:'#fff',backgroundColor:{covered:'#147d70',blind:'#b54708',unknown:'#64748b'}[state],border:'0',padding:'3px'});
        instance.addOverlay(label);
      }
      if (route.length>1 && api.Polyline) instance.addOverlay(new api.Polyline(route.map(p=>new api.Point(...p)),{strokeColor:'#7c3aed',strokeWeight:5}));
      if (resultCenter) instance.addOverlay(new api.Marker(new api.Point(resultCenter.lng, resultCenter.lat), { title: '已分析中心（与报告一致）' }));
      if (!resultCenter || center.lng !== resultCenter.lng || center.lat !== resultCenter.lat) {
        instance.addOverlay(new api.Marker(new api.Point(center.lng, center.lat), { title: '待分析选点（BD09LL）' }));
      }
    } catch { setError(true); }
  }, [api, map, center, result, resultCenter, layers, minutes, facilities, assessments, blindRegions, route, onFacility]);
  const unavailable = error || mode === 'fallback';
  const failureMessage = failureReason === 'missing-key'
    ? '尚未配置浏览器地图密钥，请联系项目管理员完成地图配置。仍可输入坐标、执行分析和查看结果摘要。'
    : error
      ? '地图初始化或图层绘制失败，请刷新页面重试；持续失败时请联系项目管理员检查浏览器和地图兼容性。'
      : '百度地图脚本未能加载，请检查网络及浏览器地图密钥的权限和来源限制，修复后刷新页面。仍可输入坐标执行分析。';
  return <div className="api-map-shell">
    <div ref={container} className="api-map" data-testid="algorithm-map" aria-label="等时圈地图" />
    {(unavailable || mode === 'loading') && <div className="api-map-notice" role="status">
      <strong>{unavailable ? '地图不可用' : '正在加载百度地图'}</strong>
      <p>{unavailable ? failureMessage : '地图就绪后可点击选择分析中心。'}</p>
    </div>}
    <div className="api-map-caption">百度坐标 BD09LL · 点击地图选点
      {resultCenter && <><br />图层与报告中心：{resultCenter.lng.toFixed(6)}, {resultCenter.lat.toFixed(6)}</>}
    </div>
  </div>;
}
