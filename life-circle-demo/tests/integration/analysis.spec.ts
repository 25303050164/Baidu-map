import { test, expect, type APIRequestContext, type Page, type Route } from '@playwright/test';

/** 主入口默认是体检 v2；旧版两条分析在这两个地址。 */
const E82 = '/#/legacy/e82';
const HYBRID = '/#/legacy/hybrid';
const API = 'http://127.0.0.1:8018';

type LocationFixture = {
  geolocation?: 'ok' | 'denied' | 'timeout';
  accuracy?: number;
  nearbyEmpty?: boolean;
  places?: { title: string; address?: string; uid?: string; lng: number; lat: number }[];
  farPlaces?: { title: string; address?: string; uid?: string; lng: number; lat: number }[];
  geocode?: { lng: number; lat: number } | null;
};

async function mockMap(page: Page, location: LocationFixture = {}) {
  await page.addInitScript((fixture: LocationFixture) => {
    const storage = window as unknown as { __polygons: { points: string[]; options: { fillColor?: string } }[] };
    storage.__polygons = [];
    class Overlay { addEventListener() {} removeEventListener() {} }
    class Point { constructor(public lng: number, public lat: number) {} }
    class Size { constructor(public width: number, public height: number) {} }
    class Icon { constructor(public url: string, public size: Size, public options: { anchor?: Size }) {} }
    // __polygons 只记"此刻在地图上"的面：ApiMap 按组 removeOverlay，不再整张 clearOverlays。
    const shown = new globalThis.Map<Polygon, SVGPathElement>();
    class Map {
      private center = new Point(116.404, 39.915);
      private svg: SVGSVGElement;
      constructor(private container: HTMLElement) {
        container.dataset.sdk = 'offline';
        this.svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        this.svg.setAttribute('viewBox', '0 0 1000 900');
        this.svg.setAttribute('width', '100%'); this.svg.setAttribute('height', '100%');
        const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        label.setAttribute('x', '24'); label.setAttribute('y', '36'); label.textContent = '离线 SDK 替身 · 不含真实底图';
        this.svg.append(label);
        container.append(this.svg);
      }
      centerAndZoom(center: Point) { this.center = center; }
      panTo(center: Point) { this.center = center; }
      enableScrollWheelZoom() {}
      removeOverlay(overlay: Polygon) {
        const path = shown.get(overlay);
        if (!path) return;
        path.remove(); shown.delete(overlay);
        storage.__polygons = storage.__polygons.filter(item => item !== overlay.record);
      }
      addOverlay(overlay: Polygon) {
        if (!overlay.points) return;
        storage.__polygons.push(overlay.record);
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
        shown.set(overlay, path);
      }
      destroy() {
        for (const [overlay, path] of shown) if (path.ownerSVGElement === this.svg) this.removeOverlay(overlay);
        this.svg.remove();
      }
      addEventListener() {}
    }
    class Polygon extends Overlay {
      record: { points: string[]; options: { fillColor?: string; fillOpacity?: number; strokeColor?: string } };
      constructor(public points: string[], public options: { fillColor?: string; fillOpacity?: number; strokeColor?: string }) {
        super(); this.record = { points, options };
      }
    }
    class Geolocation {
      private status = 0;
      getCurrentPosition(callback: (result: unknown) => void) {
        const mode = fixture.geolocation ?? 'ok';
        if (mode === 'denied') { this.status = 6; setTimeout(() => callback(null), 0); return; }
        if (mode === 'timeout') { this.status = 8; setTimeout(() => callback(null), 0); return; }
        setTimeout(() => callback({
          point: { lng: 116.418, lat: 39.921 }, accuracy: fixture.accuracy ?? 30,
          address: { province: '北京市', city: '北京市', district: '东城区', street: '测试街', streetNumber: '1号' },
        }), 0);
      }
      getStatus() { return this.status; }
    }
    class LocalSearch {
      constructor(public location: unknown, public options: { onSearchComplete?: (results: unknown) => void; pageCapacity?: number }) {}
      private respond(places: NonNullable<LocationFixture['places']>) {
        const result = {
          getPoi: (index: number) => places[index]
            ? { ...places[index], point: { lng: places[index].lng, lat: places[index].lat } }
            : undefined,
          getCurrentNumPois: () => places.length,
          getNumPois: () => places.length,
        };
        setTimeout(() => this.options.onSearchComplete?.(result), 0);
      }
      searchNearby() { this.respond(fixture.nearbyEmpty ? [] : fixture.places ?? []); }
      searchInBounds() { this.respond(fixture.farPlaces ?? []); }
      search() {}
      clearResults() {}
    }
    class Bounds { constructor(public sw: unknown, public ne: unknown) {} }
    class Geocoder {
      getPoint(_address: string, callback: (point: unknown) => void) {
        setTimeout(() => callback(fixture.geocode ?? null), 0);
      }
    }
    class Label extends Overlay { setStyle() {} }
    class Polyline extends Overlay {}
    Object.assign(window, { BMapGL: { Map, Point, Size, Icon, Polygon, Marker: Overlay, Label, Polyline, Geolocation, LocalSearch, Bounds, Geocoder } });
  }, location);
}

async function guard(page: Page) {
  await page.route('**/*', route => {
    const host = new URL(route.request().url()).hostname;
    return host === '127.0.0.1' || host === 'localhost' ? route.continue() : route.abort();
  });
}

test.beforeEach(async ({ page }) => { await guard(page); });

async function analyze(page: Page, lng = '116.404') {
  await page.getByRole('spinbutton', { name: '经度', exact: true }).fill(lng);
  const response = page.waitForResponse(r => /\/api\/analyses\/[^/]+\/result$/.test(r.url()) && r.status() === 200);
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  const result = await (await response).json();
  await expect(page.getByText('分析完成', { exact: true })).toBeVisible();
  if (result.isochrone.quality !== 'insufficient' && result.isochrone.geometry !== null) {
    await expect(page.getByTestId('analysis-report')).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.getByTestId('analysis-report')).not.toBeVisible();
  }
  return result;
}

test('real HTTP algorithm chain, direct BD09 polygons and responsive layout', async ({ page }, info) => {
  await mockMap(page);
  await page.goto(E82);
  await expect(page.getByText('设施统计尚未接入', { exact: true })).toBeVisible();
  const result = await analyze(page);
  expect(result.isochrone.algorithm).toBe('local-multicross-e82');
  expect(result.isochrone.timeBands.map((b: any) => b.minutes)).toEqual([15]);
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

test('switches independent algorithms and renders Hybrid as exterior lines only', async ({ page }, info) => {
  // Hybrid runs its own OSM/百度 search rebuild on every pass and is far slower
  // than the offline grid path, so this case uses the smallest supported budget
  // and its own generous timeout.
  test.setTimeout(300_000);
  await mockMap(page);
  await page.goto(E82);
  const baidu = await analyze(page);
  await page.getByTestId('algorithm-hybrid').click();
  await page.getByRole('combobox', { name: '百度验证预算' }).click();
  await page.getByTitle('200 次', { exact: true }).click();
  const response = page.waitForResponse(r => /\/api\/v1\/analysis\/hybrid\/[^/]+\/result$/.test(r.url()) && r.status() === 200);
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  const result = await (await response).json();
  expect(result.taskId).not.toBe(baidu.taskId);
  expect(result.isochrone.algorithm).toBe('hybrid');
  await expect(page.getByText('结果质量：部分结果', { exact: true })).toBeVisible();
  const polygons = await page.evaluate(() => (window as any).__polygons);
  expect(polygons.length).toBeGreaterThan(0);
  expect(polygons.every((p: any) => p.options.fillOpacity === 0 && p.points.length === 1)).toBe(true);
  expect(polygons.map((p: any) => p.points)).toEqual(result.isochrone.displayGeometry.coordinates.map(
    (p: number[][][]) => [p[0].map(x => x.join(',')).join(';')]));
  await page.screenshot({ path: info.outputPath('hybrid-outline.png'), fullPage: true });
  // 切回 E8.2：地图与结果回到 E8.2 自己那一次任务，Hybrid 的外轮廓一条都不残留。
  const hybridRings = result.isochrone.displayGeometry.coordinates.map(
    (p: number[][][]) => p[0].map(x => x.join(',')).join(';'));
  await page.getByTestId('algorithm-e82').click();
  await expect(page.getByText('结果质量：部分结果', { exact: true })).toHaveCount(0);
  await expect(page.getByTestId('legacy-map-section')).toHaveAttribute('data-task-id', baidu.taskId);
  await expect.poll(() => page.evaluate(() => (window as any).__polygons
    .filter((p: any) => p.options.fillColor === '#2da990').map((p: any) => p.points)))
    .toEqual(baidu.isochrone.geometry.coordinates.map((p: number[][][]) => p.map(r => r.map(x => x.join(',')).join(';'))));
  expect(await page.evaluate(rings => (window as any).__polygons
    .filter((p: any) => p.points.some((ring: string) => rings.includes(ring))).length, hybridRings)).toBe(0);
});

test('geometry contract preserves supplied holes and components independently of the search strategy', async ({ page }) => {
  await mockMap(page);
  // This verifies rendering, not E8.2's ability to discover unsampled interior islands.
  await page.route('**/api/analyses/*/result', async route => {
    const data = await (await route.fetch()).json();
    const { lng: x, lat: y } = data.center;
    const ring = [[x-.002,y-.002],[x+.002,y-.002],[x+.002,y+.002],[x-.002,y+.002],[x-.002,y-.002]];
    const hole = [[x-.001,y-.001],[x-.001,y+.001],[x+.001,y+.001],[x+.001,y-.001],[x-.001,y-.001]];
    const coordinates = x === 116.405 ? [[ring, hole]] : [[ring], [ring.map(([a,b]) => [a+.01,b])]];
    const geometry = { type: 'MultiPolygon', coordinateSystem: 'bd09ll', coordinates };
    data.data.geometry = geometry;
    data.isochrone.geometry = geometry; data.isochrone.timeBands = [{ minutes: 15, geometry }];
    data.algorithm = data.isochrone;
    await route.fulfill({ json: data });
  });
  await page.goto(E82);
  const hole = await analyze(page, '116.405');
  expect(hole.isochrone.geometry.coordinates.some((polygon: unknown[]) => polygon.length > 1)).toBe(true);
  const overlays = await page.evaluate(() => (window as any).__polygons.filter((p: any) => p.options.fillColor === '#2da990'));
  expect(overlays.some((p: any) => p.points.length > 1)).toBe(true);
  const components = await analyze(page, '116.406');
  expect(components.isochrone.geometry.coordinates.length).toBeGreaterThan(1);
});

test('missing endpoint evidence never becomes a claimed empty reachable region', async ({ page }) => {
  await mockMap(page);
  await page.goto(E82);
  const unknown = await analyze(page, '116.407');
  expect(unknown.isochrone.geometry).toBeNull();
  await expect(page.getByText('证据不足，无法确定可达区域', { exact: true })).toBeVisible();
  const empty = await analyze(page, '116.408');
  expect(empty.isochrone.quality).toBe('insufficient');
  await expect(page.getByText('证据不足，无法确定可达区域', { exact: true })).toBeVisible();
});

test('local unknown remains a separate layer', async ({ page }) => {
  await mockMap(page);
  await page.goto(E82);
  // 116.410 is only an unsupported neighbourhood for the offline harness; the
  // partial scene below is the one that carries local unlocalised faces.
  const result = await analyze(page, '116.405');
  expect(result.isochrone.unknownRegion.coordinates.length).toBeGreaterThan(0);
  const unknown = await page.evaluate(() => (window as any).__polygons.filter((p: any) => p.options.fillColor === '#64748b'));
  expect(unknown.length).toBe(result.isochrone.unknownRegion.coordinates.length);
  const unreachableBefore = await page.evaluate(() => (window as any).__polygons.filter((p: any) => p.options.fillColor === '#6b7280'));
  await page.getByRole('checkbox', { name: '未核验区域（灰色）' }).uncheck();
  expect(await page.evaluate(() => (window as any).__polygons.filter((p: any) => p.options.fillColor === '#6b7280'))).toEqual(unreachableBefore);
  await expect(page.getByRole('checkbox', { name: '已知不可达区域', exact: true })).toBeChecked();
  expect(await page.evaluate(() => (window as any).__polygons.filter((p: any) => p.options.fillColor === '#64748b').length)).toBe(0);
});

test('moving the center never cancels a running task; only the explicit cancel stops it', async ({ page, request }) => {
  await mockMap(page);
  await page.goto(E82);
  await page.getByRole('spinbutton', { name: '经度', exact: true }).fill('116.409');
  const created = page.waitForResponse(r => r.url().endsWith('/api/analyses') && r.status() === 202);
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  const id = (await (await created).json()).taskId;
  // 改选点只改左栏草稿：任务照跑，页面说明它仍按提交时的中心计算，也不许并行再开一个。
  await page.getByRole('spinbutton', { name: '经度', exact: true }).fill('116.404');
  await expect(page.getByText('进行中的任务仍按它提交时的中心计算', { exact: false })).toBeVisible();
  await expect(page.getByRole('button', { name: '开始分析', exact: true })).toBeDisabled();
  expect((await (await request.get(`${API}/api/analyses/${id}`)).json()).status).toBe('running');
  await page.getByRole('button', { name: '取消任务', exact: true }).click();
  await expect(page.getByText('任务已取消', { exact: true })).toBeVisible();
  const status = await (await request.get(`${API}/api/analyses/${id}`)).json();
  expect(status.status).toBe('cancelled');
  expect(status.requests).toBeLessThan(status.budget);
  // Browser scheduling changes the count before the click; cancellation must stop growth.
  await page.waitForTimeout(500);
  const settled = await (await request.get(`${API}/api/analyses/${id}`)).json();
  expect(settled.status).toBe('cancelled');
  expect(settled.requests).toBe(status.requests);
  expect((await request.get(`${API}/api/analyses/${id}/result`)).status()).toBe(409);
  await expect(page.getByRole('button', { name: '开始分析', exact: true })).toBeEnabled();
});

test('SDK failure keeps coordinate analysis and summary usable without demo fallback', async ({ page }) => {
  await page.goto(E82);
  await expect(page.getByText('地图不可用', { exact: true })).toBeVisible();
  await analyze(page);
  await expect(page.getByRole('region', { name: '分析结果', exact: true }).getByText(/已重建 \d+ 个可达分量/)).toBeVisible();
  await expect(page.getByText('演示数据', { exact: true })).not.toBeVisible();
});

test('a create that never reached the server is reported and resubmitted only on request, with the same key', async ({ page }) => {
  await mockMap(page);
  const keys: string[] = [];
  page.on('request', r => {
    if (r.method() === 'POST' && new URL(r.url()).pathname === '/api/analyses') keys.push(r.postDataJSON().clientRequestId);
  });
  await page.route('**/api/analyses', route => route.abort(), { times: 1 });
  await page.goto(E82);
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  // 按请求标识查过、服务端确实没有：说清楚，不自动重提。
  const error = page.getByTestId('legacy-error');
  await expect(error).toContainText('创建请求没有送达服务端');
  await expect(error.getByRole('button', { name: '重试' })).toHaveText('重新提交');
  expect(keys).toHaveLength(1);
  await error.getByRole('button', { name: '重试' }).click();
  await expect(page.getByText('分析完成', { exact: true })).toBeVisible();
  await expect(page.getByTestId('analysis-report')).toBeVisible();
  expect(keys).toHaveLength(2);
  expect(keys[1]).toBe(keys[0]);
});

test('reports retain their original conditions after edits, unavailable results and failed retries', async ({ page }) => {
  await mockMap(page);
  await page.goto(E82);
  await analyze(page);
  await page.getByRole('button', { name: '查看分析报告', exact: true }).click();
  const report = page.getByTestId('analysis-report');
  await expect(report).toContainText('116.404000, 39.915000');
  await expect(report).toContainText('合成时间场（非真实社区）');
  await expect(page.getByTestId('analysis-facility-stats').getByRole('cell', { name: '无法确定', exact: true })).toHaveCount(3);
  await expect(report).toContainText('设施盲区数量：无法确定');
  await page.keyboard.press('Escape');
  await analyze(page, '116.407');
  await expect(page.getByText('本次步行证据不足，未生成新的体检报告。', { exact: true })).toBeVisible();
  await expect(report).not.toBeVisible();
  await page.getByRole('button', { name: '查看分析报告', exact: true }).click();
  await expect(report).toContainText('116.404000, 39.915000');
  await expect(report).toContainText('分析条件已修改');
  await expect(report).toContainText('最近一次分析未成功');
  await page.keyboard.press('Escape');
  await page.getByRole('spinbutton', { name: '经度', exact: true }).fill('116.406');
  await page.route('**/api/analyses', route => route.fulfill({ status: 503, body: '{}' }), { times: 1 });
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  await expect(page.getByText('分析服务当前不可用，请联系管理员检查步行服务配置', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '查看分析报告', exact: true }).click();
  await expect(report).toContainText('116.404000, 39.915000');
  await expect(report).toContainText('最近一次分析未成功');
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: '重试', exact: true }).click();
  await expect(report).toBeVisible();
  await expect(report).toContainText('116.406000, 39.915000');
  await expect(report).not.toContainText('分析条件已修改');
  await expect(report).not.toContainText('最近一次分析未成功');
});

test('a lost create response is recovered by its request key without a second POST', async ({ page, request }) => {
  await mockMap(page);
  let taskId = '';
  let creates = 0;
  page.on('request', r => { if (r.method() === 'POST' && new URL(r.url()).pathname === '/api/analyses') creates++; });
  await page.route('**/api/analyses', async route => {
    const accepted = await route.fetch();
    taskId = (await accepted.json()).taskId;
    await route.abort(); // Server accepted the slow job, but the browser never received its ID.
  }, { times: 1 });
  await page.goto(E82);
  await page.getByRole('spinbutton', { name: '经度', exact: true }).fill('116.409');
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  await expect.poll(() => taskId).not.toBe('');
  await expect(page.getByTestId('legacy-task-id')).toHaveText(taskId);
  expect(creates).toBe(1);
  await page.getByRole('button', { name: '取消任务', exact: true }).click();
  await expect(page.getByText('任务已取消', { exact: true })).toBeVisible();
  expect((await (await request.get(`${API}/api/analyses/${taskId}`)).json()).status).toBe('cancelled');
  expect(creates).toBe(1);
  await analyze(page);
});

test('malformed successful result shows a format error instead of crashing the page', async ({ page }) => {
  await mockMap(page);
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/analyses/*/result', route => route.fulfill({ status: 200, contentType: 'application/json', body: '{}' }));
  await page.goto(E82);
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  await expect(page.getByText('后端返回格式异常，请检查服务版本', { exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});

test('device location fills the center and reports accuracy and address', async ({ page }) => {
  await mockMap(page, { geolocation: 'ok' });
  await page.goto(E82);
  await page.getByRole('button', { name: '获取当前位置', exact: true }).click();
  await expect(page.getByText(/已定位：北京市东城区测试街1号 · 定位精度约 30 米/)).toBeVisible();
  await expect(page.getByRole('spinbutton', { name: '经度', exact: true })).toHaveValue(/116\.418/);
  await expect(page.getByRole('spinbutton', { name: '纬度', exact: true })).toHaveValue(/39\.921/);
});

test('coarse device location warns for map verification', async ({ page }) => {
  await mockMap(page, { geolocation: 'ok', accuracy: 800 });
  await page.goto(E82);
  await page.getByRole('button', { name: '获取当前位置', exact: true }).click();
  await expect(page.getByText(/定位可能偏差较大，请在地图上核对/)).toBeVisible();
});

test('denied device location keeps manual coordinates and explains how to retry', async ({ page }) => {
  await mockMap(page, { geolocation: 'denied' });
  await page.goto(E82);
  await page.getByRole('button', { name: '获取当前位置', exact: true }).click();
  await expect(page.getByText('定位权限被拒绝，请在浏览器设置中允许定位后重试', { exact: true })).toBeVisible();
  await expect(page.getByRole('spinbutton', { name: '经度', exact: true })).toHaveValue(/116\.404/);
});

test('POI search selection becomes the analysis center', async ({ page }) => {
  await mockMap(page, { places: [{ title: '测试公园', address: '测试路1号', uid: 'poi-1', lng: 116.5, lat: 39.95 }] });
  await page.goto(E82);
  await page.getByRole('textbox', { name: '搜索地点' }).fill('公园');
  await page.getByRole('button', { name: '搜索', exact: true }).click();
  await page.getByRole('button', { name: /测试公园/ }).click();
  await expect(page.getByText(/已选择：测试公园 · 测试路1号/)).toBeVisible();
  const created = page.waitForRequest(r => r.url().endsWith('/api/analyses') && r.method() === 'POST');
  const result = page.waitForResponse(r => /\/api\/analyses\/[^/]+\/result$/.test(r.url()) && r.status() === 200);
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
  expect((await created).postDataJSON().center).toEqual({ lng: 116.5, lat: 39.95 });
  await result;
});

test('empty POI search result shows a retry hint', async ({ page }) => {
  await mockMap(page, { places: [] });
  await page.goto(E82);
  await page.getByRole('textbox', { name: '搜索地点' }).fill('不存在的地方');
  await page.getByRole('button', { name: '搜索', exact: true }).click();
  await expect(page.getByText('未找到相关地点，请尝试其他关键词', { exact: true })).toBeVisible();
});

test('POI search falls back to far options when nothing is nearby', async ({ page }) => {
  await mockMap(page, {
    nearbyEmpty: true,
    farPlaces: [
      { title: '远郊公园', address: '远郊路9号', uid: 'poi-far', lng: 117.2, lat: 40.3 },
      { title: '远郊花园', address: '远郊路10号', uid: 'poi-loose', lng: 117.3, lat: 40.3 },
    ],
  });
  await page.goto(E82);
  await page.getByRole('textbox', { name: '搜索地点' }).fill('公园');
  await page.getByRole('button', { name: '搜索', exact: true }).click();
  await expect(page.getByText('较远结果（超过 5 公里）', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: /远郊花园/ })).toHaveCount(0);
  await page.getByRole('button', { name: /远郊公园/ }).click();
  await expect(page.getByRole('spinbutton', { name: '经度', exact: true })).toHaveValue(/117\.2/);
});

test('POI search lists nearby results first and keeps far options', async ({ page }) => {
  await mockMap(page, {
    places: [{ title: '近处公园', address: '近处路1号', uid: 'poi-near', lng: 116.41, lat: 39.92 }],
    farPlaces: [{ title: '远郊公园', address: '远郊路9号', uid: 'poi-far', lng: 117.2, lat: 40.3 }],
  });
  await page.goto(E82);
  await page.getByRole('textbox', { name: '搜索地点' }).fill('公园');
  await page.getByRole('button', { name: '搜索', exact: true }).click();
  await expect(page.getByText('附近结果（5 公里内）', { exact: true })).toBeVisible();
  await expect(page.getByText('较远结果（超过 5 公里）', { exact: true })).toBeVisible();
  const options = page.getByRole('button', { name: /公园/ });
  await expect(options).toHaveCount(2);
  await options.first().click();
  await expect(page.getByRole('spinbutton', { name: '经度', exact: true })).toHaveValue(/116\.41/);
});

test('POI search resolves a nationwide administrative name through address fallback', async ({ page }) => {
  await mockMap(page, { geocode: { lng: 120.43, lat: 27.52 } });
  await page.goto(E82);
  await page.getByRole('textbox', { name: '搜索地点' }).fill('苍南县');
  await page.getByRole('button', { name: '搜索', exact: true }).click();
  await expect(page.getByText('较远结果（超过 5 公里）', { exact: true })).toBeVisible();
  await expect(page.getByText('地址定位', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: /苍南县/ }).click();
  await expect(page.getByRole('spinbutton', { name: '经度', exact: true })).toHaveValue(/120\.43/);
});

test('facility report, category filtering, route and time layers share one analysis', async ({ page }, info) => {
  await mockMap(page);
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const location = { lng: 116.405, lat: 39.915 };
  const savedRoute = { poiEvidence: { version: '1.0', facilityId: 'pharmacy-fixture', status: 'verified_reachable',
    reason: null, duration: 500, observedDuration: 500, endpointVerified: true,
    requestOrigin: [116.404,39.915], destination: [location.lng,location.lat],
    routeOrigin: [116.404,39.915], routeDestination: [location.lng,location.lat], originOffsetM: 0, destinationOffsetM: 0 }, distance_m: 600, duration_s: 500, endpoint_verified: true, reason: null, path: [[116.404, 39.915], [116.405, 39.915]] };
  await page.route('**/api/analyses/*/result', async route => {
    const response = await route.fetch();
    const data = await response.json();
    data.facilitiesStatus = 'partial';
    data.data.facilities = [{ id: 'pharmacy-fixture', name: '离线测试药房', category: 'pharmacy', minor_category: 'pharmacy', major_category: 'medical', location, in_circle: true, poiEvidence: savedRoute.poiEvidence }];
    data.data.report = '离线业务样例：1处设施，未知不当盲区。';
    data.facilityAnalysis = {
      status: 'partial',
      queries: [{ category: 'pharmacy', query: '药店', status: 'truncated', pages: 2, returned: 1, excluded: 0, invalid: 0, total: 150, reason: 'page_limit' }],
      assessments: [{ location, duration_s: 500, categories: [
        { category: 'shopping', status: 'unknown', facility_id: null, distance_m: null, reason: 'incomplete' },
        { category: 'medical', status: 'covered', facility_id: 'pharmacy-fixture', distance_m: 600, reason: 'walking' },
        { category: 'education', status: 'unknown', facility_id: null, distance_m: null, reason: 'incomplete' },
      ] }],
      candidate_points: 2, assessed_points: 1, unassessed_points: 1, network_requests: 0, elapsed_seconds: 0, search_radius_m: 3500,
      routes: { 'pharmacy-fixture': savedRoute },
      serviceBlindRegions: {
        shopping: { type: 'MultiPolygon', coordinateSystem: 'bd09ll', coordinates: [] },
        medical: { type: 'MultiPolygon', coordinateSystem: 'bd09ll', coordinates: [] },
        education: { type: 'MultiPolygon', coordinateSystem: 'bd09ll', coordinates: [] },
      },
      warnings: ['离线样例，未测点不计入盲区。'],
    };
    await route.fulfill({ response, json: data });
  });
  await page.route('**/api/analyses/*/routes/*', route => route.fulfill({ json: savedRoute }));
  await page.goto(E82);
  await analyze(page);
  await expect(page.getByText('设施与基础报告', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: /离线测试药房/ }).click();
  await page.getByRole('button', { name: '查看中心到设施的步行路线' }).click();
  await expect(page.getByText('600 米 · 500 秒 · 严格核验：15分钟内可达')).toBeVisible();
  await expect(page.getByTestId('route-poi-evidence')).toContainText('严格核验：15分钟内可达');
  await page.getByRole('button', { name: '查看分析报告', exact: true }).click();
  await expect(page.getByTestId('strict-poi-counts')).toContainText('15分钟内可达 1');
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('analysis-report')).not.toBeVisible();
  await page.getByRole('combobox', { name: '步行时间层' }).click();
  await expect(page.getByTitle('5 分钟', { exact: true })).toHaveCount(0);
  await page.getByTitle('15 分钟', { exact: true }).last().click();
  await expect(page.getByText('离线业务样例：1处设施，未知不当盲区。', { exact: true })).toBeVisible();
  await page.screenshot({ path: info.outputPath('facilities-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByText('设施与基础报告', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('facilities-mobile.png'), fullPage: true });
  expect(errors).toEqual([]);
});

// ── 任务生命周期：两种算法 × 创建中、运行中、完成后、取消中、失败、断网、忙碌 ─────────────────
// 任务属于后端，句柄存在 localStorage：换页面、换算法、刷新都只是换了个观察者。每条都数创建
// 请求（POST）的次数，证明找回靠的是任务 ID 或请求标识，而不是悄悄重新提交一个花额度的任务。

const BASE = 'http://127.0.0.1:5178';
type Speed = 'slow' | 'fast' | 'failing';
type Engine = {
  name: string; route: string; tab: string; other: string; api: string; createPath: string;
  storage: string; busy: string; report: boolean;
  fill(page: Page, speed: Speed): Promise<void>;
};

const LIFECYCLE_ENGINES: Engine[] = [
  { name: 'E8.2', route: E82, tab: 'algorithm-e82', other: 'algorithm-hybrid', api: `${API}/api/analyses`,
    createPath: '/api/analyses', storage: 'life-circle:legacy:v1:e82', busy: '分析服务忙：另一项分析正在进行', report: true,
    // 离线路网替身：116.409 约 22 秒，116.404 不到 1 秒，116.412 跑约 2 秒后整体失败。
    async fill(page, speed) {
      await page.getByRole('spinbutton', { name: '经度', exact: true })
        .fill({ slow: '116.409', fast: '116.404', failing: '116.412' }[speed]);
    } },
  { name: 'OSM＋百度', route: HYBRID, tab: 'algorithm-hybrid', other: 'algorithm-e82', api: `${API}/api/v1/analysis/hybrid`,
    createPath: '/api/v1/analysis/hybrid', storage: 'life-circle:legacy:v1:hybrid', busy: '另一项 OSM＋百度分析正在进行', report: false,
    // 同一中心：预算 400 约 30 秒，200 约 9 秒；116.412 跑约 2 秒后整体失败。
    async fill(page, speed) {
      await page.getByRole('spinbutton', { name: '经度', exact: true }).fill(speed === 'failing' ? '116.412' : '116.404');
      await page.getByRole('spinbutton', { name: '纬度', exact: true }).fill('39.915');
      if (speed === 'fast') {
        await page.getByRole('combobox', { name: '百度验证预算' }).click();
        await page.getByTitle('200 次', { exact: true }).click();
      }
    } },
];

/** 本页发出的创建请求的请求标识，按顺序。 */
function countCreates(page: Page, engine: Engine) {
  const keys: string[] = [];
  page.on('request', r => {
    if (r.method() === 'POST' && new URL(r.url()).pathname === engine.createPath) {
      const body = r.postDataJSON();
      keys.push(body.clientRequestId ?? body.client_request_id); // E8.2 驼峰，Hybrid 蛇形
    }
  });
  return keys;
}

const started: { api: string; id: string }[] = [];
/** 等页面显示出一个（与 previous 不同的）任务 ID，并登记下来供用例结束后收尾。 */
async function shownTask(page: Page, engine: Engine, previous?: string) {
  const node = page.getByTestId('legacy-task-id');
  let value = '';
  await expect.poll(async () => {
    value = (await node.textContent({ timeout: 500 }).catch(() => null))?.trim() ?? '';
    return value !== '' && value !== previous;
  }, { timeout: 30_000 }).toBe(true);
  if (!started.some(item => item.id === value)) started.push({ api: engine.api, id: value });
  return value;
}

async function serverStatus(request: APIRequestContext, engine: Engine, id: string): Promise<string> {
  return (await (await request.get(`${engine.api}/${id}`)).json()).status;
}

const storedHandle = (page: Page, engine: Engine) =>
  page.evaluate(key => JSON.parse(localStorage.getItem(key) ?? 'null')?.handle, engine.storage);

const drawn = (page: Page) => page.evaluate(() => (window as any).__polygons
  .map((p: any) => ({ points: p.points, fill: p.options.fillColor ?? null, opacity: p.options.fillOpacity ?? null })));

async function start(page: Page) {
  await page.getByRole('button', { name: '开始分析', exact: true }).click();
}

for (const e of LIFECYCLE_ENGINES) test.describe(`task lifecycle · ${e.name}`, () => {
  test.afterEach(async ({ request }) => {
    // 后端每种算法只有一个名额：把本条开过的任务停干净，下一条才不会撞上"忙"。
    for (const { api, id } of started.splice(0)) {
      await request.post(`${api}/${id}/cancel`, { data: {} }).catch(() => undefined);
      await expect.poll(async () => (await (await request.get(`${api}/${id}`)).json()).status, { timeout: 45_000 })
        .toMatch(/^(completed|cancelled|failed)$/);
    }
  });

  test('running: switching algorithm, switching page and reloading all keep the same task', async ({ page, request }) => {
    test.setTimeout(120_000);
    await mockMap(page);
    const creates = countCreates(page, e);
    await page.goto(e.route);
    await e.fill(page, 'slow');
    await start(page);
    const id = await shownTask(page, e);
    await expect(page.getByTestId('legacy-phase')).toHaveAttribute('data-phase', 'running');
    await page.getByTestId(e.other).click();
    await expect(page.getByTestId(e.tab)).toHaveAttribute('data-activity', '运行中');
    await page.getByTitle('体检 v2（热力与报告）', { exact: true }).click();
    await expect(page).toHaveURL(/#\/checkup\//);
    await page.getByTitle('旧版成圈分析', { exact: true }).click();
    await expect(page.getByTestId(e.tab)).toHaveAttribute('data-activity', '运行中');
    await page.getByTestId(e.tab).click();
    await expect(page.getByTestId('legacy-task-id')).toHaveText(id);
    expect(await serverStatus(request, e, id)).toBe('running');
    await page.reload();
    await expect(page.getByTestId('legacy-task-id')).toHaveText(id);
    expect(await serverStatus(request, e, id)).toBe('running');
    await expect(page.getByTestId('legacy-map-section')).toHaveAttribute('data-task-id', id, { timeout: 60_000 });
    await expect(page.getByTestId('legacy-phase')).toHaveAttribute('data-phase', 'completed');
    expect(creates).toHaveLength(1);
  });

  test('creating: a reload while the create request is in flight finds the task by its request key', async ({ page, request }) => {
    test.setTimeout(90_000);
    await mockMap(page);
    const creates = countCreates(page, e);
    let reached = '';
    let release!: () => void;
    const held = new Promise<void>(resolve => { release = resolve; });
    await page.route(`**${e.createPath}`, async route => {
      // 服务端收下了、建了任务，但回答一直没回到页面。
      reached = (await (await route.fetch()).json()).taskId;
      await held;
      await route.abort().catch(() => undefined);
    }, { times: 1 });
    await page.goto(e.route);
    await e.fill(page, 'slow');
    await start(page);
    await expect(page.getByTestId('legacy-phase')).toHaveAttribute('data-phase', 'submitting');
    await expect.poll(() => reached).not.toBe('');
    started.push({ api: e.api, id: reached });
    const handle = await storedHandle(page, e);
    expect(handle.taskId).toBeUndefined();
    expect(handle.input.clientRequestId).toBe(creates[0]);
    await page.reload();
    release();
    await expect(page.getByTestId('legacy-task-id')).toHaveText(reached);
    await expect(page.getByTestId('legacy-phase')).toHaveAttribute('data-phase', 'running');
    expect((await (await request.get(`${e.api}/by-request/${creates[0]}`)).json()).taskId).toBe(reached);
    expect((await storedHandle(page, e)).taskId).toBe(reached);
    expect(creates).toHaveLength(1);
  });

  test('completed: the same result and the same map come back after switching away and after a reload', async ({ page }) => {
    test.setTimeout(90_000);
    await mockMap(page);
    const creates = countCreates(page, e);
    await page.goto(e.route);
    await e.fill(page, 'fast');
    await start(page);
    const id = await shownTask(page, e);
    await expect(page.getByTestId('legacy-map-section')).toHaveAttribute('data-task-id', id, { timeout: 60_000 });
    if (e.report) {
      await expect(page.getByTestId('analysis-report')).toBeVisible();
      await page.keyboard.press('Escape');
      await expect(page.getByTestId('analysis-report')).not.toBeVisible();
    }
    const before = await drawn(page);
    expect(before.length).toBeGreaterThan(0);
    await page.getByTestId(e.other).click();
    await expect(page.getByTestId('legacy-map-section')).not.toHaveAttribute('data-task-id', id);
    await page.getByTestId(e.tab).click();
    await expect(page.getByTestId('legacy-map-section')).toHaveAttribute('data-task-id', id);
    await expect.poll(() => drawn(page)).toEqual(before);
    await page.reload();
    await expect(page.getByTestId('legacy-phase')).toHaveAttribute('data-phase', 'completed');
    await expect(page.getByTestId('legacy-map-section')).toHaveAttribute('data-task-id', id);
    await expect.poll(() => drawn(page)).toEqual(before);
    // 刷新后取回的旧结果不自动弹报告；也没有再提交。
    await expect(page.getByTestId('analysis-report')).toHaveCount(0);
    expect(creates).toHaveLength(1);
  });

  test('cancelling: a cancel the server has not confirmed is re-sent after a reload', async ({ page, request }) => {
    test.setTimeout(90_000);
    await mockMap(page);
    const creates = countCreates(page, e);
    let pending: Route | undefined;
    // 第一次取消请求挂住不放：页面只知道"已请求取消"，服务端还在跑。
    await page.route(`**${e.createPath}/*/cancel`, route => { pending = route; }, { times: 1 });
    await page.goto(e.route);
    await e.fill(page, 'slow');
    await start(page);
    const id = await shownTask(page, e);
    await expect(page.getByTestId('legacy-phase')).toHaveAttribute('data-phase', 'running');
    await page.getByRole('button', { name: '取消任务', exact: true }).click();
    await expect(page.getByTestId('legacy-phase')).toHaveAttribute('data-phase', 'cancelling');
    await expect.poll(() => pending !== undefined).toBe(true);
    expect(await storedHandle(page, e)).toMatchObject({ taskId: id, cancelRequested: true });
    expect(await serverStatus(request, e, id)).toBe('running');
    await page.reload();
    await pending!.abort().catch(() => undefined);
    await expect(page.getByText('任务已取消', { exact: true })).toBeVisible();
    await expect(page.getByTestId('legacy-task-id')).toHaveText(id);
    await expect.poll(() => serverStatus(request, e, id)).toBe('cancelled');
    await expect(page.getByRole('button', { name: '开始分析', exact: true })).toBeEnabled();
    expect(creates).toHaveLength(1);
  });

  test('failed: the failure stays attached to its task across switches and a reload and reruns only on request', async ({ page }) => {
    test.setTimeout(90_000);
    await mockMap(page);
    const creates = countCreates(page, e);
    await page.goto(e.route);
    await e.fill(page, 'failing');
    await start(page);
    const id = await shownTask(page, e);
    const error = page.getByTestId('legacy-error');
    await expect(error).toContainText('分析执行失败');
    await page.getByTestId(e.other).click();
    await page.getByTestId(e.tab).click();
    await expect(error).toContainText('分析执行失败');
    await expect(page.getByTestId('legacy-task-id')).toHaveText(id);
    await page.reload();
    await expect(error).toContainText('分析执行失败');
    await expect(page.getByTestId('legacy-task-id')).toHaveText(id);
    await expect(error.getByRole('button', { name: '重试' })).toHaveText('重新分析');
    expect(creates).toHaveLength(1);
    await error.getByRole('button', { name: '重试' }).click();
    const rerun = await shownTask(page, e, id);
    expect(rerun).not.toBe(id);
    await expect(error).toContainText('分析执行失败');
    // 失败的任务不能沿用同一请求标识，否则后端会把它认成同一次请求。
    expect(creates).toHaveLength(2);
    expect(creates[1]).not.toBe(creates[0]);
  });

  test('offline: losing the network neither fails nor resubmits; the same task finishes after reconnecting', async ({ page, request }) => {
    test.setTimeout(120_000);
    await mockMap(page);
    const creates = countCreates(page, e);
    await page.goto(e.route);
    await e.fill(page, 'slow');
    await start(page);
    const id = await shownTask(page, e);
    await expect(page.getByTestId('legacy-phase')).toHaveAttribute('data-phase', 'running');
    await page.context().setOffline(true);
    await expect(page.getByTestId('legacy-connection')).toBeVisible();
    await page.waitForTimeout(3000);
    expect(await serverStatus(request, e, id)).toBe('running');
    await expect(page.getByTestId('legacy-error')).toHaveCount(0);
    await page.context().setOffline(false);
    await expect(page.getByTestId('legacy-connection')).toHaveCount(0);
    await expect(page.getByTestId('legacy-map-section')).toHaveAttribute('data-task-id', id, { timeout: 60_000 });
    expect(creates).toHaveLength(1);
  });

  test("busy: another tab adopts this browser's own task; another browser is told who holds the slot", async ({ page, browser }) => {
    test.setTimeout(120_000);
    await mockMap(page);
    const creates = countCreates(page, e);
    await page.goto(e.route);
    // 同一浏览器的第二个页签在任务开始前就开着，模块里没有这个任务。
    const tab = await page.context().newPage();
    await guard(tab); await mockMap(tab);
    const tabCreates = countCreates(tab, e);
    await tab.goto(e.route);
    await e.fill(page, 'slow');
    await start(page);
    const id = await shownTask(page, e);
    await e.fill(tab, 'slow');
    await start(tab);
    await expect(tab.getByTestId('legacy-notice')).toContainText('占用名额的正是本浏览器此前提交');
    await expect(tab.getByTestId('legacy-task-id')).toHaveText(id);
    expect(tabCreates).toHaveLength(1);
    expect((await storedHandle(tab, e)).taskId).toBe(id);
    // 另一台浏览器（存储不共享）：只说明占用情况，不给它别人的任务，也不锁住它的按钮。
    const stranger = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const other = await stranger.newPage();
    await guard(other); await mockMap(other);
    await other.goto(BASE + e.route);
    await e.fill(other, 'slow');
    await start(other);
    const refused = other.getByTestId('legacy-error');
    await expect(refused).toContainText(e.busy);
    await expect(refused).toContainText('已运行');
    await expect(other.getByTestId('legacy-task-id')).toHaveCount(0);
    await expect(other.getByRole('button', { name: '开始分析', exact: true })).toBeEnabled();
    await stranger.close();
    // 从第二个页签取消：两个页签看到的是同一个任务的同一个结局。
    await tab.getByRole('button', { name: '取消任务', exact: true }).click();
    await expect(tab.getByText('任务已取消', { exact: true })).toBeVisible();
    await expect(page.getByText('任务已取消', { exact: true })).toBeVisible();
    expect(creates).toHaveLength(1);
    await tab.close();
  });
});
