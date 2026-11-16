import { useId } from 'react';
import type { ReactNode } from 'react';

/** Static, conditional guidance also works when a table fits without scrolling. */
export function TableScroll({ label, children }: { label: string; children: ReactNode }) {
  const hintId = useId();
  return <>
    <p id={hintId} className="table-scroll-hint">If columns extend beyond this panel, scroll horizontally. Keyboard: focus the table region and use Left/Right arrows.</p>
    <div className="holdings-table-scroll financial-table-scroll" role="region" tabIndex={0} aria-label={label} aria-describedby={hintId}>{children}</div>
  </>;
}
