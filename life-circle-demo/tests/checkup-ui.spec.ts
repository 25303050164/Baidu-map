/**
 * v2 体检工作台的界面测试。所有外部请求都被挡下，不用后端也不用真实 AK。
 *
 * 这一套盯的是**图上的行为**，也就是单元测试碰不到的那部分：
 *
 * - 勾选图层是各画各的，摘掉一层不会连累别层（旧页面用的是 `clearOverlays()`）；
 * - 视角只在选点与新结果时移动，勾图层、缩放都不该把用户正在看的地方拽走；
 * - 几百处设施同格合并、数量写在标记上，**一条也不截断**（旧页面有 100 条上限）；
 * - 灰区的空洞照原样画出来：带洞的面积与报告里的数字是同一份几何。
 */
import { test, expect, type Page } from '@playwright/test';
import { installMapSdk } from './mapSdk';
import { capabilities, collection, feature, layer, point, polygon, report, snapshot, task, zone }
  from '../src/checkup/fixtures';

const REVISION = 5;
const HASH = 'hash-5';

/** 带空洞的灰区：面积与报告一致，画的时候少一圈就是两回事。 */
function holed(offset = 0): Record<string, unknown> {
  const [x, y] = [116.39 + offset, 39.89];
  return { type: 'Polygon', coordinates: [
    [[x, y], [x + 0.02, y], [x + 0.02, y + 0.02], [x, y + 0.02], [x, y]],
    [[x + 0.005, y + 0.005], [x + 0.01, y + 0.005], [x + 0.01, y + 0.01], [x + 0.005, y + 0.01],
      [x + 0.005, y + 0.005]],
  ] };
}

type Options = {
  facilityCount?: number;
  holedGaps?: boolean;
  gapsNotReady?: boolean;
  graphConfigured?: boolean;
  runningFirst?: boolean;
};

async function setup(page: Page, options: Options = {}) {
  let submitted: { center: { lng: number; lat: number }; engine: string } | undefined;
  let statusCalls = 0;
  await installMapSdk(page);
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.hostname !== '127.0.0.1') return route.abort();
    if (!url.pathname.startsWith('/api/v2/')) return route.continue();

    if (url.pathname === '/api/v2/capabilities') {
      return route.fulfill({ json: capabilities({
        coverage: { graphConfigured: options.graphConfigured ?? true } }) });
    }
    if (url.pathname === '/api/v2/checkups') {
      const body = route.request().postDataJSON() as { center: { lng: number; lat: number };
        engine: string; clientRequestId: string };
      submitted = body;
      // 任务视图必须回报**这次请求**的 ID：客户端拿它对账，错一个就会（正确地）拒绝整份响应。
      return route.fulfill({ status: 202, json: task({ status: 'queued', stage: null, revision: 1,
        budget: 400, engine: body.engine, clientRequestId: body.clientRequestId }) });
    }
    const layerMatch = url.pathname.match(/\/layers\/([a-z_]+)$/);
    if (layerMatch) {
      if (layerMatch[1] === 'service_gaps' && options.gapsNotReady) {
        return route.fulfill({ status: 409, json: { code: 'checkup_service_gaps_not_ready',
          message: '服务灰区图层尚未生成，请等待该阶段完成' } });
      }
      return route.fulfill({ json: layerFor(layerMatch[1], options) });
    }
    if (url.pathname.endsWith('/result')) {
      return route.fulfill({ json: snapshotFor(options, submitted!) });
    }
    if (url.pathname.endsWith('/cancel')) {
      return route.fulfill({ json: task({ status: 'cancelled', stage: null, revision: 1 }) });
    }
    if (url.pathname.includes('/routes/')) {
      return route.fulfill({ json: { taskId: 'task-1', revision: REVISION,
        facilityId: 'f-0', category: 'pharmacy', majorCategory: 'medical',
        origin: { lng: 116.405, lat: 39.916 }, destination: { lng: 116.41, lat: 39.92 },
        straightLineM: 700, withinRule: true, routeDistanceM: 805.05, durationS: 670.87,
        observedDurationS: 670.87, poiStatus: 'verified_reachable', poiReason: null,
        evidenceGrade: 'verified', routeOrigin: { lng: 116.405, lat: 39.916 },
        routeDestination: { lng: 116.41, lat: 39.92 }, originOffsetM: 0, destinationOffsetM: 0,
        reason: null, provider: 'offline-fixture', network: true, attempts: 1, budget: {},
        notes: [] } });
    }
    // 任务状态：默认一次就完成；要看阶段推进时，先给一次"进行中"。
    statusCalls++;
    if (options.runningFirst && statusCalls === 1) {
      return route.fulfill({ json: task({ status: 'running', stage: 'poi', revision: 1,
        budget: 400, engine: submitted?.engine ?? 'baidu_e82' }) });
    }
    return route.fulfill({ json: task({ status: 'completed', stage: 'ready', revision: REVISION,
      businessStatus: 'partial', budget: 400, engine: submitted?.engine ?? 'baidu_e82' }) });
  });
}

/** 每层的载荷都按后端实际返回的形态给：等时圈是单个面，其余五层是要素集合。 */
function layerFor(id: string, options: Options): unknown {
  const base = { revision: REVISION, resultHash: HASH, document: null };
  if (id === 'isochrone') return layer({ ...base, layerId: 'isochrone', geometry: polygon(),
    displayGeometry: polygon() });
  if (id === 'accessibility') return layer({ ...base, layerId: 'accessibility', displayGeometry: null,
    geometry: collection([feature(polygon(0.03), { id: 'domain' })]) });
  if (id === 'service_gaps') {
    return layer({ ...base, layerId: 'service_gaps', displayGeometry: null, geometry: collection(
      options.holedGaps
        ? [feature(holed(), { id: 'zone-0', queryStatus: 'complete' }),
          feature(polygon(0.05), { id: 'zone-1', queryStatus: 'partial' })]
        : [feature(polygon(0.05), { id: 'zone-1', queryStatus: 'partial' })]) });
  }
  if (id === 'facilities') {
    const count = options.facilityCount ?? 2;
    return layer({ ...base, layerId: 'facilities', displayGeometry: null, geometry: collection(
      Array.from({ length: count }, (_, index) => feature(
        // 20 × N 的紧密网格：默认缩放下必然同格，用来验证"合并而不是截断"。
        point(+(116.405 + (index % 10) * 0.00002).toFixed(6),
          +(39.916 + Math.floor(index / 10) * 0.00002).toFixed(6)),
        { id: `f-${index}`, name: `设施 ${index}`,
          majorCategory: index % 3 === 2 ? 'shopping' : 'medical' }))) });
  }
  if (id === 'verification') {
    return layer({ ...base, layerId: 'verification', displayGeometry: null, geometry: collection([
      feature(point(116.405, 39.916), { facilityId: 'f-0', status: 'verified_reachable' }),
      feature(point(116.406, 39.9165), { facilityId: 'f-1', status: 'verified_unreachable' })]) });
  }
  return layer({ ...base, layerId: id, displayGeometry: null, geometry: collection([]) });
}

function snapshotFor(options: Options, submitted: { center: { lng: number; lat: number } }) {
  const zones = options.holedGaps
    ? [zone({ id: 'zone-0', geometry: holed(), displayGeometry: holed(), reason: 'no_valid_entrance',
      suggestion: '核对通道接入与门牌归属。' }),
      zone({ id: 'zone-1', index: 1, queryStatus: 'partial', geometry: polygon(0.05),
        displayGeometry: polygon(0.05), suggestion: '设施检索未完成，先补采再判定。' })]
    : [zone({ id: 'zone-1', index: 1, queryStatus: 'partial', geometry: polygon(0.05),
      displayGeometry: polygon(0.05), suggestion: '设施检索未完成，先补采再判定。' })];
  const base = snapshot();
  return snapshot({
    center: { lng: submitted.center.lng, lat: submitted.center.lat },
    serviceGaps: { ...base.serviceGaps!, zones },
    report: report({ gaps: { ...report().gaps, zones } }),
    accessibility: { ...base.accessibility!, domain: polygon(0.03) },
  });
}

/** 地图审计：只取当前挂在图上的多边形与标记。 */
const audit = (page: Page) => page.evaluate(() => (window as unknown as { __mapAudit: {
  paths: string[][]; markers: { uid: number; point: { lng: number; lat: number };
    options: { title: string } }[]; pans: { lng: number; lat: number }[]; creations: number;
  active: number } }).__mapAudit);

/** 标题里的点数：同格合并时写的是这一格的数量，没有数字的按一个算。 */
function counted(markers: { options: { title: string } }[]): number {
  return markers.reduce((sum, marker) =>
    sum + (Number(/^(\d+) 个点/.exec(marker.options.title)?.[1]) || 1), 0);
}

const pickAndStart = async (page: Page) => {
  await page.getByTestId('checkup-map').click();
  await page.getByRole('button', { name: '开始体检', exact: true }).click();
  await expect(page.getByTestId('checkup-report')).toBeVisible();
  await page.keyboard.press('Escape');
};

/**
 * 图层是一层一层取的，报告先出来、图后画完。所以断言图上的东西之前要先等到位 ——
 * 直接读审计数组测的是"此刻画到哪了"，不是"最终画成了什么"。
 */
const pathsAre = (page: Page, count: number) =>
  expect.poll(async () => (await audit(page)).paths.length).toBe(count);
const markersAre = (page: Page, count: number) =>
  expect.poll(async () => (await audit(page)).markers.length).toBe(count);
const pointsAre = (page: Page, count: number) =>
  expect.poll(async () => counted((await audit(page)).markers)).toBe(count);

test('a published revision draws its layers, opens the report and keeps the view still', async ({ page }) => {
  const errors: string[] = [];
  await setup(page);
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await expect(page.getByTestId('quota-label'))
    .toHaveText('本应用预算余额（不含浏览器 SDK、其他应用及旧接口流量）');
  // 选点：只会平移这一次；后面取图层、画标记都不再动视角。
  await pickAndStart(page);
  const report = page.getByTestId('checkup-report');
  await expect(report).toContainText('15 分钟生活圈体检报告');
  await expect(report).toContainText('task-1 · 第 5 版');
  await expect(page.getByTestId('coverage-shopping')).toContainText('40.0%');
  await expect(page.getByTestId('coverage-shopping')).toContainText('70.0%');
  // 灰区清单照抄后端给的说法：它是"要做的事"，不是界面重新措辞的提示。
  await expect(page.getByTestId('zone-zone-1')).toContainText('设施检索未完成，先补采再判定。');
  await expect(page.getByTestId('verification-summary')).toContainText('已核验 4 处设施');

  const drawn = await audit(page);
  // 等时圈 1 + 评估域 1 + 灰区 1：三层各画各的，谁也不清空谁。
  await pathsAre(page, 3);
  expect(drawn.creations).toBe(1);
  expect(drawn.pans.map(pan => [pan.lng, pan.lat])).toEqual([[116.405, 39.916]]);
  expect(errors).toEqual([]);
});

test('layers are independent: unchecking one leaves the others on the map', async ({ page }) => {
  await setup(page, { facilityCount: 3 });
  await page.goto('/');
  await pickAndStart(page);
  await pathsAre(page, 3);
  // 3 处设施合成 1 枚 + 2 处核验 + 1 枚中心标记。
  await markersAre(page, 4);
  await page.getByRole('checkbox', { name: '服务灰区', exact: true }).uncheck();
  await pathsAre(page, 2);
  // 设施点位不受影响：摘掉一层不该顺手把别层也摘了。
  await markersAre(page, 4);
  await page.getByRole('checkbox', { name: '服务灰区', exact: true }).check();
  await pathsAre(page, 3);
});

test('hundreds of facilities are merged by cell, never truncated', async ({ page }) => {
  await setup(page, { facilityCount: 200 });
  await page.goto('/');
  await pickAndStart(page);
  // 200 处设施 + 2 处核验 + 1 枚中心标记：合并之后标记远少于点数，但总数一个不少 ——
  // 这正是"聚合而不是截断"（中心标记不写数量，按一个算，所以这里连它一起数）。
  await pointsAre(page, 203);
  const drawn = await audit(page);
  expect(drawn.markers.length).toBeLessThan(20);
  const merged = drawn.markers.filter(marker => /^\d+ 个点/.test(marker.options.title));
  expect(merged.length).toBeGreaterThan(0);
  expect(Number(/^(\d+) 个点/.exec(merged[0].options.title)![1])).toBeGreaterThan(1);
  await expect(page.getByTestId('checkup-legend')).toContainText('同格合并显示，数据不截断');
});

test('a grey zone keeps its hole, and the partial one is drawn as a different conclusion', async ({ page }) => {
  await setup(page, { holedGaps: true });
  await page.goto('/');
  await pickAndStart(page);
  // 等时圈 1 + 评估域 1 + 两个灰区 = 4 个覆盖物，其中带洞的那个有两圈。
  await pathsAre(page, 4);
  const drawn = await audit(page);
  expect(drawn.paths.filter(rings => rings.length === 2)).toHaveLength(1);
  await expect(page.getByTestId('checkup-legend')).toContainText('服务灰区');
});

test('zoom and pan end re-project the points without moving the view or dropping layers', async ({ page }) => {
  await setup(page, { facilityCount: 40 });
  await page.goto('/');
  await pickAndStart(page);
  await pathsAre(page, 3);
  await markersAre(page, 4);
  const before = await audit(page);
  // 真实 SDK 在缩放/平移结束时会发事件：点图层据此重新投影，面不动，视角也不该被拽走。
  await page.evaluate(() => {
    const audit = (window as unknown as { __mapAudit: { views: Record<string, (() => void)[]> } }).__mapAudit;
    for (const type of ['zoomend', 'moveend']) for (const handler of audit.views[type] ?? []) handler();
  });
  const after = await audit(page);
  // 点图层重新投影过：设施与核验的标记是新建的（中心标记不动，所以不是"全都换了"）。
  expect(after.markers.filter((marker, index) => marker.uid !== before.markers[index]?.uid).length)
    .toBeGreaterThan(0);
  expect(after.markers).toHaveLength(before.markers.length);
  expect(counted(after.markers)).toBe(counted(before.markers));
  // 面是按地理坐标画的，视图变化不影响它们：既没重画，也没被清掉。
  expect(after.paths).toEqual(before.paths);
  expect(after.pans).toEqual(before.pans);
  // 重新投影过：标记重建了，但数量与内容一致。
  expect(after.markers.map(marker => marker.uid))
    .not.toEqual(before.markers.map(marker => marker.uid));
});

test('a layer that is not ready says so by name, and the rest still draw', async ({ page }) => {
  await setup(page, { gapsNotReady: true });
  await page.goto('/');
  await pickAndStart(page);
  // 后端的原话照登：把 409 说成"这一层是空的"，读者会以为灰区已经查过了。
  await expect(page.getByText('服务灰区图层尚未生成，请等待该阶段完成', { exact: true })).toBeVisible();
  await pathsAre(page, 2);
});

test('stages advance as the backend reports them, and the engines come from the service', async ({ page }) => {
  await setup(page, { runningFirst: true, graphConfigured: false });
  await page.goto('/');
  // 还没提交时没有阶段可显示：进度条不是"空着等"，是没有这一栏。
  await expect(page.getByTestId('checkup-stages')).toHaveCount(0);
  await pickAndStart(page);
  const stages = page.getByTestId('checkup-stages');
  await expect(stages).toBeVisible();
  for (const label of ['等时圈', '设施检索', '服务覆盖', '步行核验', '报告', '完成']) {
    await expect(stages.locator('li', { hasText: label })).toHaveAttribute('data-reached', 'yes');
  }
  // 档位是引擎自己带来的，不是界面写死的三档。
  // antd 的下拉项由虚拟列表渲染，可见性判定不稳，这里断言挂载与文本 —— 那才是"能选什么"。
  await page.getByRole('combobox', { name: '调用预算' }).click();
  await expect(page.getByRole('option', { name: '800 次' })).toBeAttached();
  // 选项正文只有数字，"次"在无障碍标签上 —— 断言两者，免得哪天单位丢了也没人发现。
  expect(await page.getByRole('option').allTextContents()).toEqual(['200', '400', '800']);
  await page.keyboard.press('Escape');
  // 两个引擎都列着：路网没配好只影响后端的取舍，界面不替它隐藏其中一个。
  await page.getByRole('combobox', { name: '引擎' }).click();
  await expect(page.getByRole('option', { name: '百度边界搜索（E8.2）' })).toBeAttached();
  await expect(page.getByRole('option', { name: 'OSM＋百度' })).toBeAttached();
});
