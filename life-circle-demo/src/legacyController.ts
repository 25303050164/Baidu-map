/**
 * 旧版两条分析（E8.2 `/api/analyses`、OSM＋百度 `/api/v1/analysis/hybrid`）共用的任务控制器。
 *
 * 规矩与 v2 体检控制器（`checkup/controller.ts`）相同：
 *
 * - **任务不属于页面。** 控制器活在模块里，组件卸载只是不再有人看；换中心点、换算法、
 *   换页面都不取消任务。能让服务端停下的只有用户点的"取消"。
 * - **先存句柄再发请求。** 请求标识在创建请求发出之前就交给 `onHandle` 存起来，刷新后
 *   凭它向后端核对：有任务 ID 问状态，没有就按请求标识查（只读，不替用户重提）。
 * - **断网不是失败。** 连接失败按 1、2、4、8、10 秒退避重试并标出"连接中断"；只有服务端
 *   明确的回答（404、失败、结果对不上）才结束这一轮。
 *
 * 与 v2 不同的两处：
 *
 * - 旧版任务只在后端内存里，后端重启或任务结束 30 分钟后即被清理。这时按任务 ID 查到
 *   404，如实说"已过期、结果无法恢复"，而不是悄悄重新提交一个花额度的新任务。
 * - 旧版后端同一时刻只跑一个任务，别人的任务占着时创建会被拒（409 忙）。后端的拒绝
 *   文案写明了占用情况（已运行多久、已用多少调用），原样给用户看；若占着名额的正是
 *   本浏览器此前提交的任务（例如另一个页签），就直接切回去接着看。并发限制仍在后端。
 */

export type LegacyStatus = 'running' | 'cancelling' | 'completed' | 'cancelled' | 'failed';
export type LegacyTaskView = { taskId: string; status: LegacyStatus; error?: string | null };
export type LegacyInput = { clientRequestId: string };

/** 刷新后找回任务所需的全部信息：请求条件与标识。结果一律回后端重取。 */
export type LegacyHandle<I> = { input: I; taskId?: string; cancelRequested?: boolean; savedAt: number };

export type LegacyPhase =
  | 'idle' | 'submitting' | 'restoring' | 'running' | 'fetching' | 'cancelling'
  | 'completed' | 'cancelled' | 'error';

export type LegacyState<I, T, R> = {
  phase: LegacyPhase;
  /** 这一轮提交时的条件：刷新后还没取到结果时，界面靠它说明"在算哪个点"。 */
  input?: I;
  task?: T;
  result?: R;
  error?: string;
  /** 与后端的连接中断、正在退避重连；任务本身没有因此失败。 */
  connection?: 'lost';
  /** `unconfirmed`：创建请求没送达（按请求标识查不到）；`expired`：任务 ID 服务端已不认识。 */
  recovery?: 'unconfirmed' | 'expired';
  /** 不是错误、但用户该知道的事，例如"服务忙，已切回你此前提交的任务"。 */
  notice?: string;
};

/**
 * 各客户端的错误归成五类，控制器只按类别决定去留：
 * `network` 等一等再问；`missing` 服务端不认识；`busy` 名额被占；`refused` 服务端明确拒绝创建；
 * `other` 其余明确的失败。
 */
export type Failure = 'network' | 'missing' | 'busy' | 'refused' | 'other';

export interface LegacyApi<I extends LegacyInput, T extends LegacyTaskView, R> {
  create(input: I): Promise<T>;
  byRequest(clientRequestId: string, signal?: AbortSignal): Promise<T>;
  status(taskId: string, signal?: AbortSignal): Promise<T>;
  result(taskId: string, signal?: AbortSignal): Promise<R>;
  cancel(taskId: string): Promise<T>;
  classify(error: unknown): Failure;
  /** 给读者的话；只在 `classify` 之后调用。 */
  describe(error: unknown): string;
  /** 结果确实属于这个任务、这次提交的中心与预算。 */
  matches(result: R, task: T, input: I): boolean;
}

type Run<I> = {
  input: I;
  id?: string;
  abort: AbortController;
  revision: number;
  expired?: boolean;
  refused?: boolean;
  unconfirmed?: boolean;
  creating?: boolean;
  cancelRequested?: boolean;
  /** 创建请求收到了服务端的错误回答（5xx）：按请求标识也查不到时，给读者看的是这句。 */
  createError?: string;
};

export const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000, 10_000];
const POLL_MS = 1000;

export const LEGACY_EXPIRED_MESSAGE = '后端已找不到这个分析任务：旧版任务只保存在后端内存里，后端重启或任务结束 30 分钟后即被清理，'
  + '结果无法恢复；可重新分析。';
export const LEGACY_UNCONFIRMED_MESSAGE = '创建请求没有送达服务端：按请求标识查不到任务。为避免重复消耗额度，页面没有自动重新提交；'
  + '点"重新提交"会沿用同一请求标识，服务端若已建过任务会直接认出来，不会重复创建。';
export const LEGACY_ADOPTED_NOTICE = '服务正忙：占用名额的正是本浏览器此前提交、仍在运行的任务，已切回该任务继续查看。';

export class ResultMismatchError extends Error {
  constructor() { super('分析结果与提交条件不一致，请检查服务版本'); }
}

export const isLegacyTerminal = (status: LegacyStatus | undefined) =>
  status === 'completed' || status === 'cancelled' || status === 'failed';

export function isLegacyBusy(state: { phase: LegacyPhase }) {
  return ['submitting', 'restoring', 'running', 'fetching', 'cancelling'].includes(state.phase);
}

export class LegacyController<I extends LegacyInput, T extends LegacyTaskView, R> {
  state: LegacyState<I, T, R> = { phase: 'idle' };
  private run?: Run<I>;
  private revision = 0;
  private pendingStart = false;
  private wake?: () => void;

  constructor(
    private api: LegacyApi<I, T, R>,
    private publish: (state: LegacyState<I, T, R>) => void,
    private onHandle: (handle: LegacyHandle<I> | undefined) => void = () => {},
    /** 存储里现有的句柄（可能是本浏览器另一个页签写的）：服务忙时用来认出"自己的任务"。 */
    private recall: () => LegacyHandle<I> | undefined = () => undefined,
  ) {}

  private set(state: LegacyState<I, T, R>) { this.state = state; this.publish(state); }
  private patch(state: Partial<LegacyState<I, T, R>>) { this.set({ ...this.state, ...state }); }
  private current(run: Run<I>) { return run.revision === this.revision && !run.abort.signal.aborted; }

  private save(run: Run<I>) {
    this.onHandle({
      input: run.input, savedAt: Date.now(),
      ...(run.id ? { taskId: run.id } : {}),
      ...(run.cancelRequested ? { cancelRequested: true } : {}),
    });
  }

  /** 还有一个没结束的任务（或一次没确认的创建）：这时不许再开新的，只有用户取消能让它停。 */
  get hasLiveTask(): boolean {
    const run = this.run;
    if (!run || run.refused || run.expired || run.unconfirmed) return false;
    return !run.id || !isLegacyTerminal(this.state.task?.status);
  }

  async start(input: Omit<I, 'clientRequestId'>) {
    if (this.pendingStart || isLegacyBusy(this.state) || this.hasLiveTask) return;
    this.pendingStart = true;
    try {
      const previous = this.recall();
      this.run?.abort.abort();
      const run: Run<I> = {
        input: { ...input, clientRequestId: crypto.randomUUID() } as I,
        abort: new AbortController(), revision: ++this.revision,
      };
      this.run = run;
      this.set({ phase: 'submitting', input: run.input });
      this.save(run);
      this.pendingStart = false;
      await this.execute(run, previous);
    } finally { this.pendingStart = false; }
  }

  /** 页面刷新或重新挂载后，凭存下来的句柄接着看；记着"已请求取消"的再发一遍取消。 */
  async resume(handle: LegacyHandle<I>) {
    if (this.run) return;
    const run: Run<I> = {
      input: handle.input, id: handle.taskId, cancelRequested: handle.cancelRequested,
      abort: new AbortController(), revision: ++this.revision,
    };
    this.run = run;
    this.set({ phase: 'restoring', input: run.input });
    await this.follow(run);
  }

  async retry() {
    if (this.pendingStart || isLegacyBusy(this.state)) return;
    const run = this.run;
    if (!run) { this.set({ phase: 'idle' }); return; }
    const status = this.state.task?.status;
    if (run.expired || run.refused || status === 'failed' || status === 'cancelled') {
      // 这些任务不能沿用同一请求标识：后端会把它认成同一次请求。
      this.run = undefined;
      const { clientRequestId: _, ...input } = run.input;
      await this.start(input);
      return;
    }
    run.abort.abort();
    const next: Run<I> = { ...run, abort: new AbortController(), revision: ++this.revision, unconfirmed: false };
    this.run = next;
    if (!next.id && run.unconfirmed) {
      this.patch({ phase: 'submitting', error: undefined, connection: undefined, recovery: undefined });
      await this.execute(next);
      return;
    }
    this.patch({ phase: next.id ? 'running' : 'restoring', error: undefined, recovery: undefined });
    await this.follow(next);
  }

  private async execute(run: Run<I>, previous?: LegacyHandle<I>) {
    run.creating = true;
    let created: T;
    try {
      // POST 不跟着任何页面动作中止：它回来的任务 ID 是唯一能取消服务端工作的东西。
      created = await this.api.create(run.input);
    } catch (error) {
      run.creating = false;
      if (!this.current(run)) return;
      const kind = this.api.classify(error);
      // 创建地址 404 是地址或版本不对，服务端同样什么都没建。
      if (kind === 'busy' || kind === 'refused' || kind === 'missing') {
        // 服务端明确说了没建：这次的句柄作废，存储换回原来那一个。
        run.refused = true;
        const other = previous && previous.input.clientRequestId !== run.input.clientRequestId ? previous : undefined;
        this.onHandle(other);
        this.patch({ phase: 'error', error: this.api.describe(error) });
        if (kind === 'busy' && other) await this.adoptIfLive(run, other);
        return;
      }
      // 响应丢了：服务端可能已经建了任务。只按请求标识去查，不自动重提。
      if (kind === 'network') this.patch({ connection: 'lost' });
      else run.createError = this.api.describe(error);
      await this.follow(run);
      return;
    }
    run.creating = false;
    run.id = created.taskId;
    this.save(run);
    if (!this.current(run)) return;
    this.patch({ task: created, phase: run.cancelRequested ? 'cancelling' : 'running' });
    await this.follow(run);
  }

  /** 服务忙时看一眼存储里的那个任务：还在跑，说明占着名额的就是它，切回去接着看。 */
  private async adoptIfLive(refused: Run<I>, handle: LegacyHandle<I>) {
    let task: T;
    try {
      task = handle.taskId ? await this.api.status(handle.taskId)
        : await this.api.byRequest(handle.input.clientRequestId);
    } catch { return; }
    if (!this.current(refused) || isLegacyTerminal(task.status)) return;
    const run: Run<I> = { input: handle.input, id: task.taskId, cancelRequested: handle.cancelRequested,
      abort: new AbortController(), revision: ++this.revision };
    this.run = run;
    this.save(run);
    this.set({ phase: run.cancelRequested || task.status === 'cancelling' ? 'cancelling' : 'running',
      input: run.input, task, notice: LEGACY_ADOPTED_NOTICE });
    await this.follow(run);
  }

  private async follow(run: Run<I>) {
    let failures = 0;
    let cancelSent = false;
    while (this.current(run)) {
      try {
        if (!run.id) {
          let found: T;
          try { found = await this.api.byRequest(run.input.clientRequestId, run.abort.signal); }
          catch (error) {
            if (this.api.classify(error) !== 'missing') throw error;
            if (!this.current(run)) return;
            if (run.cancelRequested) {
              // 服务端没有这个任务，取消的目的已经达到了。
              this.onHandle(undefined);
              this.run = undefined;
              this.set({ phase: 'cancelled', input: run.input });
              return;
            }
            run.unconfirmed = true;
            this.patch({ phase: 'error', error: run.createError ?? LEGACY_UNCONFIRMED_MESSAGE, connection: undefined,
              recovery: 'unconfirmed' });
            return;
          }
          if (!this.current(run)) return;
          run.id = found.taskId;
          this.save(run);
          this.patch({ task: found });
        }
        const task = run.cancelRequested && !cancelSent && !isLegacyTerminal(this.state.task?.status)
          ? await this.api.cancel(run.id) : await this.api.status(run.id, run.abort.signal);
        if (run.cancelRequested) cancelSent = true;
        failures = 0;
        if (!this.current(run)) return;
        if (this.state.connection) this.patch({ connection: undefined });
        if (await this.accept(run, task)) return;
      } catch (error) {
        if (!this.current(run)) return;
        const kind = this.api.classify(error);
        if (kind === 'network') {
          this.patch({ connection: 'lost' });
          await this.pause(run, RECONNECT_DELAYS_MS[Math.min(failures++, RECONNECT_DELAYS_MS.length - 1)]);
          continue;
        }
        if (run.id && kind === 'missing') {
          run.expired = true;
          this.patch({ phase: 'error', error: LEGACY_EXPIRED_MESSAGE, connection: undefined, recovery: 'expired' });
          return;
        }
        this.patch({ phase: 'error', connection: undefined,
          error: run.cancelRequested ? '未能确认取消，请再次取消或检查后端状态' : this.api.describe(error) });
        return;
      }
      await this.pause(run, POLL_MS);
    }
  }

  private async accept(run: Run<I>, task: T): Promise<boolean> {
    if (!this.current(run)) return true;
    if (task.status === 'completed') {
      this.patch({ phase: 'fetching', task });
      const result = await this.api.result(run.id!, run.abort.signal);
      if (!this.current(run)) return true;
      if (!this.api.matches(result, task, run.input)) throw new ResultMismatchError();
      this.set({ phase: 'completed', input: run.input, task, result, notice: this.state.notice });
      return true;
    }
    if (task.status === 'cancelled') { this.patch({ phase: 'cancelled', task }); return true; }
    if (task.status === 'failed') {
      this.patch({ phase: 'error', task, error: '分析执行失败，请检查后端配置后重试' });
      return true;
    }
    this.patch({ task, phase: task.status === 'cancelling' || run.cancelRequested ? 'cancelling' : 'running' });
    return false;
  }

  private pause(run: Run<I>, ms: number) {
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

  /** 网络恢复或页面重新可见时调用：不等退避计时，马上再问一次。 */
  nudge() { this.wake?.(); }

  /** 用户明确的取消 —— 唯一会让服务端停下的动作。先把"已请求取消"写进句柄再发。 */
  async cancel() {
    const old = this.run;
    if (!old || old.cancelRequested && this.state.phase === 'cancelling') return;
    if (old.id && isLegacyTerminal(this.state.task?.status)) return;
    if (old.refused || old.expired) { this.clear(); return; }
    old.cancelRequested = true;
    this.save(old);
    if (old.creating) { this.patch({ phase: 'cancelling', error: undefined }); return; }
    old.abort.abort();
    const run: Run<I> = { ...old, abort: new AbortController(), revision: ++this.revision, unconfirmed: false };
    this.run = run;
    this.patch({ phase: 'cancelling', error: undefined, recovery: undefined, notice: undefined });
    await this.follow(run);
  }

  /** 忘掉当前这一个已经结束（或被拒、过期、未送达）的任务；还在跑的不管，返回 false。 */
  clear() {
    const run = this.run;
    if (run && !run.refused && !run.expired && !run.unconfirmed && run.id && !isLegacyTerminal(this.state.task?.status)) return false;
    if (run && !run.id && !run.unconfirmed && !run.refused) return false;
    ++this.revision;
    run?.abort.abort();
    this.run = undefined;
    // 被拒的那一轮已经把存储换回了原来的句柄，这里不去动它。
    if (!run?.refused) this.onHandle(undefined);
    this.set({ phase: 'idle' });
    return true;
  }

  /** 不再发布状态、停止轮询；**不取消**服务端的任务。 */
  dispose() { this.publish = () => {}; this.run?.abort.abort(); ++this.revision; }
}
