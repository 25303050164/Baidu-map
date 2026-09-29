import { defineConfig, devices } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';

/**
 * 水系复核的真实底图验收：真 AK 加载的 BMapGL、真后端里存着的体检任务（离线重算后的第 7 版
 * 与早于复核的旧版各有）。与热力验收一样**不花服务额度**：只凭存下来的任务标识恢复，用例里把
 * 创建、取消与路线核验请求一律挡掉并记账；后端最好以空的 `BAIDU_MAP_AK` 启动。
 *
 * 页面与后端由操作者自己起，这里不起 webServer：
 *
 * - `WATER_BASE_URL`（默认 http://127.0.0.1:5181）：Vite，`VITE_API_BASE_URL` 指向下面的后端，
 *   浏览器 AK 来自 `.env.local`，不在这里赋值；
 * - `WATER_API`（默认 http://127.0.0.1:8019）：后端，`CORS_ORIGINS` 要包含上面的源，
 *   `CHECKUP_DIR` 指向存有这些任务的目录（`scripts/recompute_checkups.py` 重算过的那一份副本）。
 */
const out = resolve(process.env.WATER_OUTPUT_DIR ?? 'output/water-acceptance');
mkdirSync(out, { recursive: true });
process.env.TEMP = out;
process.env.TMP = out;

export default defineConfig({
  testDir: './tests', testMatch: 'water-acceptance.spec.ts', workers: 1, fullyParallel: false,
  timeout: 240000, expect: { timeout: 20000 },
  outputDir: resolve(out, 'results'), reporter: [['list']],
  use: { baseURL: process.env.WATER_BASE_URL ?? 'http://127.0.0.1:5181', ...devices['Desktop Edge'],
    channel: 'msedge', viewport: { width: 1440, height: 1000 },
    // 真实 SDK 请求 URL 含浏览器 AK：不录 Trace、不录视频，失败截图也不要（截图由用例自己挑时机）。
    trace: 'off', video: 'off', screenshot: 'off',
    actionTimeout: 30000, navigationTimeout: 60000 },
});
