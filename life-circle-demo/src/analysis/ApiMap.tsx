import { useEffect, useRef, useState } from 'react';
import type { Center } from '../types';
import { useBaiduMap } from '../map/useBaiduMap';
import type { BMapMap } from '../map/baiduMapTypes';
import type { Isochrone } from './types';
import { drawGeometry } from './geometry';

export type Layers = { reachable: boolean; unknown: boolean; uncertain: boolean; extent: boolean };
export function ApiMap({ center, result, layers, onPick }: { center: Center; result?: Isochrone; layers: Layers; onPick: (center: Center) => void }) {
  const { api, mode } = useBaiduMap();
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<BMapMap | null>(null);
  const pick = useRef(onPick);
  pick.current = onPick;
  const initialCenter = useRef(center);
  const [error, setError] = useState(false);
  useEffect(() => {
    if (!api || !container.current) return;
    let instance: BMapMap | undefined;
    try {
      instance = new api.Map(container.current);
      map.current = instance;
      instance.centerAndZoom(new api.Point(initialCenter.current.lng, initialCenter.current.lat), 15);
      instance.enableScrollWheelZoom(true);
      instance.addEventListener('click', event => pick.current({ lng: +event.latlng.lng.toFixed(6), lat: +event.latlng.lat.toFixed(6) }));
    } catch { setError(true); }
    return () => { instance?.destroy?.(); map.current = null; };
  }, [api]);
  useEffect(() => {
    const instance = map.current;
    if (!instance || !api) return;
    try {
      instance.clearOverlays();
      instance.panTo(new api.Point(center.lng, center.lat));
      if (result) {
        if (layers.extent) drawGeometry(instance, api, result.computationExtent, { strokeColor: '#64748b', fillOpacity: 0, strokeStyle: 'dashed', strokeWeight: 1 });
        if (layers.reachable) drawGeometry(instance, api, result.geometry, { strokeColor: '#147d70', fillColor: '#2da990', fillOpacity: .28, strokeWeight: 2 });
        if (layers.unknown) drawGeometry(instance, api, result.unknownRegion, { strokeColor: '#64748b', fillColor: '#64748b', fillOpacity: .24, strokeStyle: 'dashed' });
        if (layers.uncertain) drawGeometry(instance, api, result.uncertainRegion, { strokeColor: '#ca8a04', fillColor: '#facc15', fillOpacity: .15, strokeWeight: 1 });
      }
      instance.addOverlay(new api.Marker(new api.Point(center.lng, center.lat), { title: '分析中心（BD09LL）' }));
    } catch { setError(true); }
  }, [api, center, result, layers]);
  const unavailable = error || mode === 'fallback';
  return <div className="api-map-shell">
    <div ref={container} className="api-map" data-testid="algorithm-map" aria-label="等时圈地图" />
    {(unavailable || mode === 'loading') && <div className="api-map-notice" role="status">
      <strong>{unavailable ? '地图不可用' : '正在加载百度地图'}</strong>
      <p>{unavailable ? '请检查浏览器地图 AK 或网络，修复后刷新页面。仍可输入坐标、执行分析和查看结果摘要。' : '地图就绪后可点击选择分析中心。'}</p>
    </div>}
    <div className="api-map-caption">百度坐标 BD09LL · 点击地图选点</div>
  </div>;
}
