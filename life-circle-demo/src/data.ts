import { centerToPoint, distance, pointInPolygon, pointToCenter } from './domain';
import { buildFacilities } from './pipeline';
import type { AnalysisResult, Category, Center, Point, Sample, Scenario, Zone } from './types';
export const samples: Sample[] = [
  { id: 'a', name: '青禾街区 · A 点', subtitle: '邻里中心 / 虚构演示街区', position: { x: 450, y: 355 } },
  { id: 'b', name: '青禾街区 · B 点', subtitle: '公园东侧 / 虚构演示街区', position: { x: 650, y: 265 } },
  { id: 'c', name: '青禾街区 · C 点', subtitle: '南侧住区 / 虚构演示街区', position: { x: 305, y: 510 } }
].map(s => ({ ...s, center: pointToCenter(s.position) }));
export function customSample(center: Center): Sample {
  return { id: 'custom', name: '自定义位置', subtitle: '地图选点 / 自定义坐标', position: centerToPoint(center), center };
}

// The 15-minute reach is drawn as a fixed irregular shape translated to the chosen
// center. Facilities, by contrast, sit at fixed geographic coordinates in the
// neighbourhood, so each center sees a different subset.
const reachShape: [number, number][] = [[-190,-120],[-100,-190],[20,-175],[130,-190],[205,-95],[185,-5],[215,85],[140,160],[30,185],[-70,210],[-155,135],[-175,55],[-220,-20]];

type Fixture = { uid: string; name: string; category: Category; x: number; y: number; missing?: boolean };
export const fixtures: Fixture[] = [
  { uid: 'demo-market-01', name: '青禾菜市场', category: 'market', x: 350, y: 300 },
  { uid: 'demo-market-02', name: '邻里鲜市', category: 'market', x: 580, y: 175, missing: true },
  { uid: 'demo-market-03', name: '东里菜场', category: 'market', x: 705, y: 205, missing: true },
  { uid: 'demo-market-04', name: '南苑生鲜市集', category: 'market', x: 250, y: 620 },
  { uid: 'demo-market-05', name: '河畔菜市场', category: 'market', x: 905, y: 190 },
  { uid: 'demo-market-06', name: '中心街市集', category: 'market', x: 475, y: 440 },
  { uid: 'demo-market-07', name: '青禾生鲜', category: 'market', x: 395, y: 285 },
  { uid: 'demo-market-08', name: '西里菜场', category: 'market', x: 110, y: 330 },
  { uid: 'demo-pharmacy-01', name: '青禾药房', category: 'pharmacy', x: 405, y: 395 },
  { uid: 'demo-pharmacy-02', name: '康宁药店', category: 'pharmacy', x: 700, y: 320 },
  { uid: 'demo-pharmacy-03', name: '邻家药房', category: 'pharmacy', x: 320, y: 560 },
  { uid: 'demo-pharmacy-04', name: '文景药房', category: 'pharmacy', x: 615, y: 115 },
  { uid: 'demo-pharmacy-05', name: '西街药房', category: 'pharmacy', x: 140, y: 230 },
  { uid: 'demo-pharmacy-06', name: '中心街药房', category: 'pharmacy', x: 480, y: 410 },
  { uid: 'demo-school-01', name: '青禾实验小学', category: 'school', x: 255, y: 235 },
  { uid: 'demo-school-02', name: '东里小学', category: 'school', x: 545, y: 300 },
  { uid: 'demo-school-03', name: '文景小学', category: 'school', x: 660, y: 140 },
  { uid: 'demo-school-04', name: '南桥小学', category: 'school', x: 330, y: 730 },
  { uid: 'demo-school-05', name: '南苑小学', category: 'school', x: 200, y: 610 }
];

export const BLIND_RANGE = 450;
const presetZones: Record<'missing' | 'insufficient', Zone> = {
  missing: {
    id: 'zone-market-northeast', name: '东北住区', category: 'market', status: 'blind', position: { x: 655, y: 205 },
    points: [{ x: 560, y: 120 }, { x: 690, y: 105 }, { x: 770, y: 190 }, { x: 700, y: 275 }, { x: 585, y: 245 }],
    reason: '预设演示结论：东北住区周边 1 公里内缺少菜市场。距离口径与评估范围尚待正式确认。'
  },
  insufficient: {
    id: 'zone-pharmacy-south', name: '南侧住区', category: 'pharmacy', status: 'unknown', position: { x: 250, y: 615 },
    points: [{ x: 150, y: 555 }, { x: 265, y: 535 }, { x: 340, y: 610 }, { x: 300, y: 700 }, { x: 175, y: 690 }],
    reason: '药店信息不完整，暂时无法判断该区域的服务情况；不计为服务盲区。'
  }
};

export function createResult(sample: Sample, scenario: Scenario): AnalysisResult {
  const { x, y } = sample.position;
  const move = (a: number, b: number): Point => ({ x: x + a, y: y + b });
  const circle = reachShape.map(([a, b]) => move(a, b));
  const active = fixtures.filter(f => !(scenario === 'missing' && f.missing));
  const { facilities } = buildFacilities(
    active.map(f => ({ uid: f.uid, name: f.name, x: f.x, y: f.y })),
    circle
  );
  const sceneZone = scenario === 'missing' ? presetZones.missing : scenario === 'insufficient' ? presetZones.insufficient : undefined;
  const showZone = !!sceneZone && (pointInPolygon(sample.position, sceneZone.points) || sceneZone.points.some(p => distance(p, sample.position) <= BLIND_RANGE));
  return {
    sample, scenario, facilities, circle,
    zones: sceneZone && showZone ? [sceneZone] : [],
    quality: { market: 'complete', pharmacy: scenario === 'insufficient' ? 'unknown' : 'complete', school: 'complete' },
    generatedAt: new Date().toISOString()
  };
}
