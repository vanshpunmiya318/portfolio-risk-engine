import { TableScroll } from '../components/TableScroll';
import { useEffect, useRef, useState } from 'react';
import { onSessionLost } from '../api/localSession';
import type { ReactNode } from 'react';
import { createMarkdownLoader, createReportLoader, draftQuery, hasDraftChanges, initialReportDraft, ReportRequestError } from '../api/reports';
import type { MetricValue, ReportAmount, ReportArtifact, ReportQuery, ReportResponse } from '../api/reports';
import type { Coverage } from '../api/performance';
import { QuantityValue } from '../components/QuantityValue';
import { previewJsonDownload, saveDownload } from '../lib/reportDownloads';
import { formatDisplayValue, formatOptionalCurrency, formatValuationDate, formatWeight } from '../lib/format';

const date = (value: string | null) => value === null ? 'Unavailable' : formatValuationDate(value);
const number = (value: number | null, digits = 2) => value === null ? 'Unavailable' : new Intl.NumberFormat('en-GB', { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value);
export function reportMetricLabel(metric: MetricValue, difference = false) {
  if (metric.value === null) return metric.unavailable_reasons.length > 0 && metric.unavailable_reasons.every(r => r.code === 'not_applicable') ? 'Not applicable' : 'Unavailable';
  return formatDisplayValue(metric.value, metric.unit === 'ratio' ? 'ratio' : difference ? 'percentage_points' : 'fraction');
}
function Metric({ value, difference = false }: { value: MetricValue; difference?: boolean }) {
  return <>{reportMetricLabel(value, difference)}
    {value.value === null && <Notes items={value.unavailable_reasons} />}</>;
}
type Note = { code: string; message: string; category?: string; scope?: string; ticker?: string | null; metric?: string | null; start_date?: string | null; end_date?: string | null };
// Current diagnostic contracts have flat JSON fields. Compare every supplied field,
// with keys sorted lexically so object-property order cannot change identity.
// Missing fields and explicit nulls remain distinct; message text alone is never a key.
const noteKey = (item: Note) => JSON.stringify(Object.entries(item).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0));
function Notes({ items, label = 'Availability notes', affectedSections }: {
  items: readonly Note[]; label?: string; affectedSections?: ReadonlyMap<string, readonly string[]>;
}) {
  const unique = [...new Map(items.map(item => [noteKey(item), item])).values()];
  const informational = unique.every(item => ['not_applicable', 'investment_policy_unavailable'].includes(item.code));
  if (!unique.length) return null;
  return <div className={`report-notes ${informational ? 'report-information' : ''}`}>
    <ul aria-label={label}>{unique.map((item, i) => <li key={i}><strong>{item.scope}{item.ticker ? ` · ${item.ticker}` : ''}{item.metric ? ` · ${item.metric}` : ''}</strong>{' '}
      {item.message} <code>{item.code}</code>{item.start_date || item.end_date ? <span> ({date(item.start_date ?? null)} – {date(item.end_date ?? null)})</span> : <small className="cell-note">Date not supplied; see section cutoff.</small>}
      {affectedSections?.has(noteKey(item)) && <span className="cell-note">Affects: {affectedSections.get(noteKey(item))!.join(', ')}</span>}</li>)}</ul></div>;
}
function ObservationCounts({ rows }: { rows: { label: string; counts: string }[] }) {
  return <details className="report-observations"><summary>Observation counts by metric</summary><dl className="report-definitions">
    {rows.map(row => <div key={row.label}><dt>{row.label}</dt><dd>{row.counts}</dd></div>)}
  </dl></details>;
}
type ColumnRole = 'identity' | 'number' | 'status';
function Table({ title, headings, rows, emptyMessage = 'No rows supplied for this section.', metadata, columnRoles }: {
  title: string; headings: string[]; rows: ReactNode[][]; emptyMessage?: string; metadata?: ReactNode; columnRoles?: ColumnRole[];
}) {
  const role = (index: number) => columnRoles?.[index] ?? (index === 0 ? 'identity' : 'number');
  return <section className="holdings-panel report-panel"><h3>{title}</h3>{rows.length ? <TableScroll label={title}>
    <table className="holdings-table report-table"><caption className="sr-only">{title}</caption><thead><tr>{headings.map((h, j) => <th scope="col" key={h} className={`table-${role(j)}`}>{h}</th>)}</tr></thead>
      <tbody>{rows.map((row, i) => <tr key={i}>{row.map((cell, j) => j === 0 ? <th scope="row" key={j} className={`table-${role(j)}`}>{cell}</th> : <td key={j} className={`table-${role(j)}`}>{cell}</td>)}</tr>)}</tbody></table></TableScroll> : <p className="valuation-note" role="status">{emptyMessage}</p>}{metadata}</section>;
}

const amountNames = [
  ['total_equity', 'Total equity'], ['invested_value', 'Invested holdings'], ['cash_balance', 'Separate cash'],
  ['net_contributions', 'Net external contributions through cutoff'], ['remaining_average_cost_basis', 'Remaining average-cost basis'],
] as const;
const metricNames: Record<string, string> = { cumulative_return: 'Cumulative return', annualized_return: 'Annualized return', annualized_volatility: 'Annualized volatility',
  sharpe_ratio: 'Sharpe ratio', maximum_drawdown: 'Maximum drawdown', beta: 'Beta versus selected benchmark', historical_var: 'One-session historical VaR', historical_cvar: 'One-session historical CVaR' };

function CoverageSummary({ label, coverage }: { label: string; coverage: Coverage }) {
  const missing = coverage.missing_sessions;
  return <div><dt>{label}</dt><dd>{coverage.complete === null ? 'Unknown' : coverage.complete ? 'Complete' : 'Incomplete'} coverage · {coverage.valid_valuation_count} / {coverage.valuation_count} valid valuations · {coverage.interval_count} / {coverage.expected_intervals ?? 'unknown'} intervals · Calendar {coverage.calendar_verified ? 'verified' : 'unverified'}</dd>
    <dd>{missing.length} missing sessions{missing.length ? `; ${date(missing[0])} to ${date(missing[missing.length - 1])}` : ''}. A range does not imply uninterrupted coverage.</dd></div>;
}

export function ReportContent({ data }: { data: ReportResponse }) {
  const a = data.allocation, h = data.holdings, p = data.period;
  const metricReasons = (section: 'performance' | 'risk') => [
    ...Object.values(data[section].portfolio_metrics), ...Object.values(data[section].benchmark_metrics),
    ...(section === 'performance' ? Object.values(data.performance.comparison) : []),
  ].flatMap(value => value.value === null ? value.unavailable_reasons : []);
  const performanceReasons = metricReasons('performance'), riskReasons = metricReasons('risk');
  const allocationNotes = a ? [...a.current.unavailable_reasons, ...a.reference.unavailable_reasons,
    ...(a.reference.snapshot?.unavailable_reasons ?? []), ...a.comparison_unavailable_reasons,
    ...a.target_unavailable_reasons, ...a.warnings] : [];
  const remainingNotes = (items: readonly Note[], alreadyShown: readonly Note[]) => {
    const shown = new Set(alreadyShown.map(noteKey));
    return items.filter(item => !shown.has(noteKey(item)));
  };
  const performanceNotes = remainingNotes([...data.performance.warnings, ...data.performance.unavailable_reasons], performanceReasons);
  const riskNotes = remainingNotes([...data.risk.warnings, ...data.risk.unavailable_reasons], riskReasons);
  // Preserve API order within each source: Performance warnings/reasons first,
  // then Risk warnings/reasons. Notes retains the first position for each identity.
  // Track all source memberships, including diagnostics also explained at a figure.
  const affectedSections = new Map<string, string[]>();
  for (const section of ['performance', 'risk'] as const) {
    for (const item of [...data[section].warnings, ...data[section].unavailable_reasons]) {
      const key = noteKey(item), label = section === 'performance' ? 'Performance' : 'Risk';
      const labels = affectedSections.get(key) ?? [];
      if (!labels.includes(label)) labels.push(label);
      affectedSections.set(key, labels);
    }
  }
  const analysisNotes = [...performanceNotes, ...riskNotes];
  const coverageNotes = remainingNotes([...data.warnings, ...data.unavailable_reasons], [
    ...amountNames.flatMap(([key]) => data.endpoint[key].unavailable_reasons),
    ...performanceReasons, ...riskReasons, ...analysisNotes,
    ...(h?.warnings ?? []), ...allocationNotes,
  ]);
  const datesDiffer = (p.requested_start_date !== null && p.requested_start_date !== p.selected_start_date) ||
    (p.requested_end_date !== null && p.requested_end_date !== p.selected_end_date) ||
    p.selected_start_date !== p.effective_baseline_date || p.selected_end_date !== p.effective_end_date ||
    data.endpoint.valuation_date !== p.effective_end_date;
  const holdingsEmpty = h === null ? 'Holdings unavailable; see report diagnostics.' : h.valuation_complete
    ? 'No invested holdings. Zero invested value is distinct from unavailable allocation or concentration.'
    : 'No holdings rows supplied for this incomplete valuation; see availability notes.';
  return <div className="report-content">
    <div className="summary-heading"><div><p className="eyebrow">Validated report</p><h2>{data.name}</h2></div><div className="valuation-date"><span>Exact valuation cutoff</span><strong>{date(data.endpoint.valuation_date)}</strong></div></div>
    <p className="holdings-date-note">Historical stored records, not live market prices. Currency: {data.currency}. Applied benchmark: {data.performance.benchmark_ticker}. Confidence: {formatWeight(data.risk.confidence)}.</p>
    {data.status !== 'ok' && <div className="quality-banner" role="status"><strong>{data.status === 'partial' ? 'Partial report' : 'Financial results unavailable'}</strong><p>Available sections remain visible. Unavailable values are not zero. Validated reports can still be downloaded.</p></div>}
    <nav className="report-section-nav" aria-label="Report sections">{[
      ['financial', 'Financial summary'], ['returns', 'Returns and risk'], ['holdings', 'Holdings'],
      ['allocation', 'Allocation'], ['coverage', 'Coverage'], ['methodology', 'Methodology'], ['downloads', 'Downloads'],
    ].map(([id, label]) => <a key={id} href={`#report-${id}`}>{label}</a>)}</nav>
    <section id="report-financial" className="report-section" aria-labelledby="report-financial-title" tabIndex={-1}>
      <h2 id="report-financial-title">Financial summary</h2>
      <dl className="summary-metrics holdings-metrics report-metrics">{amountNames.map(([key, label]) => {
        const amount: ReportAmount = data.endpoint[key];
        return <div className="summary-metric" key={key}><dt>{label}</dt><dd>{formatOptionalCurrency(amount.value, data.endpoint.currency)}</dd><dd className="metric-note"><Notes items={amount.unavailable_reasons} /></dd></div>;
      })}</dl>
      <details className="performance-period report-date-details" open={datesDiffer}><summary>Reporting date details{datesDiffer ? ' - reporting dates differ' : ''}</summary><dl className="report-definitions">
        <div><dt>Requested boundaries</dt><dd>{p.requested_start_date === null ? 'Automatic history start' : date(p.requested_start_date)} - {p.requested_end_date === null ? 'Automatic history end' : date(p.requested_end_date)}</dd></div>
        <div><dt>Selected trading sessions</dt><dd>{date(p.selected_start_date)} - {date(p.selected_end_date)}</dd></div>
        <div><dt>Effective analysis baseline</dt><dd>{date(p.effective_baseline_date)}</dd></div>
        <div><dt>First return ending date</dt><dd>{date(p.first_return_end_date)}</dd></div>
        <div><dt>Effective analysis end</dt><dd>{date(p.effective_end_date)}</dd></div>
        <div><dt>Exact Holdings/Allocation cutoff</dt><dd>{date(data.endpoint.valuation_date)}</dd></div>
        <div><dt>Historical allocation reference</dt><dd>{date(a?.reference.reference_date ?? null)}</dd></div></dl>
        <p className="valuation-note">An explicit cutoff is not replaced by a prior trading session. Analysis sessions and point-in-time valuation dates may differ.</p></details>
    </section>
    <section id="report-returns" className="report-section" aria-labelledby="report-returns-title" tabIndex={-1}>
      <h2 id="report-returns-title">Returns and risk</h2>
      {data.performance.valuations.length === 0 && data.performance.portfolio_intervals.length === 0 && <p className="summary-state" role="status">No portfolio analysis observations for this selection. Known balances and other sections remain separate.</p>}
      {(['performance', 'risk'] as const).map(section => {
        const rows = Object.entries(data[section].portfolio_metrics).map(([key, value]) => ({
          key, value, benchmark: data[section].benchmark_metrics[key as keyof typeof data[typeof section]['benchmark_metrics']],
        }));
        return <Table key={section} title={section === 'performance' ? 'Performance summary' : 'Risk summary'} headings={['Metric', 'Portfolio', data[section].benchmark_ticker]}
          rows={rows.map(({ key, value, benchmark }) => [metricNames[key], <Metric value={value} />, <Metric value={benchmark} />])}
          metadata={<ObservationCounts rows={rows.map(({ key, value, benchmark }) => ({ label: metricNames[key], counts: `Portfolio: ${value.observations} observations; ${data[section].benchmark_ticker}: ${benchmark.observations} observations` }))} />} />;
      })}
      <Table title="Benchmark comparison" headings={['Metric', 'Value']} rows={Object.entries(data.performance.comparison).map(([key, value]) => [key === 'excess_cumulative_return' ? 'Cumulative return difference' : 'Annualized return difference', <Metric value={value} difference />])}
        metadata={<ObservationCounts rows={Object.entries(data.performance.comparison).map(([key, value]) => ({ label: key === 'excess_cumulative_return' ? 'Cumulative return difference' : 'Annualized return difference', counts: `${value.observations} observations` }))} />} />
      {analysisNotes.length > 0 && <section aria-labelledby="report-analysis-diagnostics-title">
        <h3 id="report-analysis-diagnostics-title">Performance and risk diagnostics</h3>
        <Notes label="Performance and risk diagnostics" items={analysisNotes} affectedSections={affectedSections} />
      </section>}
      <p className="valuation-note">Fractions display as percentages; defined return differences display in percentage points (pp). Ratios retain ratio units. Drawdown and VaR/CVaR signs follow backend conventions; negative tail estimates are not clamped.</p>
    </section>
    <section id="report-holdings" className="report-section" aria-labelledby="report-holdings-title" tabIndex={-1}>
      <h2 id="report-holdings-title">Holdings</h2>
      <Table columnRoles={['identity', 'number', 'number', 'number', 'number', 'status']} title="Holdings summary" headings={['Security', 'Quantity', `Market value (${data.currency})`, 'Remaining basis', 'Unrealized price gain/loss', 'Price date / status']} emptyMessage={holdingsEmpty}
        rows={h?.holdings.map(row => [row.ticker, <QuantityValue value={row.quantity} />, formatOptionalCurrency(row.market_value, data.currency), formatOptionalCurrency(row.remaining_average_cost_basis, data.currency), formatOptionalCurrency(row.unrealized_gain_loss, data.currency), <>{date(row.effective_price_date)}<small className="cell-note">{row.price_status} - Currency {row.currency_status}</small></>]) ?? []} />
      {h !== null && <Notes items={h.warnings} />}
    </section>
    <section id="report-allocation" className="report-section" aria-labelledby="report-allocation-title" tabIndex={-1}>
      <h2 id="report-allocation-title">Allocation and concentration</h2>
      <Table title="Concentration" headings={['Metric', 'Current', 'Historical starting allocation']} emptyMessage="Allocation unavailable; see report diagnostics." rows={a ? [
        ['HHI (index)', number(a.current.concentration.hhi, 4), number(a.reference.snapshot?.concentration.hhi ?? null, 4)],
        ['Effective holdings (count)', number(a.current.concentration.effective_holdings), number(a.reference.snapshot?.concentration.effective_holdings ?? null)],
        ['Largest-position weight', formatWeight(a.current.concentration.largest_position_weight), formatWeight(a.reference.snapshot?.concentration.largest_position_weight ?? null)],
      ] : []} />
      <Table title="Security allocation comparison" headings={['Security', 'Current invested weight', 'Historical starting weight', 'Change (pp)']}
        emptyMessage={a === null ? 'Allocation unavailable; see report diagnostics.' : a.changes === null ? 'Allocation comparison unavailable; see current and reference availability notes.' : 'No security comparison rows supplied.'}
        rows={a?.changes?.map(row => [row.ticker, formatWeight(row.current_weight), formatWeight(row.reference_weight), formatDisplayValue(row.weight_change_fraction, 'percentage_points')]) ?? []} />
      {a && a.changes === null && <Table columnRoles={['identity', 'number', 'number', 'status']} title="Known current allocation" headings={['Security', `Market value (${data.currency})`, 'Invested weight', 'Price status']} emptyMessage={a.current.valuation_complete ? 'No invested securities recorded at this cutoff.' : 'No current security rows supplied for this incomplete valuation.'} rows={a.current.securities.map(row => [row.ticker, formatOptionalCurrency(row.market_value, data.currency), formatWeight(row.weight), row.price_status])} />}
      <Table title="Current instrument classifications" headings={['Classification', `Market value (${data.currency})`, 'Invested weight', 'Holdings']}
        emptyMessage={a === null ? 'Allocation unavailable; see report diagnostics.' : a.current.valuation_complete && a.current.securities.length === 0 ? 'No invested securities to classify.' : 'No classification rows available; see allocation availability notes.'}
        rows={a?.current.classification_exposures.map(row => [row.classification, formatOptionalCurrency(row.market_value, data.currency), formatWeight(row.weight), row.holdings_count]) ?? []} />
      <p className="valuation-note">Allocation weights exclude cash. Historical starting allocation is reconstructed at the end of the first recorded buy date; it is not an investment-policy target or rebalancing recommendation. Classifications are current instrument metadata, not ETF look-through exposures, underlying-security overlap or historically versioned classifications.</p>
      {a && <><p className="valuation-note">Reference date: {date(a.reference.reference_date)}. Reference invested value: {formatOptionalCurrency(a.reference.snapshot?.invested_value ?? null, data.currency)}.</p><Notes items={allocationNotes} /></>}
    </section>
    <section id="report-coverage" className="holdings-panel report-panel report-section" aria-labelledby="report-coverage-title" tabIndex={-1}><h2 id="report-coverage-title">Coverage and availability</h2><dl className="performance-coverage"><CoverageSummary label="Portfolio" coverage={data.performance.portfolio_coverage} /><CoverageSummary label={`Benchmark ${data.performance.benchmark_ticker}`} coverage={data.performance.benchmark_coverage} /></dl>
      <Notes items={coverageNotes} />
      <p className="valuation-note">Observation arrays are retained in preview JSON. They are summarized here rather than printed individually.</p></section>
    <section id="report-methodology" className="holdings-panel report-panel report-section" aria-labelledby="report-methodology-title" tabIndex={-1}><h2 id="report-methodology-title">Methodology and provenance</h2>
      <p className="valuation-note">{data.performance.benchmark_ticker} benchmark: {data.performance.methodology.benchmark_return_basis === 'raw_close_price_return; dividends_not_reinvested' ? 'raw-close price returns, excluding dividends and dividend reinvestment.' : data.performance.methodology.benchmark_return_basis} No implicit FX conversion. Cash flows use the backend's documented end-of-period convention. These are historical estimates, not predictions.</p>
      <p className="valuation-note">Corporate-action checks depend on the price source; complete price coverage alone does not establish corporate-action coverage.</p>
      <details className="performance-methodology"><summary>Corporate-action coverage by price source</summary>
        <p>Active holdings valued with Tiingo raw closes require continuous stored Tiingo coverage from first portfolio trade through cutoff. Other sources use adjusted close and do not receive the same Tiingo coverage check. Complete price coverage and latest price dates do not establish corporate-action coverage.</p>
      </details>
      <p className="valuation-note">Dataset origin is not provided by this report; no real-account or simulated-account claim is inferred. Legacy Attribution is excluded.</p>
      {(['performance', 'risk'] as const).map(section => <details className="performance-methodology" key={section}><summary>{section === 'performance' ? 'Performance' : 'Risk'} calculation conventions</summary><dl className="report-definitions">{Object.entries({ ...data[section].methodology, ...data[section].metric_conventions }).map(([key, value]) => <div key={key}><dt>{key.replaceAll('_', ' ')}</dt><dd>{String(value ?? 'Unavailable')}</dd></div>)}</dl></details>)}
      <details className="performance-methodology"><summary>Report provenance and reproducibility</summary><dl className="report-definitions">{Object.entries(data.provenance).map(([key, value]) => <div key={key}><dt>{key.replaceAll('_', ' ')}</dt><dd>{key === 'persisted_snapshot_id' && value === null ? 'None (no persisted snapshot)' : Array.isArray(value) ? value.join(', ') || 'None recorded' : String(value ?? 'Unavailable')}</dd></div>)}</dl></details>
      <p className="valuation-note">Cutoff dates are reporting dates, not snapshot identifiers. {data.provenance.reproducibility}</p>
    </section>
  </div>;
}

type State = { status: 'loading' } | { status: 'error'; message: string; details: string[] } | { status: 'ready'; artifact: ReportArtifact };
const errorMessage = (error: unknown) => error instanceof Error ? error.message : 'Unable to retrieve report.';
export function Reports({ portfolioId = 1 }: { portfolioId?: number }) {
  const [draft, setDraft] = useState({ ...initialReportDraft });
  const [applied, setApplied] = useState<ReportQuery>(() => draftQuery(initialReportDraft));
  const [state, setState] = useState<State>({ status: 'loading' });
  const [validation, setValidation] = useState(''); const [attempt, setAttempt] = useState(0);
  const [markdown, setMarkdown] = useState<{ status: 'idle' | 'loading' | 'error'; message?: string }>({ status: 'idle' });
  const [jsonError, setJsonError] = useState('');
  const loaders = useRef<{ preview: ReturnType<typeof createReportLoader>; markdown: ReturnType<typeof createMarkdownLoader> } | null>(null);
  if (!loaders.current) loaders.current = { preview: createReportLoader(), markdown: createMarkdownLoader() };
  const artifact = state.status === 'ready' && state.artifact.id === portfolioId && JSON.stringify(state.artifact.query) === JSON.stringify(applied) ? state.artifact : null;
  const activeArtifact = useRef<ReportArtifact | null>(null); activeArtifact.current = artifact;
  useEffect(() => {
    const requests = loaders.current!;
    setState({ status: 'loading' }); setMarkdown({ status: 'idle' }); setJsonError('');
    void requests.preview.load({ id: portfolioId, query: applied }, result => setState({ status: 'ready', artifact: result }), error => setState({ status: 'error', message: errorMessage(error), details: error instanceof ReportRequestError ? error.details : [] }));
    const cancel = () => { requests.preview.cancel(); requests.markdown.cancel(); };
    const unsubscribe = onSessionLost(() => { activeArtifact.current = null; cancel(); });
    return () => { unsubscribe(); cancel(); };
  }, [portfolioId, applied, attempt]);
  function invalidate() { loaders.current!.preview.cancel(); loaders.current!.markdown.cancel(); activeArtifact.current = null; setState({ status: 'loading' }); setMarkdown({ status: 'idle' }); setJsonError(''); }
  function apply() {
    try { const query = draftQuery(draft); setValidation(''); invalidate(); setApplied(query); }
    catch (error) { setValidation(errorMessage(error)); }
  }
  function downloadJson() {
    if (!artifact) return;
    try { const download = previewJsonDownload(artifact); saveDownload(download.blob, download.filename); setJsonError(''); }
    catch (error) { setJsonError(errorMessage(error)); }
  }
  function downloadMarkdown() {
    if (!artifact) return;
    const current = artifact; setMarkdown({ status: 'loading' });
    void loaders.current!.markdown.load(current, result => {
      if (activeArtifact.current !== current) return;
      try { saveDownload(new Blob([result.text], { type: 'text/markdown;charset=utf-8' }), result.filename); setMarkdown({ status: 'idle' }); }
      catch (error) { setMarkdown({ status: 'error', message: errorMessage(error) }); }
    }, error => { if (activeArtifact.current === current) setMarkdown({ status: 'error', message: [errorMessage(error), ...(error instanceof ReportRequestError ? error.details : [])].join(' ')}); });
  }
  const field = (name: keyof typeof draft, value: string) => setDraft(current => ({ ...current, [name]: value }));
  return <>
    <section className="page-intro performance-intro"><p className="eyebrow">Portfolio intelligence</p><h1>Reports</h1><p className="lede">One validated view of performance, risk and allocation, with the coverage behind every result.</p></section>
    <form className="performance-filters risk-filters report-filters" aria-label="Report controls" onSubmit={e => { e.preventDefault(); apply(); }}>
      <button type="button" className="summary-button" onClick={() => { setDraft(current => ({ ...current, start: '', end: '' })); setValidation(''); }}>Full history</button>
      <label>Start date<input type="date" value={draft.start} onChange={e => field('start', e.target.value)} /></label>
      <label>End date<input type="date" value={draft.end} onChange={e => field('end', e.target.value)} /></label>
      <label>Benchmark<input type="text" value={draft.benchmark} onChange={e => field('benchmark', e.target.value)} /></label>
      <label>Confidence level<select value={draft.confidence} onChange={e => field('confidence', e.target.value)}><option value=".90">90%</option><option value=".95">95%</option><option value=".99">99%</option></select></label>
      <button type="submit" className="summary-button">Apply</button>
      <p className="filter-note">Blank dates use automatic history boundaries. Full history clears draft dates only. All changes take effect on Apply.</p>
      {hasDraftChanges(draft, applied) && <p className="filter-note" role="status">Unapplied changes. Preview and downloads still use the applied parameters.</p>}
      {validation && <p className="filter-error" role="alert">{validation}</p>}
    </form>
    <section aria-label="Report preview" aria-busy={state.status === 'loading' || (state.status === 'ready' && !artifact)}>
      {(state.status === 'loading' || (state.status === 'ready' && !artifact)) && <div className="summary-state" role="status"><h2>Loading report…</h2><p>Preparing the validated financial sections. Full history may take a few seconds.</p></div>}
      {state.status === 'error' && <div className="summary-state" role="alert"><h2>Unable to load report</h2><p>{state.message}</p><ul>{state.details.map((item, i) => <li key={i}>{item}</li>)}</ul><button className="summary-button" onClick={() => { invalidate(); setAttempt(n => n + 1); }}>Try again</button></div>}
      {artifact && <ReportContent data={artifact.report} />}
    </section>
    <section id="report-downloads" className="holdings-panel report-downloads report-section" tabIndex={-1} aria-label="Report downloads"><h2>Take the report with you</h2>
      <p className="valuation-note">Applied selection: {applied.start_date ?? 'Automatic history start'} – {applied.end_date ?? 'Automatic history end'} · {applied.benchmark} · {formatWeight(applied.confidence)} confidence.</p>
      <div className="report-actions"><button className="summary-button" disabled={!artifact} onClick={downloadJson}>Download preview JSON</button>
        <button className="summary-button" disabled={!artifact || markdown.status === 'loading'} onClick={downloadMarkdown}>{markdown.status === 'loading' ? 'Generating fresh Markdown…' : 'Generate fresh Markdown'}</button></div>
      <p className="valuation-note">JSON saves this preview exactly, without another request. Markdown is generated afresh from the applied parameters and may differ if stored data or automatic date boundaries change.</p>
      {markdown.status === 'loading' && <p role="status" className="valuation-note">Generating a fresh Markdown report. Preview JSON remains available.</p>}
      {markdown.status === 'error' && <p role="alert" className="filter-error">{markdown.message} The preview and JSON download remain available. Use Generate fresh Markdown to retry.</p>}
      {jsonError && <p role="alert" className="filter-error">{jsonError}</p>}
    </section>
  </>;
}
