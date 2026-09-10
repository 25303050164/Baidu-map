import { pointToCenter } from './domain';
import type { AnalysisResult, Category, Point, Sample, Scenario } from './types';
export const samples: Sample[] = [
  { id: 'a', name: '青禾街区 · A 点', subtitle: '邻里中心 / 虚构演示街区', position: { x: 450, y: 355 } },
  { id: 'b', name: '青禾街区 · B 点', subtitle: '公园东侧 / 虚构演示街区', position: { x: 650, y: 265 } },
  { id: 'c', name: '青禾街区 · C 点', subtitle: '南侧住区 / 虚构演示街区', position: { x: 305, y: 510 } }
].map(s => ({ ...s, center: pointToCenter(s.position) }));
const fixtures: [string, Category, number, number, boolean][] = [
  ['青禾菜市场','market',-110,-80,true], ['邻里鲜市','market',85,-120,true],
  ['南苑生鲜市集','market',-55,150,true], ['东里菜场','market',140,55,true],
  ['青禾药房','pharmacy',-140,25,true], ['康宁药店','pharmacy',40,-45,true], ['邻家药房','pharmacy',60,130,true],
  ['青禾实验小学','school',-100,100,true], ['东里小学','school',160,-50,true],
  ['河畔菜市场','market',285,80,false], ['西街药房','pharmacy',-270,-110,false], ['南桥小学','school',70,275,false]
];
export function createResult(sample: Sample, scenario: Scenario): AnalysisResult {
  const { x, y } = sample.position;
  const move = (a: number, b: number): Point => ({ x: x + a, y: y + b });
  const facilities = fixtures.filter((f, i) => !(scenario === 'missing' && f[1] === 'market' && f[2] > 0 && f[4]) && !(scenario === 'insufficient' && f[1] === 'pharmacy' && i !== 4)).map((f, i) => ({ id: `${sample.id}-${i}`, name: f[0], category: f[1], ...move(f[2], f[3]), inCircle: f[4] }));
  return {
    sample, scenario, facilities,
    circle: [[-190,-120],[-100,-190],[20,-175],[130,-190],[205,-95],[185,-5],[215,85],[140,160],[30,185],[-70,210],[-155,135],[-175,55],[-220,-20]].map(([a,b]) => move(a,b)),
    zones: scenario === 'missing' || scenario === 'insufficient' ? [{
      id: 'zone-1', name: scenario === 'missing' ? '东北住区' : '南侧住区',
      category: scenario === 'missing' ? 'market' : 'pharmacy', status: scenario === 'missing' ? 'blind' : 'unknown',
      position: scenario === 'missing' ? move(145,-95) : move(-40,125),
      points: (scenario === 'missing' ? [[65,-160],[150,-170],[195,-100],[160,-25],[80,-40]] : [[-120,65],[5,80],[60,160],[-65,195],[-145,130]]).map(([a,b])=>move(a,b)),
      reason: scenario === 'missing' ? '预设演示结论：该点位周边 1 公里内缺少菜市场。距离口径与评估范围尚待正式确认。' : '药店信息不完整，暂时无法判断该区域的服务情况；不计为服务盲区。'
    }] : [],
    quality: { market: 'complete', pharmacy: scenario === 'insufficient' ? 'unknown' : 'complete', school: 'complete' },
    generatedAt: new Date().toISOString()
  };
}
