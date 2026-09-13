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

/** 地址分量：定位与逆地理结果共用，字段均可能缺失。 */
export interface BMapAddressComponent {
  province?: string;
  city?: string;
  district?: string;
  street?: string;
  streetNumber?: string;
}

/** 浏览器定位参数；与官方 PositionOptions 对齐。 */
export interface BMapPositionOptions {
  enableHighAccuracy?: boolean;
  timeout?: number;
  maximumAge?: number;
}

export interface BMapGeolocationResult {
  point: BMapPoint;
  /** 定位精度半径，单位米；缺失表示不可知。 */
  accuracy?: number;
  address?: BMapAddressComponent;
}

/** 浏览器定位（失败自动 IP 兜底）；成功坐标已是 BD09LL。 */
export interface BMapGeolocation {
  getCurrentPosition(
    callback: (this: BMapGeolocation | undefined, result: BMapGeolocationResult | null) => void,
    options?: BMapPositionOptions
  ): void;
  getStatus(): number;
}

export interface BMapLocalResultPoi {
  title: string;
  point: BMapPoint;
  address?: string;
  uid?: string;
  province?: string;
  city?: string;
}

/** 检索结果容器：官方提供 getPoi 与两个计数方法。 */
export interface BMapLocalSearchResult {
  getPoi(index: number): BMapLocalResultPoi;
  getCurrentNumPois(): number;
  getNumPois(): number;
}

export interface BMapLocalSearchOptions {
  /** 1 - 100，默认 10。 */
  pageCapacity?: number;
  /** 单个关键词时 results 为单个 LocalResult；不做数组关键词检索。 */
  onSearchComplete?: (results: BMapLocalSearchResult | null) => void;
}

export interface BMapLocalSearch {
  search(keyword: string): void;
  searchNearby(keyword: string, center: BMapPoint | string, radius: number): void;
  searchInBounds(keyword: string, bounds: BMapBounds): void;
  clearResults(): void;
}

/** 矩形范围，供区域检索使用；由 Bounds(sw, ne) 构造。 */
export interface BMapBounds {}

/** 地址解析：把地址/行政区名称转为 BD09LL 坐标；city 可选，缺省不限定城市。 */
export interface BMapGeocoder {
  getPoint(address: string, callback: (point: BMapPoint | null) => void, city?: string): void;
}

/** 脚本加载完成后挂到 window 的全局对象。 */
export interface BaiduMapApi {
  Map: new (container: HTMLElement) => BMapMap;
  Point: new (lng: number, lat: number) => BMapPoint;
  Size: new (width: number, height: number) => BMapSize;
  Polygon: new (points: BMapPoint[] | string[], options?: BMapPolygonOptions) => BMapOverlay;
  Polyline?: new (points: BMapPoint[], options?: BMapPolygonOptions) => BMapOverlay;
  Label: new (content: string, options?: BMapLabelOptions) => BMapOverlay & BMapLabel;
  Marker: new (point: BMapPoint, options?: BMapMarkerOptions) => BMapOverlay;
  Icon: new (url: string, size: BMapSize, options?: BMapIconOptions) => BMapIcon;
  InfoWindow: new (content: string, options?: BMapInfoWindowOptions) => BMapInfoWindow;
  /** 定位与检索构造器为可选：旧版脚本或测试替身可能不提供，调用方必须运行时判断。 */
  Geolocation?: new () => BMapGeolocation;
  LocalSearch?: new (location: string | BMapMap | BMapPoint, options?: BMapLocalSearchOptions) => BMapLocalSearch;
  Bounds?: new (sw: BMapPoint, ne: BMapPoint) => BMapBounds;
  Geocoder?: new () => BMapGeocoder;
}

declare global {
  interface Window {
    BMapGL?: BaiduMapApi;
    /** 脚本就绪回调（名字与 baiduMapConfig.BAIDU_MAP_CALLBACK 保持一致）。 */
    __baiduMapReady__?: () => void;
  }
}
