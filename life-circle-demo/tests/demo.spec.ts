import { test, expect } from '@playwright/test';
import { mkdirSync } from 'node:fs';
test.beforeEach(async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('button', { name: '开始体检', exact: true })).toBeEnabled();
});
async function expectAnalysisLoadingPasses(page: import('@playwright/test').Page) {
  await expect(page.getByTestId('analysis-loading')).toBeVisible();
  await expect(page.getByTestId('analysis-loading')).not.toBeVisible({ timeout: 10000 });
}
async function closeAutoOpenedReport(page: import('@playwright/test').Page) {
  await expect(page.getByTestId('report')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('report')).not.toBeVisible();
}
async function analyze(page: import('@playwright/test').Page) {
  await page.getByRole('button', { name: '开始体检', exact: true }).click();
  await expectAnalysisLoadingPasses(page);
  await closeAutoOpenedReport(page);
  await expect(page.getByTestId('circle-layer')).toBeVisible();
}
test('complete flow, filters, layers, facilities and full report', async ({ page }) => {
  const errors: string[] = [], external: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', e => { if (e.type() === 'error') errors.push(e.text()); });
  page.on('request', r => { if (!r.url().startsWith('http://127.0.0.1:5173') && !r.url().startsWith('data:')) external.push(r.url()); });
  mkdirSync('output/playwright', { recursive: true });
  await page.screenshot({ path: 'output/playwright/desktop-initial.png', fullPage: true, animations: 'disabled' });
  await analyze(page);
  await expect(page.getByTestId('total-count')).toHaveText('7');
  await page.getByRole('button', { name: '药店', exact: true }).click();
  await expect(page.getByTestId('facility-market')).toHaveCount(0);
  await expect(page.getByTestId('facility-pharmacy')).toHaveCount(2);
  await page.getByRole('checkbox', { name: '民生设施', exact: true }).uncheck();
  await expect(page.getByTestId('facilities-layer')).toHaveCount(0);
  await page.getByRole('checkbox', { name: '民生设施', exact: true }).check();
  await page.getByRole('button', { name: '青禾药房，圈内', exact: true }).click();
  await expect(page.getByRole('button', { name: '关闭地图详情' })).toBeVisible();
  await page.getByRole('button', { name: '复位地图' }).click();
  await page.getByRole('button', { name: '全部设施', exact: true }).click();
  await page.screenshot({ path: 'output/playwright/desktop-result.png', fullPage: true, animations: 'disabled' });
  await page.getByRole('button', { name: /查看完整体检报告/ }).click();
  await expect(page.getByTestId('report')).toContainText('全部类别，不受主界面筛选影响');
  await expect(page.getByTestId('facility-stats-table').getByRole('row')).toHaveCount(4);
  await expect(page.getByTestId('zone-stats-table').getByRole('row')).toHaveCount(4);
  await page.screenshot({ path: 'output/playwright/report.png', fullPage: true, animations: 'disabled' });
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('report')).not.toBeVisible();
  expect(errors).toEqual([]);
  expect(external).toEqual([]);
});
test('missing and unknown data remain distinct', async ({ page }) => {
  await page.getByLabel('演示场景', { exact: true }).selectOption('missing');
  await analyze(page);
  await expect(page.getByTestId('zone-blind')).toHaveCount(1);
  await expect(page.getByTestId('count-market')).toHaveText('3');
  await page.getByRole('button', { name: /东北住区.*服务盲区/ }).click();
  await expect(page.getByRole('button', { name: '关闭地图详情' })).toBeVisible();
  await page.screenshot({ path: 'output/playwright/desktop-blind.png', fullPage: true, animations: 'disabled' });
  await page.getByLabel('演示场景', { exact: true }).selectOption('insufficient');
  await expect(page.getByText(/条件已修改，需重新分析/)).toBeVisible();
  await page.getByRole('button', { name: '开始体检', exact: true }).click();
  await expectAnalysisLoadingPasses(page);
  await closeAutoOpenedReport(page);
  await expect(page.getByTestId('zone-unknown')).toHaveCount(1);
  await expect(page.getByTestId('zone-blind')).toHaveCount(0);
  await expect(page.getByTestId('count-pharmacy')).toHaveText('—');
});
test('failure recovers on retry and custom position never inherits a result', async ({ page }) => {
  await page.getByLabel('演示场景', { exact: true }).selectOption('failure');
  await page.getByRole('button', { name: '开始体检', exact: true }).click();
  // 失败时 Loading 提前结束，错误横幅接管，报告不弹出。
  await expectAnalysisLoadingPasses(page);
  await expect(page.getByText(/本次模拟分析失败/)).toBeVisible();
  await expect(page.getByTestId('report')).not.toBeVisible();
  await page.getByRole('button', { name: '重试分析', exact: true }).click();
  await expectAnalysisLoadingPasses(page);
  await expect(page.getByTestId('total-count')).toHaveText('7');
  await closeAutoOpenedReport(page);
  await page.getByRole('textbox', { name: '中心点经度' }).fill('110');
  await page.getByRole('button', { name: '应用坐标', exact: true }).click();
  await page.getByRole('button', { name: '开始体检', exact: true }).click();
  await expectAnalysisLoadingPasses(page);
  await expect(page.getByText(/超出示意地图范围/)).toBeVisible();
  await expect(page.getByText(/下方保留的是/)).toBeVisible();
  await expect(page.getByTestId('report')).not.toBeVisible();
});
test('report stays accessible from the header entry after closing', async ({ page }) => {
  // 尚无报告时主界面不出现入口
  await expect(page.getByRole('button', { name: '查看体检报告', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '开始体检', exact: true }).click();
  await expectAnalysisLoadingPasses(page);
  await closeAutoOpenedReport(page);
  // 关闭报告后：页头入口出现，悬停可见最近报告信息，点击可重新打开
  const entry = page.getByRole('button', { name: '查看体检报告', exact: true });
  await expect(entry).toBeVisible();
  await entry.hover();
  await expect(page.getByText(/最近报告：青禾街区 · A 点/)).toBeVisible();
  await entry.click();
  await expect(page.getByTestId('report')).toBeVisible();
  await expect(page.getByTestId('report')).toContainText('青禾街区 · A 点');
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('report')).not.toBeVisible();
  // 再次打开：报告生命周期持续
  await entry.click();
  await expect(page.getByTestId('report')).toBeVisible();
  // 条件变更后：旧报告仍可访问，并带过期提示
  await page.keyboard.press('Escape');
  await page.getByLabel('演示场景', { exact: true }).selectOption('missing');
  await entry.click();
  await expect(page.getByTestId('report')).toBeVisible();
  await expect(page.getByTestId('report')).toContainText('分析条件已修改');
  await expect(page.getByTestId('report')).toContainText('青禾街区 · A 点');
});
test('all presets, coordinates and retained old results', async ({ page }) => {
  // 每次“开始体检”都会完整播放 AI 体检 Loading（约 6s），放宽本用例超时。
  test.slow();
  await analyze(page);
  for (const id of ['b','c']) {
    await page.getByLabel('演示样例', { exact: true }).selectOption(id);
    await page.getByRole('button', { name: '开始体检', exact: true }).click();
    await expectAnalysisLoadingPasses(page);
    await closeAutoOpenedReport(page);
    await expect(page.getByText(/条件已修改，需重新分析/)).not.toBeVisible();
    await expect(page.getByTestId('total-count')).toHaveText('6');
  }
  await page.getByRole('button', { name: '选择演示点 A', exact: true }).press('Enter');
  await page.getByRole('button', { name: '开始体检', exact: true }).click();
  await expectAnalysisLoadingPasses(page);
  await closeAutoOpenedReport(page);
  await page.getByLabel('演示样例', { exact: true }).selectOption('b');
  await expect(page.getByRole('textbox', { name: '中心点经度' })).toHaveValue('116.403');
  await expect(page.getByText(/下方保留的是「青禾街区 · A 点/)).toBeVisible();
  await page.getByRole('textbox', { name: '中心点纬度' }).fill('99');
  await page.getByRole('button', { name: '应用坐标', exact: true }).click();
  await expect(page.getByText(/请输入有效经纬度/)).toBeVisible();
});
test('mobile panels and report are operable without overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByLabel('演示场景', { exact: true }).selectOption('missing');
  await page.getByRole('button', { name: '开始体检', exact: true }).click();
  await expectAnalysisLoadingPasses(page);
  await closeAutoOpenedReport(page);
  await expect(page.getByTestId('zone-blind')).toBeVisible();
  await page.screenshot({ path: 'output/playwright/mobile-map.png', fullPage: true, animations: 'disabled' });
  await page.getByRole('button', { name: '查看结果', exact: true }).click();
  await page.getByRole('button', { name: /查看完整体检报告/ }).click();
  await expect(page.getByTestId('report')).toBeVisible();
  await page.screenshot({ path: 'output/playwright/mobile-report.png', fullPage: true, animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('medium width stacks the analysis panel below the map at equal width', async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 1000 });
  await expect(page.getByRole('heading', { name: '分析设置' })).toBeVisible();
  const mapBox = (await page.getByLabel('可交互演示地图', { exact: true }).evaluate(el => {
    const r = el.closest('section')!.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height };
  }))!;
  const panelBox = await page.getByRole('heading', { name: '分析设置' }).evaluate(el => {
    const r = el.closest('aside')!.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height };
  });
  expect(panelBox.y).toBeGreaterThan(mapBox.y + mapBox.height - 6);
  expect(Math.abs(panelBox.x - mapBox.x)).toBeLessThan(4);
  expect(Math.abs(panelBox.width - mapBox.width)).toBeLessThan(4);
  await page.screenshot({ path: 'output/playwright/tablet-stacked.png', fullPage: true, animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('very narrow width shows an interaction notice without overflow', async ({ page }) => {
  await page.setViewportSize({ width: 340, height: 800 });
  await expect(page.getByText('窗口过窄，交互空间有限，请加宽窗口或横屏使用。')).toBeVisible();
  await page.screenshot({ path: 'output/playwright/narrow-notice.png', fullPage: true, animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('zoom and pan preserve the selection; background click selects a new position', async ({ page }) => {
  const svg = page.getByLabel('可交互演示地图', { exact: true });
  const initial = await svg.getAttribute('viewBox');
  await page.getByRole('button', { name: '放大地图', exact: true }).click();
  await expect(svg).not.toHaveAttribute('viewBox', initial!);
  await page.getByRole('button', { name: '复位地图', exact: true }).click();
  await expect(svg).toHaveAttribute('viewBox', initial!);
  const bounds = (await svg.boundingBox())!;
  const x = bounds.x + bounds.width * .22, y = bounds.y + bounds.height * .3;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + 45, y + 35, { steps: 8 });
  await page.mouse.up();
  await expect(svg).not.toHaveAttribute('viewBox', initial!);
  await expect(page.getByRole('textbox', { name: '中心点经度' })).toHaveValue('116.399');
  await page.getByRole('button', { name: '复位地图', exact: true }).click();
  await page.mouse.click(x, y);
  await expect(page.getByRole('textbox', { name: '中心点经度' })).not.toHaveValue('116.399');
  await page.getByRole('button', { name: '开始体检', exact: true }).click();
  await expectAnalysisLoadingPasses(page);
  await closeAutoOpenedReport(page);
  await expect(page.getByTestId('circle-layer')).toBeVisible();
  await expect(page.getByTestId('total-count')).toHaveText(/^\d+$/);
});

test('any in-map point produces an analysis result', async ({ page }) => {
  await page.getByRole('textbox', { name: '中心点经度' }).fill('116.395');
  await page.getByRole('button', { name: '应用坐标', exact: true }).click();
  await page.getByRole('button', { name: '开始体检', exact: true }).click();
  await expectAnalysisLoadingPasses(page);
  await closeAutoOpenedReport(page);
  await expect(page.getByTestId('circle-layer')).toBeVisible();
  await expect(page.getByTestId('total-count')).toHaveText(/^\d+$/);
  await page.screenshot({ path: 'output/playwright/custom-point.png', fullPage: true, animations: 'disabled' });
});

