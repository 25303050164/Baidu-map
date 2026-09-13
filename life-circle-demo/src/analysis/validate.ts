import type { RouteEvidence } from '../api-contract';
import type { AnalysisResult, BusinessStatus, TaskStatus } from './types';

type RecordValue = Record<string, unknown>;
const object = (v: unknown): v is RecordValue => v !== null && typeof v === 'object' && !Array.isArray(v);
const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const count = (v: unknown) => finite(v) && Number.isInteger(v) && v >= 0;
const source = (v: unknown) => v === 'synthetic' || v === 'baidu_walking';
const businessStatus = (v: unknown): v is BusinessStatus =>
  v === 'complete' || v === 'partial' || v === 'failed' || v === 'empty';
const point = (v: unknown) => Array.isArray(v) && v.length === 2 && finite(v[0]) && finite(v[1])
  && v[0] >= -180 && v[0] <= 180 && v[1] > -85 && v[1] < 85;

function geometry(v: unknown): boolean {
  return object(v) && v.type === 'MultiPolygon' && v.coordinateSystem === 'bd09ll'
    && Array.isArray(v.coordinates) && v.coordinates.every(p => Array.isArray(p) && p.length > 0
      && p.every(r => Array.isArray(r) && r.length >= 4 && r.every(point)
        && r[0][0] === r[r.length - 1][0] && r[0][1] === r[r.length - 1][1]));
}

export function validTask(v: unknown): v is TaskStatus {
  return object(v) && typeof v.taskId === 'string' && v.taskId.length > 0
    && v.schema_version === '1.0' && v.responseType === 'task'
    && ['running', 'cancelling', 'completed', 'cancelled', 'failed'].includes(v.status as string)
    && (v.businessStatus === null || businessStatus(v.businessStatus))
    && typeof v.stage === 'string' && count(v.requests) && count(v.networkRequests)
    && [200, 400, 800].includes(v.budget as number) && finite(v.elapsedSeconds) && v.elapsedSeconds >= 0
    && source(v.dataSource) && (v.error === null || typeof v.error === 'string');
}

export function validResult(v: unknown): v is AnalysisResult {
  if (!object(v) || typeof v.taskId !== 'string' || !v.taskId || !source(v.dataSource)
    || v.schema_version !== '1.0' || v.responseType !== 'result' || v.taskStatus !== 'completed'
    || !businessStatus(v.status) || !businessStatus(v.businessStatus) || v.status !== v.businessStatus
    || !object(v.center) || !point([v.center.lng, v.center.lat]) || !finite(v.generatedAt)
    || v.generatedAt < 0 || !Number.isFinite(new Date(v.generatedAt * 1000).getTime())
    || !['not_integrated', 'complete', 'partial', 'failed'].includes(v.facilitiesStatus as string) || v.coordinateSystem !== 'bd09ll'
    || v.coordinateOrder !== 'longitude,latitude' || !object(v.units) || !object(v.rules)
    || !object(v.data) || !object(v.isochrone)) return false;
  const r = v.isochrone;
  if (r.timeBands !== undefined && (!Array.isArray(r.timeBands) || !r.timeBands.every(b => object(b) && [5,10,15].includes(b.minutes as number) && (b.geometry === null || geometry(b.geometry))))) return false;
  if (r.unreachableRegion !== undefined && r.unreachableRegion !== null && !geometry(r.unreachableRegion)) return false;
  if (!validFacilities(v.facilityAnalysis, v.facilitiesStatus, v.data)) return false;
  if (r.coordinateSystem !== 'bd09ll' || !(r.geometry === null || geometry(r.geometry))
    || !geometry(r.unknownRegion) || !geometry(r.uncertainRegion) || !geometry(r.computationExtent)
    || !['usable', 'partial', 'insufficient'].includes(r.quality as string)
    || typeof r.stopReason !== 'string' || !Array.isArray(r.warnings) || !r.warnings.every(x => typeof x === 'string')
    || !object(r.statistics) || !object(r.config)) return false;
  const s = r.statistics;
  return count(s.requests) && count(s.network_requests) && count(s.retries) && count(s.unfinished_boundary)
    && finite(s.total_seconds) && s.total_seconds >= 0 && finite(s.unknown_area) && s.unknown_area >= 0
    && object(s.failures) && Object.values(s.failures).every(count)
    && point(r.config.origin) && (r.config.origin as number[])[0] === v.center.lng
    && (r.config.origin as number[])[1] === v.center.lat
    && [200, 400, 800].includes(r.config.budget as number) && count(r.config.seed);
}

const group = (v: unknown) => ['shopping', 'medical', 'education'].includes(v as string);
const nullableNonnegative = (v: unknown) => v === null || (finite(v) && v >= 0);

export function validRoute(v: unknown): v is RouteEvidence {
  return object(v) && typeof v.endpoint_verified === 'boolean'
    && nullableNonnegative(v.distance_m) && nullableNonnegative(v.duration_s)
    && (v.reason === null || typeof v.reason === 'string')
    && Array.isArray(v.path) && v.path.every(point)
    && (v.endpoint_verified || v.path.length === 0);
}

function validQuery(v: unknown): boolean {
  return object(v) && typeof v.query === 'string'
    && ['complete', 'partial', 'failed', 'truncated'].includes(v.status as string);
}

function validAssessment(v: unknown): boolean {
  return object(v) && object(v.location) && point([v.location.lng, v.location.lat])
    && finite(v.duration_s) && v.duration_s >= 0 && Array.isArray(v.categories)
    && v.categories.every(c => object(c) && group(c.category)
      && ['covered', 'blind', 'unknown'].includes(c.status as string));
}

function validFacility(v: unknown): boolean {
  return object(v) && typeof v.id === 'string' && v.id.length > 0 && typeof v.name === 'string'
    && group(v.major_category) && object(v.location) && point([v.location.lng, v.location.lat])
    && (v.in_circle === null || typeof v.in_circle === 'boolean');
}

function validFacilities(v: unknown, status: unknown, data: RecordValue): boolean {
  if (v === null || v === undefined) return status === 'not_integrated';
  if (!object(v) || v.status !== status || !['complete', 'partial', 'failed'].includes(v.status as string)) return false;
  const counts = [v.candidate_points, v.assessed_points, v.unassessed_points, v.network_requests];
  return counts.every(count) && finite(v.elapsed_seconds) && v.elapsed_seconds >= 0
    && Array.isArray(v.warnings) && v.warnings.every(w => typeof w === 'string')
    && Array.isArray(v.queries) && v.queries.every(validQuery)
    && Array.isArray(v.assessments) && v.assessments.every(validAssessment)
    && object(v.routes) && Object.values(v.routes).every(validRoute)
    && (v.serviceBlindRegions === undefined || (object(v.serviceBlindRegions) && Object.values(v.serviceBlindRegions).every(geometry)))
    && Array.isArray(data.facilities) && data.facilities.every(validFacility) && typeof data.report === 'string';
}
