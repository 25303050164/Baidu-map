import { test, expect, type Page } from '@playwright/test';
import { resultFixture } from '../src/analysis/testFixtures';

/** Contract-only browser tests. All external requests are blocked; no backend/AK is used. */
async function setup(page: Page, options: { failOnce?: boolean; unavailable?: boolean; mismatch?: boolean;
  createGate?: Promise<void>; resultGate?: Promise<void>; running?: boolean; createError?: number; statusError?: number; failed?: boolean } = {}) {
  let submitted: { center: { lng: number; lat: number }; budget: number };
  let count = 0;
  await page.addInitScript(() => {
    const audit = { creations: 0, active: 0, paths: [] as string[][],
      markers: [] as { point: Point; options: { title: string } }[], click: undefined as undefined | ((e: unknown) => void) };
    class Overlay { addEventListener() {} removeEventListener() {} }
    class Point { constructor(public lng: number, public lat: number) {} }
    class Marker extends Overlay { constructor(public point: Point, public options: { title: string }) { super(); } }
    class Polygon extends Overlay { constructor(public rings: string[]) { super(); } }
    class Map {
      constructor(el: HTMLElement) {
        audit.creations++; audit.active++;
        el.addEventListener('click', () => audit.click?.({ latlng: { lng: 116.405, lat: 39.916 } }));
      }
      centerAndZoom() {} panTo() {} enableScrollWheelZoom() {}
      addEventListener(_event: string, handler: (e: unknown) => void) { audit.click = handler; }
      addOverlay(overlay: Polygon | Marker) {
        if (overlay instanceof Polygon) audit.paths.push(overlay.rings);
        if (overlay instanceof Marker) audit.markers.push(overlay);
      }
      clearOverlays() { audit.paths = []; audit.markers = []; }
      destroy() { audit.active--; }
    }
    Object.assign(window, { BMapGL: { Map, Point, Polygon, Marker }, __mapAudit: audit });
  });
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.hostname !== '127.0.0.1') return route.abort();
    if (!url.pathname.startsWith('/api/analyses')) return route.continue();
    if (url.pathname === '/api/analyses') {
      submitted = route.request().postDataJSON(); count++;
      await options.createGate;
      if (options.createError) return route.fulfill({ status: options.createError, body: 'private upstream details' });
      if (options.failOnce) { options.failOnce = false; return route.fulfill({ status: 503, json: {} }); }
    }
    const taskId = `task-${count}`;
    if (url.pathname.endsWith('/result')) {
      await options.resultGate;
      const result = resultFixture();
      result.taskId = taskId; result.center = { ...submitted.center };
      result.isochrone.config = { ...result.isochrone.config, budget: submitted.budget,
        origin: [submitted.center.lng, submitted.center.lat] };
      result.isochrone.quality = 'partial';
      if (options.unavailable) { result.isochrone.geometry = null; result.isochrone.quality = 'insufficient'; }
      if (options.mismatch) {
        result.center.lng = 120;
        result.isochrone.config.origin[0] = 120;
      }
      return route.fulfill({ json: result });
    }
    if (route.request().method() === 'GET' && options.statusError) return route.fulfill({ status: options.statusError, json: {} });
    const status = url.pathname.endsWith('/cancel') ? 'cancelled' : options.failed ? 'failed' : options.running ? 'running' : 'completed';
    return route.fulfill({ status: route.request().method() === 'POST' ? 202 : 200,
      json: { taskId, status, stage: status === 'running' ? 'refining' : status, requests: 200, networkRequests: 0,
        budget: submitted.budget, elapsedSeconds: 1, dataSource: 'synthetic', error: null } });
  });
  return { creations: () => count };
}

test('validated partial result drives map and automatic report, preserving holes and unknown counts', async ({ page }) => {
  await setup(page);
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto('/');
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  const report = page.getByTestId('analysis-report');
  await expect(report).toBeVisible();
  await expect(report).toContainText('部分体检结果');
  await expect(report).toContainText('证据质量：部分结果');
  await expect(page.getByTestId('analysis-facility-stats').getByRole('cell', { name: '无法确定', exact: true })).toHaveCount(3);
  const audit = await page.evaluate(() => (window as any).__mapAudit);
  expect(audit.active).toBe(1);
  expect(audit.creations).toBe(1);
  expect(audit.paths).toEqual(resultFixture().isochrone.geometry!.coordinates.map(p => p.map(r => r.map(x => x.join(',')).join(';'))));
  await page.keyboard.press('Escape');
  await page.getByRole('checkbox', { name: '可达区域', exact: true }).uncheck();
  expect(await page.evaluate(() => (window as any).__mapAudit.paths.length)).toBe(0);
  await page.getByRole('button', { name: '查看分析报告', exact: true }).click();
  await expect(report).toContainText('已重建 2 个可达分量');
  expect(errors).toEqual([]);
});

test('map selection and failed retry retain the old report until a valid replacement arrives', async ({ page }) => {
  const options = { failOnce: false };
  await setup(page, options);
  await page.goto('/');
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  const report = page.getByTestId('analysis-report');
  await expect(report).toBeVisible();
  await page.keyboard.press('Escape');
  await page.getByTestId('algorithm-map').click();
  await expect(page.getByRole('spinbutton', { name: '经度', exact: true })).toHaveValue('116.405000');
  options.failOnce = true;
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  await expect(page.getByText('分析服务当前不可用，请联系管理员检查步行服务配置', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '查看分析报告', exact: true }).click();
  await expect(report).toContainText('116.404000, 39.915000');
  await expect(report).toContainText('分析条件已修改');
  await expect(report).toContainText('最近一次分析未成功');
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: '重试', exact: true }).click();
  await expect(report).toBeVisible();
  await expect(report).toContainText('116.405000, 39.916000');
  await expect(report).not.toContainText('分析条件已修改');
  await expect(report).not.toContainText('最近一次分析未成功');
});

test('insufficient evidence does not replace a prior report or automatically open a new one', async ({ page }) => {
  const options = { unavailable: false };
  await setup(page, options);
  await page.goto('/');
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  await expect(page.getByTestId('analysis-report')).toBeVisible();
  const originalPaths = await page.evaluate(() => (window as any).__mapAudit.paths);
  await page.keyboard.press('Escape');
  options.unavailable = true;
  await page.getByRole('spinbutton', { name: '经度', exact: true }).fill('116.407');
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  await expect(page.getByText('本次步行证据不足，未生成新的体检报告。', { exact: true })).toBeVisible();
  await expect(page.getByTestId('analysis-report')).not.toBeVisible();
  expect(await page.evaluate(() => (window as any).__mapAudit.paths)).toEqual(originalPaths);
  await expect(page.getByText('图层与报告中心：116.404000, 39.915000', { exact: false })).toBeVisible();
  const markers = await page.evaluate(() => (window as any).__mapAudit.markers);
  expect(markers.map((m: any) => [m.point.lng, m.options.title])).toEqual([
    [116.404, '已分析中心（与报告一致）'], [116.407, '待分析选点（BD09LL）'],
  ]);
  await expect(page.getByRole('button', { name: '开始分析', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '查看分析报告', exact: true }).click();
  await expect(page.getByTestId('analysis-report')).toContainText('116.404000, 39.915000');
});

test('creation, real polling stages and fetching have a single waiting flow without percentages', async ({ page }) => {
  let created!: () => void;
  let fetched!: () => void;
  const options = { running: true, createGate: new Promise<void>(resolve => { created = resolve; }),
    resultGate: new Promise<void>(resolve => { fetched = resolve; }) };
  const calls = await setup(page, options);
  await page.goto('/');
  const start = page.getByRole('button', { name: '开始分析', exact: true });
  await start.click();
  const progress = page.getByTestId('analysis-progress');
  await expect(progress).toContainText('正在创建任务');
  await expect(start).toBeDisabled();
  await start.dispatchEvent('click');
  await expect.poll(calls.creations).toBe(1);
  created();
  await expect(progress).toContainText('边界细化与补测');
  await expect(progress).toContainText('200 / 400');
  await expect(progress).not.toContainText(/\d+%/);
  await page.screenshot({ path: test.info().outputPath('api-progress.png'), fullPage: true });
  options.running = false;
  await expect(progress).toContainText('正在整理结果');
  await expect(start).toBeDisabled();
  await expect(page.getByTestId('analysis-report')).not.toBeVisible();
  fetched();
  await expect(page.getByTestId('analysis-report')).toBeVisible();
  await expect(progress).toHaveAttribute('aria-busy', 'false');
  expect(calls.creations()).toBe(1);
});

test('cancel and backend task failure leave the waiting state explicitly', async ({ page }) => {
  const options = { running: true, failed: false };
  await setup(page, options);
  await page.goto('/');
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  await expect(page.getByTestId('analysis-progress')).toContainText('边界细化与补测');
  await page.getByRole('button', { name: '取消任务', exact: true }).click();
  await expect(page.getByText('任务已取消', { exact: true })).toBeVisible();
  await expect(page.getByTestId('analysis-progress')).not.toBeVisible();
  options.failed = true;
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  await expect(page.getByText('分析执行失败，请检查后端配置后重试', { exact: true })).toBeVisible();
  await expect(page.getByTestId('analysis-progress')).not.toBeVisible();
  await expect(page.getByRole('button', { name: '开始分析', exact: true })).toBeEnabled();
});

for (const scenario of [
  { createError: 404, message: '分析 API 地址或服务配置异常，请检查服务地址' },
  { statusError: 404, message: '任务不存在或已过期，请重新分析' },
  { createError: 422, message: '分析参数无效，请检查中心坐标和调用预算' },
  { createError: 503, message: '分析服务当前不可用，请联系管理员检查步行服务配置' },
  { createError: 500, message: '分析服务异常，请稍后重试' },
]) {
  test(`request errors exit waiting: ${scenario.message}`, async ({ page }) => {
    await setup(page, scenario);
    await page.goto('/');
    await page.getByRole('button', { name: '开始分析', exact: true }).click();
    await expect(page.getByText(scenario.message, { exact: true })).toBeVisible();
    await expect(page.getByTestId('analysis-progress')).not.toBeVisible();
    await expect(page.getByRole('button', { name: '开始分析', exact: true })).toBeEnabled();
    await expect(page.getByTestId('analysis-report')).not.toBeVisible();
    await expect(page.locator('body')).not.toContainText('private upstream details');
  });
}

test('a structurally valid response for different input is rejected before rendering', async ({ page }) => {
  await setup(page, { mismatch: true });
  await page.goto('/');
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  await expect(page.getByText('分析结果与提交条件不一致，请检查服务版本', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '查看分析报告', exact: true })).toBeDisabled();
  await expect(page.getByTestId('analysis-report')).not.toBeVisible();
});
