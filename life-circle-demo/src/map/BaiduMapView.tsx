/** 百度地图实景视图：与 DemoMap（示意地图）同 Props，叠加演示数据图层。
 *
 * 坐标说明：分析数据（设施/等时圈/盲区）沿用示意街区平面坐标，经 pointToCenter
 * 线性换算为经纬度后叠加；点击选点直接取百度地图坐标（六位小数），通过 onPick
 * 走既有分析链路，分析逻辑不变。
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { Button, Tooltip } from 'antd';
import { AimOutlined, PlusOutlined, MinusOutlined } from '@ant-design/icons';
import { categoryMeta, type Category } from '../types';
import { filterFacilities, inMapBounds, pointToCenter } from '../domain';
import type { Props } from '../Map';
import type { BaiduMapApi, BMapIcon, BMapMap, BMapOverlay } from './baiduMapTypes';
import { createDotIcon } from './mapIcons';
import styles from '../styles.module.css';

/** 演示街区（示意平面 1000x760）的经纬度四角，画边界提示数据范围。 */
const DEMO_REGION = [{ x: 0, y: 0 }, { x: 1000, y: 0 }, { x: 1000, y: 760 }, { x: 0, y: 760 }].map(p => pointToCenter(p));
/** 默认视图：演示街区中心，能看到整个演示范围。 */
const DEFAULT_ZOOM = 16;
/** 中心点变化超过该经纬度阈值才平移视图（约 50m），避免微调抖动。 */
const PAN_THRESHOLD = 0.0005;
const round6 = (value: number) => Number(value.toFixed(6));
const escapeHtml = (text: string) => text.replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch] ?? ch);

export function BaiduMapView({ api, onInitError, center, samples, result, filter, layers, focus, onFocus, onPick, loading }: Props & { api: BaiduMapApi; onInitError: () => void }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [map, setMap] = useState<BMapMap | null>(null);
  // 标记点击会连带动一次地图 click；用标记抑制，避免选点误触发。setTimeout 后自动复位。
  const suppressMapClick = useRef(false);
  // 持有最新回调与中心点，避免重建地图实例上的监听。
  const onPickRef = useRef(onPick); onPickRef.current = onPick;
  const onFocusRef = useRef(onFocus); onFocusRef.current = onFocus;
  const onInitErrorRef = useRef(onInitError); onInitErrorRef.current = onInitError;
  const centerRef = useRef(center); centerRef.current = center;

  const icons = useMemo(() => ({
    facility: Object.fromEntries(
      (['market', 'pharmacy', 'school'] as const).map(c => [c, createDotIcon(api, { color: categoryMeta[c].color, text: categoryMeta[c].symbol })])
    ) as Record<Category, BMapIcon | undefined>,
    sample: samples.map((s, i) => createDotIcon(api, { color: '#466f70', text: String.fromCharCode(65 + i), textColor: '#335b5b' })),
    center: createDotIcon(api, { color: '#137c70', layered: true })
  }), [api, samples]);

  // 初始化：容器、默认中心/缩放、滚轮缩放、点击选点。卸载时销毁实例；初始化抛错时回退示意地图。
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    let instance: BMapMap | null = null;
    // StrictMode 会立即执行一次 setup → cleanup → setup。先取消试运行，
    // 避免 SDK 异步创建 pane 时访问已被 destroy 的地图。
    const frame = requestAnimationFrame(() => {
      try {
        instance = new api.Map(el);
        const initial = centerRef.current;
        instance.centerAndZoom(new api.Point(initial.lng, initial.lat), DEFAULT_ZOOM);
        instance.enableScrollWheelZoom(true);
        instance.addEventListener('click', e => {
          if (suppressMapClick.current) return;
          onFocusRef.current(undefined);
          onPickRef.current({ lng: round6(e.latlng.lng), lat: round6(e.latlng.lat) });
        });
        setMap(instance);
      } catch {
        instance?.destroy?.();
        instance = null;
        onInitErrorRef.current();
      }
    });
    return () => { cancelAnimationFrame(frame); instance?.destroy?.(); };
  }, [api]);

  // 中心点变化（样例切换/坐标应用/选点）时平移跟随。
  useEffect(() => {
    if (!map) return;
    const current = map.getCenter();
    if (Math.abs(current.lng - center.lng) >= PAN_THRESHOLD || Math.abs(current.lat - center.lat) >= PAN_THRESHOLD) {
      map.panTo(new api.Point(center.lng, center.lat));
    }
  }, [center, api, map]);

  // 图层渲染：依赖变化时全量重绘（演示数据规模小，简单可靠）。
  useEffect(() => {
    if (!map) return;
    map.closeInfoWindow();
    map.clearOverlays();
    const add = (overlay: BMapOverlay) => { map.addOverlay(overlay); };
    const point = (lng: number, lat: number) => new api.Point(lng, lat);
    const toPoint = (x: number, y: number) => { const c = pointToCenter({ x, y }); return point(c.lng, c.lat); };

    // 演示街区边界：提示演示数据覆盖范围
    add(new api.Polygon(DEMO_REGION.map(c => point(c.lng, c.lat)), { strokeColor: '#79a8a0', strokeWeight: 1.5, strokeStyle: 'dashed', fillColor: '#49b89d', fillOpacity: 0.04 }));
    const regionLabel = new api.Label('青禾街区 · 演示数据范围', { position: point(DEMO_REGION[0].lng, DEMO_REGION[0].lat), offset: new api.Size(8, -20) });
    regionLabel.setStyle({ color: '#6c8b84', borderColor: '#cfe0da', background: '#f4faf7', fontSize: '10px', padding: '1px 6px' });
    add(regionLabel);

    // 15 分钟范围（演示）
    if (result && layers.circle) {
      add(new api.Polygon(result.circle.map(p => toPoint(p.x, p.y)), { strokeColor: '#238b77', strokeWeight: 2.5, strokeStyle: 'dashed', fillColor: '#49b89d', fillOpacity: 0.16 }));
      const anchor = pointToCenter(result.circle[1]);
      const label = new api.Label('15 分钟步行范围 · 演示', { position: point(anchor.lng, anchor.lat), offset: new api.Size(0, -10) });
      label.setStyle({ color: '#1d6e5f', borderColor: '#bfe0d6', background: '#f2faf7', fontSize: '11px', padding: '2px 6px' });
      add(label);
    }

    // 问题区域：blind 实底 / unknown 网格感（黄色系）
    if (result && layers.blind) {
      for (const zone of result.zones.filter(z => filter === 'all' || z.category === filter)) {
        const unknown = zone.status === 'unknown';
        add(new api.Polygon(zone.points.map(p => toPoint(p.x, p.y)), { strokeColor: unknown ? '#bb9135' : '#64777b', strokeWeight: 2, strokeStyle: 'dashed', fillColor: unknown ? '#f2d488' : '#859295', fillOpacity: 0.42 }));
        const pos = pointToCenter(zone.position);
        const label = new api.Label(unknown ? '? 数据待补充' : `! ${categoryMeta[zone.category].label}盲区`, { position: point(pos.lng, pos.lat), offset: new api.Size(0, -8) });
        label.setStyle({ color: unknown ? '#8a6a1f' : '#4d5f63', background: 'rgba(255,255,255,.92)', border: 'none', padding: '1px 4px', fontSize: '11px', fontWeight: '700' });
        add(label);
      }
    }

    // 圈内设施标记：点击查看详情（与示意地图一致）
    if (result && layers.facilities) {
      for (const facility of filterFacilities(result, filter)) {
        const pos = pointToCenter({ x: facility.x, y: facility.y });
        const marker = new api.Marker(point(pos.lng, pos.lat), { icon: icons.facility[facility.category] });
        marker.addEventListener('click', () => {
          suppressMapClick.current = true;
          setTimeout(() => { suppressMapClick.current = false; }, 0);
          onFocusRef.current({ ...facility, detail: `${categoryMeta[facility.category].label} · 15 分钟圈内（演示）`, nonce: Date.now() });
        });
        add(marker);
      }
    }

    // 预设演示点 A / B / C：点击选为中心点
    samples.forEach((sample, i) => {
      const marker = new api.Marker(point(sample.center.lng, sample.center.lat), { icon: icons.sample[i] });
      marker.addEventListener('click', () => {
        suppressMapClick.current = true;
        setTimeout(() => { suppressMapClick.current = false; }, 0);
        onFocusRef.current(undefined);
        onPickRef.current(sample.center);
      });
      add(marker);
    });

    // 分析中心点（越界时不显示，仅提示）
    const visibleCenter = inMapBounds(center);
    if (visibleCenter) {
      add(new api.Marker(point(center.lng, center.lat), { icon: icons.center }));
      const label = new api.Label('分析中心点', { position: point(center.lng, center.lat), offset: new api.Size(0, -38) });
      label.setStyle({ color: '#fff', background: '#163f40', border: 'none', padding: '2px 8px', borderRadius: '6px', fontSize: '11px' });
      add(label);
    }

    // 焦点：平移到目标并用信息窗展示（对应示意地图的 focus 弹层）
    if (focus) {
      const pos = pointToCenter({ x: focus.x, y: focus.y });
      const target = point(pos.lng, pos.lat);
      const current = map.getCenter();
      if (Math.abs(current.lng - pos.lng) >= PAN_THRESHOLD || Math.abs(current.lat - pos.lat) >= PAN_THRESHOLD) map.panTo(target);
      const info = new api.InfoWindow(
        `<div style="min-width:150px"><strong>${escapeHtml(focus.name)}</strong><p style="margin:4px 0 0;color:#5b6b66;font-size:12px">${escapeHtml(focus.detail)}</p></div>`,
        { offset: new api.Size(0, -28) }
      );
      map.openInfoWindow(info, target);
    }
  }, [api, map, result, filter, layers, samples, center, focus, icons]);

  return <div className={styles.mapSurface}>
    <div ref={containerRef} className={styles.baiduMapContainer} role="application" aria-label="百度地图，点击选择分析中心点"/>
    <div className={styles.mapHeading}><span className={styles.liveDot}/><span>街区空间视图</span><span className={styles.smallMuted}>百度地图实景</span></div>
    <div className={styles.mapTools}>
      <Tooltip title="放大"><Button aria-label="放大地图" icon={<PlusOutlined/>} onClick={()=>map?.zoomIn()}/></Tooltip>
      <Tooltip title="缩小"><Button aria-label="缩小地图" icon={<MinusOutlined/>} onClick={()=>map?.zoomOut()}/></Tooltip>
      <Tooltip title="复位视图"><Button aria-label="复位地图" icon={<AimOutlined/>} onClick={()=>{const c=pointToCenter({x:500,y:380});map?.centerAndZoom(new api.Point(c.lng,c.lat),DEFAULT_ZOOM);onFocus(undefined);}}/></Tooltip>
    </div>
    {loading && <div className={styles.mapLoading} role="status"><span className={styles.pulse}/>正在加载演示结果…</div>}
    {!inMapBounds(center) && <div className={styles.mapNotice}>所选坐标超出演示街区范围，附近暂无演示数据，请在青禾街区内选点。</div>}
    <div className={styles.mapFoot}><span>拖动平移 · 滚轮缩放 · 点击选点 · A / B / C 为预设点</span><span>百度地图实景 · 演示数据叠加</span></div>
  </div>;
}
