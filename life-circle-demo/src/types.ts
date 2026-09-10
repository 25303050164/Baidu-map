export type Category = 'market' | 'pharmacy' | 'school';
export type Filter = Category | 'all';
export type Scenario = 'normal' | 'missing' | 'insufficient' | 'failure';
export type Point = { x: number; y: number };
export type Center = { lng: number; lat: number };
export type Sample = { id: string; name: string; subtitle: string; center: Center; position: Point };
export type Facility = Point & { id: string; name: string; category: Category; inCircle: boolean };
export type Zone = { id: string; name: string; category: Category; status: 'blind' | 'unknown'; points: Point[]; position: Point; reason: string };
export type Conditions = { center: Center; scenario: Scenario };
export type AnalysisResult = { sample: Sample; scenario: Scenario; facilities: Facility[]; circle: Point[]; zones: Zone[]; quality: Record<Category, 'complete' | 'unknown'>; generatedAt: string };
export type TaskStatus = 'running' | 'completed' | 'failed' | 'unavailable';
export interface AnalysisService {
  getSamples(): Promise<Sample[]>;
  createAnalysis(conditions: Conditions): Promise<string>;
  getStatus(id: string): Promise<{ status: TaskStatus }>;
  getResult(id: string): Promise<AnalysisResult>;
}
export const categories: Category[] = ['market', 'pharmacy', 'school'];
export const categoryMeta: Record<Category, { label: string; color: string; symbol: string }> = {
  market: { label: '菜市场', color: '#168875', symbol: '菜' },
  pharmacy: { label: '药店', color: '#397ac6', symbol: '+' },
  school: { label: '小学', color: '#c78b36', symbol: '学' }
};
export const scenarioLabels: Record<Scenario, string> = { normal: '正常结果', missing: '设施缺失', insufficient: '数据不足', failure: '分析失败' };
