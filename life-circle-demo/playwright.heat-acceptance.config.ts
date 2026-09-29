import { defineConfig, devices } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';

/**
 * 热力图真实底图验收：真 AK 加载的 BMapGL、真后端里存着的两条真实体检任务。
 *
 * 与 `playwright.checkup-live.config.ts` 的区别是**不花服务额度**：不创建任务，只凭存下来的
 * 任务标识恢复，用例里把创建、取消与路线核验请求一律挡掉并记账。后端最好以空的
 * `BAIDU_MAP_AK` 启动（`/health` 的 `baidu_ak_configured: false`），双保险。
 *
 * 页面与后端由操作者自己起，这里不起 webServer：
 *
 * - `HEAT_BASE_URL`（默认 http://127.0.0.1:5181）：Vite，`VITE_API_BASE_URL` 指向下面的后端，
 *   浏览器 AK 来自 `.env.local`，不在这里赋值（赋空串就成了降级地图）；
 * - `HEAT_API`（默认 http://127.0.0.1:8019）：后端，`CORS_ORIGINS` 要包含上面的源，
 *   `CHECKUP_DIR` 指向存有这两条任务的目录（建议复制一份，别直接用工作目录）。
 */
const out = resolve(process.env.HEAT_OUTPUT_DIR ?? 'output/heat-acceptance');
mkdirSync(out, { recursive: true });
process.env.TEMP = out;
process.env.TMP = out;

export default defineConfig({
  testDir: './tests', testMatch: 'heat-acceptance.spec.ts', workers: 1, fullyParallel: false,
  timeout: 240000, expect: { timeout: 20000 },
  outputDir: resolve(out, 'results'), reporter: [['list']],
  use: { baseURL: process.env.HEAT_BASE_URL ?? 'http://127.0.0.1:5181', ...devices['Desktop Edge'],
    channel: 'msedge', viewport: { width: 1440, height: 1000 },
    // 真实 SDK 请求 URL 含浏览器 AK：不录 Trace、不录视频，失败截图也不要（截图由用例自己挑时机）。
    trace: 'off', video: 'off', screenshot: 'off',
    actionTimeout: 30000, navigationTimeout: 60000 },
});
