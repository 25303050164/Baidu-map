/** 百度地图 JavaScript API（BMapGL/WebGL 版）的最小类型声明。
 *
 * 只声明本项目用到的 API 子集，避免引入第三方类型依赖。
 * 字段与官方文档 https://lbsyun.baidu.com/index.php?title=jspopularGL 对齐。
 */

export interface BMapPoint {
  lng: number;
  lat: number;
}

export interface BMapSize {
  width: number;
  height: number;
}

/** 地图 click 事件（GL 版坐标在 e.latlng 上）。 */
export interface BMapClickEvent {
  latlng: { lng: number; lat: number };
}

export interface BMapPolygonOptions {
  strokeColor?: string;
  fillColor?: string;
  strokeWeight?: number;
  strokeOpacity?: number;
  fillOpacity?: number;
  strokeStyle?: 'solid' | 'dashed';
}

export interface BMapLabelOptions {
  position?: BMapPoint;
  offset?: BMapSize;
}

export interface BMapLabel {
  setStyle(style: Partial<CSSStyleDeclaration>): void;
}

export interface BMapIconOptions {
  anchor?: BMapSize;
  imageOffset?: BMapSize;
}

export interface BMapIcon {
  /** 官方未暴露可用的公开成员，仅作标记类型传递给 Marker。 */
}

export interface BMapMarkerOptions {
  icon?: BMapIcon;
  offset?: BMapSize;
  title?: string;
}

export interface BMapInfoWindowOptions {
  offset?: BMapSize;
}

export interface BMapInfoWindow {}

/** 叠加物公共能力：事件监听（click 等事件参数本项目不消费）。 */
export interface BMapOverlay {
  addEventListener(type: string, handler: () => void): void;
  removeEventListener(type: string, handler: () => void): void;
}

export interface BMapMap {
  centerAndZoom(center: BMapPoint, zoom: number): void;
  panTo(center: BMapPoint): void;
  getCenter(): BMapPoint;
  zoomIn(): void;
  zoomOut(): void;
  enableScrollWheelZoom(enable: boolean): void;
  addOverlay(overlay: BMapOverlay): void;
  clearOverlays(): void;
  openInfoWindow(window: BMapInfoWindow, point: BMapPoint): void;
  closeInfoWindow(): void;
  addEventListener(type: 'click', handler: (event: BMapClickEvent) => void): void;
  /** GL 版提供 destroy；声明为可选，实例化失败时不致命。 */
  destroy?(): void;
}

/** 脚本加载完成后挂到 window 的全局对象。 */
export interface BaiduMapApi {
  Map: new (container: HTMLElement) => BMapMap;
  Point: new (lng: number, lat: number) => BMapPoint;
  Size: new (width: number, height: number) => BMapSize;
  Polygon: new (points: BMapPoint[] | string[], options?: BMapPolygonOptions) => BMapOverlay;
  Label: new (content: string, options?: BMapLabelOptions) => BMapOverlay & BMapLabel;
  Marker: new (point: BMapPoint, options?: BMapMarkerOptions) => BMapOverlay;
  Icon: new (url: string, size: BMapSize, options?: BMapIconOptions) => BMapIcon;
  InfoWindow: new (content: string, options?: BMapInfoWindowOptions) => BMapInfoWindow;
}

declare global {
  interface Window {
    BMapGL?: BaiduMapApi;
    /** 脚本就绪回调（名字与 baiduMapConfig.BAIDU_MAP_CALLBACK 保持一致）。 */
    __baiduMapReady__?: () => void;
  }
}
