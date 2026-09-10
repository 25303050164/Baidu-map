import { createContext, useContext, useEffect, useReducer, useRef, type ReactNode } from 'react';
import { initialState, reducer, type Action, type State } from './state';
import { demoService } from './service';
const Context = createContext<{ state: State; dispatch: React.Dispatch<Action>; analyze: () => Promise<void> } | null>(null);
export function Store({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, initialState);
  const live = useRef(state); live.current = state;
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; demoService.getSamples().then(samples => { if (mounted.current) dispatch({ type: 'samples', samples }); }); return () => { mounted.current = false; }; }, []);
  async function analyze() {
    const requestId = live.current.requestId + 1;
    const conditions = { center: { ...live.current.center }, scenario: live.current.scenario };
    dispatch({ type: 'start', requestId });
    try {
      const id = await demoService.createAnalysis(conditions);
      let status = await demoService.getStatus(id);
      while (status.status === 'running') {
        await new Promise(resolve => setTimeout(resolve, 100));
        if (!mounted.current || live.current.requestId !== requestId) return;
        status = await demoService.getStatus(id);
      }
      if (!mounted.current || live.current.requestId !== requestId) return;
      if (status.status !== 'completed') { dispatch({ type: 'error', requestId, status: status.status === 'unavailable' ? 'unavailable' : 'failed', error: status.status === 'unavailable' ? '暂无演示数据，请选择地图上的 A、B、C 预设点。' : '本次模拟分析失败，重试后将返回正常演示结果。' }); return; }
      const result = await demoService.getResult(id);
      if (mounted.current) dispatch({ type: 'success', requestId, result });
    } catch { if (mounted.current) dispatch({ type: 'error', requestId, status: 'failed', error: '分析暂不可用，请重新体检。' }); }
  }
  return <Context.Provider value={{ state, dispatch, analyze }}>{children}</Context.Provider>;
}
export function useStore() { const ctx = useContext(Context); if (!ctx) throw new Error('Store missing'); return ctx; }
