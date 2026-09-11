import type { RawPoi } from '../classify';
import type { Point } from '../types';

export const sampleCircle: Point[] = [
  { x: 100, y: 100 },
  { x: 450, y: 100 },
  { x: 450, y: 450 },
  { x: 100, y: 450 }
];

export const sampleFacilities: RawPoi[] = [
  { uid: 'BM-1001', name: '青禾菜市场', x: 300, y: 300, tags: ['农贸市场'] },
  { uid: 'BM-1002', name: '邻里鲜市', x: 340, y: 320 },
  { uid: 'BM-1003', name: '南苑生鲜市集', x: 200, y: 150 },
  { uid: 'BM-1004', name: '东里菜场', x: 500, y: 500 },
  { uid: 'BM-1005', name: '河畔生鲜超市', x: 120, y: 120 },
  { uid: 'BM-1006', name: '西里果蔬店', x: 160, y: 140 },

  { uid: 'BM-2001', name: '青禾药房', x: 280, y: 310 },
  { uid: 'BM-2002', name: '康宁药店', x: 360, y: 280 },
  { uid: 'BM-2003', name: '邻家药房', x: 220, y: 180 },
  { uid: 'BM-2004', name: '文景大药房', x: 520, y: 400 },
  { uid: 'BM-2005', name: '中心街药局', x: 140, y: 360 },
  { uid: 'BM-2006', name: '人民医院药房', x: 180, y: 200 },

  { uid: 'BM-3001', name: '青禾实验小学', x: 310, y: 260 },
  { uid: 'BM-3002', name: '东里小学', x: 420, y: 300 },
  { uid: 'BM-3003', name: '文景中心小学', x: 150, y: 170 },
  { uid: 'BM-3004', name: '南桥小学', x: 600, y: 600 },
  { uid: 'BM-3005', name: '南苑完小', x: 240, y: 230 },
  { uid: 'BM-3006', name: '西里教学点', x: 90, y: 90 },

  { uid: 'BM-4001', name: '新东方英语培训中心', x: 300, y: 300 },
  { uid: 'BM-4002', name: '学而思辅导班', x: 310, y: 310 },
  { uid: 'BM-4003', name: '青禾美术兴趣班', x: 320, y: 320 },
  { uid: 'BM-4004', name: '青禾花鸟市场', x: 350, y: 200 },
  { uid: 'BM-4005', name: '宠物药店', x: 360, y: 210 },
  { uid: 'BM-4006', name: '阳光幼儿园', x: 200, y: 200 },

  { uid: 'BM-5001', name: '市第一人民医院', x: 400, y: 400 },
  { uid: 'BM-5002', name: '青禾社区卫生服务中心', x: 250, y: 350 },
  { uid: 'BM-5003', name: '幸福养老院', x: 150, y: 250 },
  { uid: 'BM-5004', name: '康复护理中心', x: 130, y: 230 },
  { uid: 'BM-5005', name: '老街面馆', x: 300, y: 330 },
  { uid: 'BM-5006', name: '街角咖啡馆', x: 310, y: 340 },
  { uid: 'BM-5007', name: '工商银行', x: 330, y: 250 },
  { uid: 'BM-5008', name: '青禾公园', x: 180, y: 130 },
  { uid: 'BM-5009', name: '公交车站', x: 340, y: 360 },
  { uid: 'BM-5010', name: '社区理发店', x: 320, y: 370 },

  { uid: 'BM-9100', name: '重复菜市场', x: 400, y: 400 },
  { uid: 'BM-9100', name: '重复菜市场', x: 400, y: 400, tags: ['农贸市场'], address: '青禾路 1 号' },
  { name: '无名生鲜', x: 260, y: 260 },
  { name: '无名生鲜', x: 260, y: 260, tags: ['生鲜'] }
];
