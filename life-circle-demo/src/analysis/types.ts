import type { Center } from '../types';
import type { TaskResultResponse as ContractTaskResult, TaskStatusResponse as ContractTaskStatus } from '../api-contract';

export type Budget = 200 | 400 | 800;
export type BusinessStatus = 'complete' | 'partial' | 'failed' | 'empty';
export type AnalysisMode = 'baidu_online' | 'osm_offline' | 'hybrid';
export type AnalysisInput = { center: Center; budget: Budget; clientRequestId: string; analysisMode?: AnalysisMode };
export type DataSource = 'synthetic' | 'baidu_walking' | 'osm_offline' | 'hybrid';
export type BusinessGeometry = {
  type: 'MultiPolygon'; coordinateSystem: 'bd09ll'; coordinates: [number, number][][][];
};
export type TaskStatus = Omit<ContractTaskStatus, 'analysisMode'> & { analysisMode?: AnalysisMode };
export type Isochrone = {
  timeBands?: { minutes: number; geometry: BusinessGeometry | null }[];
  unreachableRegion?: BusinessGeometry | null;
  coordinateSystem: 'bd09ll'; geometry: BusinessGeometry | null;
  uncertainRegion: BusinessGeometry; unknownRegion: BusinessGeometry; computationExtent: BusinessGeometry;
  quality: 'usable' | 'partial' | 'insufficient'; stopReason: string; warnings: string[];
  statistics: { requests: number; network_requests: number; retries: number; unknown_area: number;
    unfinished_boundary: number; total_seconds: number; failures: Record<string, number> };
  config: { origin: [number, number]; budget: number; seed: number; [key: string]: unknown };
};
export type AnalysisResult = Omit<ContractTaskResult, 'isochrone' | 'facilityAnalysis' | 'analysisMode' | 'provenance' | 'hybridResult'> & {
  center: Center; isochrone: Isochrone; facilityAnalysis?: ContractTaskResult['facilityAnalysis'];
  analysisMode?: AnalysisMode; provenance?: ContractTaskResult['provenance']; hybridResult?: ContractTaskResult['hybridResult'];
};
export type ModeCapability = {
  available: boolean; availability?: 'ready' | 'degraded' | 'unavailable' | null;
  coverageCity?: string | null; dataVersion?: string | null; dataDate?: string | null;
  dependencies?: string[]; limitations?: string[]; reason?: string | null;
};
export type AnalysisCapabilities = { modes: Record<AnalysisMode, ModeCapability> };
export interface AnalysisService {
  create(input: AnalysisInput): Promise<TaskStatus>;
  status(id: string, signal?: AbortSignal): Promise<TaskStatus>;
  result(id: string, signal?: AbortSignal): Promise<AnalysisResult>;
  cancel(id: string): Promise<TaskStatus>;
  cancelByRequest(clientRequestId: string): Promise<TaskStatus>;
  capabilities?: (signal?: AbortSignal) => Promise<AnalysisCapabilities>;
}
export type AnalysisState = {
  phase: 'idle' | 'submitting' | 'running' | 'fetching' | 'cancelling' | 'completed' | 'cancelled' | 'error';
  task?: TaskStatus; result?: AnalysisResult; error?: string;
};

export function isAnalysisBusy(state: AnalysisState) {
  return ['submitting', 'running', 'fetching', 'cancelling'].includes(state.phase);
}
