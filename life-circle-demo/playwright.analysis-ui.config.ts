import { defineConfig, devices } from '@playwright/test';
import { resolve } from 'node:path';

export default defineConfig({
  testDir: './tests', testMatch: 'analysis-ui.spec.ts', workers: 1,
  timeout: 30000, expect: { timeout: 10000 },
  outputDir: 'output/analysis-ui/results', reporter: [['list']],
  use: { baseURL: 'http://127.0.0.1:5179', ...devices['Desktop Edge'],
    channel: process.env.PLAYWRIGHT_BROWSER_CHANNEL,
    ...(process.env.PLAYWRIGHT_EXECUTABLE_PATH ? { executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH } : {}),
    trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  webServer: { command: 'npm run dev -- --port 5179', url: 'http://127.0.0.1:5179', reuseExistingServer: false,
    env: { VITE_ANALYSIS_MODE: 'api', VITE_API_BASE_URL: '', VITE_BAIDU_MAP_AK: 'offline-sdk-fixture',
      TEMP: resolve('output/analysis-ui'), TMP: resolve('output/analysis-ui') } },
});
