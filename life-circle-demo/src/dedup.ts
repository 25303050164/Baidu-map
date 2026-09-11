import { normalizeName, type Classification, type RawPoi } from './classify';

export type ClassifiedPoi = RawPoi & Classification;

export type DuplicateGroup = {
  key: string;
  kind: 'uid' | 'fallback';
  count: number;
  kept: string;
  removed: string[];
  names: string[];
};

export type DedupResult = {
  unique: ClassifiedPoi[];
  duplicates: DuplicateGroup[];
  removedCount: number;
};

export function stableUid(poi: RawPoi): string | undefined {
  if (poi.uid?.trim()) return poi.uid.trim();
  if (poi.source && poi.sourceId?.trim()) return `${poi.source}:${poi.sourceId.trim()}`;
  if (poi.poiId?.trim()) return poi.poiId.trim();
  return undefined;
}

export function fallbackKey(poi: RawPoi): string {
  const coordinate = poi.lng !== undefined && poi.lat !== undefined
    ? `${poi.lng.toFixed(5)},${poi.lat.toFixed(5)}`
    : poi.x !== undefined && poi.y !== undefined
      ? `${Math.round(poi.x)},${Math.round(poi.y)}`
      : '';
  return `${normalizeName(poi.name)}|${coordinate}`;
}

export function dedupeKey(poi: RawPoi): { key: string; kind: 'uid' | 'fallback' } {
  const uid = stableUid(poi);
  return uid ? { key: `uid:${uid}`, kind: 'uid' } : { key: `fb:${fallbackKey(poi)}`, kind: 'fallback' };
}

function completeness(poi: ClassifiedPoi): number {
  return (poi.uid ? 3 : 0) + (poi.sourceId ? 1 : 0) + (poi.address ? 1 : 0) + (poi.tags?.length ?? 0) + (poi.lng !== undefined && poi.lat !== undefined ? 1 : 0);
}

export function dedupe(pois: ClassifiedPoi[]): DedupResult {
  const groups = new Map<string, { kind: 'uid' | 'fallback'; items: ClassifiedPoi[] }>();
  for (const poi of pois) {
    const { key, kind } = dedupeKey(poi);
    const group = groups.get(key) ?? { kind, items: [] };
    group.items.push(poi);
    groups.set(key, group);
  }
  const unique: ClassifiedPoi[] = [];
  const duplicates: DuplicateGroup[] = [];
  let removedCount = 0;
  for (const [key, group] of groups) {
    const sorted = [...group.items].sort((a, b) =>
      completeness(b) - completeness(a) ||
      (stableUid(a) ?? fallbackKey(a)).localeCompare(stableUid(b) ?? fallbackKey(b)) ||
      a.name.localeCompare(b.name)
    );
    const [kept, ...rest] = sorted;
    unique.push(kept);
    if (rest.length) {
      removedCount += rest.length;
      duplicates.push({ key, kind: group.kind, count: group.items.length, kept: kept.name, removed: rest.map(item => item.name), names: group.items.map(item => item.name) });
    }
  }
  unique.sort((a, b) => (stableUid(a) ?? fallbackKey(a)).localeCompare(stableUid(b) ?? fallbackKey(b)));
  return { unique, duplicates, removedCount };
}
