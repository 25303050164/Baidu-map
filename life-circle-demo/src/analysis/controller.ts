import type { AnalysisInput, AnalysisService, AnalysisState, TaskStatus } from './types';
import { isAnalysisBusy } from './types';
import { ApiError } from './service';
import { matchesAnalysisInput } from './adapter';

type Run = { input: AnalysisInput; id?: string; abort: AbortController; revision: number; expired?: boolean; creationFailed?: boolean };
function pause(signal: AbortSignal) {
  return new Promise<void>(resolve => {
    const finish = () => { clearTimeout(timer); signal.removeEventListener('abort', finish); resolve(); };
    const timer = setTimeout(finish, 1000);
    signal.addEventListener('abort', finish, { once: true });
    if (signal.aborted) finish();
  });
}

export class AnalysisController {
  state: AnalysisState = { phase: 'idle' };
  private run?: Run;
  private revision = 0;
  private pendingStart?: symbol;
  private pendingCancels = new Set<string>();
  private pendingRequestCancels = new Set<string>();
  constructor(private api: AnalysisService, private publish: (state: AnalysisState) => void) {}
  private set(state: AnalysisState) { this.state = state; this.publish(state); }
  private current(run: Run) { return run.revision === this.revision; }

  async start(input: Omit<AnalysisInput, 'clientRequestId'>) {
    // Guard synchronously, including the gap while old cancellation is being confirmed.
    if (this.pendingStart || isAnalysisBusy(this.state)) return;
    const starting = this.pendingStart = Symbol();
    try {
      const resetting = this.run ? this.reset() : undefined;
      const revision = this.revision;
      if (resetting) await resetting;
      if ((this.pendingCancels.size || this.pendingRequestCancels.size) && !await this.clearPending()) return;
      if (revision !== this.revision) return;
       const run = { input: { ...input, analysisMode: input.analysisMode ?? 'baidu_online', clientRequestId: crypto.randomUUID() }, abort: new AbortController(), revision: ++this.revision };
      this.run = run;
      this.pendingStart = undefined;
      await this.execute(run);
    } finally { if (this.pendingStart === starting) this.pendingStart = undefined; }
  }

  async retry() {
    if (this.pendingStart || isAnalysisBusy(this.state)) return;
    const run = this.run;
    if (!run) {
      const starting = this.pendingStart = Symbol();
      const revision = this.revision;
      try { if (await this.clearPending() && revision === this.revision) this.set({ phase: 'idle' }); }
      finally { if (this.pendingStart === starting) this.pendingStart = undefined; }
      return;
    }
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
        // Do not abort POST on a center change: its late ID is needed to cancel server work.
        const created = await this.api.create(run.input);
        run.id = created.taskId;
        if (!this.current(run)) { await this.abandon(run.id); return; }
        this.set({ phase: 'running', task: created });
      }
      await this.watch(run);
    } catch (error) {
      if (!run.id && !(error instanceof ApiError && [404, 409, 422, 503].includes(error.status))) {
        run.creationFailed = true;
        if (!this.current(run)) {
          try { await this.abandonRequest(run.input.clientRequestId); }
          catch { /* Retained for retry before starting any new task. */ }
        }
      }
      if (run.id && error instanceof ApiError && error.status === 404) run.expired = true;
      if (this.current(run) && !run.abort.signal.aborted) this.set({ ...this.state, phase: 'error', error: error instanceof Error ? error.message : '分析失败，请重试' });
    }
  }

  private async accept(run: Run, task: TaskStatus): Promise<boolean> {
    if (!this.current(run) || run.abort.signal.aborted) return true;
    if (task.status === 'completed') {
      this.set({ phase: 'fetching', task });
      const result = await this.api.result(run.id!, run.abort.signal);
      if (!this.current(run) || run.abort.signal.aborted) return true;
       const expectedMode = run.input.analysisMode ?? 'baidu_online';
       if (result.taskId !== run.id || result.dataSource !== task.dataSource
         || (task.analysisMode !== undefined && task.analysisMode !== expectedMode)
         || (result.analysisMode !== undefined && result.analysisMode !== expectedMode)
         || !matchesAnalysisInput(result, run.input)) {
        throw new Error('分析结果与提交条件不一致，请检查服务版本');
      }
      if (this.current(run) && !run.abort.signal.aborted) this.set({ phase: 'completed', task, result });
      return true;
    }
    if (task.status === 'cancelled') { this.set({ phase: 'cancelled', task }); return true; }
    if (task.status === 'failed') { this.set({ phase: 'error', task, error: '分析执行失败，请检查后端配置后重试' }); return true; }
    this.set({ phase: task.status, task });
    return false;
  }

  private async watch(run: Run) {
    while (this.current(run) && !run.abort.signal.aborted) {
      if (await this.accept(run, await this.api.status(run.id!, run.abort.signal))) return;
      await pause(run.abort.signal);
    }
  }

  async cancel() {
    const old = this.run;
    if (!old || this.state.phase === 'cancelling') return;
    if (!old.id || this.state.task?.status === 'completed') {
      const resetting = this.reset();
      const revision = this.revision;
      await resetting;
      if (revision === this.revision && this.state.phase === 'idle') this.set({ phase: 'cancelled' });
      return;
    }
    old.abort.abort();
    const run = { ...old, abort: new AbortController(), revision: ++this.revision };
    this.run = run;
    this.set({ ...this.state, phase: 'cancelling' });
    try {
      const task = await this.api.cancel(run.id!);
      if (!await this.accept(run, task)) await this.watch(run);
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) run.expired = true;
      if (this.current(run)) this.set({ ...this.state, phase: 'error', error: '未能确认取消，请再次取消或检查后端状态' });
    }
  }

  async reset() {
    const old = this.run;
    const terminal = ['completed', 'cancelled', 'failed'].includes(this.state.task?.status || '');
    const revision = ++this.revision;
    this.run = undefined;
    old?.abort.abort();
    this.set({ phase: 'idle' });
    if (old?.id && !terminal) {
      try { await this.abandon(old.id); }
      catch { if (revision === this.revision) this.set({ phase: 'error', error: '旧任务取消未获确认，后端可能仍在运行，请检查服务后重试' }); }
    } else if (old?.creationFailed) {
      try { await this.abandonRequest(old.input.clientRequestId); }
      catch { if (revision === this.revision) this.set({ phase: 'error', error: '旧任务取消未获确认，后端可能仍在运行，请检查服务后重试' }); }
    }
  }

  private async abandonRequest(key: string) {
    this.pendingRequestCancels.add(key);
    try { await this.api.cancelByRequest(key); }
    catch (error) { if (!(error instanceof ApiError && error.status === 404)) throw error; }
    this.pendingRequestCancels.delete(key);
  }

  private async abandon(id: string) {
    this.pendingCancels.add(id);
    try { await this.api.cancel(id); }
    catch (error) { if (!(error instanceof ApiError && error.status === 404)) throw error; }
    this.pendingCancels.delete(id);
  }

  private async clearPending() {
    try {
      for (const key of this.pendingRequestCancels) await this.abandonRequest(key);
      for (const id of this.pendingCancels) await this.abandon(id);
      return true;
    } catch {
      this.set({ phase: 'error', error: '旧任务取消未获确认，后端可能仍在运行，请检查服务后重试' });
      return false;
    }
  }

  dispose() { this.publish = () => {}; void this.reset(); }
}
