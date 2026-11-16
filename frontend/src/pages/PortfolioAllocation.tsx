import { useEffect, useRef, useState } from 'react';
import { allocationQuery, AllocationRequestError, createAllocationLoader } from '../api/allocation';
import type { AllocationIssue, AllocationResponse } from '../api/allocation';
import { allocationComparison, formatAllocationChange } from '../lib/allocationComparison';
import { formatOptionalCurrency, formatValuationDate, formatWeight } from '../lib/format';
import { AllocationComparisonChart } from '../components/AllocationComparisonChart';

const date = (day: string | null) => day ? formatValuationDate(day) : 'Unavailable';
const number = (value: number | null, digits: number) => value === null ? 'Unavailable' : new Intl.NumberFormat('en-GB', { maximumFractionDigits: digits, minimumFractionDigits: digits }).format(value);
function Reasons({ title, items }: { title: string; items: AllocationIssue[] }) {
  return items.length ? <section className="allocation-notes"><h3>{title}</h3><ul>{items.map((issue, i) => <li key={i}><strong>{issue.scope}{issue.ticker ? ` · ${issue.ticker}` : ''}: </strong>{issue.message} <code>{issue.code}</code></li>)}</ul></section> : null;
}
export function PortfolioAllocation({ portfolioId = 1 }: { portfolioId?: number }) {
  const [state, setState] = useState<{ status: 'loading' } | { status: 'error'; message: string; details: string[] } | { status: 'ready'; data: AllocationResponse }>({ status: 'loading' });
  const [draft, setDraft] = useState(''); const [validation, setValidation] = useState('');
  const [applied, setApplied] = useState<{ day?: string }>({}); const [attempt, setAttempt] = useState(0);
  const loader = useRef<ReturnType<typeof createAllocationLoader> | null>(null);
  if (!loader.current) loader.current = createAllocationLoader();
  useEffect(() => {
    const requests = loader.current!; setState({ status: 'loading' });
    void requests.load(portfolioId, applied.day, data => setState({ status: 'ready', data }), error => setState({ status: 'error', message: error instanceof Error ? error.message : 'Unable to load allocation.', details: error instanceof AllocationRequestError ? error.details : [] }));
    return () => requests.cancel();
  }, [portfolioId, applied, attempt]);
  function apply() {
    const day = draft || undefined;
    try { allocationQuery(day); } catch (error) { setValidation(error instanceof Error ? error.message : 'Invalid date.'); return; }
    setValidation(''); setApplied({ day });
  }
  return <>
    <section className="page-intro performance-intro"><p className="eyebrow">Portfolio intelligence</p><h1>Portfolio Allocation</h1><p className="lede">Composition and concentration, compared with the recorded historical starting allocation.</p></section>
    <form className="performance-filters" aria-label="Allocation valuation date" onSubmit={event => { event.preventDefault(); apply(); }}>
      <label>As-of date<input type="date" value={draft} onChange={e => setDraft(e.target.value)} /></label>
      <button type="button" className="summary-button" onClick={() => { setDraft(''); setValidation(''); }}>Latest available</button>
      <button type="submit" className="summary-button">Apply</button><p className="filter-note">Blank date uses the latest available cutoff. Latest available clears the draft date only; changes take effect on Apply. Explicit historical cutoffs require exact-date prices.</p>
      {draft !== (applied.day ?? '') && <p className="filter-note" role="status">Unapplied changes. Displayed results still use the applied valuation cutoff.</p>}
      {validation && <p className="filter-error" role="alert">{validation}</p>}
    </form>
    <section aria-label="Allocation data" aria-busy={state.status === 'loading'}>
      {state.status === 'loading' && <div className="summary-state" role="status"><h2>Loading allocation…</h2><p>Checking invested positions and historical reference data.</p></div>}
      {state.status === 'error' && <div className="summary-state" role="alert"><h2>Allocation unavailable</h2><p>{state.message}</p><ul>{state.details.map((detail, i) => <li key={i}>{detail}</li>)}</ul><button className="summary-button" onClick={() => setAttempt(n => n + 1)}>Try again</button></div>}
      {state.status === 'ready' && <AllocationContent data={state.data} />}
    </section>
  </>;
}
export function AllocationContent({ data }: { data: AllocationResponse }) {
  const c = data.current; const ref = data.reference; const table = allocationComparison(data);
  const metrics = [
    ['Invested value', formatOptionalCurrency(c.valuation_complete ? c.invested_value : null, data.currency), 'Active holdings; excludes cash'],
    ['Separate cash', formatOptionalCurrency(c.cash_balance, data.currency), 'Excluded from weights and concentration'],
    ['HHI', number(table.currentValid ? c.concentration.hhi : null, 4), 'Security-level concentration · sum of squared weights'],
    ['Effective holdings', number(table.currentValid ? c.concentration.effective_holdings : null, 2), 'Equivalent equally weighted positions'],
    ['Largest-position weight', formatWeight(table.currentValid ? c.concentration.largest_position_weight : null), 'Share of invested holdings, excluding cash'],
  ];
  return <>
    <div className="summary-heading"><div><p className="eyebrow">Historical allocation</p><h2>{data.name}</h2></div><div className="valuation-date"><span>Effective valuation date</span><strong>{date(data.effective_valuation_date)}</strong></div></div>
    <p className="holdings-date-note">Requested date: {data.requested_valuation_date ? date(data.requested_valuation_date) : 'Latest available'}. Effective cutoff: {date(data.effective_valuation_date)}. Historical records, not live market prices. All amounts in {data.currency}.</p>
    {!c.valuation_complete && <div className="quality-banner" role="status"><strong>Incomplete current valuation</strong><p>Aggregate weights, concentration and comparisons are unavailable. Known individual values may still be displayed; missing values are never zero.</p></div>}
    <dl className="summary-metrics allocation-metrics">{metrics.map(([title, value, note]) => <div className="summary-metric" key={title}><dt>{title}</dt><dd>{value}</dd><dd className="metric-note">{note}</dd></div>)}</dl>
    {c.valuation_complete && c.securities.length === 0 && <div className="summary-state holdings-empty" role="status"><h2>No invested holdings</h2><p>Invested value is zero. Cash may still be present; concentration and comparisons are undefined.</p></div>}
    <section className="holdings-panel" aria-labelledby="allocation-reference"><h2 id="allocation-reference">Historical starting reference</h2>
      <p className="valuation-note">Reference date: <strong>{date(ref.reference_date)}</strong>. Reconstructed from recorded transactions and stored exact-date prices at the end of the first recorded buy date. The date is not advanced to bypass missing data.</p>
      <p className="valuation-note">This is a historical starting allocation, not an investment-policy target. Changes are not target drift or required rebalancing. No investment-policy targets are recorded.</p>
      {ref.snapshot && <p className="valuation-note">Reference invested value: {formatOptionalCurrency(ref.snapshot.valuation_complete ? ref.snapshot.invested_value : null, data.currency)}. Separate reference cash: {formatOptionalCurrency(ref.snapshot.cash_balance, data.currency)}. Both sets of weights exclude cash.</p>}
      {!table.referenceValid && <p role="status" className="allocation-unavailable">Historical reference unavailable or incomplete. Valid current allocation remains visible.</p>}
      <Reasons title="Reference availability" items={ref.unavailable_reasons} />
      {ref.snapshot && <Reasons title="Reference valuation notes" items={ref.snapshot.unavailable_reasons} />}
      <Reasons title="Investment-policy availability" items={data.target_unavailable_reasons} />
    </section>
    <section className="holdings-panel" aria-labelledby="allocation-comparison"><div className="panel-heading"><h2 id="allocation-comparison">Change from historical starting allocation</h2><span className="panel-caption">Invested holdings excluding cash</span></div>
      {!table.comparisonValid && <p className="allocation-unavailable" role="status">Comparison unavailable. Independently valid current or reference weights are shown where available.</p>}
      <Reasons title="Comparison availability" items={data.comparison_unavailable_reasons} />
      {table.rows.length > 0 ? <div className="holdings-table-scroll" role="region" tabIndex={0} aria-label="Allocation comparison table, scroll horizontally for all columns"><table className="holdings-table"><caption className="sr-only">Current and historical starting weights exclude cash. Changes are percentage points, not investment-policy recommendations.</caption><thead><tr>{['Instrument', 'Current invested weight', 'Historical starting weight', 'Change (pp)', 'Current market value'].map(title => <th scope="col" key={title}>{title}</th>)}</tr></thead><tbody>{table.rows.map(row => <tr key={row.ticker}><th scope="row"><strong>{row.ticker}</strong><span className="instrument-name">{row.instrument}</span></th><td>{formatWeight(row.currentWeight)}</td><td>{formatWeight(row.referenceWeight)}</td><td>{formatAllocationChange(row.change)}</td><td>{formatOptionalCurrency(row.marketValue, data.currency)}</td></tr>)}</tbody></table></div> : <p className="allocation-unavailable">No security observations are available.</p>}
    </section>
    <AllocationComparisonChart key={`${data.effective_valuation_date}/${ref.reference_date}`} data={data} />
    <section className="holdings-panel" aria-labelledby="allocation-classifications"><h2 id="allocation-classifications">Instrument classification exposures</h2><p className="valuation-note">Classifications are current instrument metadata, not ETF look-through exposure, underlying-security overlap or historically versioned sector data.</p>
      {table.currentValid && c.classification_exposures.length > 0 ? <div className="holdings-table-scroll" role="region" tabIndex={0} aria-label="Classification exposure table"><table className="holdings-table"><caption className="sr-only">Classification weights exclude cash.</caption><thead><tr><th scope="col">Classification</th><th scope="col">Holdings</th><th scope="col">Invested weight</th><th scope="col">Market value</th></tr></thead><tbody>{c.classification_exposures.map(row => <tr key={row.classification}><th scope="row">{row.classification}</th><td>{row.holdings_count}</td><td>{formatWeight(row.weight)}</td><td>{formatOptionalCurrency(row.market_value, data.currency)}</td></tr>)}</tbody></table></div> : <p className="allocation-unavailable">No complete invested classification exposures available.</p>}
    </section>
    <Reasons title="Current valuation availability" items={c.unavailable_reasons} /><Reasons title="API data-quality notes" items={data.warnings} />
    <p className="valuation-note">Exact-date pricing applies (maximum age {data.max_price_age_days} calendar days). No implicit FX conversion. The historical reference is reconstructed from stored records, not an immutable investment policy.</p>
  </>;
}
