import { defineConfig, devices } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
const temp = resolve('output/tmp');
mkdirSync(temp, { recursive: true });
process.env.TEMP = temp;
process.env.TMP = temp;
export default defineConfig({
  testDir: './tests', fullyParallel: false, workers: 1,
  timeout: 30000, expect: { timeout: 7000 },
  reporter: [['list'], ['html', { open: 'never' }]],
  outputDir: 'test-results',
  use: { baseURL: 'http://127.0.0.1:5173', ...devices['Desktop Edge'], channel: 'msedge', viewport: { width: 1440, height: 1000 }, trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  webServer: { command: 'npm run dev', url: 'http://127.0.0.1:5173', reuseExistingServer: true, timeout: 30000 }
});
