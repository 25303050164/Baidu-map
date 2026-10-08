/**
 * 客户端测两件事：**请求发对了没有**（路径、版本、请求体），以及**回来的东西对不上时
 * 会不会当场翻脸**。
 *
 * 后半是这里的重点。错配的任务 ID、错配的修订号、结构不合法的响应，如果被当成正常
 * 数据放过去，界面会安静地显示另一件事 —— 这类错没有异常、没有日志，只有看到数的人才
 * 会发现，而那时已经没人记得它本来是错的。
 */
import { describe, expect, it, vi } from 'vitest';
import { CheckupError, DETAIL_BUDGET_EXHAUSTED, createCheckupService, isNotFound } from './client';
import { capabilities, extensionDocument, extensionView, layer, route, snapshot, task } from './fixtures';

type Reply = { body?: unknown; status?: number; raw?: unknown };

function service(replies: Reply[], base = '') {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const fetcher = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    calls.push({ url: String(url), init: init ?? {} });
    const reply = replies.shift();
    if (!reply) throw new Error('这一轮没有安排回答了');
    const status = reply.status ?? 200;
    return {
      ok: status >= 200 && status < 300, status,
      json: async () => (reply.raw !== undefined ? reply.raw : reply.body),
    } as unknown as Response;
  });
  return { api: createCheckupService(base, fetcher as unknown as typeof fetch), calls, fetcher };
}

const request = { schemaVersion: 'checkup-v1' as const, clientRequestId: 'request-1',
  engine: 'baidu_e82', center: { lng: 116.404, lat: 39.915 }, coordinateSystem: 'bd09ll' as const };

describe('checkup client', () => {
  it('posts to the v2 root and strips one trailing slash from the base', async () => {
    const { api, calls } = service([{ body: task({ status: 'queued', stage: null }) }], '/');
    await api.create(request);
    expect(calls[0].url).toBe('/api/v2/checkups');
    expect(calls[0].init.method).toBe('POST');
    expect(JSON.parse(String(calls[0].init.body))).toEqual(request);
  });

  it('refuses a task minted for a different request', async () => {
    // 错配的 ID 会让轮询去盯别人的任务：看得见的错总比看不见的好。
    const { api } = service([{ body: task({ clientRequestId: 'request-9' }) }]);
    await expect(api.create(request)).rejects.toMatchObject({ code: 'mismatched_request' });
  });

  it('reads the capability list from the v2 root, not from the checkup root', async () => {
    // 能力表是这一版接口的公共部分：挂到 `/checkups` 下面会变成 404，而 404 的应用
    // 直接表现为"工作台里一个引擎都选不了"。
    const { api, calls } = service([{ body: capabilities() }]);
    const view = await api.capabilities();
    expect(calls[0].url).toBe('/api/v2/capabilities');
    expect(view.engines.map(engine => engine.engineId)).toEqual(['baidu_e82', 'osm_hybrid']);
  });

  it('refuses a capability list it cannot choose from', async () => {
    const broken = service([{ body: capabilities({ engines: [] }) }]);
    await expect(broken.api.capabilities())
      .rejects.toMatchObject({ code: 'invalid_response' });
  });

  it('refuses another task\'s status or a different revision', async () => {
    const status = service([{ body: task({ taskId: 'task-2' }) }]);
    await expect(status.api.status('task-1')).rejects.toMatchObject({ code: 'mismatched_task' });

    const stale = service([{ body: snapshot({ revision: 3 }) }]);
    await expect(stale.api.result('task-1', 5)).rejects.toMatchObject({ code: 'mismatched_revision' });
  });

  it('refuses a layer that came back at another revision', async () => {
    const { api } = service([{ body: layer({ revision: 4 }) }]);
    await expect(api.layer('task-1', 'service_gaps', 5))
      .rejects.toMatchObject({ code: 'mismatched_revision' });
    // 图层 ID 对不上也一样：取的是服务覆盖，画上去的是别的东西。
    const wrong = service([{ body: layer({ layerId: 'heatmap' }) }]);
    await expect(wrong.api.layer('task-1', 'service_gaps', 5))
      .rejects.toMatchObject({ code: 'invalid_response' });
  });

  it('refuses a structurally invalid body instead of passing it on', async () => {
    const { api } = service([{ body: { ...snapshot(), serviceGaps: { status: 'failed' } } }]);
    const error = await api.result('task-1', 5).catch((thrown: unknown) => thrown);
    expect(error).toBeInstanceOf(CheckupError);
    expect((error as CheckupError).code).toBe('invalid_response');
  });

  it('keeps the backend code and message, and only falls back when there is none', async () => {
    const coded = service([{ status: 429, body: { code: DETAIL_BUDGET_EXHAUSTED,
      message: '本任务的详情路线预算已用尽' } }]);
    const error = await coded.api.route('task-1', 'facility-1').catch((thrown: unknown) => thrown);
    expect(error).toMatchObject({ status: 429, code: DETAIL_BUDGET_EXHAUSTED,
      message: '本任务的详情路线预算已用尽' });

    // 后端没说原因时给出兜底文案 —— 界面永远不该显示空白或 "429"。
    const bare = service([{ status: 503 }]);
    const shutdown = await bare.api.status('task-1').catch((thrown: unknown) => thrown);
    expect((shutdown as CheckupError).code).toBe('http_503');
    expect((shutdown as CheckupError).message).toContain('不可用');
    expect(isNotFound(shutdown)).toBe(false);
  });

  it('treats a missing task as missing, not as a failure', async () => {
    // 404 是重试流程里的正常分支（"服务端本来就没建成"），必须能被单独认出来。
    const { api } = service([{ status: 404, body: { code: 'checkup_task_not_found' } }]);
    const error = await api.status('task-1').catch((thrown: unknown) => thrown);
    expect(isNotFound(error)).toBe(true);
  });

  it('reports a network failure as a connection problem, but re-throws its own abort', async () => {
    const fetcher = vi.fn(async () => { throw new TypeError('fetch failed'); });
    const offline = createCheckupService('', fetcher as unknown as typeof fetch);
    await expect(offline.status('task-1')).rejects.toMatchObject({ status: 0, code: 'network' });

    // 调用方取消不是故障：把它原样抛出去，重试逻辑才分得清"我取消的"和"断网了"。
    const abort = new AbortController();
    abort.abort();
    const cancelling = createCheckupService('', fetcher as unknown as typeof fetch);
    await expect(cancelling.status('task-1', abort.signal)).rejects.toMatchObject({ name: 'AbortError' });
  });

  it('accepts an honest route and refuses one whose verdict has no distance', async () => {
    const good = service([{ body: route() }]);
    await expect(good.api.route('task-1', 'facility-1')).resolves.toMatchObject({ routeDistanceM: 805.05 });

    const bad = service([{ body: route({ routeDistanceM: null }) }]);
    await expect(bad.api.route('task-1', 'facility-1'))
      .rejects.toMatchObject({ code: 'invalid_response' });
  });
});

describe('on-demand facility extensions', () => {
  it('posts the extension under its own task and keeps the request id', async () => {
    const { api, calls } = service([{ body: extensionView({ status: 'queued', stage: null }) }]);
    const body = { schemaVersion: 'checkup-v1' as const, clientRequestId: 'ext-request-1',
      categories: ['dining', 'leisure'] as const };
    await api.extensionCreate('task-1', { ...body, categories: [...body.categories] });
    expect(calls[0].url).toBe('/api/v2/checkups/task-1/facility-extensions');
    expect(calls[0].init.method).toBe('POST');
    expect(JSON.parse(String(calls[0].init.body))).toEqual(body);
  });

  it('refuses an extension view minted for a different task or request', async () => {
    // 补查的标识决定后面去轮询谁，错配就是把别人的补查读成自己的。
    const otherRequest = service([{ body: extensionView({ clientRequestId: 'ext-request-9' }) }]);
    await expect(otherRequest.api.extensionCreate('task-1',
      { schemaVersion: 'checkup-v1', clientRequestId: 'ext-request-1', categories: ['dining'] }))
      .rejects.toMatchObject({ code: 'mismatched_request' });
    const otherTask = service([{ body: extensionView({ taskId: 'task-9' }) }]);
    await expect(otherTask.api.extensionStatus('task-1', 'extension-1'))
      .rejects.toMatchObject({ code: 'mismatched_task' });
  });

  it('refuses a list that carries an extension of another task', async () => {
    // 列表里混进别的任务的记录，等于把一次越权读取当成一次成功读取。
    const { api } = service([{ body: [extensionView(), extensionView({ taskId: 'task-9' })] }]);
    await expect(api.extensionList('task-1')).rejects.toMatchObject({ code: 'mismatched_task' });
  });

  it('reads the result from the extension path and checks both identifiers', async () => {
    const { api, calls } = service([{ body: extensionDocument() }]);
    const document_ = await api.extensionResult('task-1', 'extension-1');
    expect(calls[0].url).toBe('/api/v2/checkups/task-1/facility-extensions/extension-1/result');
    // 数量在结果文档的 group 里（它是那次检索自己的记录），视图上的那一份是摘要。
    expect(document_.group?.countsByCategory).toEqual({ dining: 1, leisure: 2 });
    const mismatched = service([{ body: extensionDocument({ extensionId: 'extension-9' }) }]);
    await expect(mismatched.api.extensionResult('task-1', 'extension-1'))
      .rejects.toMatchObject({ code: 'mismatched_revision' });
  });

  it('refuses a result that says "no facilities" without saying why', async () => {
    // 空清单与空目录是两件事：没有原因的空结果会被读成"这片区域没有这类设施"。
    const { api } = service([{ body: extensionDocument({ group: null, issues: [] }) }]);
    await expect(api.extensionResult('task-1', 'extension-1'))
      .rejects.toMatchObject({ code: 'invalid_response' });
  });

  it('cancels through the extension path', async () => {
    const { api, calls } = service([{ body: extensionView({ status: 'cancelled' }) }]);
    const view = await api.extensionCancel('task-1', 'extension-1');
    expect(calls[0].url).toBe('/api/v2/checkups/task-1/facility-extensions/extension-1/cancel');
    expect(view.status).toBe('cancelled');
  });
});
