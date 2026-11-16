import assert from 'node:assert/strict';
import { test, before, after } from 'node:test';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { existsSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
let server, api, page, charts;
const nativeFetch = globalThis.fetch;
before(async () => {
  server = await createServer({ configFile: false, plugins: [react()], server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
  api = await server.ssrLoadModule('/src/api/risk.ts');
  page = await server.ssrLoadModule('/src/pages/RiskAnalytics.tsx');
  charts = await server.ssrLoadModule('/src/lib/riskCharts.ts');
});
after(async () => { globalThis.fetch = nativeFetch; await server?.close(); });
const reason = () => ({ code: 'missing_benchmark', message: 'Benchmark coverage unavailable.', scope: 'benchmark', category: 'coverage', metric: 'portfolio.beta', ticker: 'SPY', start_date: null, end_date: null });
const metric = (value = .12, unit = 'fraction') => ({ value, unit, observations: 2, unavailable_reasons: value === null ? [reason()] : [] });
const metrics = () => ({ annualized_volatility: metric(), beta: metric(.8, 'ratio'), historical_var: metric(-.01), historical_cvar: metric(-.02), maximum_drawdown: metric(.2), sharpe_ratio: metric(1.2, 'ratio') });
const coverage = () => ({ calendar_verified: true, expected_sessions: 3, expected_intervals: 2, valuation_count: 3, valid_valuation_count: 3, interval_count: 2, missing_sessions: [], complete: true, daily_eligible: true });
// Synthetic values are restricted to test fixtures.
function fixture() {
  return { status: 'ok', portfolio_id: 1, name: 'Risk test portfolio', currency: 'USD', benchmark_ticker: 'SPY', benchmark_currency: 'USD', confidence: .95,
    period: { requested_start_date: null, requested_end_date: null, selected_start_date: '2026-01-05', selected_end_date: '2026-01-07', effective_baseline_date: '2026-01-05', effective_end_date: '2026-01-07', first_return_end_date: '2026-01-06' },
    portfolio_coverage: coverage(), benchmark_coverage: coverage(), comparison_eligible: true,
    methodology: { calendar_source: 'Verified test XNYS', price_age_days: 0, external_flow_timing: 'end_of_period; flows in (start, end]', portfolio_price_basis: 'raw_close_for_tiingo; adjusted_close_otherwise', benchmark_return_basis: 'raw_close_price_return; dividends_not_reinvested', return_unit: 'fraction', monetary_unit: 'portfolio_currency; no_FX_conversion', annualization_sessions: 252 },
    metric_conventions: { risk_free_rate: 0, annualization_sessions: 252, volatility_ddof: 1, maximum_drawdown_sign: 'positive_loss_magnitude', drawdown_series_sign: 'positive_loss_magnitude', var_method: 'linear_quantile; not_clamped', cvar_method: 'fractional_tail_weighted_mean', tail_horizon: 'one_verified_trading_session', chart_policy: 'no_gap_bridging' },
    valuations: [{ date: '2026-01-05', holdings_value: 100, cash_balance: 0, total_equity: 100, net_contributions: 100, complete: true }],
    portfolio_intervals: [{ start_date: '2026-01-05', end_date: '2026-01-06', return_value: -.01, trading_sessions: 1 }],
    benchmark_intervals: [], warnings: [], unavailable_reasons: [], portfolio_metrics: metrics(), benchmark_metrics: metrics(),
    drawdown: { portfolio: { unit: 'fraction', points: [{ date: '2026-01-05', value: 0 }], unavailable_reasons: [] }, benchmark: { unit: 'fraction', points: [], unavailable_reasons: [] } } };
}
const signal = () => new AbortController().signal;
const respond = data => new Response(JSON.stringify(data));
test('Risk client encodes dates, confidence and benchmark; preserves backend values', async () => {
  const data = fixture();
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/portfolios/1/risk?start_date=2026-01-05&end_date=2026-01-07&benchmark=A%26B&confidence=0.95');
    assert.ok(options.signal instanceof AbortSignal); return respond(data);
  };
  assert.deepEqual(await api.getPortfolioRisk(1, { start_date: '2026-01-05', end_date: '2026-01-07', benchmark: 'A&B', confidence: .95 }, signal()), data);
});
test('confidence and date selections validate before fetching, including full-history boundaries', async () => {
  globalThis.fetch = () => assert.fail('Invalid controls must not fetch');
  for (const query of [{ confidence: 0 }, { confidence: 1 }, { confidence: NaN }, { confidence: Infinity }, { confidence: -.1 }, { start_date: '2026-02-30' }, { start_date: '2026-01-07', end_date: '2026-01-05' }]) await assert.rejects(api.getPortfolioRisk(1, query, signal()));
  assert.equal(api.riskQuery({ confidence: .99 }), 'confidence=0.99');
  assert.equal(api.riskQuery({}), '');
  assert.equal(api.riskQuery({ start_date: '2024-02-29', confidence: .9 }), 'start_date=2024-02-29&confidence=0.9');
});
test('runtime validation rejects missing fields, bad units, nonfinite numbers and undefined metrics without reasons', () => {
  assert.equal(api.isRiskResponse(fixture()), true);
  for (const change of [d => { delete d.confidence; }, d => { d.confidence = 1; }, d => { d.portfolio_metrics.beta.unit = 'fraction'; }, d => { d.portfolio_metrics.historical_var.value = Infinity; }, d => { d.portfolio_metrics.historical_cvar.value = null; }, d => { d.drawdown.portfolio.unit = 'wealth_index'; }, d => { d.drawdown.portfolio.points[0].date = '2026-02-30'; }, d => { delete d.methodology; }]) {
    const data = fixture(); change(data); assert.equal(api.isRiskResponse(data), false);
  }
});
test('missing benchmark and null metrics preserve independent portfolio estimates', async () => {
  const d = fixture(); d.status = 'partial'; d.benchmark_currency = null; d.comparison_eligible = false;
  d.portfolio_metrics.beta = metric(null, 'ratio'); d.benchmark_metrics = Object.fromEntries(Object.entries(metrics()).map(([key, m]) => [key, metric(null, m.unit)]));
  d.unavailable_reasons = [reason()];
  globalThis.fetch = async () => respond(d);
  const data = await api.getPortfolioRisk(1, {}, signal());
  assert.equal(data.portfolio_metrics.historical_var.value, -.01);
  assert.equal(data.portfolio_metrics.beta.value, null);
  const html = renderToStaticMarkup(createElement(page.RiskContent, { data }));
  for (const text of ['Partial risk analysis', 'Unavailable', 'missing_benchmark', '-1.00%', '2 observations']) assert.ok(html.includes(text), text);
});
test('structured 409/422 errors retain issue codes, field locations and status', async () => {
  for (const [status, detail, expected] of [[409, [reason()], /missing_benchmark/], [422, [{ loc: ['query', 'confidence'], msg: 'Invalid confidence' }], /query \/ confidence/], [422, 'Bad date range', /Bad date range/]]) {
    globalThis.fetch = async () => new Response(JSON.stringify({ detail }), { status });
    await assert.rejects(api.getPortfolioRisk(1, {}, signal()), e => e instanceof api.RiskRequestError && e.status === status && expected.test(e.details.join(' ')));
  }
  globalThis.fetch = async () => respond({ ...fixture(), portfolio_id: 2 });
  await assert.rejects(api.getPortfolioRisk(1, {}, signal()), /mismatched/);
});
test('request identity and cancellation prevent stale responses; retry recovers', async () => {
  const loader = api.createRiskLoader(), pending = [], seen = [], errors = [];
  globalThis.fetch = (_url, options) => new Promise((resolve, reject) => pending.push({ resolve, reject, signal: options.signal }));
  const first = loader.load(1, { confidence: .9 }, d => seen.push(d.name), e => errors.push(e));
  const second = loader.load(1, { confidence: .95 }, d => seen.push(d.name), e => errors.push(e));
  assert.equal(pending[0].signal.aborted, true);
  const newer = fixture(); newer.name = 'Newest'; pending[1].resolve(respond(newer)); await second;
  pending[0].resolve(respond(fixture())); await first; assert.deepEqual(seen, ['Newest']);
  const third = loader.load(1, {}, d => seen.push(d.name), e => errors.push(e));
  loader.cancel(); pending[2].reject(new Error('Late failure')); await third; assert.equal(errors.length, 0);
  globalThis.fetch = async () => { throw new Error('Offline'); };
  await loader.load(1, {}, d => seen.push(d.name), e => errors.push(e)); assert.equal(errors.length, 1);
  globalThis.fetch = async () => respond(fixture()); await loader.load(1, {}, d => seen.push(d.name), e => errors.push(e));
  assert.equal(seen.length, 2);
});
test('Risk page preserves Apply-only controls, dates, negative estimates and ratios', () => {
  const loading = renderToStaticMarkup(createElement(page.RiskAnalytics));
  for (const text of ['Full history', 'Start date', 'End date', 'Confidence level', '90%', '95%', '99%', 'Apply', 'Full history clears draft dates only', 'Changes take effect on Apply', 'Loading risk analysis']) assert.ok(loading.includes(text), text);
  assert.equal(page.riskMetricLabel(metric(-.02)), '-2.00%'); assert.equal(page.riskMetricLabel(metric(0)), '0.00%');
  assert.equal(page.riskMetricLabel(metric(.8, 'ratio')), '0.80'); assert.equal(page.riskMetricLabel(metric(null)), 'Unavailable');
  const d = fixture(); d.period.requested_start_date = '2026-01-04';
  const html = renderToStaticMarkup(createElement(page.RiskContent, { data: d }));
  for (const text of ['4 January 2026', '5 January 2026', 'requested and effective dates differ', 'not guaranteed loss limits', 'raw-close price returns excluding dividends', 'Historical drawdown', '2 observations']) assert.ok(html.includes(text), text);
  assert.ok(!html.includes('<svg'), 'Incomplete chart windows are withheld');
  d.valuations = []; d.portfolio_intervals = []; d.status = 'unavailable';
  assert.match(renderToStaticMarkup(createElement(page.RiskContent, { data: d })), /No risk observations/);
});

function chartFixture() {
  const d = fixture();
  const intervals = [{ start_date: '2026-01-05', end_date: '2026-01-06', return_value: -.01, trading_sessions: 1 },
    { start_date: '2026-01-06', end_date: '2026-01-07', return_value: .01, trading_sessions: 1 }];
  d.portfolio_intervals = intervals; d.benchmark_intervals = intervals.map(i => ({ ...i }));
  d.drawdown.portfolio.points = [{ date: '2026-01-05', value: -0 }, { date: '2026-01-06', value: .125 }, { date: '2026-01-07', value: .0625 }];
  d.drawdown.benchmark.points = [{ date: '2026-01-05', value: 0 }, { date: '2026-01-06', value: .20 }, { date: '2026-01-07', value: .10 }];
  return d;
}
test('drawdown maps every backend value and date directly, including the baseline', () => {
  const d = chartFixture(); const lines = charts.riskDrawdownLines(d);
  assert.equal(lines[0].points, d.drawdown.portfolio.points);
  assert.equal(lines[1].points, d.drawdown.benchmark.points);
  assert.deepEqual(lines[0].points, d.drawdown.portfolio.points);
  assert.equal(lines[0].points.length, 3);
  assert.equal(lines[0].points[0].value, -0);
  // Metric values and returns deliberately do not reconstruct these points.
  d.portfolio_metrics.maximum_drawdown.value = .7;
  assert.equal(charts.riskDrawdownLines(d)[0].points, d.drawdown.portfolio.points);
});
test('missing benchmark, currency conflicts and unavailable chart reasons retain portfolio visualization', () => {
  for (const change of [
    d => { d.benchmark_coverage.complete = false; d.drawdown.benchmark.points = []; },
    d => { d.drawdown.benchmark.unavailable_reasons = [reason()]; },
    d => { d.benchmark_currency = null; },
    d => { d.benchmark_intervals.splice(0, 1); },
  ]) {
    const d = chartFixture(); change(d); const lines = charts.riskDrawdownLines(d);
    assert.equal(lines[0].points.length, 3); assert.equal(lines[1].points.length, 0); assert.ok(lines[1].unavailable.length);
    const html = renderToStaticMarkup(createElement(page.RiskContent, { data: d }));
    assert.ok(html.includes('<svg')); assert.ok(html.includes('SPY unavailable'));
  }
});
test('coverage gaps, duplicate dates, invalid boundaries and nonfinite observations cannot be bridged', () => {
  for (const change of [
    d => { d.portfolio_coverage.complete = false; }, d => { d.portfolio_coverage.calendar_verified = false; },
    d => { d.portfolio_coverage.missing_sessions = ['2026-01-06']; },
    d => { d.drawdown.portfolio.points.splice(1, 1); },
    d => { d.drawdown.portfolio.points[1].date = '2026-01-05'; },
    d => { d.drawdown.portfolio.points[1].value = NaN; },
    d => { d.drawdown.portfolio.points[1].value = -.1; },
    d => { d.portfolio_intervals[1].trading_sessions = null; },
    d => { d.portfolio_intervals[1].start_date = '2026-01-05'; },
    d => { d.period.effective_baseline_date = null; },
  ]) {
    const d = chartFixture(); change(d); const lines = charts.riskDrawdownLines(d);
    assert.equal(lines[0].points.length, 0); assert.ok(lines[0].unavailable.length);
  }
});
test('one interval and a single supplied baseline do not depend on the legacy daily_eligible flag', () => {
  const d = chartFixture();
  for (const count of [2, 1]) {
    for (const side of ['portfolio', 'benchmark']) {
      d.drawdown[side].points = d.drawdown[side].points.slice(0, count);
      d[`${side}_intervals`] = d[`${side}_intervals`].slice(0, count - 1);
      d[`${side}_coverage`] = { ...coverage(), expected_sessions: count, expected_intervals: count - 1, interval_count: count - 1, valuation_count: count, valid_valuation_count: count, daily_eligible: false };
    }
    d.period.effective_end_date = d.drawdown.portfolio.points.at(-1).date;
    assert.equal(charts.riskDrawdownLines(d)[0].points.length, count);
    const html = renderToStaticMarkup(createElement(page.RiskContent, { data: d }));
    assert.ok(html.includes('<svg')); assert.ok(!html.includes('NaN'));
    if (count === 1) { assert.ok(html.includes('Only one dated observation')); assert.ok(html.includes('class="series-point"')); }
  }
});
test('drawdown chart percentages, maximum-versus-selected labels and accessibility are explicit', () => {
  const html = renderToStaticMarkup(createElement(page.RiskContent, { data: chartFixture() }));
  for (const text of ['6.25%', '10.00%', 'fraction 0.0625', '7 January 2026', 'drawdown at the selected date', 'Maximum drawdown above is the largest loss', 'Zero represents a previous peak', 'type="range"', 'Home/End', 'role="img"', 'aria-live="polite"', 'scope="col"', 'scope="row"', 'series-0', 'series-1']) assert.ok(html.includes(text), text);
  assert.equal((html.match(/class="series-line"/g) ?? []).length, 2);
  assert.ok(html.includes('0.125'), 'Exact data table retains supplied values');
});
test('expected self-beta information stays neutral while actual warnings remain highlighted', () => {
  const d = chartFixture();
  d.unavailable_reasons = [{ ...reason(), code: 'not_applicable', message: 'Benchmark self-beta is not applicable.' }];
  const neutral = renderToStaticMarkup(createElement(page.RiskContent, { data: d }));
  assert.ok(neutral.includes('class="risk-information"'));
  assert.ok(neutral.includes('Benchmark self-beta is not applicable.'));
  assert.ok(!neutral.includes('class="quality-warnings"'));
  d.warnings = [reason()];
  const warning = renderToStaticMarkup(createElement(page.RiskContent, { data: d }));
  assert.ok(warning.includes('class="quality-warnings"'));
  assert.ok(warning.includes('missing_benchmark'));
});
test('historical Risk chart mapping preserves all backend observations without transformation', { skip: !existsSync('../data/historical_portfolio.db') }, () => {
  const python = process.platform === 'win32' ? '../.venv/Scripts/python.exe' : '../.venv/bin/python';
  const script = `from contextlib import closing
from pathlib import Path
import sqlite3
from riskengine.risk.performance_service import get_risk_response
with closing(sqlite3.connect(Path('../data/historical_portfolio.db').resolve().as_uri()+'?mode=ro',uri=True)) as c:
 c.execute('BEGIN')
 print(get_risk_response(c,1).model_dump_json())
`;
  const data = JSON.parse(execFileSync(python, ['-c', script], { encoding: 'utf8', maxBuffer: 4 * 1024 * 1024, timeout: 120_000 }));
  const lines = charts.riskDrawdownLines(data);
  for (const [i, side] of ['portfolio', 'benchmark'].entries()) {
    assert.equal(lines[i].points.length, 716); assert.equal(lines[i].points[0].date, '2024-01-02');
    assert.deepEqual(lines[i].points, data.drawdown[side].points); assert.deepEqual(lines[i].unavailable, []);
  }
});
