/**
 * v2 体检控制器：提交、轮询、取修订与图层、取消，以及"迟到的响应不许上屏"。
 *
 * 与旧分析控制器的分工一致，但有两处必须按 v2 的语义重写：
 *
 * - **修订是内容，不是序号。** 一份修订发布后永不改写，所以取回来的修订要与任务视图
 *   报告的修订号对得上；对不上就不渲染，而不是"用最新的覆盖一下"。
 * - **一个任务多条图层。** 图层按 `图层:修订` 缓存，同一版不重取；修订变了就整组丢掉，
 *   否则图上画的是上一版的灰区、面板里写的是这一版的面积。
 *
 * 每个异步步骤结束回写状态之前都要问一次"这一轮还算数吗"（``current``）：取消、重新
 * 提交、切中心点都会让旧的一轮作废，而它可能正好在这时返回。
 */
import type { CheckupLayer, CheckupTaskView } from './contract';
import type { LayerId } from './validate';
import { CheckupError, DETAIL_BUDGET_EXHAUSTED, isNotFound, type CheckupService } from './client';
import type { CheckupInput, CheckupState } from './types';
import { isCheckupBusy, isTerminal } from './types';

type Run = {
  input: CheckupInput;
  id?: string;
  abort: AbortController;
  revision: number;
  expired?: boolean;
  /** 创建请求发出去了但没读到任务 ID：服务端可能已经建了任务。 */
  creationFailed?: boolean;
};

function pause(signal: AbortSignal, ms = 1000) {
  return new Promise<void>(resolve => {
    const finish = () => { clearTimeout(timer); signal.removeEventListener('abort', finish); resolve(); };
    const timer = setTimeout(finish, ms);
    signal.addEventListener('abort', finish, { once: true });
    if (signal.aborted) finish();
  });
}

export class CheckupController {
  state: CheckupState = { phase: 'idle' };
  private run?: Run;
  private revision = 0;
  private pendingStart?: symbol;
  private pendingCancels = new Set<string>();

  constructor(private api: CheckupService, private publish: (state: CheckupState) => void) {}

  private set(state: CheckupState) { this.state = state; this.publish(state); }
  private current(run: Run) { return run.revision === this.revision; }

  async start(input: Omit<CheckupInput, 'clientRequestId'>) {
    if (this.pendingStart || isCheckupBusy(this.state)) return;
    const starting = this.pendingStart = Symbol();
    try {
      const resetting = this.run ? this.reset() : undefined;
      const revision = this.revision;
      if (resetting) await resetting;
      if (this.pendingCancels.size && !await this.clearPending()) return;
      if (revision !== this.revision) return;
      const run: Run = {
        input: { ...input, clientRequestId: crypto.randomUUID() },
        abort: new AbortController(), revision: ++this.revision,
      };
      this.run = run;
      this.pendingStart = undefined;
      await this.execute(run);
    } finally { if (this.pendingStart === starting) this.pendingStart = undefined; }
  }

  async retry() {
    if (this.pendingStart || isCheckupBusy(this.state)) return;
    const run = this.run;
    if (!run) { this.set({ phase: 'idle' }); return; }
    if (run.expired || this.state.task?.status === 'failed' || this.state.task?.status === 'cancelled') {
      await this.start(run.input);
      return;
    }
    run.abort = new AbortController();
    await this.execute(run);
  }

  private async execute(run: Run) {
    this.set({ phase: run.id ? 'running' : 'submitting', task: this.state.task });
    try {
      if (!run.id) {
        // POST 不跟着中心点变化一起中止：它回来的任务 ID 是唯一能取消服务端工作的东西。
        const created = await this.api.create(toRequest(run.input));
        run.id = created.taskId;
        if (!this.current(run)) { await this.abandon(run.id); return; }
        this.set({ phase: created.status === 'queued' ? 'queued' : 'running', task: created });
      }
      await this.watch(run);
    } catch (error) {
      if (!run.id && !(error instanceof CheckupError && [404, 409, 422, 503].includes(error.status))) {
        run.creationFailed = true;
        if (!this.current(run)) { try { await this.abandonRequest(run.input.clientRequestId); } catch { /* 留给重试 */ } }
      }
      if (run.id && isNotFound(error)) run.expired = true;
      if (this.current(run) && !run.abort.signal.aborted) {
        this.set({ ...this.state, phase: 'error',
          error: error instanceof Error ? error.message : '体检失败，请重试' });
      }
    }
  }

  private async accept(run: Run, task: CheckupTaskView): Promise<boolean> {
    if (!this.current(run) || run.abort.signal.aborted) return true;
    if (task.status === 'completed') {
      this.set({ ...this.state, phase: 'fetching', task });
      // 取的是任务视图报告的那一版修订，而不是"最新的一版"：两者不一致时，说明
      // 服务端还有一版没被这次轮询看到，取最新会让报告和任务状态描述不同的结论。
      const snapshot = await this.api.result(run.id!, task.revision, run.abort.signal);
      if (!this.current(run) || run.abort.signal.aborted) return true;
      if (snapshot.revision !== task.revision) {
        throw new CheckupError('体检修订与任务状态不符，请检查服务版本', 0, 'mismatched_revision');
      }
      this.set({ phase: 'completed', task, snapshot, layers: this.state.layers });
      return true;
    }
    if (task.status === 'cancelled') { this.set({ ...this.state, phase: 'cancelled', task }); return true; }
    if (task.status === 'failed') {
      this.set({ ...this.state, phase: 'error', task,
        error: task.error ? `体检执行失败：${task.error}` : '体检执行失败，请检查后端配置后重试' });
      return true;
    }
    this.set({ ...this.state, task, phase: task.status === 'queued' ? 'queued'
      : task.status === 'cancelling' ? 'cancelling' : 'running' });
    return false;
  }

  private async watch(run: Run) {
    while (this.current(run) && !run.abort.signal.aborted) {
      if (await this.accept(run, await this.api.status(run.id!, run.abort.signal))) return;
      await pause(run.abort.signal);
    }
  }

  /**
   * 取一层图层。缓存键含修订号：同一版不重取，换版不沿用。
   *
   * 后端对"这一层还不存在"和"这一层这次没有内容"给的是两种回答（前者 409 带名字，
   * 后者是一份空集合），这里不把它们合并：把 409 显示成空图层，读者会以为那一层
   * 已经画过了。
   */
  async layer(layerId: LayerId): Promise<CheckupLayer | undefined> {
    const task = this.state.task;
    if (!task) throw new CheckupError('任务尚未创建，无法取图层', 0, 'no_task');
    const cached = this.state.layers?.[layerId];
    if (cached && cached.revision === task.revision) return cached;
    const layer = await this.api.layer(task.taskId, layerId, task.revision);
    // 取的过程里任务换了（取消后重开、或者修订又前进了一版）：这一张属于上一版，
    // 丢掉而不是画上去 —— 图上画着旧灰区、面板写着新面积是最难发现的一类错。
    const now = this.state.task;
    if (!now || now.taskId !== task.taskId || now.revision !== task.revision) return undefined;
    this.set({ ...this.state, layers: { ...this.state.layers, [layerId]: layer } });
    return layer;
  }

  /** 点击某个设施的详情路线。它不发布修订，所以结果只留在 `route` 里。 */
  async detail(facilityId: string) {
    const task = this.state.task;
    if (!task) return;
    this.set({ ...this.state, route: undefined, routeError: undefined });
    try {
      const route = await this.api.route(task.taskId, facilityId);
      if (this.state.task?.taskId !== task.taskId) return;
      this.set({ ...this.state, route });
    } catch (error) {
      if (this.state.task?.taskId !== task.taskId) return;
      const message = error instanceof CheckupError ? error.message : '未能取到这条设施的步行路线';
      this.set({ ...this.state, routeError: message,
        ...(error instanceof CheckupError && error.code === DETAIL_BUDGET_EXHAUSTED ? { route: undefined } : {}) });
    }
  }

  async cancel() {
    const old = this.run;
    if (!old || this.state.phase === 'cancelling') return;
    if (!old.id || this.state.task?.status === 'completed') {
      // 顺序要紧：`reset()` 一进函数体就把修订号推进了，所以要在**调用它之后**再读。
      // 先读后调的话，这里拿到的恒是被推进前的旧值，整个判断恒为假 —— 取消会安静地
      // 退回"空闲"，看起来像什么都没发生。
      const resetting = this.reset();
      const revision = this.revision;
      await resetting;
      if (revision === this.revision && this.state.phase === 'idle') this.set({ phase: 'cancelled' });
      return;
    }
    old.abort.abort();
    const run: Run = { ...old, abort: new AbortController(), revision: ++this.revision };
    this.run = run;
    this.set({ ...this.state, phase: 'cancelling' });
    try {
      const task = await this.api.cancel(run.id!);
      if (!await this.accept(run, task)) await this.watch(run);
    } catch (error) {
      if (isNotFound(error)) run.expired = true;
      if (this.current(run)) this.set({ ...this.state, phase: 'error', error: '未能确认取消，请再次取消或检查后端状态' });
    }
  }

  async reset() {
    const old = this.run;
    const terminal = isTerminal(this.state.task);
    const revision = ++this.revision;
    this.run = undefined;
    old?.abort.abort();
    this.set({ phase: 'idle' });
    const target = old?.id;
    if (target && !terminal) {
      try { await this.abandon(target); }
      catch { if (revision === this.revision) this.set({ phase: 'error', error: '旧任务取消未获确认，后端可能仍在运行，请检查服务后重试' }); }
    } else if (old?.creationFailed) {
      try { await this.abandonRequest(old.input.clientRequestId); }
      catch { if (revision === this.revision) this.set({ phase: 'error', error: '旧任务取消未获确认，后端可能仍在运行，请检查服务后重试' }); }
    }
  }

  /**
   * 创建请求失败、任务 ID 未知时收拾残局：v2 没有"按请求 ID 取消"，只有"按请求 ID
   * 查任务"。所以先查回来拿 ID，再取消。查不到（404）说明服务端本来就没建成，那就
   * 没有什么要收拾的。
   */
  private async abandonRequest(clientRequestId: string) {
    let task: CheckupTaskView;
    try { task = await this.api.byRequest(clientRequestId); }
    catch (error) {
      // 查不到就是"服务端本来就没有这个任务"，正是想要的结果，不是失败。
      if (isNotFound(error)) return;
      throw error;
    }
    await this.abandon(task.taskId);
  }

  private async abandon(id: string) {
    this.pendingCancels.add(id);
    try { await this.api.cancel(id); }
    catch (error) { if (!isNotFound(error)) throw error; }
    this.pendingCancels.delete(id);
  }

  private async clearPending() {
    try {
      for (const id of this.pendingCancels) await this.abandon(id);
      return true;
    } catch {
      this.set({ phase: 'error', error: '旧任务取消未获确认，后端可能仍在运行，请检查服务后重试' });
      return false;
    }
  }

  dispose() { this.publish = () => {}; void this.reset(); }
}

/** 输入 → 请求体。`isochrone` 只在真的给了预算时才带上，让后端用它自己的默认档。 */
function toRequest(input: CheckupInput) {
  return {
    schemaVersion: 'checkup-v1' as const, clientRequestId: input.clientRequestId,
    engine: input.engine, center: { lng: input.center.lng, lat: input.center.lat },
    coordinateSystem: 'bd09ll' as const,
    ...(input.budget === undefined ? {} : { isochrone: { budget: input.budget } }),
  };
}
