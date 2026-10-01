/**
 * 体检任务生命周期与实时进度：真实底图，可控长任务，零百度服务额度。
 *
 * 后端是 `backend/tests/checkup_lifecycle_app.py`：任务、修订、进度、取消与恢复全是生产代码，
 * 只有出进程的那一层换成了能停住的替身。用例在指定处停住任务、放行，于是下面这些情形都能
 * 在真实底图上按需复现，而不必等一次真实体检恰好走到那里：
 *
 * - 首次请求前：创建的回答被压住时刷新 —— 按请求标识找回同一个任务；回答超过 15 秒超时 ——
 *   不重提，照样找回。任务停在第一次采样之前，网络尝试 0 次，已用时照走。
 * - 同一阶段持续运行：采样按节奏推进，界面上的计数跟着后端涨，阶段不变。
 * - 阶段切换：后端换到设施检索，界面在几秒内跟上。
 * - 长时间无进展：检索停住 90 秒以上 —— "疑似停滞"，说明停在哪一步、可以取消；放行后恢复。
 *   不报中间计数的步骤（载入路网）停住同样久仍是"正在计算或等待"，不会提前报停滞。
 * - 断网与服务端不可达：连接中断、失败次数与已用时照走；后端期间继续推进，恢复后马上跟上。
 * - 切算法、刷新：接回同一个任务与进度，已用时不归零。
 * - 取消中恢复：取消后刷新仍是"正在取消"，放行后停在"已取消"。
 * - 两个引擎、两个中心：后一个排队；地图、修订与报告各归各的，来回切换、刷新都不串。
 *
 * 全程记两本账：后端数创建与取消请求、页面数自己发出的创建请求，都要与用例的意图一致；后端的
 * 套接字守卫数外部连接，必须是 0。输出（截图与 `summary.json`，不含任何 URL 与 AK）写到
 * `LIFECYCLE_OUTPUT_DIR`。
 */
import { test, expect, type APIRequestContext, type BrowserContext, type Page } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { QUIET_STALL_SECONDS, STALL_SECONDS } from '../src/checkup/live';

const API = `http://127.0.0.1:${process.env.LIFECYCLE_API_PORT ?? 8021}`;
const OUTPUT = resolve(process.env.LIFECYCLE_OUTPUT_DIR ?? 'output/checkup-lifecycle');
const redact = (value: string) => value.replace(/([?&](?:ak|key|token)=)[^&\s]+/gi, '$1[REDACTED]');

type LngLat = { lng: number; lat: number };
/** 国定一社区；B 在它东边约 700 米。两个中心的等时圈、设施与报告都不同，串了看得出来。 */
const A: LngLat = { lng: 121.513925, lat: 31.313079 };
const B: LngLat = { lng: 121.5213, lat: 31.313079 };

const EARTH_R = 6_371_008.8;
const rad = (deg: number) => (deg * Math.PI) / 180;
function meters(a: LngLat, b: LngLat): number {
  const dLat = rad(b.lat - a.lat), dLng = rad(b.lng - a.lng);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dLng / 2) ** 2;
  return 2 * EARTH_R * Math.asin(Math.min(1, Math.sqrt(h)));
}

// ---------------------------------------------------------------- 控制后端

type StoredTask = { taskId: string; clientRequestId: string; engine: string; status: string;
  stage: string | null; revision: number; center: LngLat };
type ControlState = { graph: string; requests: Record<string, number>;
  external: { attempts: number; targets: string[] }; tasks: StoredTask[];
  loop: { worstSeconds: number; stallCount: number; stalls: { at: number; seconds: number; stacks: string[][] }[] };
  armed: string[]; holding: string[]; passes: Record<string, number>; pace: Record<string, number> };
type TaskView = { taskId: string; status: string; stage: string | null; revision: number; networkRequests: number;
  serverTime?: number; lastActivityAt?: number | null;
  progress?: { step: string; label: string; count: number | null; unit: string | null } | null };

async function control(request: APIRequestContext, path: string, data?: unknown): Promise<ControlState> {
  const response = data === undefined ? await request.get(`${API}${path}`) : await request.post(`${API}${path}`, { data });
  expect(response.ok(), `${path} → ${response.status()}`).toBe(true);
  return await response.json() as ControlState;
}
const backend = (request: APIRequestContext) => control(request, '/control/state');
const arm = (request: APIRequestContext, point: string, after = 0) => control(request, '/control/arm', { point, after });
const release = (request: APIRequestContext, point: string) => control(request, '/control/release', { point });
const pace = (request: APIRequestContext, point: string, seconds: number) =>
  control(request, '/control/pace', { point, seconds });

async function taskView(request: APIRequestContext, taskId: string): Promise<TaskView> {
  const response = await request.get(`${API}/api/v2/checkups/${taskId}`);
  expect(response.ok()).toBe(true);
  return await response.json() as TaskView;
}

/** 等到后端真的停在某处（任务确实走到了那里，而不是还没走到）。 */
async function heldAt(request: APIRequestContext, point: string, timeout = 120000) {
  await expect.poll(async () => (await backend(request)).holding, { timeout, intervals: [250] }).toContain(point);
}

async function settled(request: APIRequestContext, taskId: string, statuses: string[], timeout = 400000) {
  await expect.poll(async () => (await taskView(request, taskId)).status, { timeout, intervals: [1000] })
    .toMatch(new RegExp(`^(${statuses.join('|')})$`));
}

// ---------------------------------------------------------------- 页面

/** 记下地图实例：BMapGL 不把它挂在任何全局上。只挂钩子，不改任何行为。 */
function installProbe() {
  const w = window as unknown as Record<string, unknown> & { BMapGL?: { Map?: { prototype: Record<string, unknown> } } };
  const maps = new Set<unknown>();
  w.__lifecycleMaps = maps;
  const hook = () => {
    const proto = w.BMapGL?.Map?.prototype;
    if (!proto) return false;
    if (proto.__lifecycleHooked) return true;
    Object.defineProperty(proto, '__lifecycleHooked', { value: true });
    for (const name of ['addOverlay', 'centerAndZoom', 'panTo']) {
      const original = proto[name];
      if (typeof original !== 'function') continue;
      proto[name] = function (this: unknown, ...args: unknown[]) {
        maps.add(this);
        return (original as (...a: unknown[]) => unknown).apply(this, args);
      };
    }
    return true;
  };
  const timer = window.setInterval(() => { if (hook()) window.clearInterval(timer); }, 25);
}

type Drawn = { centre: LngLat | null; resultMarker: LngLat | null; polygons: number; polygonCentre: LngLat | null };

/** 当前体检地图上画着什么：视野中心、"本次体检中心"标记、多边形图层的外包框中心。 */
async function drawn(page: Page): Promise<Drawn> {
  return page.evaluate(() => {
    type Point = { lng: number; lat: number };
    type Overlay = { getPath?: () => Point[]; getPosition?: () => Point; getTitle?: () => string };
    type AnyMap = { getContainer: () => HTMLElement; getCenter: () => Point; getOverlays: () => Overlay[] };
    const host = document.querySelector('[data-testid="checkup-map"]');
    const maps = [...((window as unknown as { __lifecycleMaps?: Set<AnyMap> }).__lifecycleMaps ?? [])];
    // 切换引擎时旧地图被销毁，它的 getContainer() 之后回 undefined：只认还挂在页面上的那张。
    const live = (item: AnyMap) => {
      const box = item.getContainer() as HTMLElement | undefined;
      return Boolean(host && box?.isConnected && host.contains(box));
    };
    const map = maps.find(live);
    if (!map) return { centre: null, resultMarker: null, polygons: 0, polygonCentre: null };
    const centre = map.getCenter();
    let resultMarker: Point | null = null;
    let polygons = 0;
    const box = [Infinity, Infinity, -Infinity, -Infinity];
    for (const overlay of map.getOverlays()) {
      // 带内环的多边形（OSM＋百度的圈面扣掉水体）getPath() 回的是一组环：摊平后再取点。
      const raw = typeof overlay.getPath === 'function' ? overlay.getPath() as unknown[] : null;
      const path = raw && (raw.flat(2) as Point[]).filter(p => Number.isFinite(p?.lng) && Number.isFinite(p?.lat));
      if (path && path.length > 2) {
        polygons += 1;
        for (const p of path) {
          box[0] = Math.min(box[0], p.lng); box[1] = Math.min(box[1], p.lat);
          box[2] = Math.max(box[2], p.lng); box[3] = Math.max(box[3], p.lat);
        }
      } else if (typeof overlay.getTitle === 'function' && overlay.getTitle()?.startsWith('本次体检中心')) {
        const p = overlay.getPosition!();
        resultMarker = { lng: p.lng, lat: p.lat };
      }
    }
    return { centre: { lng: centre.lng, lat: centre.lat }, resultMarker, polygons,
      polygonCentre: polygons ? { lng: (box[0] + box[2]) / 2, lat: (box[1] + box[3]) / 2 } : null };
  });
}

type Reading = { at: number; kind: string | null; phase: string | null; taskId: string | null; revision: number | null;
  requests: number | null; elapsed: number | null; stage: string | null; step: string | null; count: number | null;
  contact: number | null; activity: number | null; title: string; hint: string; elapsedText: string };

/** 进度卡此刻的读数：全部取自 data-* 属性与提示框原文。 */
async function read(page: Page): Promise<Reading> {
  const raw = await page.evaluate(() => {
    const el = (id: string) => document.querySelector(`[data-testid="${id}"]`);
    const attr = (id: string, name: string) => el(id)?.getAttribute(name) ?? null;
    const live = el('checkup-live');
    return { kind: attr('checkup-live', 'data-kind'), phase: attr('checkup-phase', 'data-phase'),
      taskId: el('checkup-task-id')?.textContent ?? null, revision: attr('checkup-revision', 'data-revision'),
      requests: attr('checkup-requests', 'data-count'), elapsed: attr('checkup-elapsed', 'data-seconds'),
      stage: attr('checkup-stage-now', 'data-stage'), step: attr('checkup-step', 'data-step'),
      count: attr('checkup-step', 'data-count'), contact: attr('checkup-contact', 'data-seconds'),
      activity: attr('checkup-activity', 'data-seconds'),
      title: live?.querySelector('.ant-alert-title, .ant-alert-message')?.textContent ?? '',
      hint: live?.querySelector('.ant-alert-description')?.textContent ?? '',
      elapsedText: el('checkup-elapsed')?.textContent ?? '' };
  });
  const num = (value: string | null) => value === null || value === '' ? null : Number(value);
  return { ...raw, at: Date.now(), revision: num(raw.revision), requests: num(raw.requests), elapsed: num(raw.elapsed),
    count: num(raw.count), contact: num(raw.contact), activity: num(raw.activity) };
}

const timeline: (Reading & { scenario: string })[] = [];

/** 每秒读一次，读 `seconds` 秒：已用时必须单调不减、并且真的在走。 */
async function watch(page: Page, scenario: string, seconds: number): Promise<Reading[]> {
  const readings: Reading[] = [];
  for (let i = 0; i <= seconds; i += 1) {
    if (i) await page.waitForTimeout(1000);
    const reading = await read(page);
    readings.push(reading);
    timeline.push({ scenario, ...reading, hint: reading.hint.slice(0, 160) });
  }
  const elapsed = readings.map(r => r.elapsed).filter((v): v is number => v !== null);
  expect(elapsed.length, `${scenario}：每次都读得到已用时`).toBe(readings.length);
  for (let i = 1; i < elapsed.length; i += 1) expect(elapsed[i]).toBeGreaterThanOrEqual(elapsed[i - 1]);
  expect(elapsed[elapsed.length - 1] - elapsed[0], `${scenario}：已用时在走`).toBeGreaterThanOrEqual(seconds - 1.5);
  return readings;
}

async function expectRealBasemap(page: Page) {
  await expect(page.getByText('地图不可用')).toHaveCount(0);
  await expect(page.getByText('正在加载百度地图')).toHaveCount(0, { timeout: 60000 });
  await expect.poll(() => page.getByTestId('checkup-map').locator('canvas:not([data-map-layer])').count(),
    { timeout: 60000 }).toBeGreaterThan(0);
}

type Ledger = { creates: number; cancels: number; problems: string[] };

/** 打开体检页并开始记账：页面自己发出的创建与取消请求、页面报错（已脱敏）。 */
async function open(page: Page, slot: 'e82' | 'hybrid', ledger: Ledger) {
  await page.addInitScript(installProbe);
  page.on('request', request => {
    const { pathname } = new URL(request.url());
    if (request.method() !== 'POST' || !pathname.startsWith('/api/v2/checkups')) return;
    if (pathname.replace(/\/$/, '') === '/api/v2/checkups') ledger.creates += 1;
    else if (pathname.endsWith('/cancel')) ledger.cancels += 1;
  });
  page.on('pageerror', error => ledger.problems.push(redact(`page: ${error.message}`)));
  await page.goto(`/#/checkup/${slot}`);
  await expectRealBasemap(page);
}

async function startAt(page: Page, centre: LngLat) {
  // 坐标输入在「我的位置」选项卡里；切选项卡不影响任务，只是界面导航。
  await page.getByTestId('checkup-tab-location').click();
  await page.getByRole('spinbutton', { name: '经度' }).fill(centre.lng.toFixed(6));
  await page.getByRole('spinbutton', { name: '纬度' }).fill(centre.lat.toFixed(6));
  await page.getByRole('spinbutton', { name: '纬度' }).press('Tab');
  const button = page.getByRole('button', { name: '开始体检' });
  await expect(button).toBeEnabled();
  await button.click();
}

const segment = (page: Page, text: string) => page.locator('.ant-segmented-item', { hasText: text });
async function switchTo(page: Page, text: string) {
  // 成圈算法切换已并入「采样与引擎」选项卡：先切到那一块，再点算法。
  await page.getByTestId('checkup-tab-engine').click();
  await segment(page, text).click();
  await expect(segment(page, text)).toHaveClass(/ant-segmented-item-selected/);
}

const liveKind = (page: Page) => page.getByTestId('checkup-live');

async function shot(page: Page, name: string) {
  await page.screenshot({ path: resolve(OUTPUT, name) });
  return name;
}

async function expectResult(page: Page, taskId: string, revision: number, centre: LngLat, other: LngLat) {
  const section = page.getByTestId('checkup-map-section');
  await expect(section).toHaveAttribute('data-task-id', taskId, { timeout: 60000 });
  await expect(section).toHaveAttribute('data-revision', String(revision));
  await expect(section).toHaveAttribute('data-drawn-revisions', String(revision), { timeout: 60000 });
  // 地图上画的是这个任务的：等时圈等多边形围着它的中心，"本次体检中心"标记就在它的中心。
  let map: Drawn | null = null;
  await expect.poll(async () => { map = await drawn(page); return map.polygons > 0 && map.resultMarker !== null; },
    { timeout: 60000 }).toBe(true);
  const seen = map as unknown as Drawn;
  expect(meters(seen.resultMarker!, centre)).toBeLessThan(1);
  expect(meters(seen.polygonCentre!, centre), '多边形围着本任务的中心').toBeLessThan(250);
  expect(meters(seen.polygonCentre!, other), '而不是另一个任务的中心').toBeGreaterThan(450);
  // 报告同样属于这个任务。
  const report = page.getByTestId('checkup-report');
  if (!await report.isVisible()) await page.getByRole('button', { name: '查看体检报告' }).click();
  await expect(report).toHaveAttribute('data-task-id', taskId);
  await expect(report).toHaveAttribute('data-revision', String(revision));
  await expect(report).toContainText(`${centre.lng.toFixed(6)}, ${centre.lat.toFixed(6)}`);
  await expect(report).not.toContainText(`${other.lng.toFixed(6)}, ${other.lat.toFixed(6)}`);
  return seen;
}

async function closeReport(page: Page) {
  const report = page.getByTestId('checkup-report');
  if (await report.isVisible()) {
    await page.locator('.ant-drawer-close').click();
    await expect(report).toBeHidden();
  }
}

const summary: Record<string, unknown> = {};
const record = (key: string, value: unknown) => { summary[key] = value; };
let startState: ControlState | null = null;
let startedAt = 0;

test.describe.configure({ mode: 'serial' });

test.beforeAll(async ({ request }) => {
  mkdirSync(OUTPUT, { recursive: true });
  await control(request, '/control/reset', {});
  startState = await backend(request);
  startedAt = Date.now() / 1000;
});

test.afterEach(async ({ request }) => {
  // 每条用例结束时后端不留停点：下一条从干净的状态开始。
  await control(request, '/control/reset', {});
});

test.afterAll(async ({ request }) => {
  const end = await backend(request);
  const before = startState!;
  record('backend', { creates: (end.requests.create ?? 0) - (before.requests.create ?? 0),
    cancels: (end.requests.cancel ?? 0) - (before.requests.cancel ?? 0),
    externalAttempts: end.external.attempts, externalTargets: end.external.targets,
    tasks: end.tasks.filter(task => !before.tasks.some(old => old.taskId === task.taskId)) });
  // 状态接口和任务共用后端的事件循环：这一轮里它有没有被占住超过 1 秒、占在哪一行。
  const stalls = end.loop.stalls.filter(stall => stall.at >= startedAt);
  record('eventLoop', { thresholdSeconds: 1, stalls: stalls.length,
    worstSeconds: Math.max(0, ...stalls.map(stall => stall.seconds)), detail: stalls });
  writeFileSync(resolve(OUTPUT, 'summary.json'), `${JSON.stringify({ generatedAt: new Date().toISOString(),
    stallSeconds: STALL_SECONDS, quietStallSeconds: QUIET_STALL_SECONDS, ...summary,
    timeline }, null, 1)}\n`);
  expect(end.external.attempts, '整轮没有任何出进程的连接（不花百度额度）').toBe(0);
});

// ---------------------------------------------------------------- 用例

test('百度边界搜索（E8.2）一个任务走完全程：首次请求前刷新、同一阶段持续运行、阶段切换、疑似停滞、断网与服务端不可达、切算法与刷新 —— 进度一路真实，只建一个任务', async ({ page, context, request }) => {
  const ledger: Ledger = { creates: 0, cancels: 0, problems: [] };
  const before = await backend(request);
  await open(page, 'e82', ledger);

  // 1. 首次请求前：创建的回答压住，任务停在第一次采样之前（还没预留任何一次采样）。
  await arm(request, 'create');
  await arm(request, 'isochrone');
  await startAt(page, A);
  await expect(liveKind(page)).toHaveAttribute('data-kind', 'submitting');
  await expect(page.getByTestId('checkup-task-id')).toHaveCount(0);
  await heldAt(request, 'create');
  await heldAt(request, 'isochrone');
  const created = (await backend(request)).tasks.filter(task => !before.tasks.some(old => old.taskId === task.taskId));
  expect(created).toHaveLength(1);
  const taskId = created[0].taskId;
  record('submitting', { reading: await read(page), screenshot: await shot(page, '01-submitting.png') });

  // 回答还没到就刷新：按请求标识找回同一个任务，不重提。
  await page.reload();
  await expectRealBasemap(page);
  await expect(page.getByTestId('checkup-task-id')).toHaveText(taskId, { timeout: 30000 });
  await release(request, 'create');
  await expect(liveKind(page)).toHaveAttribute('data-kind', 'working');
  const first = await watch(page, 'before-first-request', 4);
  for (const reading of first) {
    expect(reading.stage).toBe('isochrone');
    expect(reading.requests, '第一次采样之前：一次网络尝试也没有').toBe(0);
    expect(reading.taskId).toBe(taskId);
  }
  // 服务端没有新进展：最近活动的秒数跟着涨，最近一次成功连接一直是刚才。
  expect(first[first.length - 1].activity! - first[0].activity!).toBeGreaterThanOrEqual(2.5);
  expect(Math.max(...first.map(r => r.contact!))).toBeLessThan(3);
  record('beforeFirstRequest', { taskId, readings: first.length, screenshot: await shot(page, '02-before-first-request.png') });

  // 2. 同一阶段持续运行：采样按 0.2 秒一次推进；设施检索发出 2 页后停住。
  await arm(request, 'places', 2);
  await pace(request, 'sampling', 0.2);
  await release(request, 'isochrone');
  const sampling = await watch(page, 'same-stage', 8);
  const counts = sampling.map(r => r.count ?? 0);
  for (let i = 1; i < counts.length; i += 1) expect(counts[i]).toBeGreaterThanOrEqual(counts[i - 1]);
  expect(counts[counts.length - 1] - counts[0], '采样计数在涨').toBeGreaterThan(10);
  expect(sampling.filter(r => r.stage === 'isochrone').length).toBeGreaterThan(5);
  // 界面上的计数跟得上后端：先读后端，界面 2.5 秒内不低于它。
  const server = await taskView(request, taskId);
  if (server.stage === 'isochrone' && server.progress?.count != null) {
    const target = server.progress.count;
    await expect.poll(async () => (await read(page)).count ?? -1, { timeout: 2500, intervals: [200] })
      .toBeGreaterThanOrEqual(target);
  }
  record('sameStage', { counts, screenshot: await shot(page, '03-same-stage.png') });

  // 3. 阶段切换：采样放开，后端换到设施检索的那一刻起算，界面几秒内跟上。
  await pace(request, 'sampling', 0);
  await expect.poll(async () => (await taskView(request, taskId)).stage, { timeout: 120000, intervals: [100] }).toBe('poi');
  const switchedAt = Date.now();
  await expect.poll(async () => (await read(page)).stage, { timeout: 5000, intervals: [100] }).toBe('poi');
  const lag = (Date.now() - switchedAt) / 1000;
  expect(lag, '阶段切换在界面上的延迟（秒）').toBeLessThan(3);
  await heldAt(request, 'places');
  const places = await taskView(request, taskId);
  expect(places.progress?.count, '停住时已发出 2 页').toBe(2);
  await expect.poll(async () => (await read(page)).count, { timeout: 3000 }).toBe(2);
  await expect.poll(async () => (await read(page)).revision, { timeout: 3000 }).toBe(places.revision);
  record('stageSwitch', { lagSeconds: lag, revision: places.revision, screenshot: await shot(page, '04-stage-switch.png') });

  // 4. 长时间无进展：停在第 3 页之前。未满 90 秒是"正在计算或等待"，满了是"疑似停滞"。
  const quietFrom = (await taskView(request, taskId)).lastActivityAt!;
  await page.waitForTimeout(30000);
  const waiting = await read(page);
  expect(waiting.kind).toBe('working');
  expect(waiting.hint).toContain('最近进展');
  await expect(liveKind(page)).toHaveAttribute('data-kind', 'stalled', { timeout: (STALL_SECONDS + 15) * 1000 });
  const stalled = await read(page);
  const serverNow = (await taskView(request, taskId)).serverTime!;
  expect(serverNow - quietFrom, '疑似停滞出现在服务端沉默满 90 秒之后').toBeGreaterThanOrEqual(STALL_SECONDS - 1);
  expect(stalled.title).toContain('疑似停滞');
  expect(stalled.hint).toContain('检索');
  expect(stalled.hint).toContain('可取消');
  expect(stalled.activity!).toBeGreaterThanOrEqual(STALL_SECONDS - 1);
  expect(stalled.contact!, '连接本身是好的').toBeLessThan(3);
  expect(stalled.count).toBe(2);
  await watch(page, 'stalled', 3);
  record('stalled', { reading: stalled, serverSilence: serverNow - quietFrom, screenshot: await shot(page, '05-stalled.png') });

  // 放行：有了新进展，马上回到"正在计算或等待"。
  await pace(request, 'places', 0.4);
  const released = Date.now();
  await release(request, 'places');
  await expect(liveKind(page)).toHaveAttribute('data-kind', 'working', { timeout: 5000 });
  await expect.poll(async () => (await read(page)).count ?? 0, { timeout: 5000 }).toBeGreaterThan(2);
  record('unstalled', { seconds: (Date.now() - released) / 1000 });

  // 5. 断网：连接中断、失败次数与已用时照走；后端这期间继续检索。
  await arm(request, 'assessment', 3);
  const offlineFrom = await taskView(request, taskId);
  await context.setOffline(true);
  await expect(liveKind(page)).toHaveAttribute('data-kind', 'lost', { timeout: 5000 });
  const offline = await watch(page, 'offline', 8);
  const attempts = offline.map(r => Number(/连续失败 (\d+) 次/.exec(r.hint)?.[1] ?? 0));
  expect(attempts[attempts.length - 1], '断网期间一直在重连').toBeGreaterThan(attempts[0]);
  expect(offline.every(r => r.kind === 'lost' && r.taskId === taskId)).toBe(true);
  const progressed = await taskView(request, taskId);
  expect(progressed.networkRequests, '断网期间后端照样推进').toBeGreaterThan(offlineFrom.networkRequests);
  record('offline', { attempts, screenshot: await shot(page, '06-offline.png') });
  const online = Date.now();
  await context.setOffline(false);
  await expect(liveKind(page)).not.toHaveAttribute('data-kind', 'lost', { timeout: 5000 });
  const back = await read(page);
  expect(back.requests!).toBeGreaterThanOrEqual(progressed.networkRequests);
  record('online', { seconds: (Date.now() - online) / 1000, reading: back });

  // 6. 在线但服务端不可达（连接被拒）：退避重连；恢复后自己接上。
  const refuse = (route: import('@playwright/test').Route) => route.abort('connectionrefused');
  await page.route('**/api/v2/checkups/**', refuse);
  await expect(liveKind(page)).toHaveAttribute('data-kind', 'lost', { timeout: 5000 });
  await watch(page, 'unreachable', 5);
  await page.unroute('**/api/v2/checkups/**', refuse);
  const reachable = Date.now();
  await expect(liveKind(page)).not.toHaveAttribute('data-kind', 'lost', { timeout: 15000 });
  record('unreachable', { recoverSeconds: (Date.now() - reachable) / 1000 });

  // 7. 切算法：回来还是同一个任务，已用时接着走。（旧版成圈页面已移除，只剩算法切换。）
  await heldAt(request, 'assessment', 400000);
  const leaving = await read(page);
  await switchTo(page, 'OSM＋百度');
  await expect(page.getByTestId('checkup-task-id')).toHaveCount(0);
  await page.waitForTimeout(5000);
  await switchTo(page, '百度边界搜索（E8.2）');
  await expect(page.getByTestId('checkup-task-id')).toHaveText(taskId);
  const returned = await read(page);
  expect(returned.elapsed! - leaving.elapsed!, '切出去的时间也算在已用时里').toBeGreaterThanOrEqual(4);
  expect(returned.stage).toBe('accessibility');
  record('algorithmSwitch', { leaving, returned });

  // 8. 刷新：同一个任务，已用时不归零，评估计数接着显示。
  await page.reload();
  await expectRealBasemap(page);
  await expect(page.getByTestId('checkup-task-id')).toHaveText(taskId, { timeout: 30000 });
  const reloaded = (await watch(page, 'reloaded', 3))[0];
  expect(reloaded.elapsed!).toBeGreaterThanOrEqual(returned.elapsed!);
  expect(reloaded.stage).toBe('accessibility');
  expect(reloaded.step).toBe('category');
  record('reload', { reloaded, screenshot: await shot(page, '07-reloaded.png') });

  // 9. 放行到底：完成，地图与报告都是这个任务的。
  await control(request, '/control/reset', {});
  await settled(request, taskId, ['completed']);
  await expect(page.getByTestId('checkup-phase')).toHaveAttribute('data-phase', 'completed', { timeout: 30000 });
  await expect(liveKind(page)).toHaveCount(0);
  const finalView = await taskView(request, taskId);
  const map = await expectResult(page, taskId, finalView.revision, A, B);
  record('completed', { revision: finalView.revision, networkRequests: finalView.networkRequests, map,
    screenshot: await shot(page, '08-completed.png') });

  const after = await backend(request);
  expect((after.requests.create ?? 0) - (before.requests.create ?? 0), '后端只收到一次创建').toBe(1);
  expect(ledger.creates, '页面只发了一次创建').toBe(1);
  expect(ledger.cancels).toBe(0);
  expect(after.tasks.length - before.tasks.length).toBe(1);
  expect(after.external.attempts).toBe(0);
  expect(ledger.problems).toEqual([]);
  record('e82Ledger', ledger);
});

test('创建的回答超时与取消中恢复：不重提；取消后刷新、切算法仍是"正在取消"，放行后停在"已取消"', async ({ page, request }) => {
  const ledger: Ledger = { creates: 0, cancels: 0, problems: [] };
  const before = await backend(request);
  await open(page, 'e82', ledger);

  // 创建的回答压过 15 秒的请求超时：页面按请求标识查到任务，不再提交第二次。
  await arm(request, 'create');
  await arm(request, 'places', 1);
  await startAt(page, B);
  await heldAt(request, 'create');
  const created = (await backend(request)).tasks.filter(task => !before.tasks.some(old => old.taskId === task.taskId));
  expect(created).toHaveLength(1);
  const taskId = created[0].taskId;
  await expect(page.getByTestId('checkup-task-id')).toHaveText(taskId, { timeout: 30000 });
  await release(request, 'create');
  await heldAt(request, 'places');
  await expect(liveKind(page)).toHaveAttribute('data-kind', 'working');
  record('createTimeout', { reading: await read(page) });

  // 取消：一次检索请求停在半路。发出的请求收不回，后端要等它回来才走到下一个检查点 ——
  // 这期间是"正在取消"，已用时照走。
  await page.getByRole('button', { name: '取消任务' }).click();
  await expect(liveKind(page)).toHaveAttribute('data-kind', 'cancelling');
  await expect.poll(async () => (await taskView(request, taskId)).status).toBe('cancelling');
  await watch(page, 'cancelling', 3);
  record('cancelling', { screenshot: await shot(page, '09-cancelling.png') });

  // 取消中刷新、切算法：仍是同一个任务、仍在取消；不重提，也不把取消当成"没有任务"。
  await page.reload();
  await expectRealBasemap(page);
  await expect(page.getByTestId('checkup-task-id')).toHaveText(taskId, { timeout: 30000 });
  await expect(liveKind(page)).toHaveAttribute('data-kind', 'cancelling');
  await switchTo(page, 'OSM＋百度');
  await page.waitForTimeout(2000);
  await switchTo(page, '百度边界搜索（E8.2）');
  await expect(page.getByTestId('checkup-task-id')).toHaveText(taskId);
  await expect(liveKind(page)).toHaveAttribute('data-kind', 'cancelling');
  await watch(page, 'cancelling-restored', 3);

  await release(request, 'places');
  await settled(request, taskId, ['cancelled'], 60000);
  await expect(page.getByTestId('checkup-phase')).toHaveAttribute('data-phase', 'cancelled', { timeout: 15000 });
  await expect(page.getByText('任务已取消')).toBeVisible();
  const section = page.getByTestId('checkup-map-section');
  await expect(section).toHaveAttribute('data-task-id', taskId);
  record('cancelled', { screenshot: await shot(page, '10-cancelled.png') });

  const after = await backend(request);
  expect((after.requests.create ?? 0) - (before.requests.create ?? 0), '后端只收到一次创建').toBe(1);
  expect(ledger.creates, '页面只发了一次创建').toBe(1);
  // 取消是幂等的：点一次，刷新后按句柄补发一次（这一次在后端是同一个取消）。
  expect(ledger.cancels).toBeGreaterThanOrEqual(1);
  expect(ledger.cancels).toBeLessThanOrEqual(2);
  expect(after.tasks.find(task => task.taskId === taskId)?.status).toBe('cancelled');
  expect(ledger.problems).toEqual([]);
  record('cancelLedger', ledger);
});

test('两个引擎、两个中心：OSM＋百度的任务排在 E8.2 之后；不报计数的一步（取用路网）停住超过 90 秒仍是"正在计算或等待"；地图、修订与报告各归各的', async ({ page, request }) => {
  const ledger: Ledger = { creates: 0, cancels: 0, problems: [] };
  const before = await backend(request);
  const graphPasses = before.passes.graph ?? 0;
  await open(page, 'e82', ledger);

  // E8.2 @ A 停在设施检索。
  await arm(request, 'places', 1);
  await startAt(page, A);
  await heldAt(request, 'places');
  const e82 = (await backend(request)).tasks.filter(task => !before.tasks.some(old => old.taskId === task.taskId));
  expect(e82).toHaveLength(1);
  const e82Id = e82[0].taskId;

  // 切到 OSM＋百度，在 B 提交：排队，已排队的秒数在走。
  await switchTo(page, 'OSM＋百度');
  await expectRealBasemap(page);
  await startAt(page, B);
  await expect(liveKind(page)).toHaveAttribute('data-kind', 'queued', { timeout: 15000 });
  const hybridId = (await page.getByTestId('checkup-task-id').textContent())!;
  expect(hybridId).not.toBe(e82Id);
  const queued1 = await read(page);
  await page.waitForTimeout(3000);
  const queued2 = await read(page);
  expect(queued2.elapsedText).toContain('尚未开始（已排队');
  expect(queued2.elapsedText).not.toBe(queued1.elapsedText);
  record('queued', { reading: queued2, screenshot: await shot(page, '11-queued.png') });

  // E8.2 的评估还要经过路网一次（每个 E8.2 任务一次）；放它过去，停住再往后的那一次：Hybrid 成圈之前。
  await arm(request, 'graph', 1);
  await release(request, 'places');
  await settled(request, e82Id, ['completed']);
  await heldAt(request, 'graph');
  expect((await backend(request)).passes.graph).toBe(graphPasses + 2);

  // Hybrid 停在"载入 OSM 步行路网"的入口：路网早已载好，停点拦在取用之前，没有边可数 ——
  // 这一步不报中间计数，过了 90 秒仍是"正在计算或等待"。（真正首次载入时按边计数，见文档。）
  await expect(page.getByTestId('checkup-step')).toHaveAttribute('data-step', 'graph', { timeout: 15000 });
  const quiet = await read(page);
  expect(quiet.kind).toBe('working');
  expect(quiet.count).toBeNull();
  expect(quiet.hint).toContain('最近进展');
  const quietFrom = Date.now();

  // 等待的同时切回 E8.2：它已完成，地图与报告都是 A 的那一个。
  await switchTo(page, '百度边界搜索（E8.2）');
  const e82View = await taskView(request, e82Id);
  const e82Map = await expectResult(page, e82Id, e82View.revision, A, B);
  record('e82WhileHybridHeld', { taskId: e82Id, revision: e82View.revision, map: e82Map,
    screenshot: await shot(page, '12-e82-result.png') });
  await closeReport(page);
  await switchTo(page, 'OSM＋百度');
  await expect(page.getByTestId('checkup-task-id')).toHaveText(hybridId);
  const remaining = STALL_SECONDS + 5 - (Date.now() - quietFrom) / 1000;
  if (remaining > 0) await page.waitForTimeout(remaining * 1000);
  const longQuiet = await read(page);
  expect(longQuiet.step).toBe('graph');
  expect(longQuiet.kind, '不报计数的步骤另有更长的停滞阈值').toBe('working');
  expect(longQuiet.activity!).toBeGreaterThan(STALL_SECONDS);
  record('quietStep', { reading: longQuiet, screenshot: await shot(page, '13-quiet-graph.png') });

  await release(request, 'graph');
  await settled(request, hybridId, ['completed']);
  await expect(page.getByTestId('checkup-phase')).toHaveAttribute('data-phase', 'completed', { timeout: 30000 });
  const hybridView = await taskView(request, hybridId);
  const hybridMap = await expectResult(page, hybridId, hybridView.revision, B, A);
  record('hybrid', { taskId: hybridId, revision: hybridView.revision, map: hybridMap,
    screenshot: await shot(page, '14-hybrid-result.png') });
  await closeReport(page);

  // 来回切换、刷新：各归各的。
  for (const round of [1, 2]) {
    await switchTo(page, '百度边界搜索（E8.2）');
    await expectResult(page, e82Id, e82View.revision, A, B);
    await closeReport(page);
    await switchTo(page, 'OSM＋百度');
    await expectResult(page, hybridId, hybridView.revision, B, A);
    await closeReport(page);
    if (round === 1) { await page.reload(); await expectRealBasemap(page); }
  }

  const after = await backend(request);
  const mine = after.tasks.filter(task => !before.tasks.some(old => old.taskId === task.taskId));
  expect(mine.map(task => [task.engine, task.center?.lng, task.status]).sort()).toEqual(
    [['baidu_e82', A.lng, 'completed'], ['osm_hybrid', B.lng, 'completed']]);
  expect((after.requests.create ?? 0) - (before.requests.create ?? 0), '后端只收到两次创建').toBe(2);
  expect(ledger.creates, '页面只发了两次创建').toBe(2);
  expect(after.external.attempts).toBe(0);
  expect(ledger.problems).toEqual([]);
  record('twoEnginesLedger', ledger);
});
