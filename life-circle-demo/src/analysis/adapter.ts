import { categories, categoryMeta } from '../types';
import type { AnalysisInput, AnalysisResult, BusinessGeometry, Isochrone } from './types';
import { validResult } from './validate';
import { geometryMessage } from './geometry';

/** Task API → geographic frontend AnalysisResult. Never use the legacy demo projection. */
export function decodeAnalysisResult(value: unknown): AnalysisResult {
  if (!validResult(value)) throw new Error('后端返回格式异常，请检查服务版本');
  const keys = ['schema_version', 'responseType', 'taskId', 'taskStatus', 'status', 'businessStatus',
    'analysisMode', 'dataSource', 'center', 'generatedAt', 'facilitiesStatus', 'facilityAnalysis', 'coordinateSystem',
    'coordinateOrder', 'units', 'rules', 'data', 'algorithm', 'warnings', 'errors', 'provenance', 'hybridResult', 'isochrone'] as const;
  const result = structuredClone(Object.fromEntries(keys.filter(k => k in value).map(k => [k, value[k]]))) as AnalysisResult;
  // Normalize the two wire geometry shapes to the map's existing MultiPolygon model.
  const iso = result.isochrone as Isochrone;
  iso.geometry = normalizeGeometry(iso.geometry);
  iso.unknownRegion = normalizeGeometry(iso.unknownRegion);
  iso.uncertainRegion = normalizeGeometry(iso.uncertainRegion);
  iso.computationExtent = normalizeGeometry(iso.computationExtent);
  if (iso.unreachableRegion !== undefined) iso.unreachableRegion = iso.unreachableRegion === null ? null : normalizeGeometry(iso.unreachableRegion);
  iso.timeBands?.forEach(band => { band.geometry = band.geometry === null ? null : normalizeGeometry(band.geometry); });
  return result;
}

function normalizeGeometry(value: BusinessGeometry | { type: 'Polygon'; coordinateSystem: 'bd09ll'; coordinates: [number, number][][] } | null): BusinessGeometry {
  if (value?.type === 'Polygon') return { type: 'MultiPolygon', coordinateSystem: value.coordinateSystem, coordinates: [value.coordinates] };
  return value as BusinessGeometry;
}

export function matchesAnalysisInput(result: AnalysisResult, input: AnalysisInput) {
  // Backend normalizes the requested center to six decimal places.
  return result.center.lng === +input.center.lng.toFixed(6)
    && result.center.lat === +input.center.lat.toFixed(6)
    && result.isochrone.config.budget === input.budget
    && (result.analysisMode === undefined || result.analysisMode === (input.analysisMode ?? 'baidu_online'));
}

/** Completion is a task state; it does not mean every business module has run. */
export function analysisAvailability(result: AnalysisResult): 'partial' | 'unavailable' {
  return result.isochrone.quality === 'insufficient' || result.isochrone.geometry === null
    ? 'unavailable' : 'partial';
}

export function analysisReportView(result: AnalysisResult) {
  const sourceLabel = result.dataSource === 'synthetic' ? '合成时间场（非真实社区）'
    : result.dataSource === 'osm_offline' ? 'OSM 离线路网计算'
      : result.dataSource === 'hybrid' ? '百度在线 + OSM 离线（并行对比）' : '百度步行路线数据';
  const osm = result.provenance?.osm;
  return {
    taskId: result.taskId, center: result.center,
    generatedAt: new Date(result.generatedAt * 1000).toISOString(),
    analysisMode: result.analysisMode ?? 'baidu_online', dataSource: sourceLabel,
    osmProvenance: osm ? {
      availability: osm.availability, coverageCity: osm.coverageCity, dataDate: osm.dataDate,
      dataVersion: osm.dataVersion, downloadedAt: osm.downloadedAt, preparedAt: osm.preparedAt,
      coverageCheckAvailable: osm.coverageCheckAvailable, attribution: osm.attribution,
      license: osm.license, limitations: osm.limitations,
    } : null,
    hybridStrategy: result.hybridResult?.comparison.strategy ?? null,
    availability: analysisAvailability(result),
    geometrySummary: geometryMessage(result.isochrone.geometry),
    qualityLabel: { usable: '可用', partial: '部分结果', insufficient: '证据不足' }[result.isochrone.quality],
    budget: result.isochrone.config.budget,
    statistics: result.isochrone.statistics,
    warnings: result.isochrone.warnings,
    // not_integrated is unknown, never zero facilities or zero blind zones.
    facilityStats: categories.map(category => {
      const entry = result.data.categories.find(c => c.category === category);
      return { category, label: categoryMeta[category].label,
        count: result.facilityAnalysis ? entry?.count_in_circle ?? null : null,
        state: result.facilityAnalysis ? '检索记录 · 估算圈内' : '尚未接入' };
    }),
    blindZoneCount: null,
  };
}
