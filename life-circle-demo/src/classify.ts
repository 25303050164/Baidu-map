import { defaultBoundaryFlags, subCategories, type BoundaryFlags, type MajorCategory, type SubCategoryDef } from './taxonomy';
import type { Category } from './types';

export type RawPoi = {
  uid?: string;
  poiId?: string;
  source?: string;
  sourceId?: string;
  name: string;
  x?: number;
  y?: number;
  lng?: number;
  lat?: number;
  tags?: string[];
  address?: string;
  updatedAt?: string;
};

export type Classification = {
  major?: MajorCategory;
  minor?: string;
  minorLabel?: string;
  category?: Category;
  matchedKeyword?: string;
  negative: boolean;
  reason: string;
  confidence: number;
};

export function normalizeName(input: string): string {
  return input.normalize('NFKC').toLowerCase().replace(/[^\p{L}\p{N}]/gu, '');
}

function matchText(poi: RawPoi): string {
  return normalizeName([poi.name, ...(poi.tags ?? [])].join(' '));
}

function isGated(def: SubCategoryDef, flags: BoundaryFlags): boolean {
  return !!def.boundary && !flags[def.boundary];
}

export function classifyPoi(poi: RawPoi, flags: BoundaryFlags = defaultBoundaryFlags): Classification {
  const text = matchText(poi);
  if (!text) return { negative: true, reason: '缺少可用的名称或标签，无法分类', confidence: 0 };
  const candidates: { def: SubCategoryDef; keyword: string }[] = [];
  for (const def of subCategories) {
    if (isGated(def, flags)) continue;
    if (def.exclude?.some(keyword => text.includes(normalizeName(keyword)))) continue;
    let best: string | undefined;
    for (const keyword of def.include) {
      const normalized = normalizeName(keyword);
      if (normalized && text.includes(normalized) && (!best || normalized.length > best.length)) best = normalized;
    }
    if (best) candidates.push({ def, keyword: best });
  }
  if (!candidates.length) return { negative: false, reason: '未命中任何已知小类，保留为未分类', confidence: 0 };
  candidates.sort((a, b) =>
    (b.def.priority ?? 0) - (a.def.priority ?? 0) ||
    b.keyword.length - a.keyword.length ||
    a.def.key.localeCompare(b.def.key)
  );
  const winner = candidates[0];
  const negative = !!winner.def.negative;
  return {
    major: winner.def.major,
    minor: winner.def.key,
    minorLabel: winner.def.label,
    category: negative ? undefined : winner.def.category,
    matchedKeyword: winner.keyword,
    negative,
    reason: negative
      ? `命中「${winner.def.label}」关键词“${winner.keyword}”，按误类排除`
      : `命中「${winner.def.label}」关键词“${winner.keyword}”`,
    confidence: Math.min(1, winner.keyword.length / 4)
  };
}
