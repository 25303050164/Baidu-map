/**
 * 可展开的目录项：原生 `<details>`，收起时内容仍在 DOM 里。
 *
 * 不用 antd Collapse：它在展开前不渲染内容，而额度说明、水系说明这类文字即使收起也要能被
 * 读取（测试按 DOM 读它们，屏幕阅读器也照样能找到）。
 */
import type { ReactNode } from 'react';

export function Fold({ title, count, defaultOpen, className, testId, children }: {
  title: ReactNode;
  /** 标题右侧的小计数，例如"7 条"。 */
  count?: ReactNode;
  defaultOpen?: boolean;
  className?: string;
  testId?: string;
  children: ReactNode;
}) {
  return <details className={`wb-fold${className ? ` ${className}` : ''}`} open={defaultOpen}
    data-testid={testId}>
    <summary><span className="wb-fold-title">{title}</span>
      {count !== undefined && <span className="wb-fold-count">{count}</span>}</summary>
    <div className="wb-fold-body">{children}</div>
  </details>;
}
