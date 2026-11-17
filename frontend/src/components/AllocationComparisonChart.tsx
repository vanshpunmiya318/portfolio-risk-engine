import { useId, useState } from 'react';
import type { AllocationResponse } from '../api/allocation';
import { allocationBars, exactAllocationChange, exactAllocationPercent } from '../lib/allocationBars';
import { formatAllocationChange } from '../lib/allocationComparison';
import { formatWeight } from '../lib/format';

export function AllocationComparisonChart({ data, referenceDescriptionId }: { data: AllocationResponse; referenceDescriptionId?: string }) {
  const id = useId();
  const [selected, setSelected] = useState<string | null>(null);
  const chart = allocationBars(data);
  const active = chart.rows.find(row => row.ticker === selected);
  return <section className="holdings-panel allocation-bars" aria-labelledby={`${id}-title`} aria-describedby={referenceDescriptionId}>
    <div className="panel-heading"><h2 id={`${id}-title`}>Current versus historical starting allocation</h2><span className="panel-caption">Invested holdings excluding cash</span></div>
    <p className="valuation-note">Both series use the same 0–100% scale. Securities are ordered by descending current weight, then ticker.</p>
    {chart.reasons.length > 0 ? <div className="allocation-unavailable" role="status"><strong>Allocation comparison chart unavailable</strong><ul>{chart.reasons.map((reason, i) => <li key={i}>{reason}</li>)}</ul></div> : <>
      <ul className="allocation-bars-legend" aria-label="Allocation chart legend"><li><span className="bar-key bar-key-start" aria-hidden="true" />Historical starting</li><li><span className="bar-key bar-key-current" aria-hidden="true" />Current invested</li></ul>
      <p className="panel-caption" id={`${id}-help`}>Tab to a security; press Enter or Space to show exact values. The comparison table above provides the same data with full column labels.</p>
      <div className="allocation-bar-axis" aria-hidden="true">{[0, 25, 50, 75, 100].map(tick => <span key={tick} style={{ left: `${tick}%` }}>{tick}%</span>)}</div>
      <ul className="allocation-bar-rows" aria-describedby={`${id}-help`}>{chart.rows.map(row => {
        const label = `${row.ticker}, ${row.instrument}. Historical starting ${exactAllocationPercent(row.referenceWeight!)}; current invested ${exactAllocationPercent(row.currentWeight!)}; change ${exactAllocationChange(row.change!)}.`;
        return <li key={row.ticker}><button type="button" className="allocation-bar-row" aria-label={label} aria-expanded={selected === row.ticker}
          aria-controls={`${id}-values`} onClick={() => setSelected(selected === row.ticker ? null : row.ticker)} title={label}>
          <span className="allocation-bar-instrument"><strong>{row.ticker}</strong><span className="instrument-name">{row.instrument}</span></span>
          <svg className="allocation-bar-plot" viewBox="0 0 100 30" preserveAspectRatio="none" aria-hidden="true">
            {[0, 25, 50, 75, 100].map(tick => <line key={tick} x1={tick} x2={tick} y1="0" y2="30" className="allocation-bar-grid" vectorEffect="non-scaling-stroke" />)}
            <rect className="allocation-bar-start" x="0" y="3" width={row.referenceWeight! * 100} height="9" vectorEffect="non-scaling-stroke" />
            <rect className="allocation-bar-current" x="0" y="18" width={row.currentWeight! * 100} height="9" />
          </svg>
          <span className="allocation-bar-values"><span>Starting {formatWeight(row.referenceWeight)}</span><span>Current {formatWeight(row.currentWeight)}</span><strong>{formatAllocationChange(row.change)}</strong></span>
        </button></li>;
      })}</ul>
      <div id={`${id}-values`} className="allocation-bar-exact" role="status" aria-live="polite" aria-atomic="true">{active ? <>
        <strong>{active.ticker} · Exact allocation values</strong><span>Historical starting: {exactAllocationPercent(active.referenceWeight!)}</span>
        <span>Current invested: {exactAllocationPercent(active.currentWeight!)}</span><span>Change: {exactAllocationChange(active.change!)}</span>
        <span className="panel-caption">API fractions: starting {String(active.referenceWeight)}, current {String(active.currentWeight)}, change {String(active.change)}.</span>
      </> : <span className="panel-caption">Select a security for exact values. Zero-length bars represent genuine zero invested weights.</span>}</div>
    </>}
  </section>;
}
