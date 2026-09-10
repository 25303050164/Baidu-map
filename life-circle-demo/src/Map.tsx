import { useEffect, useRef, useState } from 'react';
import { Button, Tooltip } from 'antd';
import { AimOutlined, PlusOutlined, MinusOutlined, CloseOutlined, EnvironmentOutlined } from '@ant-design/icons';
import { categoryMeta, type AnalysisResult, type Center, type Facility, type Filter, type Point, type Sample } from './types';
import { centerToPoint, filterFacilities, pointToCenter } from './domain';
import styles from './styles.module.css';
export type Focus = Point & { id: string; name: string; detail: string; nonce?: number };
type Props = { center: Center; samples: Sample[]; result?: AnalysisResult; filter: Filter; layers: { circle: boolean; facilities: boolean; blind: boolean }; focus?: Focus; onFocus: (focus?: Focus) => void; onPick: (center: Center) => void; loading: boolean };
const points = (p: Point[]) => p.map(a => `${a.x},${a.y}`).join(' ');
export function DemoMap({ center, samples, result, filter, layers, focus, onFocus, onPick, loading }: Props) {
  const svg = useRef<SVGSVGElement>(null);
  const [view, setView] = useState({ x: 0, y: 0, w: 1000, h: 760 });
  const drag = useRef<{ px: number; py: number; sx: number; sy: number; x: number; y: number; moved: boolean } | null>(null);
  const centerPoint = centerToPoint(center);
  const visibleCenter = centerPoint.x >= 0 && centerPoint.x <= 1000 && centerPoint.y >= 0 && centerPoint.y <= 760;
  useEffect(() => { if (focus) setView({ x: focus.x - 300, y: focus.y - 228, w: 600, h: 456 }); }, [focus]);
  const zoom = (factor: number) => setView(v => { const w = Math.min(1500, Math.max(300, v.w * factor)); return { x: v.x + (v.w - w) / 2, y: v.y + (v.h - w * .76) / 2, w, h: w * .76 }; });
  const locate = (f: Facility) => onFocus({ ...f, detail: `${categoryMeta[f.category].label} · ${f.inCircle ? '15 分钟圈内（演示）' : '圈外参考，不计入圈内数量'}`, nonce: Date.now() });
  return <div className={styles.mapSurface}>
    <div className={styles.mapHeading}><span className={styles.liveDot}/><span>街区空间视图</span><span className={styles.smallMuted}>本地示意地图</span></div>
    <svg ref={svg} className={styles.mapSvg} aria-label="可交互演示地图" preserveAspectRatio="xMidYMid slice" viewBox={`${view.x} ${view.y} ${view.w} ${view.h}`}
      onPointerDown={e => { if(e.button !== 0) return; const matrix = svg.current?.getScreenCTM(); if (!matrix) return; drag.current = { px: e.clientX, py: e.clientY, sx: matrix.a, sy: matrix.d, x: view.x, y: view.y, moved: false }; e.currentTarget.setPointerCapture(e.pointerId); }}
      onPointerMove={e => { const d = drag.current; if (!d) return; const dx = e.clientX-d.px, dy=e.clientY-d.py; if (Math.hypot(dx,dy)>5) d.moved=true; if(d.moved) setView(v=>({...v,x:d.x-dx/d.sx,y:d.y-dy/d.sy})); }}
      onPointerUp={e => { const d=drag.current; drag.current=null; if(!d || d.moved) return; const matrix=svg.current?.getScreenCTM()?.inverse(); if(!matrix) return; const p=new DOMPoint(e.clientX,e.clientY).matrixTransform(matrix); if(p.x>=0&&p.x<=1000&&p.y>=0&&p.y<=760) {onFocus(undefined);onPick(pointToCenter(p));} }} onPointerCancel={()=>{drag.current=null;}}>
      <defs>
        <pattern id="blocks" width="150" height="124" patternUnits="userSpaceOnUse"><rect width="150" height="124" fill="#eef0e9"/><rect x="11" y="11" width="57" height="40" rx="5" fill="#e1e5dc"/><rect x="80" y="11" width="58" height="40" rx="5" fill="#e3e7de"/><rect x="11" y="65" width="127" height="45" rx="5" fill="#e4e7df"/><path d="M0 0H150M0 0V124" stroke="#fff" strokeWidth="12"/><path d="M74 10V111M10 58H140" stroke="#f9faf5" strokeWidth="6"/></pattern>
        <pattern id="unknownHatch" width="12" height="12" patternUnits="userSpaceOnUse" patternTransform="rotate(35)"><rect width="12" height="12" fill="#f9e8ba" fillOpacity=".6"/><line x1="0" y1="0" x2="0" y2="12" stroke="#c69b43" strokeWidth="3"/></pattern>
        <radialGradient id="reach"><stop offset="0" stopColor="#49b89d" stopOpacity=".27"/><stop offset="1" stopColor="#8fd2b8" stopOpacity=".1"/></radialGradient>
      </defs>
      <rect x="-2500" y="-2500" width="6000" height="6000" fill="#e9eee9"/>
      <rect width="1000" height="760" fill="url(#blocks)"/>
      <path d="M810 -50C650 100 975 110 830 290S765 490 880 560S890 790 710 830" fill="none" stroke="#cfdddd" strokeWidth="103"/>
      <path d="M810 -50C650 100 975 110 830 290S765 490 880 560S890 790 710 830" fill="none" stroke="#b6d5d8" strokeWidth="73"/>
      <g fill="#cbdcc5" stroke="#edf3e8" strokeWidth="8"><path d="M165 120H325V240H195L165 205Z"/><path d="M520 530H700V688H490V595Z"/><path d="M35 575H155V690H35Z"/></g>
      <g fill="none" stroke="#abc6a5" strokeWidth="2"><path d="M187 195Q235 115 304 175T220 220"/><path d="M518 622Q580 540 654 589T540 663"/></g>
      <g fill="#afc8a6">{[[197,147],[298,220],[215,228],[315,145],[530,568],[660,663],[631,550],[67,601],[120,661]].map(([x,y],i)=><circle key={i} cx={x} cy={y} r="11"/>)}</g>
      <g fill="none" stroke="#d4d9d0" strokeWidth="25"><path d="M-40 312L370 302L530 340L1060 340"/><path d="M395 -30L392 180L450 410L428 820"/><path d="M0 535L330 535L510 470L1040 460"/></g>
      <g fill="none" stroke="#fffefa" strokeWidth="20"><path d="M-40 312L370 302L530 340L1060 340"/><path d="M395 -30L392 180L450 410L428 820"/><path d="M0 535L330 535L510 470L1040 460"/></g>
      <g fill="none" stroke="#d8d9bd" strokeWidth="1.5" strokeDasharray="12 7"><path d="M0 312L370 302L530 340L1000 340"/><path d="M395 0L392 180L450 410L428 760"/></g>
      <g className={styles.streetLabels}><text x="95" y="296">青 禾 路</text><text x="583" y="331">邻 里 大 道</text><text x="86" y="526">南 苑 路</text><text x="606" y="456">文 景 路</text><text x="410" y="145" transform="rotate(90 410 145)">中 心 街</text></g>
      <g className={styles.placeLabels}><text x="207" y="186">青禾公园</text><text x="550" y="624">南苑绿地</text><text x="92" y="400">西里住区</text><text x="558" y="110">文景住区</text><text x="579" y="390">东里住区</text><text x="235" y="681">南苑住区</text><text x="875" y="180" transform="rotate(75 875 180)" fill="#6e9da4">青 禾 河</text></g>
      {result && layers.circle && <g data-testid="circle-layer"><polygon points={points(result.circle)} fill="url(#reach)" stroke="#238b77" strokeWidth="2.5" strokeDasharray="7 5"/><text x={result.circle[1].x} y={result.circle[1].y-14} className={styles.circleLabel}>15 分钟步行范围 · 演示</text></g>}
      {result && layers.blind && result.zones.filter(z=>filter==='all'||z.category===filter).map(z=><g key={z.id} data-testid={`zone-${z.status}`}><polygon points={points(z.points)} fill={z.status==='unknown'?'url(#unknownHatch)':'#859295'} fillOpacity=".5" stroke={z.status==='unknown'?'#bb9135':'#64777b'} strokeWidth="2" strokeDasharray="5 3"/><text x={z.position.x} y={z.position.y} textAnchor="middle" className={styles.zoneLabel}>{z.status==='unknown'?'? 数据待补充':'! 菜市场盲区'}</text></g>)}
      {result && layers.facilities && <g data-testid="facilities-layer">{filterFacilities(result,filter).map(f=><g key={f.id} data-testid={`facility-${f.category}`} role="button" tabIndex={0} aria-label={`${f.name}，${f.inCircle?'圈内':'圈外参考'}`} transform={`translate(${f.x} ${f.y})`} onPointerDown={e=>e.stopPropagation()} onClick={e=>{e.stopPropagation();locate(f);}} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();locate(f);}}} className={styles.mapMarker}>
        <circle r="17" fill="white" stroke={categoryMeta[f.category].color} strokeWidth="2" opacity={f.inCircle?1:.65}/><text textAnchor="middle" dy="5" fill={categoryMeta[f.category].color} fontSize="15" fontWeight="700">{categoryMeta[f.category].symbol}</text>{!f.inCircle&&<text y="30" textAnchor="middle" fill="#677980" fontSize="10">圈外参考</text>}
      </g>)}</g>}
      {samples.map((s,i)=><g key={s.id} role="button" tabIndex={0} aria-label={`选择演示点 ${String.fromCharCode(65+i)}`} transform={`translate(${s.position.x} ${s.position.y})`} onPointerDown={e=>e.stopPropagation()} onClick={e=>{e.stopPropagation();onFocus(undefined);onPick(s.center);}} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();onPick(s.center);}}} className={styles.mapMarker}>
        <circle r="12" fill="#fff" stroke="#466f70" strokeWidth="1.5"/><text dy="4" textAnchor="middle" fontSize="11" fill="#335b5b" fontWeight="700">{String.fromCharCode(65+i)}</text>
      </g>)}
      {visibleCenter && <g pointerEvents="none" transform={`translate(${centerPoint.x} ${centerPoint.y})`}><circle r="29" fill="#137c70" opacity=".12"/><circle r="20" fill="#fff"/><circle r="14" fill="#137c70"/><circle r="5" fill="#fff"/><rect x="-42" y="-52" width="84" height="24" rx="6" fill="#163f40"/><text y="-36" textAnchor="middle" fontSize="11" fill="#fff">分析中心点</text></g>}
      {focus && <circle cx={focus.x} cy={focus.y} r="25" fill="none" stroke="#203c4a" strokeWidth="2" strokeDasharray="4 3" pointerEvents="none"/>}
    </svg>
    <div className={styles.mapTools}><Tooltip title="放大"><Button aria-label="放大地图" icon={<PlusOutlined/>} onClick={()=>zoom(.8)}/></Tooltip><Tooltip title="缩小"><Button aria-label="缩小地图" icon={<MinusOutlined/>} onClick={()=>zoom(1.25)}/></Tooltip><Tooltip title="复位视图"><Button aria-label="复位地图" icon={<AimOutlined/>} onClick={()=>{setView({x:0,y:0,w:1000,h:760});onFocus(undefined);}}/></Tooltip></div>
    {focus && <div className={styles.mapPopup}><EnvironmentOutlined/><div><strong>{focus.name}</strong><p>{focus.detail}</p></div><Button size="small" type="text" aria-label="关闭地图详情" icon={<CloseOutlined/>} onClick={()=>onFocus(undefined)}/></div>}
    {loading && <div className={styles.mapLoading} role="status"><span className={styles.pulse}/>正在加载演示结果…</div>}
    {!visibleCenter && <div className={styles.mapNotice}>所选坐标超出示意地图，请选择预设演示点。</div>}
    <div className={styles.mapFoot}><span>拖动平移 · 点击选点 · A / B / C 为预设点</span><span>示意地图 / 非真实地理数据</span></div>
  </div>;
}
