import assert from 'node:assert/strict';
import { test, before, after } from 'node:test';
import { existsSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { withDetailsClosed } from './helpers/closed-details.mjs';
let server, api, page, comparison, bars, AllocationComparisonChart;
const originalFetch = globalThis.fetch;
before(async () => {
  server = await createServer({ configFile: false, plugins: [react()], server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
  api = await server.ssrLoadModule('/src/api/allocation.ts');
  page = await server.ssrLoadModule('/src/pages/PortfolioAllocation.tsx');
  comparison = await server.ssrLoadModule('/src/lib/allocationComparison.ts');
  bars = await server.ssrLoadModule('/src/lib/allocationBars.ts');
  ({ AllocationComparisonChart } = await server.ssrLoadModule('/src/components/AllocationComparisonChart.tsx'));
});
after(async () => { globalThis.fetch = originalFetch; await server?.close(); });

test('allocation tables retain row headers, numeric roles and described scroll regions', () => {
  const data = fixture(); const before = structuredClone(data);
  const html = renderToStaticMarkup(createElement(page.AllocationContent, { data }));
  assert.equal((html.match(/class="holdings-table allocation-table"/g) ?? []).length, 2);
  assert.match(html, /scope="row" class="table-identity"/);
  assert.match(html, /scope="col" class="table-number">Market value/);
  const hints = [...html.matchAll(/<p id="([^"]+)" class="table-scroll-hint">If columns extend/g)];
  assert.equal(hints.length, 2);
  for (const [, id] of hints) assert.ok(html.includes(`aria-describedby="${id}"`));
  assert.deepEqual(data, before);
});
const issue = (scope = 'reference') => ({ code: 'incomplete_valuation', message: 'Exact-date valuation unavailable.', scope, ticker: null });
const security = (ticker, weight, value, day) => ({ ticker, instrument_name: `${ticker} fund`, classification: 'Instrument metadata', quantity: 1, effective_price_date: day, price_status: 'current', market_value: value, weight });
const snapshot = (day, weights) => ({ valuation_date: day, valuation_complete: true, invested_value: 100, cash_balance: 20,
  securities: weights.map(([ticker, weight]) => security(ticker, weight, weight * 100, day)),
  classification_exposures: [{ classification: 'Instrument metadata', market_value: 100, weight: 1, holdings_count: weights.length }],
  concentration: { hhi: .52, effective_holdings: 1.923, largest_position_weight: .6 }, unavailable_reasons: [] });
// Financial values are synthetic test fixtures, never application data.
function fixture() {
  return { portfolio_id: 1, name: 'Allocation test', currency: 'USD', requested_valuation_date: null, effective_valuation_date: '2026-01-09',
    weight_basis: 'invested_holdings_excluding_cash', weight_unit: 'fraction', classification_basis: 'current_instrument_metadata_not_look_through_or_historically_versioned', max_price_age_days: 0,
    current: snapshot('2026-01-09', [['A', .6], ['B', .4]]), reference: { reference_date: '2026-01-05', methodology: 'first_recorded_buy_date_end_of_day_valuation', provenance: 'recorded_transactions_and_stored_exact_date_prices', is_investment_policy_target: false, snapshot: snapshot('2026-01-05', [['A', .5], ['B', .5]]), unavailable_reasons: [] },
    changes: [{ ticker: 'A', current_weight: .6, reference_weight: .5, weight_change_fraction: .1 }, { ticker: 'B', current_weight: .4, reference_weight: .5, weight_change_fraction: -.1 }],
    comparison_unavailable_reasons: [], target_allocation: null, target_unavailable_reasons: [{ code: 'investment_policy_unavailable', message: 'No investment-policy targets recorded.', scope: 'target', ticker: null }], warnings: [] };
}
const response = data => new Response(JSON.stringify(data));
const signal = () => new AbortController().signal;

test('reference meaning and missing data stay visible while reconstruction mechanics are expandable', () => {
  const d = fixture(); d.reference.reference_date = '2026-01-02';
  d.reference.unavailable_reasons = [issue()];
  const before = structuredClone(d);
  const html = renderToStaticMarkup(createElement(page.AllocationContent, { data: d }));
  const visible = withDetailsClosed(html);
  for (const text of ['Compared with the historical starting allocation, not an investment-policy target.', '2 January 2026', 'Reconstructed at the end of the first recorded buy date', 'Exact-date valuation unavailable.', 'Historical reference unavailable or incomplete', 'incomplete_valuation', 'excluding cash']) assert.ok(visible.includes(text), text);
  assert.ok(!visible.includes('Missing prices do not advance'));
  assert.ok(html.includes('Missing prices do not advance the reference date'));
  assert.ok(html.includes('recorded transactions and stored exact-date prices'));
  assert.match(html, /<details[^>]*><summary>How the historical reference is reconstructed/);
  assert.match(html, /aria-describedby="allocation-reference-meaning"/);
  assert.deepEqual(d, before);
});
test('Allocation client encodes exact cutoff, validates portfolio identity and retains values', async () => {
  const d = fixture(); d.requested_valuation_date = '2026-01-09';
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/portfolios/1/allocation?as_of_date=2026-01-09'); assert.ok(options.signal instanceof AbortSignal);
    assert.equal(options.headers.Accept, 'application/json'); return response(d);
  };
  assert.deepEqual(await api.getPortfolioAllocation(1, '2026-01-09', signal()), d);
  assert.equal(api.allocationQuery(undefined), '');
  assert.equal(api.allocationQuery('2024-02-29'), 'as_of_date=2024-02-29');
});
test('invalid controls do not fetch and shifted explicit cutoffs are rejected', async () => {
  globalThis.fetch = () => assert.fail('Invalid date must not fetch');
  for (const day of ['2026-02-30', '', '20260109']) await assert.rejects(api.getPortfolioAllocation(1, day, signal()));
  await assert.rejects(api.getPortfolioAllocation(-1, undefined, signal()));
  const d = fixture(); d.requested_valuation_date = '2026-01-08';
  globalThis.fetch = async () => response(d);
  await assert.rejects(api.getPortfolioAllocation(1, '2026-01-08', signal()), /shifted/);
  globalThis.fetch = async () => response({ ...d, portfolio_id: 2 });
  await assert.rejects(api.getPortfolioAllocation(1, undefined, signal()), /mismatched/);
});
test('runtime validation rejects bad fields, policy targets, units, dates and nonfinite values', () => {
  assert.equal(api.isAllocationResponse(fixture()), true);
  for (const change of [d => { d.target_allocation = {}; }, d => { d.reference.is_investment_policy_target = true; }, d => { d.weight_basis = 'including_cash'; }, d => { d.weight_unit = 'percent'; }, d => { d.current.concentration.hhi = Infinity; }, d => { d.current.securities[0].weight = '60%'; }, d => { d.reference.reference_date = '2026-02-30'; }, d => { delete d.current.cash_balance; }, d => { d.changes[0].weight_change_fraction = NaN; }, d => { d.current.securities.push(d.current.securities[0]); }, d => { d.reference.snapshot.valuation_date = '2026-01-04'; }]) {
    const d = fixture(); change(d); assert.equal(api.isAllocationResponse(d), false);
  }
});
test('percentage-point changes and genuine new/exited zeros never replace unavailable data', () => {
  assert.equal(comparison.formatAllocationChange(.031), '+3.10 pp'); assert.equal(comparison.formatAllocationChange(-.012), '-1.20 pp');
  assert.equal(comparison.formatAllocationChange(0), '0.00 pp'); assert.equal(comparison.formatAllocationChange(null), 'Unavailable');
  const d = fixture(); d.current.securities = [security('B', 1, 100, '2026-01-09')];
  d.reference.snapshot.securities = [security('A', 1, 100, '2026-01-05')];
  d.changes = [{ ticker: 'A', current_weight: 0, reference_weight: 1, weight_change_fraction: -1 }, { ticker: 'B', current_weight: 1, reference_weight: 0, weight_change_fraction: 1 }];
  let rows = comparison.allocationComparison(d).rows;
  assert.equal(rows[0].currentWeight, 0); assert.equal(rows[0].marketValue, 0); assert.equal(rows[1].referenceWeight, 0);
  d.current.valuation_complete = false; d.current.invested_value = null; d.current.securities[0].market_value = null; d.changes = null;
  rows = comparison.allocationComparison(d).rows;
  assert.ok(rows.every(r => r.currentWeight === null && r.change === null && r.marketValue === null));
});
test('missing reference preserves current allocation and suppresses invented comparison values', () => {
  const d = fixture(); d.reference.snapshot = null; d.reference.unavailable_reasons = [issue()]; d.changes = null; d.comparison_unavailable_reasons = [issue()];
  const table = comparison.allocationComparison(d);
  assert.equal(table.currentValid, true); assert.equal(table.comparisonValid, false);
  assert.equal(table.rows[0].currentWeight, .6); assert.equal(table.rows[0].referenceWeight, null);
  const html = renderToStaticMarkup(createElement(page.AllocationContent, { data: d }));
  for (const text of ['Historical reference unavailable', '60.00%', 'Unavailable', 'incomplete_valuation']) assert.ok(html.includes(text), text);
});
test('incomplete valuations suppress aggregates and exposure tables but preserve known cash', () => {
  const d = fixture(); d.current.valuation_complete = false; d.current.invested_value = null;
  d.current.unavailable_reasons = [issue('current')]; d.changes = null;
  const html = renderToStaticMarkup(createElement(page.AllocationContent, { data: d }));
  for (const text of ['Incomplete current valuation', 'Separate cash', '20.00', 'No complete invested classification exposures', 'Unavailable']) assert.ok(html.includes(text), text);
  assert.ok(!html.includes('0.5200'), 'Invalid aggregate concentration must not leak through');
});
test('structured HTTP errors retain status, issues and validation field locations', async () => {
  for (const [status, detail, pattern] of [[409, 'Incomplete corporate action coverage', /corporate action/], [409, [issue()], /incomplete_valuation/], [422, [{ loc: ['query', 'as_of_date'], msg: 'Invalid date' }], /as_of_date/]]) {
    globalThis.fetch = async () => new Response(JSON.stringify({ detail }), { status });
    await assert.rejects(api.getPortfolioAllocation(1, undefined, signal()), e => e instanceof api.AllocationRequestError && e.status === status && pattern.test(e.details.join(' ')));
  }
});
test('cancellation and identity prevent stale responses and retries recover', async () => {
  const loader = api.createAllocationLoader(), pending = [], seen = [], errors = [];
  globalThis.fetch = (_url, options) => new Promise((resolve, reject) => pending.push({ resolve, reject, signal: options.signal }));
  const first = loader.load(1, undefined, d => seen.push(d.name), e => errors.push(e));
  const second = loader.load(1, undefined, d => seen.push(d.name), e => errors.push(e));
  assert.equal(pending[0].signal.aborted, true);
  pending[1].resolve(response({ ...fixture(), name: 'Newest' })); await second;
  pending[0].resolve(response(fixture())); await first; assert.deepEqual(seen, ['Newest']);
  const third = loader.load(1, undefined, d => seen.push(d.name), e => errors.push(e));
  loader.cancel(); pending[2].reject(new Error('Late failure')); await third; assert.equal(errors.length, 0);
  globalThis.fetch = async () => { throw new Error('Offline'); };
  await loader.load(1, undefined, d => seen.push(d.name), e => errors.push(e)); assert.equal(errors.length, 1);
  globalThis.fetch = async () => response(fixture()); await loader.load(1, undefined, d => seen.push(d.name), e => errors.push(e)); assert.equal(seen.length, 2);
});
test('page labels cash exclusion, provenance, requested/effective dates and live chart accessibly', () => {
  const loading = renderToStaticMarkup(createElement(page.PortfolioAllocation));
  for (const text of ['Loading allocation', 'As-of date', 'Apply', 'Latest available', 'Latest available clears the draft date only', 'changes take effect on Apply']) assert.ok(loading.includes(text));
  const d = fixture(); d.requested_valuation_date = '2026-01-09';
  const html = renderToStaticMarkup(createElement(page.AllocationContent, { data: d }));
  for (const text of ['9 January 2026', '5 January 2026', 'Requested date', 'Change from historical starting allocation', 'Excluded from weights and concentration', 'not an investment-policy target', 'recorded transactions and stored exact-date prices', 'not ETF look-through exposure', '+10.00 pp', 'scope="col"', 'scope="row"', 'tabindex="0"', 'Current versus historical starting allocation']) assert.ok(html.includes(text), text);
  assert.ok(html.includes('<svg'));
  const table = comparison.allocationComparison(d); assert.equal(table.rows.length, 2); assert.ok(!table.rows.some(r => r.ticker === 'CASH'));
});

test('allocation bars map backend weights without renormalizing and use a fixed common scale', () => {
  const d = fixture(); const chart = bars.allocationBars(d);
  assert.deepEqual(chart.reasons, []); assert.equal(chart.scaleMaximum, 1);
  assert.deepEqual(chart.rows.map(r => [r.ticker, r.currentWeight, r.referenceWeight, r.change]), [['A', .6, .5, .1], ['B', .4, .5, -.1]]);
  const html = renderToStaticMarkup(createElement(AllocationComparisonChart, { data: d }));
  assert.match(html, /class="allocation-bar-start"[^>]+width="50"/);
  assert.match(html, /class="allocation-bar-current"[^>]+width="60"/);
  for (const tick of ['0%', '25%', '50%', '75%', '100%']) assert.ok(html.includes(tick));
  for (const tick of [0, 25, 50, 75, 100]) assert.ok(html.includes(`style="left:${tick}%"`), 'Axis labels anchor to the bar percentage scale');
  assert.equal(bars.exactAllocationPercent(.29995119501695583), '29.995119501695583%');
  assert.equal(bars.exactAllocationChange(.031), '+3.1 percentage points');
});
test('non-normalized, missing and out-of-range weights withhold the chart rather than filling or rescaling', () => {
  for (const change of [
    d => { d.changes[0].current_weight = .2; }, d => { d.changes[0].reference_weight = 1.1; },
    d => { d.changes[0].current_weight = .2; d.current.securities[0].weight = .2; },
    d => { d.changes[0].current_weight = -.1; }, d => { d.changes[0].current_weight = null; },
    d => { d.changes[0].weight_change_fraction = null; }, d => { d.current.valuation_complete = false; },
    d => { d.reference.snapshot = null; d.reference.unavailable_reasons = [issue()]; },
  ]) {
    const d = fixture(); change(d); const chart = bars.allocationBars(d);
    assert.equal(chart.rows.length, 0); assert.ok(chart.reasons.length);
    const html = renderToStaticMarkup(createElement(AllocationComparisonChart, { data: d }));
    assert.ok(!html.includes('<svg')); assert.ok(html.includes('chart unavailable'));
  }
});
test('genuine new/exited zeros remain in the union and plot as zero-length bars', () => {
  const d = fixture(); d.current.securities = [security('B', 1, 100, '2026-01-09')];
  d.reference.snapshot.securities = [security('A', 1, 100, '2026-01-05')];
  d.changes = [{ ticker: 'A', current_weight: 0, reference_weight: 1, weight_change_fraction: -1 }, { ticker: 'B', current_weight: 1, reference_weight: 0, weight_change_fraction: 1 }];
  const chart = bars.allocationBars(d); assert.deepEqual(chart.rows.map(r => r.ticker), ['B', 'A']);
  assert.equal(chart.rows[0].referenceWeight, 0); assert.equal(chart.rows[1].currentWeight, 0);
  const html = renderToStaticMarkup(createElement(AllocationComparisonChart, { data: d }));
  assert.match(html, /class="allocation-bar-start"[^>]+width="0"/);
  assert.match(html, /class="allocation-bar-current"[^>]+width="100"/);
  assert.ok(html.includes('0.00%')); assert.ok(html.includes('-100.00 pp')); assert.ok(html.includes('+100.00 pp'));
});
test('ordering is deterministic by descending current weight and ticker, without mutating API arrays', () => {
  const d = fixture(); d.changes.reverse(); d.current.securities.reverse();
  assert.deepEqual(bars.allocationBars(d).rows.map(r => r.ticker), ['A', 'B']);
  d.changes.forEach(r => { r.current_weight = .5; r.reference_weight = .5; r.weight_change_fraction = 0; });
  d.current.securities.forEach(r => { r.weight = .5; });
  const previous = JSON.stringify(d);
  assert.deepEqual(bars.allocationBars(d).rows.map(r => r.ticker), ['A', 'B']); assert.equal(JSON.stringify(d), previous);
});
test('chart offers keyboard-operable security controls, exact labels and the existing table equivalent', () => {
  const d = fixture(); const html = renderToStaticMarkup(createElement(page.AllocationContent, { data: d }));
  for (const text of ['Historical starting', 'Current invested', 'same 0–100% scale', 'Tab to a security', 'Enter or Space', 'type="button"', 'aria-expanded="false"', 'aria-controls=', 'aria-live="polite"', 'A, A fund. Historical starting 50%; current invested 60%; change +10 percentage points.', 'Allocation comparison table', 'scope="col"', 'scope="row"']) assert.ok(html.includes(text), text);
  assert.equal((html.match(/class="allocation-bar-row"/g) ?? []).length, 2);
});
test('empty portfolios retain zero invested value and undefined concentration', () => {
  const d = fixture(); d.current.securities = []; d.current.invested_value = 0; d.current.classification_exposures = [];
  d.current.concentration = { hhi: null, effective_holdings: null, largest_position_weight: null }; d.changes = null;
  d.reference.snapshot = null; d.reference.reference_date = null;
  const html = renderToStaticMarkup(createElement(page.AllocationContent, { data: d }));
  assert.ok(html.includes('No invested holdings')); assert.ok(html.includes('Unavailable')); assert.ok(html.includes('0.00'));
});
test('historical Allocation response passes runtime validation with genuine starting weights', { skip: !existsSync('../data/historical_portfolio.db') }, () => {
  const python = process.platform === 'win32' ? '../.venv/Scripts/python.exe' : '../.venv/bin/python';
  const script = `from contextlib import closing
from pathlib import Path
import sqlite3
from riskengine.risk.allocation_service import get_allocation_response
with closing(sqlite3.connect(Path('../data/historical_portfolio.db').resolve().as_uri()+'?mode=ro',uri=True)) as c:
 c.execute('BEGIN')
 print(get_allocation_response(c,1).model_dump_json())
`;
  const d = JSON.parse(execFileSync(python, ['-c', script], { encoding: 'utf8', timeout: 120_000 }));
  assert.equal(api.isAllocationResponse(d), true); assert.equal(d.reference.snapshot.invested_value, 99272.65);
  const table = comparison.allocationComparison(d); assert.equal(table.rows.length, 6); assert.equal(table.comparisonValid, true);
  assert.equal(table.rows.find(r => r.ticker === 'SPY').referenceWeight, 29776.949999999997 / 99272.65);
  const chart = bars.allocationBars(d); assert.deepEqual(chart.reasons, []); assert.equal(chart.rows.length, 6);
  for (const row of chart.rows) {
    const supplied = d.changes.find(c => c.ticker === row.ticker);
    assert.equal(row.currentWeight, supplied.current_weight); assert.equal(row.referenceWeight, supplied.reference_weight);
    assert.equal(row.change, supplied.weight_change_fraction);
  }
});
