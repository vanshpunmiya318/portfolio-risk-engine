import assert from 'node:assert/strict';
import { test, after } from 'node:test';
import { createPerformanceLoader, getPortfolioPerformance, isPerformanceResponse, performanceQuery, PerformanceRequestError } from '../src/api/performance.ts';

const originalFetch = globalThis.fetch;
after(() => { globalThis.fetch = originalFetch; });
const reason = () => ({ code: 'insufficient_coverage', message: 'Verified benchmark coverage is unavailable.', scope: 'benchmark', category: 'coverage', metric: null, start_date: null, end_date: null, ticker: 'SPY' });
const metric = (value = .1, unit = 'fraction') => ({ value, unit, observations: 1, unavailable_reasons: value === null ? [reason()] : [] });
const metrics = () => ({ cumulative_return: metric(), annualized_return: metric(), annualized_volatility: metric(), maximum_drawdown: metric(), sharpe_ratio: metric(1.2, 'ratio') });
const coverage = () => ({ calendar_verified: true, expected_sessions: 2, expected_intervals: 1, valuation_count: 2, valid_valuation_count: 2, interval_count: 1, missing_sessions: [], complete: true, daily_eligible: true });
const chart = () => ({ unit: 'wealth_index', points: [{ date: '2026-01-05', value: 1 }, { date: '2026-01-06', value: 1.1 }], unavailable_reasons: [] });
// Synthetic financial data is used only in tests.
function fixture() {
  return {
    status: 'ok', portfolio_id: 1, name: 'Test portfolio', currency: 'USD', benchmark_ticker: 'SPY', benchmark_currency: 'USD', confidence: .95,
    period: { requested_start_date: null, requested_end_date: null, selected_start_date: '2026-01-05', selected_end_date: '2026-01-06', effective_baseline_date: '2026-01-05', first_return_end_date: '2026-01-06', effective_end_date: '2026-01-06' },
    portfolio_coverage: coverage(), benchmark_coverage: coverage(), comparison_eligible: true,
    methodology: { calendar_source: 'Verified XNYS test calendar', price_age_days: 0, external_flow_timing: 'end_of_period; flows in (start, end]', portfolio_price_basis: 'existing_engine: raw_close_for_tiingo; adjusted_close_otherwise', benchmark_return_basis: 'raw_close_price_return; dividends_not_reinvested', return_unit: 'fraction', monetary_unit: 'portfolio_currency; no_FX_conversion', annualization_sessions: 252 },
    metric_conventions: { risk_free_rate: 0, annualization_sessions: 252, volatility_ddof: 1, maximum_drawdown_sign: 'positive_loss_magnitude', drawdown_series_sign: 'positive_loss_magnitude', var_method: 'quantile', cvar_method: 'fractional_tail', tail_horizon: 'one_verified_trading_session', chart_policy: 'no_gap_bridging' },
    valuations: ['2026-01-05', '2026-01-06'].map(date => ({ date, holdings_value: 100, cash_balance: 0, total_equity: 100, net_contributions: 100, complete: true })),
    portfolio_intervals: [{ start_date: '2026-01-05', end_date: '2026-01-06', return_value: .1, trading_sessions: 1 }],
    benchmark_intervals: [{ start_date: '2026-01-05', end_date: '2026-01-06', return_value: .1, trading_sessions: 1 }],
    warnings: [], unavailable_reasons: [], portfolio_metrics: metrics(), benchmark_metrics: metrics(),
    comparison: { excess_cumulative_return: metric(-.0397), excess_annualized_return: metric(-.01) },
    wealth_index: { portfolio: chart(), benchmark: chart() },
  };
}
const signal = () => new AbortController().signal;
const response = body => new Response(JSON.stringify(body));

test('performance client encodes queries and validates the selected portfolio', async () => {
  const data = fixture();
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/portfolios/1/performance?start_date=2026-01-05&end_date=2026-01-06&benchmark=A%26B');
    assert.equal(options.headers.Accept, 'application/json'); assert.ok(options.signal instanceof AbortSignal);
    return response(data);
  };
  assert.deepEqual(await getPortfolioPerformance(1, { start_date: '2026-01-05', end_date: '2026-01-06', benchmark: 'A&B' }, signal()), data);
  assert.equal(performanceQuery({}), '');
});
test('date validation rejects invalid and reversed dates without sending a request', async () => {
  globalThis.fetch = () => assert.fail('Invalid inputs must not fetch');
  for (const query of [{ start_date: '2026-02-30' }, { end_date: '2026-1-01' }, { start_date: '2026-01-06', end_date: '2026-01-05' }, { benchmark: ' ' }]) {
    await assert.rejects(getPortfolioPerformance(1, query, signal()));
  }
  assert.equal(performanceQuery({ start_date: '2024-02-29' }), 'start_date=2024-02-29');
  await assert.rejects(getPortfolioPerformance(0, {}, signal()));
});
test('runtime validator rejects malformed units, dates, numbers, missing fields and metric reasons', () => {
  assert.equal(isPerformanceResponse(fixture()), true);
  for (const change of [
    d => { d.portfolio_metrics.cumulative_return.value = Infinity; },
    d => { d.portfolio_metrics.cumulative_return.value = null; },
    d => { d.portfolio_metrics.cumulative_return.unit = 'ratio'; },
    d => { d.portfolio_metrics.sharpe_ratio.unit = 'fraction'; },
    d => { d.period.effective_baseline_date = '2026-02-30'; },
    d => { delete d.portfolio_coverage; },
    d => { d.valuations[0].total_equity = '100'; },
    d => { d.portfolio_intervals[0].start_date = '2026-01-07'; },
    d => { d.wealth_index.portfolio.points.reverse(); },
    d => { d.unavailable_reasons = [{ code: 'missing' }]; },
  ]) { const d = fixture(); change(d); assert.equal(isPerformanceResponse(d), false); }
});
test('client rejects mismatched portfolios and malformed successful JSON', async () => {
  const d = fixture(); d.portfolio_id = 2;
  globalThis.fetch = async () => response(d);
  await assert.rejects(getPortfolioPerformance(1, {}, signal()), /mismatched/);
  globalThis.fetch = async () => new Response('invalid JSON');
  await assert.rejects(getPortfolioPerformance(1, {}, signal()), /invalid/);
});
function missingBenchmark() {
  const d = fixture(); d.status = 'partial'; d.benchmark_currency = null; d.comparison_eligible = false;
  d.benchmark_metrics = Object.fromEntries(Object.entries(d.benchmark_metrics).map(([k, v]) => [k, metric(null, v.unit)]));
  d.comparison = { excess_cumulative_return: metric(null), excess_annualized_return: metric(null) };
  d.benchmark_intervals = [];
  d.wealth_index.benchmark = { unit: 'wealth_index', points: [], unavailable_reasons: [reason()] };
  d.benchmark_coverage = { ...coverage(), complete: false, daily_eligible: false, valuation_count: 0, valid_valuation_count: 0, interval_count: 0, missing_sessions: ['2026-01-06'] };
  d.warnings = [reason()]; d.unavailable_reasons = [reason()]; return d;
}
test('missing benchmark and nullable values preserve independent portfolio metrics', async () => {
  const d = missingBenchmark(); d.valuations[0].holdings_value = null;
  globalThis.fetch = async () => response(d);
  const result = await getPortfolioPerformance(1, {}, signal());
  assert.equal(result.portfolio_metrics.cumulative_return.value, .1);
  assert.equal(result.benchmark_metrics.cumulative_return.value, null);
  assert.equal(result.valuations[0].holdings_value, null);
});
test('structured integrity and validation errors retain details', async () => {
  for (const [status, detail, expected] of [
    [409, [reason()], /insufficient_coverage/],
    [422, [{ loc: ['query', 'start_date'], msg: 'Invalid date' }], /query \/ start_date: Invalid date/],
    [422, 'end_date cannot precede start_date', /end_date/],
    [404, 'Portfolio not found', /Portfolio not found/],
  ]) {
    globalThis.fetch = async () => new Response(JSON.stringify({ detail }), { status });
    await assert.rejects(getPortfolioPerformance(1, {}, signal()), error => error instanceof PerformanceRequestError && error.status === status && expected.test(error.details.join(' ')));
  }
});
test('request identity ignores stale success and failure even when fetch ignores abort', async () => {
  const pending = [], seen = [], errors = [];
  globalThis.fetch = (url, options) => new Promise((resolve, reject) => pending.push({ resolve, reject, signal: options.signal }));
  const loader = createPerformanceLoader();
  const first = loader.load(1, {}, d => seen.push(d.name), e => errors.push(e));
  const second = loader.load(1, { end_date: '2026-01-06' }, d => seen.push(d.name), e => errors.push(e));
  assert.equal(pending[0].signal.aborted, true);
  const newer = fixture(); newer.name = 'Newer'; pending[1].resolve(response(newer)); await second;
  pending[0].resolve(response(fixture())); await first;
  assert.deepEqual(seen, ['Newer']); assert.deepEqual(errors, []);
  const third = loader.load(1, {}, d => seen.push(d.name), e => errors.push(e));
  loader.cancel(); pending[2].reject(new Error('Late network failure')); await third;
  assert.deepEqual(errors, []);
});
test('cancellation aborts transport and retry can recover after a network error', async () => {
  const loader = createPerformanceLoader(), errors = [], ready = [];
  globalThis.fetch = async () => { throw new Error('Disconnected'); };
  await loader.load(1, {}, d => ready.push(d), e => errors.push(e));
  assert.equal(errors.length, 1);
  globalThis.fetch = async () => response(fixture());
  await loader.load(1, {}, d => ready.push(d), e => errors.push(e));
  assert.equal(ready.length, 1);
  const controller = new AbortController(); controller.abort();
  globalThis.fetch = async (_url, options) => { options.signal.throwIfAborted(); return response(fixture()); };
  await assert.rejects(getPortfolioPerformance(1, {}, controller.signal), { name: 'AbortError' });
});
test('Performance content exposes dates, null reasons, comparison units and live charts', async () => {
  const { createServer } = await import('vite');
  const { default: react } = await import('@vitejs/plugin-react');
  const { createElement } = await import('react');
  const { renderToStaticMarkup } = await import('react-dom/server');
  const server = await createServer({ configFile: false, plugins: [react()], server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
  try {
    const { Performance, PerformanceContent, metricLabel, differenceLabel } = await server.ssrLoadModule('/src/pages/Performance.tsx');
    const loading = renderToStaticMarkup(createElement(Performance));
    for (const text of ['Loading performance', 'Full history', 'Apply dates', 'type="date"']) assert.ok(loading.includes(text));
    assert.equal(metricLabel(metric(0)), '0.00%'); assert.equal(metricLabel(metric(null)), 'Unavailable');
    assert.equal(metricLabel(metric(1.2, 'ratio')), '1.20');
    assert.equal(differenceLabel(metric(-.0397)), '-3.97 percentage points');
    const d = missingBenchmark(); d.period.requested_start_date = '2026-01-04';
    const html = renderToStaticMarkup(createElement(PerformanceContent, { data: d }));
    for (const text of ['10.00%', 'Unavailable', 'Partial analysis', 'insufficient_coverage', 'raw-close price return, excluding dividends', 'requested and effective dates differ', '4 January 2026', '5 January 2026', 'First return ending date', 'Portfolio growth', 'Portfolio drawdown']) assert.ok(html.includes(text), text);
    assert.ok(html.includes('<svg'), 'Valid portfolio growth survives missing benchmark data');
    const empty = missingBenchmark(); empty.valuations = []; empty.portfolio_intervals = []; empty.status = 'unavailable';
    assert.match(renderToStaticMarkup(createElement(PerformanceContent, { data: empty })), /No analysis observations/);
  } finally { await server.close(); }
});
