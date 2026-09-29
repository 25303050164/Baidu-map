import type { BaiduMapApi, BMapMap, BMapOverlay, BMapPolygonOptions } from '../map/baiduMapTypes';
import type { BusinessGeometry, Isochrone } from './types';
import type { Geometry } from '../api-contract';

export type DrawableGeometry = Pick<Geometry, 'type' | 'coordinates'> | BusinessGeometry;

export function geometryForMinutes(result: Isochrone, minutes: number): BusinessGeometry | null {
  const band = result.timeBands?.find(b => b.minutes === minutes);
  return band ? band.geometry : minutes === 15 ? result.geometry : null;
}

/** Each component keeps its exterior and all interior rings in one overlay. */
export function polygonPaths(geometry: DrawableGeometry | null): string[][] {
  if (!geometry) return [];
  const polygons = geometry.type === 'Polygon' ? [geometry.coordinates] : geometry.coordinates;
  return polygons.map(polygon => {
    if (!Array.isArray(polygon)) throw new Error('Invalid polygon');
    return polygon.map(ring => {
      if (!Array.isArray(ring)) throw new Error('Invalid ring');
      return ring.map(point => {
        if (!Array.isArray(point) || point.length !== 2 || !point.every(Number.isFinite)) throw new Error('Invalid point');
        return point.join(',');
      }).join(';');
    });
  });
}
/** 画出几何并返回本次新增的覆盖物：调用方按图层组逐一摘除，不必清空整张地图。 */
export function drawGeometry(map: BMapMap, api: BaiduMapApi, geometry: DrawableGeometry | null, style: BMapPolygonOptions): BMapOverlay[] {
  return polygonPaths(geometry).map(rings => {
    const overlay = new api.Polygon(rings, style);
    map.addOverlay(overlay);
    return overlay;
  });
}

/** Display-only exterior paths. Preserve components; never connect across gaps. */
export function drawOutline(map: BMapMap, api: BaiduMapApi, geometry: DrawableGeometry | null): BMapOverlay[] {
  return polygonPaths(geometry).map(rings => {
    const overlay = new api.Polygon([rings[0]], {
      strokeColor: '#147d70', strokeWeight: 2, fillOpacity: 0,
    });
    map.addOverlay(overlay);
    return overlay;
  });
}
export function geometryMessage(geometry: BusinessGeometry | null) {
  if (geometry === null) return '证据不足，无法确定可达区域';
  if (!geometry.coordinates.length) return '有效证据范围内，可达区域为空';
  return `已重建 ${geometry.coordinates.length} 个可达分量`;
}
