import { defineConfig, devices } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';

/**
 * 体检任务生命周期与实时进度的真实底图验收：真 AK 加载的 BMapGL，后端是
 * `backend/tests/checkup_lifecycle_app.py` —— 生产的任务、修订、进度与取消，替换掉的只有出进程的
 * 那一层（成圈解析解、合成的设施检索与路线核验），并能在创建、载入路网、采样、检索、评估、核验处
 * 按需停住与放行。**不花任何百度服务额度**：后端以空的 `BAIDU_MAP_AK` 启动，进程里的套接字守卫
 * 拒绝一切非回环连接并记数，用例最后要求这个数为 0。
 *
 * 两个服务都由这里起（已有的会被复用）：
 *
 * - 后端 8021：`LIFECYCLE_DIR` 下是这一轮的任务库与额度账本，每次从空目录开始；启动时预载本机的
 *   OSM 步行路网，`/control/ready` 在载完之前回 503 并带上载到哪一步、处理了多少条边（本机首次
 *   载入十几分钟，大半在逐条校验边）。
 * - Vite 5182：`VITE_API_BASE_URL` 指向上面的后端，浏览器 AK 来自 `.env.local`，不在这里赋值。
 */
const out = resolve(process.env.LIFECYCLE_OUTPUT_DIR ?? 'output/checkup-lifecycle');
mkdirSync(out, { recursive: true });
process.env.TEMP = out;
process.env.TMP = out;

const API_PORT = Number(process.env.LIFECYCLE_API_PORT ?? 8021);
const WEB_PORT = Number(process.env.LIFECYCLE_WEB_PORT ?? 5182);
const data = resolve(process.env.LIFECYCLE_DIR ?? '../.tmp/checkup-lifecycle');

export default defineConfig({
  testDir: './tests', testMatch: 'checkup-lifecycle.spec.ts', workers: 1, fullyParallel: false,
  timeout: 600000, expect: { timeout: 20000 },
  outputDir: resolve(out, 'results'), reporter: [['list']],
  use: { baseURL: `http://127.0.0.1:${WEB_PORT}`, ...devices['Desktop Edge'],
    channel: 'msedge', viewport: { width: 1440, height: 1000 },
    // 真实 SDK 请求 URL 含浏览器 AK：不录 Trace、不录视频，失败截图也不要（截图由用例自己挑时机）。
    trace: 'off', video: 'off', screenshot: 'off',
    actionTimeout: 30000, navigationTimeout: 60000 },
  webServer: [
    {
      command: `.venv\\Scripts\\python.exe -m uvicorn checkup_lifecycle_app:app --app-dir tests `
        + `--host 127.0.0.1 --port ${API_PORT} --no-access-log`,
      cwd: resolve('../backend'), url: `http://127.0.0.1:${API_PORT}/control/ready`,
      timeout: 1800000, reuseExistingServer: true, stdout: 'ignore', stderr: 'pipe',
      env: { BAIDU_MAP_AK: '', PYTHONIOENCODING: 'utf-8', LIFECYCLE_DIR: data,
        LIFECYCLE_ORIGIN: `http://127.0.0.1:${WEB_PORT}` },
    },
    {
      command: `npx vite --host 127.0.0.1 --port ${WEB_PORT} --strictPort`,
      url: `http://127.0.0.1:${WEB_PORT}`, timeout: 120000, reuseExistingServer: true,
      stdout: 'ignore', stderr: 'pipe',
      env: { VITE_API_BASE_URL: `http://127.0.0.1:${API_PORT}` },
    },
  ],
});
