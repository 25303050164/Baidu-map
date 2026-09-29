import { describe, expect, it } from 'vitest';
import { getBaiduSession, getHybridSession, saveBaiduSession, saveHybridSession } from './algorithmSessions';

describe('algorithm sessions', () => {
  it('keeps the two algorithm snapshots independent', () => {
    const baidu = getBaiduSession();
    const hybrid = getHybridSession();
    saveBaiduSession({ ...baidu, reportOpen: true });
    saveHybridSession({ ...hybrid, budget: 200 });

    expect(getBaiduSession().reportOpen).toBe(true);
    expect(getHybridSession().budget).toBe(200);
    expect(getBaiduSession().budget).toBe(400);

    saveBaiduSession(baidu);
    saveHybridSession(hybrid);
  });
});
