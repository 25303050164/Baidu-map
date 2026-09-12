/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 百度地图 JavaScript API 密钥（浏览器端 AK）。未配置时地图回退为本地示意地图。 */
  readonly VITE_BAIDU_MAP_AK?: string;
}
