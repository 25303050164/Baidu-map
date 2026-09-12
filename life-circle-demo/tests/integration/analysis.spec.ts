import { test, expect, type Page } from '@playwright/test';

async function mockMap(page: Page) {
  await page.addInitScript(() => {
    const storage = window as unknown as { __polygons: { points: string[]; options: { fillColor?: string } }[] };
    storage.__polygons = [];
    class Overlay { addEventListener() {} removeEventListener() {} }
    class Point { constructor(public lng: number, public lat: number) {} }
    class Map {
      private center = new Point(116.404, 39.915);
      private svg: SVGSVGElement;
      constructor(private container: HTMLElement) {
        container.dataset.sdk = 'offline';
        this.svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        this.svg.setAttribute('viewBox', '0 0 1000 900');
        this.svg.setAttribute('width', '100%'); this.svg.setAttribute('height', '100%');
        container.append(this.svg);
      }
      centerAndZoom(center: Point) { this.center = center; }
      panTo(center: Point) { this.center = center; }
      enableScrollWheelZoom() {}
      addOverlay(overlay: Polygon) {
        if (!overlay.points) return;
        const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        path.setAttribute('d', overlay.points.map(ring => ring.split(';').map((point, index) => {
          const [lng, lat] = point.split(',').map(Number);
          return `${index ? 'L' : 'M'}${500 + (lng - this.center.lng) * 25000 * Math.cos(this.center.lat * Math.PI / 180)},${450 - (lat - this.center.lat) * 25000}`;
        }).join(' ') + 'Z').join(' '));
        path.setAttribute('fill-rule', 'evenodd');
        path.setAttribute('fill', overlay.options.fillColor || 'none');
        path.setAttribute('fill-opacity', String(overlay.options.fillOpacity ?? .2));
        path.setAttribute('stroke', overlay.options.strokeColor || '#64748b');
        this.svg.append(path);
      }
      clearOverlays() {
        storage.__polygons = []; this.svg.replaceChildren();
        const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        label.setAttribute('x', '24'); label.setAttribute('y', '36'); label.textContent = '离线 SDK 替身 · 不含真实底图';
        this.svg.append(label);
      }
      destroy() { this.svg.remove(); }
      addEventListener() {}
    }
    class Polygon extends Overlay {
      constructor(public points: string[], public options: { fillColor?: string; fillOpacity?: number; strokeColor?: string }) { super(); storage.__polygons.push({ points, options }); }
    }
    Object.assign(window, { BMapGL: { Map, Point, Polygon, Marker: Overlay } });
  });
}

test.beforeEach(async ({ page }) => {
  await page.route('**/*', route => {
    const host = new URL(route.request().url()).hostname;
    return host === '127.0.0.1' || host === 'localhost' ? route.continue() : route.abort();
  });
});

async function analyze(page: Page, lng = '116.404') {
  await page.getByRole('spinbutton', { name: '经度', exact: true }).fill(lng);
  const response = page.waitForResponse(r => /\/api\/analyses\/[^/]+\/result$/.test(r.url()) && r.status() === 200);
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  const result = await (await response).json();
  await expect(page.getByText('分析完成', { exact: true })).toBeVisible();
  return result;
}

test('real HTTP algorithm chain, direct BD09 polygons and responsive layout', async ({ page }, info) => {
  await mockMap(page);
  await page.goto('/');
  await expect(page.getByText('设施统计尚未接入', { exact: true })).toBeVisible();
  const result = await analyze(page);
  expect(result.isochrone.statistics.requests).toBeLessThanOrEqual(400);
  expect(result.isochrone.statistics.network_requests).toBe(0);
  await expect(page.getByText('合成数据 · 离线验收', { exact: true })).toBeVisible();
  const overlays = await page.evaluate(() => (window as any).__polygons.filter((p: any) => p.options.fillColor === '#2da990'));
  expect(overlays.map((p: any) => p.points)).toEqual(result.isochrone.geometry.coordinates.map((p: number[][][]) => p.map(r => r.map(x => x.join(',')).join(';'))));
  await page.screenshot({ path: info.outputPath('desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: info.outputPath('mobile.png'), fullPage: true });
});

test('holes and multiple components survive transport and map conversion', async ({ page }) => {
  await mockMap(page);
  await page.goto('/');
  const hole = await analyze(page, '116.405');
  expect(hole.isochrone.geometry.coordinates.some((polygon: unknown[]) => polygon.length > 1)).toBe(true);
  const overlays = await page.evaluate(() => (window as any).__polygons.filter((p: any) => p.options.fillColor === '#2da990'));
  expect(overlays.some((p: any) => p.points.length > 1)).toBe(true);
  const components = await analyze(page, '116.406');
  expect(components.isochrone.geometry.coordinates.length).toBeGreaterThan(1);
});

test('unknown evidence and supported empty geometry have different UI', async ({ page }) => {
  await mockMap(page);
  await page.goto('/');
  const unknown = await analyze(page, '116.407');
  expect(unknown.isochrone.geometry).toBeNull();
  await expect(page.getByText('证据不足，无法确定可达区域', { exact: true })).toBeVisible();
  const empty = await analyze(page, '116.408');
  expect(empty.isochrone.geometry.coordinates).toEqual([]);
  await expect(page.getByText('有效证据范围内，可达区域为空', { exact: true })).toBeVisible();
});

test('local unknown remains a separate layer', async ({ page }) => {
  await mockMap(page);
  await page.goto('/');
  const result = await analyze(page, '116.410');
  expect(result.isochrone.unknownRegion.coordinates.length).toBeGreaterThan(0);
  const unknown = await page.evaluate(() => (window as any).__polygons.filter((p: any) => p.options.fillColor === '#64748b'));
  expect(unknown.length).toBe(result.isochrone.unknownRegion.coordinates.length);
  await page.getByRole('checkbox', { name: '未知区域' }).uncheck();
  expect(await page.evaluate(() => (window as any).__polygons.filter((p: any) => p.options.fillColor === '#64748b').length)).toBe(0);
});

test('cancels the server task and clears old state when center changes', async ({ page, request }) => {
  await mockMap(page);
  await page.goto('/');
  await page.getByRole('spinbutton', { name: '经度', exact: true }).fill('116.409');
  const created = page.waitForResponse(r => r.url().endsWith('/api/analyses') && r.status() === 202);
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  const id = (await (await created).json()).taskId;
  await page.getByRole('button', { name: '取消任务', exact: true }).click();
  await expect(page.getByText('任务已取消', { exact: true })).toBeVisible();
  const status = await (await request.get(`http://127.0.0.1:8018/api/analyses/${id}`)).json();
  expect(status.status).toBe('cancelled');
  expect(status.requests).toBeLessThanOrEqual(4);
  await page.getByRole('spinbutton', { name: '纬度', exact: true }).fill('39.916');
  await expect(page.getByText('任务已取消', { exact: true })).not.toBeVisible();
});

test('SDK failure keeps coordinate analysis and summary usable without demo fallback', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByText('地图不可用', { exact: true })).toBeVisible();
  await analyze(page);
  await expect(page.getByText(/已重建 \d+ 个可达分量/)).toBeVisible();
  await expect(page.getByText('演示数据', { exact: true })).not.toBeVisible();
});

test('network failure is explicit and can resume the same analysis request', async ({ page }) => {
  await mockMap(page);
  await page.route('**/api/analyses', route => route.abort(), { times: 1 });
  await page.goto('/');
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  await expect(page.getByText('无法连接后端或请求超时，请检查服务后重试', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '重试', exact: true }).click();
  await expect(page.getByText('分析完成', { exact: true })).toBeVisible();
});
