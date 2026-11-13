// Synthetic values are confined to tests; the application only uses API data.
import assert from 'node:assert/strict';
import { test, after } from 'node:test';
import { getPortfolioHoldings } from '../src/api/portfolio.ts';
import { buildAllocation } from '../src/lib/allocation.ts';
import { formatOptionalCurrency, formatQuantity, formatValuationDate, formatWeight } from '../src/lib/format.ts';

const nativeFetch = globalThis.fetch;
after(() => { globalThis.fetch = nativeFetch; });
const holding = (ticker, weight) => ({
  ticker, instrument_name: `${ticker} instrument`, quantity: 1.5,
  effective_price_date: '2026-01-05', price_age_days: 0, price_status: 'current',
  market_price: 100, market_price_currency: 'USD', currency_status: 'consistent',
  market_value: 150, remaining_average_cost_basis: 120, unrealized_gain_loss: 30, invested_weight: weight,
});
const fixture = () => ({
  portfolio_id: 1, name: 'Test portfolio', currency: 'USD', requested_valuation_date: null,
  effective_valuation_date: '2026-01-05', max_price_age_days: 0, valuation_complete: true,
  weight_basis: 'invested_market_value_excluding_cash', weight_unit: 'fraction',
  cost_basis_method: 'remaining_average_cost_not_net_transaction_investment',
  holdings: [holding('ONE', .5), holding('TWO', .5)],
  totals: { holding_count: 2, valued_holding_count: 2, market_value: 300,
    remaining_average_cost_basis: 240, unrealized_gain_loss: 60 }, warnings: [],
});
const signal = () => new AbortController().signal;
function respond(body, status = 200) {
  globalThis.fetch = async () => new Response(JSON.stringify(body), { status });
}

test('typed client requests selected portfolio and preserves actual response values', async () => {
  const data = fixture();
  data.portfolio_id = 7;
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/portfolios/7/holdings');
    assert.equal(options.headers.Accept, 'application/json');
    assert.ok(options.signal instanceof AbortSignal);
    return new Response(JSON.stringify(data));
  };
  assert.deepEqual(await getPortfolioHoldings(7, signal()), data);
});

test('incomplete responses retain nulls and warnings without inventing totals', async () => {
  const data = fixture();
  data.valuation_complete = false;
  data.totals.market_value = data.totals.unrealized_gain_loss = null;
  data.totals.valued_holding_count = 1;
  data.holdings.forEach((h) => { h.invested_weight = null; });
  Object.assign(data.holdings[0], { price_status: 'missing', market_price: null,
    effective_price_date: null, price_age_days: null, market_value: null, unrealized_gain_loss: null });
  data.warnings = [{ code: 'missing_price', ticker: 'ONE', message: 'Price unavailable.' }];
  respond(data);
  const result = await getPortfolioHoldings(1, signal());
  assert.equal(result.totals.market_value, null);
  assert.equal(result.holdings[0].market_price, null);
  assert.equal(result.warnings[0].code, 'missing_price');
  assert.equal(buildAllocation(result).available, false);
});

test('empty portfolio accepts null valuation date and genuine zero totals', async () => {
  const data = fixture();
  data.holdings = [];
  data.effective_valuation_date = null;
  data.totals = { holding_count: 0, valued_holding_count: 0, market_value: 0, remaining_average_cost_basis: 0, unrealized_gain_loss: 0 };
  respond(data);
  assert.deepEqual(await getPortfolioHoldings(1, signal()), data);
  assert.equal(buildAllocation(data).available, false);
});

test('rejects malformed payloads, invalid dates, nonfinite values and mismatched IDs', async () => {
  for (const change of [
    (d) => { d.portfolio_id = 2; },
    (d) => { d.effective_valuation_date = '2026-02-30'; },
    (d) => { d.holdings[0].quantity = '1'; },
    (d) => { delete d.holdings[0].market_price_currency; },
    (d) => { d.weight_unit = 'percent'; },
    (d) => { d.totals.holding_count = 9; },
    (d) => { d.warnings = [{code: 'invalid'}]; },
  ]) {
    const data = fixture(); change(data); respond(data);
    await assert.rejects(getPortfolioHoldings(1, signal()), /invalid or mismatched/);
  }
  globalThis.fetch = async () => ({ ok: true, json: async () => {
    const data = fixture(); data.holdings[0].market_value = Infinity; return data;
  } });
  await assert.rejects(getPortfolioHoldings(1, signal()), /invalid or mismatched/);
  globalThis.fetch = async () => new Response('not JSON');
  await assert.rejects(getPortfolioHoldings(1, signal()), /invalid or mismatched/);
});

test('HTTP errors expose API conflict details and retry can succeed', async () => {
  for (const status of [404, 409, 422, 500]) {
    respond({ detail: 'Integrity conflict' }, status);
    await assert.rejects(getPortfolioHoldings(1, signal()), new RegExp(`HTTP ${status}.*Integrity conflict`));
  }
  respond(fixture());
  assert.equal((await getPortfolioHoldings(1, signal())).portfolio_id, 1);
});

test('network and cancellation failures remain errors; invalid IDs never fetch', async () => {
  globalThis.fetch = async () => { throw new TypeError('Network unavailable'); };
  await assert.rejects(getPortfolioHoldings(1, signal()), /Network unavailable/);
  globalThis.fetch = async (_url, options) => { options.signal.throwIfAborted(); return new Response('{}'); };
  const controller = new AbortController(); controller.abort();
  await assert.rejects(getPortfolioHoldings(1, controller.signal), { name: 'AbortError' });
  globalThis.fetch = async () => { assert.fail('Invalid configuration must not fetch'); };
  for (const id of [0, -1, NaN, 1.5]) await assert.rejects(getPortfolioHoldings(id, signal()), /positive integer/);
});

test('allocation uses API fractions and exact cumulative offsets', () => {
  const data = fixture();
  data.holdings[0].invested_weight = .3;
  data.holdings[1].invested_weight = .7;
  assert.deepEqual(buildAllocation(data), { available: true, segments: [
    { ticker: 'ONE', weight: .3, offset: 0 }, { ticker: 'TWO', weight: .7, offset: .3 },
  ] });
  data.holdings = [holding('ONLY', 1)];
  assert.equal(buildAllocation(data).segments[0].weight, 1);
});

test('chart never renormalizes missing, negative, oversized or inconsistent weights', () => {
  for (const weight of [null, -.1, 1.1, NaN, .2]) {
    const data = fixture(); data.holdings[0].invested_weight = weight;
    assert.equal(buildAllocation(data).available, false);
  }
  const data = fixture(); data.valuation_complete = false;
  assert.equal(buildAllocation(data).available, false);
});

test('formatting distinguishes missing values from actual zero and formats fractional weights', () => {
  assert.equal(formatOptionalCurrency(null, 'USD'), 'Unavailable');
  assert.match(formatOptionalCurrency(0, 'USD'), /0\.00/);
  assert.equal(formatWeight(.306), '30.60%');
  assert.equal(formatWeight(null), 'Unavailable');
  assert.equal(formatQuantity(1.23456789), '1.23456789');
  assert.equal(formatValuationDate('2026-01-05'), '5 January 2026');
});

test('rendered content exposes incomplete valuation, foreign price currency and warnings accessibly', async () => {
  const { createServer } = await import('vite');
  const { default: react } = await import('@vitejs/plugin-react');
  const { createElement } = await import('react');
  const { renderToStaticMarkup } = await import('react-dom/server');
  const server = await createServer({ configFile: false, plugins: [react()],
    server: { middlewareMode: true, hmr: false, watch: null } });
  try {
    const { Holdings, HoldingsContent } = await server.ssrLoadModule('/src/pages/Holdings.tsx');
    assert.match(renderToStaticMarkup(createElement(Holdings)), /Loading holdings/);
    const data = fixture();
    data.valuation_complete = false;
    data.totals.market_value = data.totals.unrealized_gain_loss = null;
    data.holdings[0].currency_status = 'conflict'; data.holdings[0].market_price_currency = 'EUR';
    data.holdings[0].price_status = 'stale'; data.holdings.forEach((h) => { h.invested_weight = null; });
    data.warnings = [{ code: 'currency_mismatch', ticker: 'ONE', message: 'No FX conversion applied.' }];
    const html = renderToStaticMarkup(createElement(HoldingsContent, { data, retry() {} }));
    for (const text of ['Incomplete valuation', 'Unavailable', 'EUR', 'Currency conflict', 'currency_mismatch', 'Stale', '5 January 2026', 'not today']) assert.ok(html.includes(text), text);
    assert.ok(!html.includes('<svg'), 'Incomplete allocations must not be charted');
    const complete = renderToStaticMarkup(createElement(HoldingsContent, { data: fixture(), retry() {} }));
    assert.match(complete, /<svg[^>]+role="img"/);
    assert.match(complete, /ONE: 50\.00%; TWO: 50\.00%/);
    assert.match(complete, /scope="col"/);
    assert.match(complete, /<details class="price-details"/);
    assert.match(complete, /ONE price freshness: Exact-date price/);
    assert.match(complete, /0 calendar days old at cutoff/);
    const empty = fixture(); empty.holdings = []; empty.totals.holding_count = 0;
    assert.match(renderToStaticMarkup(createElement(HoldingsContent, { data: empty, retry() {} })), /No open holdings/);
  } finally { await server.close(); }
});
