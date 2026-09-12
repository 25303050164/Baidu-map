import type { Center } from '../types';

export type Budget = 200 | 400 | 800;
export type AnalysisInput = { center: Center; budget: Budget; clientRequestId: string };
export type DataSource = 'synthetic' | 'baidu_walking';
export type BusinessGeometry = {
  type: 'MultiPolygon'; coordinateSystem: 'bd09ll'; coordinates: [number, number][][][];
};
export type TaskStatus = {
  taskId: string; status: 'running' | 'cancelling' | 'completed' | 'cancelled' | 'failed';
  stage: string; requests: number; networkRequests: number; budget: Budget;
  elapsedSeconds: number; dataSource: DataSource; error: string | null;
};
export type Isochrone = {
  coordinateSystem: 'bd09ll'; geometry: BusinessGeometry | null;
  uncertainRegion: BusinessGeometry; unknownRegion: BusinessGeometry; computationExtent: BusinessGeometry;
  quality: 'usable' | 'partial' | 'insufficient'; stopReason: string; warnings: string[];
  statistics: { requests: number; network_requests: number; retries: number; unknown_area: number;
    unfinished_boundary: number; total_seconds: number; failures: Record<string, number> };
  config: { origin: [number, number]; budget: number; seed: number; [key: string]: unknown };
};
export type AnalysisResult = {
  taskId: string; dataSource: DataSource; center: Center; generatedAt: number;
  facilitiesStatus: 'not_integrated'; isochrone: Isochrone;
};
export interface AnalysisService {
  create(input: AnalysisInput): Promise<TaskStatus>;
  status(id: string, signal?: AbortSignal): Promise<TaskStatus>;
  result(id: string, signal?: AbortSignal): Promise<AnalysisResult>;
  cancel(id: string): Promise<TaskStatus>;
}
export type AnalysisState = {
  phase: 'idle' | 'submitting' | 'running' | 'cancelling' | 'completed' | 'cancelled' | 'error';
  task?: TaskStatus; result?: AnalysisResult; error?: string;
};
