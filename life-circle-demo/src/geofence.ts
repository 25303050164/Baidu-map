import { centerToPoint, pointInPolygon } from './domain';
import type { RawPoi } from './classify';
import type { Point } from './types';

export function inCircle(point: Point, polygon: Point[]): boolean {
  return polygon.length > 0 && pointInPolygon(point, polygon);
}

export function poiPoint(poi: RawPoi): Point | undefined {
  if (poi.x !== undefined && poi.y !== undefined) return { x: poi.x, y: poi.y };
  if (poi.lng !== undefined && poi.lat !== undefined) return centerToPoint({ lng: poi.lng, lat: poi.lat });
  return undefined;
}
