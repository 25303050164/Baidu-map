import type { AnalysisResult, Center, Filter, Sample, Scenario } from './types';
export type State = { samples: Sample[]; center: Center; scenario: Scenario; filter: Filter; status: 'idle' | 'loading' | 'success' | 'failed' | 'unavailable'; result?: AnalysisResult; dirty: boolean; requestId: number; error?: string };
export const initialState: State = { samples: [], center: { lng: 116.399, lat: 39.9129 }, scenario: 'normal', filter: 'all', status: 'idle', dirty: false, requestId: 0 };
export type Action = { type: 'samples'; samples: Sample[] } | { type: 'edit'; center?: Center; scenario?: Scenario } | { type: 'filter'; filter: Filter } | { type: 'start'; requestId: number } | { type: 'success'; requestId: number; result: AnalysisResult } | { type: 'error'; requestId: number; status: 'failed' | 'unavailable'; error: string };
export function reducer(state: State, action: Action): State {
  switch(action.type) {
    case 'samples': return { ...state, samples: action.samples };
    case 'edit': return { ...state, center: action.center ?? state.center, scenario: action.scenario ?? state.scenario, status: 'idle', dirty: !!state.result, requestId: state.requestId + 1, error: undefined };
    case 'filter': return { ...state, filter: action.filter };
    case 'start': return { ...state, requestId: action.requestId, status: 'loading', error: undefined };
    case 'success': return action.requestId === state.requestId ? { ...state, result: action.result, status: 'success', dirty: false } : state;
    case 'error': return action.requestId === state.requestId ? { ...state, status: action.status, error: action.error } : state;
  }
}
