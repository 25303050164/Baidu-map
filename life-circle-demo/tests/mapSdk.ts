/**
 * 离线的 BMapGL 替身，两个界面测试共用。
 *
 * 它存在的理由与真实 SDK 的差别无关，而在于**审计能力**：`window.__mapAudit` 记下每一次
 * 创建、每一片被画出来的形状、每一枚标记的 uid 与每一个被平移到的中心。旧分析页与 v2
 * 体检页的位置断言、图层重建断言、聚合断言都读它。
 *
 * 约定（两个测试文件都依赖，改动要一起看）：
 *
 * - `paths` 是**当前挂在图上**的多边形环，`removeOverlay` 会同步摘掉它 —— 因此"取消勾选
 *   某一层之后图上还剩什么"可以断言，而 `clearOverlays()` 清空的痕迹同样看得见；
 * - `fills` 与 `paths` 同进同出，记下每片多边形的填充不透明度：开热力时圈面只描边，
 *   这件事只能从这里看出来；
 * - `markers` 里同一枚标记的 `uid` 只在重建时变化，用来分辨"重画"与"新建"；
 * - `pointToOverlayPixel` 同时供覆盖物定位与测试取样使用，所以像素断言和图层看到的是
 *   同一个坐标系（`DEG_PER_PX` 是它的换算率，不是真实地图比例尺）。
 */
import type { Page } from '@playwright/test';

export async function installMapSdk(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const audit = { creations: 0, active: 0, paths: [] as string[][],
      fills: [] as { rings: string[]; fillOpacity: number | null }[],
      polylines: [] as { lng: number; lat: number }[][],
      markers: [] as Marker[], click: undefined as undefined | ((e: unknown) => void),
      panes: {} as Record<string, HTMLElement>,
      views: {} as Record<string, (() => void)[]>,
      pans: [] as { lng: number; lat: number }[],
      project: undefined as undefined | ((lng: number, lat: number) => { x: number; y: number }) };
    let iconSeq = 0;
    let markerSeq = 0;
    // 简化投影：像素网格与真实 SDK 无关，但同一份 pointToOverlayPixel 同时供覆盖物
    // 定位和测试取样使用，因此像素断言与图层看到的是同一个坐标系。
    const DEG_PER_PX = 0.00005;
    // 与真实 BMapGL 同构：addOverlay 缓存 initialize 的返回值为 domElement，基类 remove 负责清空它。
    class Overlay {
      handlers: Record<string, () => void> = {};
      domElement: HTMLElement | null = null;
      addEventListener(type: string, handler: () => void) { this.handlers[type] = handler; }
      removeEventListener() {}
      remove() { this.domElement?.parentNode?.removeChild(this.domElement); this.domElement = null; }
    }
    class Point { constructor(public lng: number, public lat: number) {} }
    class Size { constructor(public width: number, public height: number) {} }
    class Icon { seq = ++iconSeq; constructor(public url: string, public size: Size, public options: { anchor?: Size }) {} }
    // uid 是标记的身份：图层没有重建时，同一枚标记必须保持同一个 uid。
    class Marker extends Overlay { uid = ++markerSeq; constructor(public point: Point, public options: { title: string; icon?: Icon }) { super(); } }
    class Polygon extends Overlay { constructor(public rings: string[], public options: { fillOpacity?: number } = {}) { super(); } }
    class Label extends Overlay { setStyle() {} }
    class Polyline extends Overlay {
      path: { lng: number; lat: number }[];
      constructor(public points: Point[]) { super(); this.path = points.map(p => ({ lng: p.lng, lat: p.lat })); audit.polylines.push(this.path); }
    }
    class Map {
      centre = { lng: 116.404, lat: 39.915 };
      panes: Record<string, HTMLElement> = {};
      overlays: { initialize?: (map: Map) => HTMLElement | undefined; remove?: () => void }[] = [];
      constructor(public el: HTMLElement) {
        audit.creations++; audit.active++;
        el.addEventListener('click', () => audit.click?.({ latlng: { lng: 116.405, lat: 39.916 } }));
        // 真实 SDK 会在容器内建自己的覆盖物容器；这里建同样的三层，尺寸取自容器。
        for (const name of ['floatPane', 'markerPane', 'overlayPane', 'mapPane', 'labelPane']) {
          const pane = document.createElement('div');
          pane.setAttribute('data-pane', name);
          pane.style.position = 'absolute'; pane.style.left = '0'; pane.style.top = '0';
          pane.style.width = '100%'; pane.style.height = '100%';
          el.appendChild(pane);
          this.panes[name] = pane;
        }
        audit.panes = this.panes;
        audit.project = (lng: number, lat: number) => this.pointToOverlayPixel(new Point(lng, lat));
      }
      viewport() { return { width: this.panes.overlayPane?.clientWidth ?? 0, height: this.panes.overlayPane?.clientHeight ?? 0 }; }
      getPanes() { return this.panes; }
      getCenter() { return new Point(this.centre.lng, this.centre.lat); }
      pointToOverlayPixel(point: Point) {
        const { width, height } = this.viewport();
        return { x: (point.lng - this.centre.lng) / DEG_PER_PX + width / 2,
          y: (this.centre.lat - point.lat) / DEG_PER_PX + height / 2 };
      }
      centerAndZoom(point: Point) { this.centre = { lng: point.lng, lat: point.lat }; }
      panTo(point: Point) { this.centre = { lng: point.lng, lat: point.lat }; audit.pans.push({ lng: point.lng, lat: point.lat }); }
      enableScrollWheelZoom() {}
      // 视角事件由覆盖物与图层订阅（moveend/zoomend/resize），但不能顶掉地图自身的选点回调。
      addEventListener(type: string, handler: (e: unknown) => void) {
        if (type === 'click') { audit.click = handler as (e: unknown) => void; return; }
        (audit.views[type] ??= []).push(handler as () => void);
      }
      removeEventListener(type: string, handler: (e: unknown) => void) {
        audit.views[type] = (audit.views[type] ?? []).filter(item => item !== handler);
      }
      /** 测试里模拟真实平移/缩放结束：图层必须据此重新投影。 */
      fireView(type: string) { for (const handler of [...(audit.views[type] ?? [])]) handler(); }
      addOverlay(overlay: Polygon | Marker) {
        if (overlay instanceof Polygon) {
          audit.paths.push(overlay.rings);
          audit.fills.push({ rings: overlay.rings, fillOpacity: overlay.options.fillOpacity ?? null });
        }
        if (overlay instanceof Marker) audit.markers.push(overlay);
        // 自定义覆盖物（如热力 Canvas）由 SDK 调 initialize 并接管返回的元素。真实 SDK 的
        // Overlay.prototype._i 只在还没有 domElement 时才调 initialize —— 覆盖物自己的 remove
        // 若不清空它，再次 addOverlay 就什么也不会发生；这里照做，好让回归套件抓到这类问题。
        const custom = overlay as { initialize?: (map: Map) => HTMLElement | undefined; domElement?: HTMLElement | null };
        this.overlays.push(custom);
        if (custom.domElement) return;
        const element = typeof custom.initialize === 'function' ? custom.initialize(this) : undefined;
        if (element) custom.domElement = element;
        if (element && !element.parentNode) this.panes.overlayPane.appendChild(element);
      }
      // 真实 SDK 的 removeOverlay 只摘掉这一枚覆盖物；审计数组必须同步，否则"取消勾选后
      // 图上还剩什么"就无法断言（clearOverlays 会把整张图连热力画布一起清空）。
      removeOverlay(overlay: unknown) {
        this.overlays = this.overlays.filter(item => item !== overlay);
        const rings = (overlay as { rings?: string[] }).rings;
        if (rings) {
          audit.paths = audit.paths.filter(path => path !== rings);
          audit.fills = audit.fills.filter(fill => fill.rings !== rings);
        }
        audit.markers = audit.markers.filter(marker => marker !== overlay);
        const path = (overlay as { path?: { lng: number; lat: number }[] }).path;
        if (path) audit.polylines = audit.polylines.filter(item => item !== path);
        (overlay as { remove?: () => void }).remove?.();
      }
      clearOverlays() {
        this.overlays.forEach(overlay => overlay.remove?.());
        this.overlays = []; audit.paths = []; audit.fills = []; audit.markers = []; audit.polylines = [];
      }
      destroy() { audit.active--; }
    }
    Object.assign(window, { BMapGL: { Map, Point, Size, Icon, Polygon, Marker, Label, Polyline, Overlay }, __mapAudit: audit });
  });
}
