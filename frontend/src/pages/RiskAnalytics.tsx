import { useEffect, useRef, useState } from 'react';
import { createRiskLoader, riskQuery, RiskRequestError } from '../api/risk';
import type { RiskResponse, RiskQuery, RiskMetrics } from '../api/risk';
import type { MetricValue } from '../api/performance';
import { formatValuationDate, formatWeight } from '../lib/format';
import { TimeSeriesChart } from '../components/TimeSeriesChart';
import { riskDrawdownLines } from '../lib/riskCharts';

export function riskMetricLabel(metric: MetricValue): string {
  if (metric.value === null) return 'Unavailable';
  return metric.unit === 'ratio' ? new Intl.NumberFormat('en-GB', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(metric.value)
    : formatWeight(metric.value === 0 ? 0 : metric.value);
}
function Metric({ value }: { value: MetricValue }) {
  return <><span>{riskMetricLabel(value)}</span><small className="risk-observations">{value.observations} observations</small>
    {value.value === null && <ul className="metric-reasons">{value.unavailable_reasons.map((reason, i) => <li key={i}>{reason.message} <code>{reason.code}</code></li>)}</ul>}</>;
}
const date = (value: string | null) => value ? formatValuationDate(value) : 'Unavailable';
export function RiskAnalytics({ portfolioId = 1, portfolioRevision }: { portfolioId?: number; portfolioRevision?: string }) {
  const [state, setState] = useState<{ status: 'loading' } | { status: 'error'; message: string; details: string[] } | { status: 'ready'; data: RiskResponse }>({ status: 'loading' });
  const [start, setStart] = useState(''); const [end, setEnd] = useState('');
  const [confidence, setConfidence] = useState('.95'); const [validation, setValidation] = useState('');
  const [applied, setApplied] = useState<RiskQuery>({ confidence: .95 }); const [attempt, setAttempt] = useState(0);
  const loader = useRef<ReturnType<typeof createRiskLoader> | null>(null);
  if (!loader.current) loader.current = createRiskLoader();
  useEffect(() => {
    const requests = loader.current!; setState({ status: 'loading' });
    void requests.load(portfolioId, applied, data => setState({ status: 'ready', data }), error => setState({ status: 'error',
      message: error instanceof Error ? error.message : 'Unable to retrieve risk analysis.', details: error instanceof RiskRequestError ? error.details : [] }));
    return () => requests.cancel();
  }, [portfolioId, portfolioRevision, applied, attempt]);
  function apply() {
    const query = { ...(start ? { start_date: start } : {}), ...(end ? { end_date: end } : {}), confidence: Number(confidence) };
    try { riskQuery(query); } catch (error) { setValidation(error instanceof Error ? error.message : 'Invalid selection.'); return; }
    setValidation(''); setApplied(query);
  }
  return <>
    <section className="page-intro performance-intro"><p className="eyebrow">Portfolio intelligence</p><h1>Risk Analytics</h1><p className="lede">Historical risk estimates, benchmark context and the coverage behind each measure.</p></section>
    <form className="performance-filters risk-filters" aria-label="Risk analysis controls" onSubmit={event => { event.preventDefault(); apply(); }}>
      <button type="button" className="summary-button" onClick={() => { setStart(''); setEnd(''); setValidation(''); }}>Full history</button>
      <label>Start date<input type="date" value={start} onChange={e => setStart(e.target.value)} /></label>
      <label>End date<input type="date" value={end} onChange={e => setEnd(e.target.value)} /></label>
      <label>Confidence level<select value={confidence} onChange={e => setConfidence(e.target.value)}><option value=".90">90%</option><option value=".95">95%</option><option value=".99">99%</option></select></label>
      <button type="submit" className="summary-button">Apply</button><p className="filter-note">Blank dates use automatic history boundaries. Full history clears draft dates only. Changes take effect on Apply.</p>
      {(start !== (applied.start_date ?? '') || end !== (applied.end_date ?? '') || Number(confidence) !== applied.confidence) && <p className="filter-note" role="status">Unapplied changes. Displayed results still use the applied dates and confidence.</p>}
      {validation && <p className="filter-error" role="alert">{validation}</p>}
    </form>
    <section aria-label="Risk analysis data" aria-busy={state.status === 'loading'}>
      {state.status === 'loading' && <div className="summary-state" role="status"><h2>Loading risk analysis…</h2><p>Preparing verified historical return intervals.</p></div>}
      {state.status === 'error' && <div className="summary-state" role="alert"><h2>Risk analysis unavailable</h2><p>{state.message}</p><ul>{state.details.map((detail, i) => <li key={i}>{detail}</li>)}</ul><button className="summary-button" onClick={() => setAttempt(n => n + 1)}>Try again</button></div>}
      {state.status === 'ready' && <RiskContent data={state.data} />}
    </section>
  </>;
}
export function RiskContent({ data }: { data: RiskResponse }) {
  const p = data.period;
  const drawdownLines = riskDrawdownLines(data);
  const notes = [...data.warnings, ...data.unavailable_reasons];
  const informationalOnly = notes.length > 0 && notes.every(note => note.code === 'not_applicable');
  const primary: [keyof RiskMetrics, string][] = [['annualized_volatility', 'Annualized volatility'], ['beta', `Beta versus ${data.benchmark_ticker}`], ['historical_var', 'One-session historical VaR'], ['historical_cvar', 'One-session historical CVaR']];
  const explanations: Record<keyof RiskMetrics, string> = {
    annualized_volatility: 'How widely historical session returns varied, expressed at an annual scale. This is not a forecast.',
    beta: `Historical sensitivity to ${data.benchmark_ticker} returns over matched intervals. A beta of 1 indicates similar sensitivity, not the same return.`,
    historical_var: 'A percentile-based estimate from historical returns.',
    historical_cvar: 'Average loss in the worst historical tail. Positive values indicate an estimated loss. Negative values indicate an average gain within that tail.',
    sharpe_ratio: `Historical return relative to variability, using a ${formatWeight(data.metric_conventions.risk_free_rate)} annual risk-free rate. A ratio, not a percentage.`,
    maximum_drawdown: 'Largest historical fall from a running peak within this reporting period, including the baseline. Displayed as a positive loss magnitude.',
  };
  const differs = (p.requested_start_date !== null && p.requested_start_date !== p.effective_baseline_date) || (p.requested_end_date !== null && p.requested_end_date !== p.effective_end_date);
  return <>
    <div className="summary-heading"><div><p className="eyebrow">Historical risk</p><h2><bdi>{data.name}</bdi></h2></div><div className="valuation-date"><span>Effective reporting period</span><strong>{date(p.effective_baseline_date)} – {date(p.effective_end_date)}</strong></div></div>
    <p className="holdings-date-note">Historical analysis, not live market risk. Applied confidence: {formatWeight(data.confidence)}. Benchmark: {data.benchmark_ticker}.</p>
    <details className="performance-period" open={differs}><summary>Reporting date details{differs ? ' · requested and effective dates differ' : ''}</summary><dl>
      <div><dt>Requested boundaries</dt><dd>{p.requested_start_date ? date(p.requested_start_date) : 'Available history'} – {p.requested_end_date ? date(p.requested_end_date) : 'Available history'}</dd></div>
      <div><dt>Selected boundaries</dt><dd>{date(p.selected_start_date)} – {date(p.selected_end_date)}</dd></div>
      <div><dt>Effective baseline / reporting end</dt><dd>{date(p.effective_baseline_date)} – {date(p.effective_end_date)}</dd></div>
      <div><dt>First return ending date</dt><dd>{date(p.first_return_end_date)}</dd></div></dl><p>Metrics use the effective reporting period. Requested non-session dates and unavailable observations can affect usable boundaries; consult coverage notes.</p></details>
    {data.status !== 'ok' && <div className="quality-banner" role="status"><strong>{data.status === 'partial' ? 'Partial risk analysis' : 'Risk estimates unavailable'}</strong><p>Valid portfolio metrics remain visible when benchmark coverage is unavailable. Unavailable values are not zero.</p></div>}
    {data.valuations.length === 0 && data.portfolio_intervals.length === 0 && <div className="summary-state" role="status"><h2>No risk observations</h2><p>No portfolio valuations or adjusted returns were reported for this selection.</p></div>}
    <dl className="summary-metrics performance-metrics">{primary.map(([key, title]) => <div className="summary-metric" key={key}><dt>{title}</dt><dd><Metric value={data.portfolio_metrics[key]} /></dd>
      {key !== 'beta' && <dd className="metric-note">{data.benchmark_ticker}: <Metric value={data.benchmark_metrics[key]} /></dd>}<dd className="risk-explanation">{explanations[key]}</dd></div>)}</dl>
    <p className="valuation-note">VaR and CVaR are historical one-session estimates at {formatWeight(data.confidence)} confidence. Actual losses can exceed these estimates. Negative estimates are shown unchanged.</p>
    <section className="holdings-panel" aria-labelledby="risk-secondary"><h2 id="risk-secondary">Risk in context</h2><dl className="performance-comparison">
      {(['sharpe_ratio', 'maximum_drawdown'] as const).map(key => <div key={key}><dt>{key === 'sharpe_ratio' ? 'Sharpe ratio' : 'Maximum drawdown · positive loss magnitude'}</dt><dd>Portfolio: <Metric value={data.portfolio_metrics[key]} /></dd><dd>{data.benchmark_ticker}: <Metric value={data.benchmark_metrics[key]} /></dd><dd className="risk-explanation">{explanations[key]}</dd></div>)}
    </dl><p className="panel-caption">Beta and Sharpe are ratios. Benchmark self-beta is not applicable. {data.comparison_eligible ? 'Comparison intervals match.' : 'Benchmark comparison is unavailable; see metric-specific reasons.'}</p></section>
    <TimeSeriesChart key={`${p.effective_baseline_date}/${p.effective_end_date}`} title="Historical drawdown"
      description="Backend-supplied drawdown at the selected date. Zero represents a previous peak; larger percentages represent deeper losses. Maximum drawdown above is the largest loss across the entire reporting period."
      lines={drawdownLines} unit="fraction" start={p.effective_baseline_date} end={p.effective_end_date} />
    {drawdownLines.some(line => line.points.length === 1) && <p className="valuation-note" role="status">Only one dated observation is available for a series. A point is shown; no drawdown trajectory is implied.</p>}
    <section className="holdings-panel" aria-labelledby="risk-methodology"><h2 id="risk-methodology">Coverage and methodology</h2>
      <dl className="performance-coverage">{([['Portfolio', data.portfolio_coverage], [data.benchmark_ticker, data.benchmark_coverage]] as const).map(([name, coverage]) => <div key={name}><dt>{name}</dt><dd>{coverage.complete === true ? 'Complete' : coverage.complete === false ? 'Incomplete' : 'Unknown'} coverage · {coverage.valid_valuation_count} / {coverage.valuation_count} valid valuations · {coverage.interval_count}{coverage.expected_intervals !== null ? ` / ${coverage.expected_intervals}` : ''} return intervals · Calendar {coverage.calendar_verified ? 'verified' : 'unverified'}</dd>{coverage.missing_sessions.length > 0 && <dd>Missing sessions: {coverage.missing_sessions.map(date).join(', ')}</dd>}</div>)}</dl>
      <p className="valuation-note">{data.benchmark_ticker}: {data.methodology.benchmark_return_basis === 'raw_close_price_return; dividends_not_reinvested' ? 'raw-close price returns excluding dividends and dividend reinvestment.' : data.methodology.benchmark_return_basis} No implicit FX conversion.</p>
      <details className="performance-methodology"><summary>Calculation conventions and calendar provenance</summary>
        <p>Annualization uses {data.metric_conventions.annualization_sessions} sessions per year. Sample volatility uses ddof {data.metric_conventions.volatility_ddof}. CVaR uses fractional tail weights. Risk-free rate: {formatWeight(data.metric_conventions.risk_free_rate)}.</p>
        <dl>{Object.entries({ 'VaR': data.metric_conventions.var_method, 'CVaR': data.metric_conventions.cvar_method, 'Horizon': data.metric_conventions.tail_horizon, 'Cash-flow timing': data.methodology.external_flow_timing, 'Portfolio prices': data.methodology.portfolio_price_basis, 'Price age (calendar days)': String(data.methodology.price_age_days), 'Calendar': data.methodology.calendar_source ?? 'Unavailable' }).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
      </details>
      {notes.length > 0 && <div className={informationalOnly ? "risk-information" : "quality-warnings"}><h3>Analysis notes</h3><ul>{notes.map((reason, i) => <li key={i}><strong>{reason.scope}{reason.metric ? ` · ${reason.metric}` : ''}: </strong>{reason.message} <code>{reason.code}</code></li>)}</ul></div>}
    </section>
  </>;
}
