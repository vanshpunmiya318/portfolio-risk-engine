import { useId } from 'react';
import type { HoldingsResponse } from '../api/portfolio';
import { buildAllocation } from '../lib/allocation';
import { formatWeight } from '../lib/format';

export function AllocationDonut({ data }: { data: HoldingsResponse }) {
  const titleId = useId();
  const descriptionId = useId();
  const allocation = buildAllocation(data);
  return (
    <section className="holdings-panel" aria-labelledby={titleId}>
      <div className="panel-heading"><div><p className="eyebrow">Positioning</p><h2 id={titleId}>Invested allocation</h2></div>
        <span className="panel-caption">Excludes cash</span></div>
      {!allocation.available ? <p className="allocation-unavailable" role="status">{allocation.reason}</p> :
        <figure className="allocation-figure">
          <svg className="allocation-donut" viewBox="0 0 200 200" role="img" aria-labelledby={`${titleId} ${descriptionId}`}>
            <desc id={descriptionId}>{allocation.segments.map((s) => `${s.ticker}: ${formatWeight(s.weight)}`).join('; ')}. Weights exclude uninvested cash.</desc>
            <circle cx="100" cy="100" r="75" fill="none" className="donut-track" strokeWidth="24" />
            {allocation.segments.map((segment, index) => <circle key={segment.ticker} cx="100" cy="100" r="75" fill="none"
              pathLength="100" strokeWidth="24" strokeDasharray={`${segment.weight * 100} ${100 - segment.weight * 100}`}
              strokeDashoffset={-segment.offset * 100} transform="rotate(-90 100 100)" className={`allocation-color-${index % 6}`} />)}
            <text x="100" y="98" textAnchor="middle" className="donut-label">Invested</text>
            <text x="100" y="116" textAnchor="middle" className="donut-caption">excluding cash</text>
          </svg>
          <figcaption><ul className="allocation-legend">{allocation.segments.map((segment, index) =>
            <li key={segment.ticker}><span className={`legend-swatch allocation-color-${index % 6}`} aria-hidden="true" />
              <span>{segment.ticker}</span><strong>{formatWeight(segment.weight)}</strong></li>,
          )}</ul><p className="panel-caption">Weights reported by the risk engine. No missing values are estimated.</p></figcaption>
        </figure>}
    </section>
  );
}
