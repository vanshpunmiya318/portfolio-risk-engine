// Synthetic values are confined to tests; the application only uses API data.
import assert from 'node:assert/strict';
import { test, after } from 'node:test';
import { getPortfolioSummary, getPortfolioHoldings } from '../src/api/portfolio.ts';
import { buildAllocation } from '../src/lib/allocation.ts';
import { formatDisplayValue, formatOptionalCurrency, formatQuantity, formatValuationDate, formatWeight } from '../src/lib/format.ts';
import { withDetailsClosed } from './helpers/closed-details.mjs';

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
    assert.match(complete, /scope="row" class="table-identity"/);
    assert.match(complete, /class="table-status"><details class="price-details"/);
    assert.match(complete, /class="table-number">/);
    const hint = complete.match(/<p id="([^"]+)" class="table-scroll-hint">If columns extend/);
    assert.ok(hint);
    assert.ok(complete.includes(`aria-describedby="${hint[1]}"`));
    assert.match(complete, /role="region" tabindex="0"/);
    const empty = fixture(); empty.holdings = []; empty.totals.holding_count = 0;
    assert.match(renderToStaticMarkup(createElement(HoldingsContent, { data: empty, retry() {} })), /No open holdings/);
  } finally { await server.close(); }
});

test('Holdings retains visible valuation limits and local definitions without repeating the cost-basis footer', async () => {
  const { createServer } = await import('vite');
  const { default: react } = await import('@vitejs/plugin-react');
  const { createElement } = await import('react');
  const { renderToStaticMarkup } = await import('react-dom/server');
  const server = await createServer({ configFile: false, plugins: [react()], server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
  try {
    const { HoldingsContent } = await server.ssrLoadModule('/src/pages/Holdings.tsx');
    const d = fixture(); d.max_price_age_days = 3; d.valuation_complete = false;
    d.holdings[0].quantity = .1 + .2;
    Object.assign(d.holdings[0], { market_price: null, market_value: null, unrealized_gain_loss: null, effective_price_date: null, price_age_days: null, price_status: 'missing', market_price_currency: 'EUR', currency_status: 'conflict' });
    d.holdings[1].quantity = 1e-11; d.holdings[1].unrealized_gain_loss = -12.34;
    d.holdings.forEach(h => { h.invested_weight = null; });
    d.totals.market_value = null; d.totals.unrealized_gain_loss = null;
    d.warnings = [{ code: 'corporate_action_coverage', message: 'Corporate-action coverage incomplete.', ticker: 'ONE' }];
    const before = structuredClone(d);
    const html = renderToStaticMarkup(createElement(HoldingsContent, { data: d, retry() {} }));
    const visible = withDetailsClosed(html);
    for (const text of ['Historical valuation cutoff', '5 January 2026', 'not today', 'Maximum price age: 3 calendar days', 'Missing price', 'Currency conflict', 'EUR', 'corporate_action_coverage', 'Corporate-action coverage incomplete.', 'Incomplete valuation', 'Unavailable', 'Excludes uninvested cash', 'Weights exclude cash', 'Excludes cash', 'No currency conversions or missing-price estimates', '≈0.3', '1E-11', '12.34']) assert.ok(visible.includes(text), text);
    const costMetric = visible.split('<dt>Remaining cost basis</dt>')[1].split('</div>')[0];
    assert.match(costMetric, /Average-cost basis of open positions, not net transaction investment/);
    assert.equal(visible.split('not net transaction investment').length - 1, 1);
    assert.ok(html.includes('Supplied quantity: 0.30000000000000004'));
    assert.ok(!visible.includes('Supplied quantity: 0.30000000000000004'));
    assert.match(html, /class="price-status price-missing"/);
    assert.deepEqual(d, before);
  } finally { await server.close(); }
});


test('Overview renders supplied financial values with visible definitions and historical limitations', async () => {
  const { createServer } = await import('vite');
  const { default: react } = await import('@vitejs/plugin-react');
  const { createElement } = await import('react');
  const { renderToStaticMarkup } = await import('react-dom/server');
  const server = await createServer({ configFile: false, plugins: [react()], server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
  try {
    const { OverviewContent } = await server.ssrLoadModule('/src/pages/Overview.tsx');
    for (const profit of [-25.125, 0, 25.125]) {
      const data = { portfolio_id: 7, name: 'Selected <portfolio>', currency: 'GBP', as_of_date: '2026-01-05', total_equity: 1234.56789, net_contributions: 1200, profit_loss: profit };
      const before = structuredClone(data);
      const html = renderToStaticMarkup(createElement(OverviewContent, { state: { status: 'ready', data }, retry() {} }));
      const visible = withDetailsClosed(html);
      for (const [label, value, definition] of [
        ['Total equity', data.total_equity, 'Holdings value plus cash balance'],
        ['Net contributions', data.net_contributions, 'Deposits minus withdrawals'],
        ['Profit / loss', profit, 'Total equity minus net contributions'],
      ]) {
        const metric = visible.split(`<dt>${label}</dt>`)[1].split('</div>')[0];
        assert.ok(metric.includes(formatOptionalCurrency(value, 'GBP')));
        assert.ok(metric.includes(definition));
        if (label === 'Profit / loss') {
          if (profit) assert.ok(metric.includes(profit < 0 ? 'value-negative' : 'value-positive'));
          else assert.doesNotMatch(metric, /value-negative|value-positive/);
        }
      }
      assert.match(visible, /Selected &lt;portfolio&gt;/);
      assert.match(visible, /<time dateTime="2026-01-05">5 January 2026<\/time>/);
      assert.match(visible, /All amounts in GBP/);
      assert.match(visible, /not today’s account valuation or live market prices/);
      assert.match(visible, /latest available historical prices without a maximum price-age restriction/);
      assert.deepEqual(data, before, 'Rendering must not calculate or replace supplied values');
    }
  } finally { await server.close(); }
});

test('Overview unavailable states expose no financial cards and retry controls retain the selected portfolio', async () => {
  const { createServer } = await import('vite');
  const { default: react } = await import('@vitejs/plugin-react');
  const { createElement } = await import('react');
  const { renderToStaticMarkup } = await import('react-dom/server');
  const server = await createServer({ configFile: false, plugins: [react()], server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
  try {
    const { OverviewContent } = await server.ssrLoadModule('/src/pages/Overview.tsx');
    const requests = [];
    const data = { portfolio_id: 7, name: 'Selected portfolio', currency: 'GBP', as_of_date: '2026-01-05', total_equity: 0, net_contributions: 0, profit_loss: 0 };
    globalThis.fetch = async url => { requests.push(url); return new Response(JSON.stringify(data)); };
    let loaded;
    const retry = async () => { loaded = await getPortfolioSummary(signal(), 7); };
    function buttons(node) {
      if (!node || typeof node !== 'object') return [];
      return [...(node.type === 'button' ? [node] : []), ...[node.props?.children].flat(Infinity).flatMap(buttons)];
    }
    for (const state of [{ status: 'loading' }, { status: 'empty' }, { status: 'error', message: 'Portfolio request failed (HTTP 404). Portfolio 7 not found' }]) {
      const tree = OverviewContent({ state, retry });
      const html = renderToStaticMarkup(tree);
      assert.doesNotMatch(html, /<dt>Total equity|summary-metrics|£0\.00/);
      if (state.status === 'loading') {
        assert.match(html, /aria-busy="true"/); assert.match(html, /role="status"/);
        assert.equal(buttons(tree).length, 0);
      } else {
        assert.match(html, /aria-busy="false"/);
        if (state.status === 'error') { assert.match(html, /role="alert"/); assert.ok(html.includes(state.message)); }
        else assert.match(html, /No portfolio summary yet/);
        const [button] = buttons(tree); assert.equal(buttons(tree).length, 1);
        assert.equal(button.props.children, state.status === 'error' ? 'Try again' : 'Check again');
        await button.props.onClick();
        const ready = renderToStaticMarkup(createElement(OverviewContent, { state: { status: 'ready', data: loaded }, retry }));
        assert.match(ready, /Selected portfolio/); assert.equal((ready.match(/£0\.00/g) ?? []).length, 3);
      }
    }
    assert.deepEqual(requests, ['/api/portfolios/7/summary', '/api/portfolios/7/summary']);
  } finally { globalThis.fetch = nativeFetch; await server.close(); }
});

test('Overview requests the configured portfolio ID and rejects a mismatched response', async () => {
  const data = { portfolio_id: 7, name: 'Configured portfolio', currency: 'GBP', as_of_date: '2026-01-05', total_equity: 0, net_contributions: 0, profit_loss: 0 };
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/portfolios/7/summary');
    assert.equal(options.headers.Accept, 'application/json');
    assert.ok(options.signal instanceof AbortSignal);
    return new Response(JSON.stringify(data));
  };
  assert.deepEqual(await getPortfolioSummary(signal(), 7), data);
  respond(data);
  await assert.rejects(getPortfolioSummary(signal(), 2), /Overview returned portfolio 7, but portfolio 2 is configured/);
  let requested = false; globalThis.fetch = async () => { requested = true; throw new Error('must not fetch'); };
  await assert.rejects(getPortfolioSummary(signal(), 0), /positive integer/);
  assert.equal(requested, false);
});

test('Overview retains empty and HTTP-error behavior when portfolio identity is configured', async () => {
  globalThis.fetch = async () => new Response(null, { status: 204 });
  assert.equal(await getPortfolioSummary(signal(), 7), null);
  respond({ detail: 'No market prices available' }, 404);
  assert.equal(await getPortfolioSummary(signal(), 7), null);
  respond({ detail: 'Conflict' }, 409);
  await assert.rejects(getPortfolioSummary(signal(), 7), /Portfolio request failed \(HTTP 409\)/);
});


test('Overview treats a missing configured portfolio as an error without legacy fallback', async () => {
  const requests = [];
  globalThis.fetch = async (url) => { requests.push(url); return new Response(JSON.stringify({ detail: 'Portfolio 42 not found' }), { status: 404 }); };
  await assert.rejects(getPortfolioSummary(signal(), 42), /HTTP 404.*Portfolio 42 not found/);
  assert.deepEqual(requests, ['/api/portfolios/42/summary']);
});

test('Overview preserves null and rejects null financial fields instead of manufacturing zeros', async () => {
  respond(null); assert.equal(await getPortfolioSummary(signal(), 7), null);
  respond({ portfolio_id: 7, name: 'Other', currency: 'GBP', as_of_date: '2026-01-05', total_equity: null, net_contributions: 0, profit_loss: 0 });
  await assert.rejects(getPortfolioSummary(signal(), 7), /invalid portfolio summary/);
});


test('display formatting distinguishes indices, ratios, percentages and signed percentage-point differences', () => {
  assert.equal(formatDisplayValue(1.123456789, 'wealth_index'), '1.1235 (index)');
  assert.equal(formatDisplayValue(.0125, 'fraction'), '1.25%');
  assert.equal(formatDisplayValue(.0125, 'ratio'), '0.01 (ratio)');
  assert.equal(formatDisplayValue(.0125, 'percentage_points'), '+1.25 pp');
  assert.equal(formatDisplayValue(-.0125, 'percentage_points'), '-1.25 pp');
  assert.equal(formatDisplayValue(-.0125, 'fraction'), '-1.25%');
  assert.equal(formatDisplayValue(0, 'fraction'), '0.00%');
  for (const unit of ['wealth_index', 'fraction', 'ratio', 'percentage_points']) assert.equal(formatDisplayValue(null, unit), 'Unavailable');
});

test('share quantities trim whole-share padding and retain fractions including tiny nonzero values', () => {
  assert.equal(formatQuantity(63), '63'); assert.equal(formatQuantity(0), '0');
  assert.equal(formatQuantity(1.5), '1.5'); assert.equal(formatQuantity(1.0000000001), '1.0000000001');
  assert.equal(formatQuantity(1e-11), '1E-11'); assert.equal(formatQuantity(Number.MIN_VALUE), '5E-324');
  assert.equal(formatQuantity(-1e-11), '-1E-11'); assert.equal(formatQuantity(null), 'Unavailable');
});
