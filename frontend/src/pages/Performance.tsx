import { useEffect, useRef, useState } from 'react';
import { createPerformanceLoader, PerformanceRequestError, performanceQuery } from '../api/performance';
import type { Coverage, MetricValue, PerformanceQuery, PerformanceResponse } from '../api/performance';
import { formatValuationDate, formatWeight } from '../lib/format';
import { TimeSeriesChart } from '../components/TimeSeriesChart';
import { performanceChartLines } from '../lib/performanceCharts';

type State = { status: 'loading' } | { status: 'error'; message: string; details: string[] } |
  { status: 'ready'; data: PerformanceResponse };
const dateLabel = (day: string | null) => day ? formatValuationDate(day) : 'Unavailable';
export function metricLabel(metric: MetricValue): string {
  return metric.value === null ? 'Unavailable' : metric.unit === 'ratio'
    ? new Intl.NumberFormat('en-GB', { maximumFractionDigits: 2, minimumFractionDigits: 2 }).format(metric.value)
    : formatWeight(metric.value);
}
export function differenceLabel(metric: MetricValue): string {
  return metric.value === null ? 'Unavailable' : `${metric.value > 0 ? '+' : ''}${new Intl.NumberFormat('en-GB', {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  }).format(metric.value * 100)} percentage points`;
}
function Metric({ metric, difference = false }: { metric: MetricValue; difference?: boolean }) {
  return <><span>{difference ? differenceLabel(metric) : metricLabel(metric)}</span>{metric.value === null && <ul className="metric-reasons">
    {metric.unavailable_reasons.map((reason, index) => <li key={index}>{reason.message} <code>{reason.code}</code></li>)}
  </ul>}</>;
}
export function Performance({ portfolioId = 1 }: { portfolioId?: number }) {
  const [state, setState] = useState<State>({ status: 'loading' });
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [validation, setValidation] = useState('');
  const [applied, setApplied] = useState<PerformanceQuery>({});
  const [attempt, setAttempt] = useState(0);
  const loader = useRef<ReturnType<typeof createPerformanceLoader> | null>(null);
  if (!loader.current) loader.current = createPerformanceLoader();
  useEffect(() => {
    const requests = loader.current!;
    setState({ status: 'loading' });
    void requests.load(portfolioId, applied, data => setState({ status: 'ready', data }), error => setState({
      status: 'error', message: error instanceof Error ? error.message : 'Unable to load Performance data.',
      details: error instanceof PerformanceRequestError ? error.details : [],
    }));
    return () => requests.cancel();
  }, [portfolioId, applied, attempt]);
  const retry = () => setAttempt(value => value + 1);
  function apply() {
    const query = { ...(start ? { start_date: start } : {}), ...(end ? { end_date: end } : {}) };
    try { performanceQuery(query); } catch (error) {
      setValidation(error instanceof Error ? error.message : 'Invalid date range.'); return;
    }
    setValidation(''); setApplied(query);
  }
  return <>
    <section className="page-intro performance-intro"><p className="eyebrow">Portfolio intelligence</p><h1>Performance</h1>
      <p className="lede">Investment returns and benchmark context, grounded in verified historical coverage.</p></section>
    <form className="performance-filters" onSubmit={event => { event.preventDefault(); apply(); }} aria-label="Performance date range">
      <button type="button" className="summary-button" onClick={() => {
        setStart(''); setEnd(''); setValidation('');
      }}>Full history</button>
      <label>Start date<input type="date" value={start} onChange={event => setStart(event.target.value)} /></label>
      <label>End date<input type="date" value={end} onChange={event => setEnd(event.target.value)} /></label>
      <button className="summary-button" type="submit">Apply dates</button>
      <p className="filter-note">Blank boundaries use available history. Full history clears draft dates only. Changes take effect on Apply dates.</p>
      {(start !== (applied.start_date ?? '') || end !== (applied.end_date ?? '')) && <p className="filter-note" role="status">Unapplied changes. Displayed results still use the applied dates.</p>}
      {validation && <p role="alert" className="filter-error">{validation}</p>}
    </form>
    <section aria-label="Performance data" aria-busy={state.status === 'loading'}>
      {state.status === 'loading' && <div className="summary-state" role="status"><h2>Loading performance…</h2>
        <p>Preparing historical valuations and verified return intervals. This may take a few seconds.</p></div>}
      {state.status === 'error' && <div className="summary-state" role="alert"><h2>Performance unavailable</h2>
        <p>{state.message}</p>{state.details.length > 0 && <ul>{state.details.map((detail, index) => <li key={index}>{detail}</li>)}</ul>}
        <button className="summary-button" onClick={retry}>Try again</button></div>}
      {state.status === 'ready' && <PerformanceContent data={state.data} />}
    </section>
  </>;
}

function CoverageSummary({ label, coverage }: { label: string; coverage: Coverage }) {
  return <div><dt>{label}</dt><dd>{coverage.complete === true ? 'Complete' : coverage.complete === false ? 'Incomplete' : 'Unknown'} coverage
    {' · '}{coverage.valid_valuation_count} / {coverage.valuation_count} valid valuations
    {' · '}{coverage.interval_count}{coverage.expected_intervals !== null ? ` / ${coverage.expected_intervals}` : ''} return intervals
    {' · '}Calendar {coverage.calendar_verified ? 'verified' : 'unverified'}
    {' · '}{coverage.daily_eligible ? 'Daily metrics eligible' : 'Daily metrics not eligible'}</dd>
    {coverage.missing_sessions.length > 0 && <dd>Missing sessions: {coverage.missing_sessions.map(dateLabel).join(', ')}</dd>}</div>;
}
export function PerformanceContent({ data }: { data: PerformanceResponse }) {
  const p = data.period;
  const benchmarkMethodology = data.methodology.benchmark_return_basis === 'raw_close_price_return; dividends_not_reinvested'
    ? 'raw-close price return, excluding dividends and dividend reinvestment.' : data.methodology.benchmark_return_basis;
  const charts = performanceChartLines(data);
  const metrics = [
    ['cumulative_return', 'Cumulative return'], ['annualized_return', 'Annualized return'],
    ['annualized_volatility', 'Annualized volatility'], ['maximum_drawdown', 'Maximum drawdown'],
  ] as const;
  const issues = [...data.warnings, ...data.unavailable_reasons];
  const empty = data.valuations.length === 0 && data.portfolio_intervals.length === 0;
  const changed = (p.requested_start_date !== null && p.requested_start_date !== p.effective_baseline_date) ||
    (p.requested_end_date !== null && p.requested_end_date !== p.effective_end_date);
  return <>
    <div className="summary-heading"><div><p className="eyebrow">Historical analysis</p><h2>{data.name}</h2></div>
      <div className="valuation-date"><span>Effective reporting period</span><strong>{dateLabel(p.effective_baseline_date)} – {dateLabel(p.effective_end_date)}</strong></div></div>
    <p className="holdings-date-note">Historical results, not today’s live performance. Benchmark: {data.benchmark_ticker}.</p>
    <details className="performance-period" open={changed}><summary>Reporting date details{changed ? ' · requested and effective dates differ' : ''}</summary>
      <dl><div><dt>Requested boundaries</dt><dd>{p.requested_start_date ? dateLabel(p.requested_start_date) : 'Available history'} – {p.requested_end_date ? dateLabel(p.requested_end_date) : 'Available history'}</dd></div>
        <div><dt>Selected boundaries</dt><dd>{dateLabel(p.selected_start_date)} – {dateLabel(p.selected_end_date)}</dd></div>
        <div><dt>Effective valuation baseline</dt><dd>{dateLabel(p.effective_baseline_date)}</dd></div>
        <div><dt>First return ending date</dt><dd>{dateLabel(p.first_return_end_date)}</dd></div>
        <div><dt>Effective reporting end</dt><dd>{dateLabel(p.effective_end_date)}</dd></div></dl>
      {changed && <p>The requested boundaries and usable valuation boundaries differ. Metrics use the effective baseline and end shown above. Consult coverage notes for unavailable observations.</p>}
    </details>
    {data.status !== 'ok' && <div className="quality-banner" role="status"><strong>{data.status === 'partial' ? 'Partial analysis' : 'Analysis unavailable'}</strong>
      <p>Available portfolio metrics remain visible independently of benchmark coverage. Unavailable metrics are not zero.</p></div>}
    {empty && <div className="summary-state" role="status"><h2>No analysis observations</h2><p>No portfolio valuations or returns were reported for this selection. Try a different date range.</p></div>}
    <dl className="summary-metrics performance-metrics">{metrics.map(([key, title]) => <div className="summary-metric" key={key}><dt>{title}</dt>
      <dd><Metric metric={data.portfolio_metrics[key]} /></dd><dd className="metric-note">{data.benchmark_ticker}: <Metric metric={data.benchmark_metrics[key]} /></dd>
      {key === 'maximum_drawdown' && <dd className="metric-note">Positive loss magnitude, including the baseline</dd>}
    </div>)}</dl>
    <section className="holdings-panel" aria-labelledby="performance-comparison"><div className="panel-heading"><h2 id="performance-comparison">Benchmark comparison</h2>
      <span className="panel-caption">{data.comparison_eligible ? 'Matching verified intervals' : 'Comparison unavailable; see reasons below'}</span></div>
      <p className="panel-caption">{data.benchmark_ticker} benchmark methodology: {benchmarkMethodology}</p>
      <dl className="performance-comparison">
        <div><dt>Cumulative return difference</dt><dd><Metric metric={data.comparison.excess_cumulative_return} difference /></dd></div>
        <div><dt>Annualized return difference</dt><dd><Metric metric={data.comparison.excess_annualized_return} difference /></dd></div>
        <div><dt>Portfolio Sharpe ratio</dt><dd><Metric metric={data.portfolio_metrics.sharpe_ratio} /></dd></div>
        <div><dt>{data.benchmark_ticker} Sharpe ratio</dt><dd><Metric metric={data.benchmark_metrics.sharpe_ratio} /></dd></div>
      </dl><p className="panel-caption">Returns display as percentages; differences are portfolio minus benchmark, in percentage points. Sharpe is a ratio.</p>
    </section>
    <div className="performance-charts" key={`${p.effective_baseline_date}/${p.effective_end_date}`}>
      <TimeSeriesChart title="Portfolio growth" description={`Cash-flow-adjusted normalized wealth, including the initial baseline. ${data.benchmark_ticker} benchmark: ${benchmarkMethodology}`}
        lines={charts.growth} unit="wealth_index" start={p.effective_baseline_date} end={p.effective_end_date} />
      <TimeSeriesChart title="Portfolio drawdown" description="Loss from the running wealth peak, including the initial baseline. Derived only from complete verified normalized wealth."
        lines={charts.drawdown} unit="fraction" start={p.effective_baseline_date} end={p.effective_end_date} compact />
    </div>
    <section className="holdings-panel" aria-labelledby="analysis-coverage"><h2 id="analysis-coverage">Coverage and methodology</h2>
      <dl className="performance-coverage"><CoverageSummary label="Portfolio" coverage={data.portfolio_coverage} />
        <CoverageSummary label={data.benchmark_ticker} coverage={data.benchmark_coverage} /></dl>
      <p className="valuation-note">Exact-date price policy: maximum age {data.methodology.price_age_days} calendar days. No implicit currency conversion.</p>
      <details className="performance-methodology"><summary>Calculation details and calendar provenance</summary>
        <p>Returns are decimal fractions displayed as percentages. Annualization uses {data.metric_conventions.annualization_sessions} verified trading sessions per year;
          volatility uses sample standard deviation (ddof {data.metric_conventions.volatility_ddof}). Risk-free rate: {formatWeight(data.metric_conventions.risk_free_rate)}.</p><dl>
        <div><dt>Cash-flow timing</dt><dd>{data.methodology.external_flow_timing}</dd></div>
        <div><dt>Portfolio price basis</dt><dd>{data.methodology.portfolio_price_basis}</dd></div>
        <div><dt>Calendar provenance</dt><dd>{data.methodology.calendar_source ?? 'Unavailable'}</dd></div></dl></details>
      {issues.length > 0 && <div className="quality-warnings"><h3>Analysis notes</h3><ul>{issues.map((issue, index) => <li key={index}>
        <strong>{issue.scope}{issue.metric ? ` · ${issue.metric}` : ''}{issue.ticker ? ` · ${issue.ticker}` : ''}: </strong>
        {issue.message} <code>{issue.code}</code>{(issue.start_date || issue.end_date) && <span> ({dateLabel(issue.start_date)} – {dateLabel(issue.end_date)})</span>}</li>)}</ul></div>}
    </section>
  </>;
}
