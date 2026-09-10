import { categories, type AnalysisResult, type Center, type Filter, type Point, type Sample } from './types';
export const pointToCenter = (p: Point): Center => ({ lng: Number((116.39 + p.x * .00002).toFixed(6)), lat: Number((39.92 - p.y * .00002).toFixed(6)) });
export const centerToPoint = (c: Center): Point => ({ x: (c.lng - 116.39) / .00002, y: (39.92 - c.lat) / .00002 });
export function findSample(samples: Sample[], center: Center) { return samples.find(s => Math.abs(s.center.lng - center.lng) < .0000001 && Math.abs(s.center.lat - center.lat) < .0000001); }
export function filterFacilities(result: AnalysisResult, filter: Filter) { return result.facilities.filter(f => filter === 'all' || f.category === filter); }
export function summarize(result: AnalysisResult) {
  const inside = [...new Map(result.facilities.filter(f => f.inCircle).map(f => [f.id, f])).values()];
  const counts = Object.fromEntries(categories.map(c => [c, result.quality[c] === 'unknown' ? null : inside.filter(f => f.category === c).length])) as Record<typeof categories[number], number | null>;
  return { ...counts, total: inside.length, blindCount: result.zones.filter(z => z.status === 'blind').length, unknownCount: result.zones.filter(z => z.status === 'unknown').length };
}
