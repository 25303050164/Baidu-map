/**
 * 网络恢复（`online`）或页面重新可见时通知所有在退避等待的任务控制器：马上再问一次，
 * 不等计时器。v2 体检与旧版分析共用这一组监听，只挂一次。
 */
const callbacks = new Set<() => void>();
let wired = false;

export function onWake(callback: () => void) {
  callbacks.add(callback);
  if (wired || typeof window === 'undefined') return;
  wired = true;
  const wake = () => { for (const fn of callbacks) fn(); };
  window.addEventListener('online', wake);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') wake(); });
}
