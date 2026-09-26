/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 浏览器端 AK。API 模式缺少地图配置时只展示错误和结果摘要。 */
  readonly VITE_BAIDU_MAP_AK?: string;
  /** `checkup` 是 v2 体检工作台（`/api/v2`），不与上面几个旧接口模式混用。 */
  readonly VITE_ANALYSIS_MODE?: 'api' | 'baidu' | 'hybrid' | 'demo' | 'checkup';
  readonly VITE_API_BASE_URL?: string;
}
