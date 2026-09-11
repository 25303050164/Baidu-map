import { classifyPoi, type Classification, type RawPoi } from './classify';
import { dedupe, fallbackKey, stableUid, type ClassifiedPoi, type DuplicateGroup } from './dedup';
import { inCircle, poiPoint } from './geofence';
import { defaultBoundaryFlags, majorMeta, type BoundaryFlags, type MajorCategory } from './taxonomy';
import type { Facility, Point } from './types';

export type PipelineFacility = Facility & {
  uid: string;
  major: MajorCategory;
  minor: string;
  minorLabel: string;
  matchedKeyword: string;
};

export type PipelineRejection = {
  name: string;
  reason: string;
  classification: Classification;
};

export type PipelineOptions = {
  boundary?: Partial<BoundaryFlags>;
};

export type PipelineResult = {
  facilities: PipelineFacility[];
  classified: ClassifiedPoi[];
  rejected: PipelineRejection[];
  duplicates: DuplicateGroup[];
  majorCounts: Record<string, number>;
  inputCount: number;
  uniqueCount: number;
  removedCount: number;
};

export function buildFacilities(pois: RawPoi[], polygon: Point[], options: PipelineOptions = {}): PipelineResult {
  const flags = { ...defaultBoundaryFlags, ...options.boundary };
  const classified: ClassifiedPoi[] = pois.map(poi => ({ ...poi, ...classifyPoi(poi, flags) }));
  const { unique, duplicates, removedCount } = dedupe(classified);
  const majorCounts: Record<string, number> = {};
  const rejected: PipelineRejection[] = [];
  const facilities: PipelineFacility[] = [];
  for (const poi of unique) {
    if (poi.major) majorCounts[poi.major] = (majorCounts[poi.major] ?? 0) + 1;
    if (!poi.major) { rejected.push({ name: poi.name, reason: poi.reason, classification: poi }); continue; }
    if (poi.negative) { rejected.push({ name: poi.name, reason: poi.reason, classification: poi }); continue; }
    if (!poi.category) {
      rejected.push({ name: poi.name, reason: `非首期三类（${majorMeta[poi.major].label} / ${poi.minorLabel}），仅分类不进入地图统计`, classification: poi });
      continue;
    }
    const point = poiPoint(poi);
    if (!point) { rejected.push({ name: poi.name, reason: '缺少坐标，无法进行圈内判定', classification: poi }); continue; }
    const uid = stableUid(poi) ?? fallbackKey(poi);
    facilities.push({
      id: uid,
      name: poi.name,
      category: poi.category,
      x: point.x,
      y: point.y,
      inCircle: inCircle(point, polygon),
      uid,
      major: poi.major,
      minor: poi.minor!,
      minorLabel: poi.minorLabel!,
      matchedKeyword: poi.matchedKeyword ?? ''
    });
  }
  facilities.sort((a, b) => a.uid.localeCompare(b.uid));
  return { facilities, classified: unique, rejected, duplicates, majorCounts, inputCount: pois.length, uniqueCount: unique.length, removedCount };
}
