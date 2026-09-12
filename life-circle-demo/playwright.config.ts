import { defineConfig, devices } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
const artifacts = process.env.DEMO_OUTPUT_DIR || 'D:/CodexOutputs/isochrone-demo-regression';
const temp = resolve(artifacts, 'tmp');
mkdirSync(temp, { recursive: true });
process.env.TEMP = temp;
process.env.TMP = temp;
export default defineConfig({
  testDir: './tests', testMatch: 'demo.spec.ts', fullyParallel: false, workers: 1,
  timeout: 30000, expect: { timeout: 7000 },
  reporter: [['list'], ['html', { open: 'never', outputFolder: resolve(artifacts, 'report') }]],
  outputDir: resolve(artifacts, 'results'),
  use: { baseURL: 'http://127.0.0.1:5173', ...devices['Desktop Edge'], channel: 'msedge', viewport: { width: 1440, height: 1000 }, trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  webServer: { command: 'npm run dev', url: 'http://127.0.0.1:5173', reuseExistingServer: false, timeout: 30000, env: { VITE_ANALYSIS_MODE: 'demo', VITE_BAIDU_MAP_AK: '' } }
});
