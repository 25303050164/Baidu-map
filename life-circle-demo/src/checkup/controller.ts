/**
 * v2 体检控制器：提交、轮询、取修订与图层、取消、恢复，以及"迟到的响应不许上屏"。
 *
 * 与旧分析控制器的分工一致，但有几处必须按 v2 的语义重写：
 *
 * - **修订是内容，不是序号。** 一份修订发布后永不改写，所以取回来的修订要与任务视图
 *   报告的修订号对得上；对不上就不渲染，而不是"用最新的覆盖一下"。
 * - **一个任务多条图层。** 图层按 `图层:修订` 缓存，同一版不重取；修订变了就整组丢掉，
 *   否则图上画的是上一版的灰区、面板里写的是这一版的面积。
 * - **任务不属于页面。** 控制器不随组件卸载而取消任务：卸载只是不再有人看，任务照跑。
 *   能让服务端停下的只有用户点的"取消"。任务句柄（请求标识、任务 ID、是否已请求取消）
 *   在发出创建请求**之前**就交给 `onHandle` 存起来，刷新后凭它向后端核对并接着看。
 * - **断网不是失败。** 轮询、取结果、取消遇到连接失败时退避重试并标出"连接中断"，
 *   网络回来就接上；只有服务端明确的回答（404、失败、修订不符）才会结束这一轮。
 *
 * 每个异步步骤结束回写状态之前都要问一次"这一轮还算数吗"（``current``）：取消、清除
 * 都会让旧的一轮作废，而它可能正好在这时返回。
 */
import type { CheckupLayer, CheckupTaskView } from './contract';
import type { LayerId } from './validate';
import { CheckupError, DETAIL_BUDGET_EXHAUSTED, isNotFound, type CheckupService } from './client';
import type { CheckupHandle, CheckupInput, CheckupState } from './types';
import { isCheckupBusy, isTerminal } from './types';

type Run = {
  input: CheckupInput;
  id?: string;
  abort: AbortController;
  revision: number;
  /** 服务端已经不认识这个任务 ID（404）：结果无法恢复，只能重新体检。 */
  expired?: boolean;
  /** 服务端明确拒绝创建（422、请求标识冲突……）：没有任务，重试要换新的请求标识。 */
  refused?: boolean;
  /** 按请求标识查过，服务端没有这个任务：创建请求没送达，等用户决定是否重提。 */
  unconfirmed?: boolean;
  /** 创建请求还在路上：取消只能先记下来，等拿到任务 ID 再发。 */
  creating?: boolean;
  cancelRequested?: boolean;
};

/** 连接失败时的退避：1、2、4、8 秒，之后每 10 秒一次，直到网络回来或用户离开这一轮。 */
export const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000, 10_000];
const POLL_MS = 1000;

/** 值得等一等的失败：连不上、超时、网关/上游暂时不可用。其余都是服务端的明确回答。 */
export function isTransient(error: unknown): boolean {
  return error instanceof CheckupError
    && ((error.status === 0 && error.code === 'network') || [502, 503, 504].includes(error.status));
}

const EXPIRED_MESSAGE = '服务端已找不到这个体检任务（数据目录被清理或更换了后端），结果无法恢复；可重新体检。';
const UNCONFIRMED_MESSAGE = '创建请求没有送达服务端：按请求标识查不到任务。为避免重复消耗额度，页面没有自动重新提交；'
  + '点"重新提交"会沿用同一请求标识，服务端若已建过任务会直接认出来，不会重复创建。';

export class CheckupController {
  state: CheckupState = { phase: 'idle' };
  private run?: Run;
  private revision = 0;
  private pendingStart = false;
  private wake?: () => void;

  constructor(
    private api: CheckupService,
    private publish: (state: CheckupState) => void,
    private onHandle: (handle: CheckupHandle | undefined) => void = () => {},
  ) {}

  private set(state: CheckupState) { this.state = state; this.publish(state); }
  private patch(state: Partial<CheckupState>) { this.set({ ...this.state, ...state }); }
  private current(run: Run) { return run.revision === this.revision && !run.abort.signal.aborted; }

  private save(run: Run) {
    this.onHandle({
      input: run.input, savedAt: Date.now(),
      ...(run.id ? { taskId: run.id } : {}),
      ...(run.cancelRequested ? { cancelRequested: true } : {}),
    });
  }

  /**
   * 还有一个没结束的任务（或者一次没确认的创建）：这时不许再开新的。
   *
   * 旧版在这里是"先取消旧的再开新的"，那等于把换中心点当成了取消；现在只有用户点
   * "取消"才会停住服务端的任务，所以新任务只能等旧的结束。
   */
  get hasLiveTask(): boolean {
    const run = this.run;
    if (!run || run.refused || run.expired) return false;
    return !run.id || !isTerminal(this.state.task);
  }

  async start(input: Omit<CheckupInput, 'clientRequestId'>) {
    if (this.pendingStart || isCheckupBusy(this.state) || this.hasLiveTask) return;
    this.pendingStart = true;
    try {
      this.run?.abort.abort();
      const run: Run = {
        input: { center: input.center, engine: input.engine,
          ...(input.budget === undefined ? {} : { budget: input.budget }),
          clientRequestId: crypto.randomUUID() },
        abort: new AbortController(), revision: ++this.revision,
      };
      this.run = run;
      this.set({ phase: 'submitting', input: run.input });
      // 先存句柄再发请求：请求发出去之后页面才被关掉，刷新回来也能按请求标识找回任务。
      this.save(run);
      this.pendingStart = false;
      await this.execute(run);
    } finally { this.pendingStart = false; }
  }

  /**
   * 页面刷新或重新挂载后，凭存下来的句柄接着看。
   *
   * 有任务 ID 就直接问状态；没有（创建的响应丢了）就按请求标识查 —— 这是只读的，
   * 不会替用户重新提交。句柄里记着"已请求取消"的，再把取消发一遍（取消是幂等的）。
   */
  async resume(handle: CheckupHandle) {
    if (this.run) return;
    const run: Run = {
      input: handle.input, id: handle.taskId, cancelRequested: handle.cancelRequested,
      abort: new AbortController(), revision: ++this.revision,
    };
    this.run = run;
    this.set({ phase: 'restoring', input: run.input });
    await this.follow(run);
  }

  async retry() {
    if (this.pendingStart || isCheckupBusy(this.state)) return;
    const run = this.run;
    if (!run) { this.set({ phase: 'idle' }); return; }
    const status = this.state.task?.status;
    if (run.expired || run.refused || status === 'failed' || status === 'cancelled') {
      // 过期、被拒、失败、已取消的任务不能再用同一个请求标识：后端会把它认成同一次请求。
      this.run = undefined;
      await this.start(run.input);
      return;
    }
    run.abort.abort();
    const next: Run = { ...run, abort: new AbortController(), revision: ++this.revision, unconfirmed: false };
    this.run = next;
    if (!next.id && run.unconfirmed) {
      // 用户点了"重新提交"：沿用同一请求标识，服务端若其实已经建过，会直接返回那一个。
      this.patch({ phase: 'submitting', error: undefined, connection: undefined, recovery: undefined });
      await this.execute(next);
      return;
    }
    this.patch({ phase: next.id ? 'running' : 'restoring', error: undefined, recovery: undefined });
    await this.follow(next);
  }

  private async execute(run: Run) {
    run.creating = true;
    let created: CheckupTaskView;
    try {
      // POST 不跟着任何页面动作一起中止：它回来的任务 ID 是唯一能取消服务端工作的东西。
      created = await this.api.create(toRequest(run.input));
    } catch (error) {
      run.creating = false;
      if (!this.current(run)) return;
      if (error instanceof CheckupError && error.status >= 400 && error.status < 500) {
        // 4xx 是服务端明确说了没建：句柄作废，重试会换新的请求标识。
        run.refused = true;
        this.onHandle(undefined);
        this.patch({ phase: 'error', error: error.message });
        return;
      }
      // 响应丢了（断网、超时、5xx、读不懂的响应）：服务端可能已经建了任务。只按请求标识
      // 去查，不自动重提。
      if (isTransient(error)) this.patch({ connection: 'lost' });
      await this.follow(run);
      return;
    }
    run.creating = false;
    run.id = created.taskId;
    this.save(run);
    if (!this.current(run)) return;
    this.patch({ task: created, phase: run.cancelRequested ? 'cancelling'
      : created.status === 'queued' ? 'queued' : 'running' });
    await this.follow(run);
  }

  /**
   * 一轮的主循环：先把任务 ID 找回来（如需要），把记着的取消发出去（如需要），再轮询到终态。
   * 连接失败按 `RECONNECT_DELAYS_MS` 退避重试；服务端的明确回答才会结束这一轮。
   */
  private async follow(run: Run) {
    let failures = 0;
    let cancelSent = false;
    while (this.current(run)) {
      try {
        if (!run.id) {
          let found: CheckupTaskView;
          try { found = await this.api.byRequest(run.input.clientRequestId); }
          catch (error) {
            if (!isNotFound(error)) throw error;
            if (!this.current(run)) return;
            if (run.cancelRequested) {
              // 服务端没有这个任务，取消的目的已经达到了。
              this.onHandle(undefined);
              this.run = undefined;
              this.set({ phase: 'cancelled', input: run.input });
              return;
            }
            run.unconfirmed = true;
            this.patch({ phase: 'error', error: UNCONFIRMED_MESSAGE, connection: undefined,
              recovery: 'unconfirmed' });
            return;
          }
          if (!this.current(run)) return;
          run.id = found.taskId;
          this.save(run);
          this.patch({ task: found });
        }
        const task = run.cancelRequested && !cancelSent && !isTerminal(this.state.task)
          ? await this.api.cancel(run.id) : await this.api.status(run.id, run.abort.signal);
        if (run.cancelRequested) cancelSent = true;
        failures = 0;
        if (!this.current(run)) return;
        if (this.state.connection) this.patch({ connection: undefined });
        if (await this.accept(run, task)) return;
      } catch (error) {
        if (!this.current(run)) return;
        if (isTransient(error)) {
          this.patch({ connection: 'lost' });
          await this.pause(run, RECONNECT_DELAYS_MS[Math.min(failures++, RECONNECT_DELAYS_MS.length - 1)]);
          continue;
        }
        if (run.id && isNotFound(error)) {
          run.expired = true;
          this.patch({ phase: 'error', error: EXPIRED_MESSAGE, connection: undefined, recovery: 'expired' });
          return;
        }
        this.patch({ phase: 'error', connection: undefined,
          error: run.cancelRequested ? '未能确认取消，请再次取消或检查后端状态'
            : error instanceof Error ? error.message : '体检失败，请重试' });
        return;
      }
      await this.pause(run, POLL_MS);
    }
  }

  private async accept(run: Run, task: CheckupTaskView): Promise<boolean> {
    if (!this.current(run)) return true;
    if (task.status === 'completed') {
      this.patch({ phase: 'fetching', task });
      // 取的是任务视图报告的那一版修订，而不是"最新的一版"：两者不一致时，说明
      // 服务端还有一版没被这次轮询看到，取最新会让报告和任务状态描述不同的结论。
      const snapshot = await this.api.result(run.id!, task.revision, run.abort.signal);
      if (!this.current(run)) return true;
      if (snapshot.revision !== task.revision) {
        throw new CheckupError('体检修订与任务状态不符，请检查服务版本', 0, 'mismatched_revision');
      }
      this.set({ phase: 'completed', input: run.input, task, snapshot, layers: this.state.layers });
      return true;
    }
    if (task.status === 'cancelled') { this.patch({ phase: 'cancelled', task }); return true; }
    if (task.status === 'failed') {
      this.patch({ phase: 'error', task,
        error: task.error ? `体检执行失败：${task.error}` : '体检执行失败，请检查后端配置后重试' });
      return true;
    }
    this.patch({ task, phase: task.status === 'queued' ? 'queued'
      : task.status === 'cancelling' || run.cancelRequested ? 'cancelling' : 'running' });
    return false;
  }

  private pause(run: Run, ms: number) {
    const signal = run.abort.signal;
    return new Promise<void>(resolve => {
      const finish = () => {
        clearTimeout(timer); signal.removeEventListener('abort', finish);
        if (this.wake === finish) this.wake = undefined;
        resolve();
      };
      const timer = setTimeout(finish, ms);
      signal.addEventListener('abort', finish, { once: true });
      this.wake = finish;
      if (signal.aborted) finish();
    });
  }

  /** 网络恢复（`online` 事件）或页面重新可见时调用：不等退避计时，马上再问一次。 */
  nudge() { this.wake?.(); }

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
    // 取的过程里任务换了（清除后重开、或者修订又前进了一版）：这一张属于上一版，
    // 丢掉而不是画上去 —— 图上画着旧灰区、面板写着新面积是最难发现的一类错。
    const now = this.state.task;
    if (!now || now.taskId !== task.taskId || now.revision !== task.revision) return undefined;
    this.patch({ layers: { ...this.state.layers, [layerId]: layer } });
    return layer;
  }

  /** 点击某个设施的详情路线。它不发布修订，所以结果只留在 `route` 里。 */
  async detail(facilityId: string) {
    const task = this.state.task;
    if (!task) return;
    this.patch({ route: undefined, routeError: undefined });
    try {
      const route = await this.api.route(task.taskId, facilityId);
      if (this.state.task?.taskId !== task.taskId) return;
      this.patch({ route });
    } catch (error) {
      if (this.state.task?.taskId !== task.taskId) return;
      const message = error instanceof CheckupError ? error.message : '未能取到这条设施的步行路线';
      this.patch({ routeError: message,
        ...(error instanceof CheckupError && error.code === DETAIL_BUDGET_EXHAUSTED ? { route: undefined } : {}) });
    }
  }

  /**
   * 用户明确的取消 —— 唯一会让服务端停下的动作。
   *
   * "已请求取消"先写进句柄再发请求：取消还没得到确认时刷新，回来会再发一遍（幂等）。
   * 创建请求还在路上时只能先记下，拿到任务 ID 立刻补发；创建没送达的，按请求标识查
   * 不到就说明服务端本来就没有这个任务，取消的目的已经达到。
   */
  async cancel() {
    const old = this.run;
    if (!old || old.cancelRequested && this.state.phase === 'cancelling') return;
    if (old.id && isTerminal(this.state.task)) return;
    if (old.refused || old.expired) { this.clear(); return; }
    old.cancelRequested = true;
    this.save(old);
    if (old.creating) { this.patch({ phase: 'cancelling', error: undefined }); return; }
    old.abort.abort();
    const run: Run = { ...old, abort: new AbortController(), revision: ++this.revision, unconfirmed: false };
    this.run = run;
    this.patch({ phase: 'cancelling', error: undefined, recovery: undefined });
    await this.follow(run);
  }

  /**
   * 忘掉当前这一个已经结束的任务（开始新体检前、或用户点"清除结果"）。
   *
   * 只对已结束、被拒、已过期或未送达的任务生效：还在跑的任务要先取消 —— 这里不替用户
   * 做这个决定。
   */
  clear() {
    const run = this.run;
    if (run && !run.refused && !run.expired && !run.unconfirmed && run.id && !isTerminal(this.state.task)) return false;
    if (run && !run.id && !run.unconfirmed && !run.refused) return false;
    ++this.revision;
    run?.abort.abort();
    this.run = undefined;
    this.onHandle(undefined);
    this.set({ phase: 'idle' });
    return true;
  }

  /** 不再发布状态、停止轮询；**不取消**服务端的任务。 */
  dispose() { this.publish = () => {}; this.run?.abort.abort(); ++this.revision; }
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
