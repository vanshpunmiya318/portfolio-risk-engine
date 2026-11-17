import assert from 'node:assert/strict';
import { test, before, after } from 'node:test';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { withDetailsClosed } from './helpers/closed-details.mjs';
let server, api, page;
const nativeFetch = globalThis.fetch;
before(async () => {
  server = await createServer({ configFile: false, plugins: [react()], server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
  api = await server.ssrLoadModule('/src/api/transactions.ts');
  page = await server.ssrLoadModule('/src/pages/Transactions.tsx');
});
after(async () => { globalThis.fetch = nativeFetch; await server?.close(); });
const fixture = () => ({ portfolio_id: 7, name: 'Recorded <portfolio>', currency: 'GBP', trades: [{ id: 12, date: '2026-01-03', transaction_type: 'BUY', instrument_id: 2, ticker: 'ABC', instrument_name: 'Example', instrument_currency: 'EUR', quantity: 0.00000000001, price: 0.0000123456789, currency: 'EUR', source: '<script>source</script>', issues: [{ code: 'currency_mismatch', message: 'Recorded currency differs; no conversion.' }] }], external_cash_flows: [{ id: 8, date: '2026-01-02', flow_type: 'DEPOSIT', amount: 100.125, currency: 'GBP', source: 'fixture', issues: [] }] });
const response = (body, status = 200) => new Response(JSON.stringify(body), { status });

test('record context and row issues stay visible while ordering and valuation mechanics are expandable', () => {
  const d = fixture();
  d.trades[0].quantity = -1e-11;
  d.trades.push({ ...structuredClone(d.trades[0]), id: 10, date: '2026-01-02', quantity: 0, price: 0, issues: [] });
  d.external_cash_flows[0].amount = -100.125;
  const before = structuredClone(d), json = JSON.stringify(d);
  const html = renderToStaticMarkup(createElement(page.TransactionsContent, { data: d }));
  const visible = withDetailsClosed(html);
  for (const text of ['Historical ledger records, not live prices', 'not external contributions', 'deposits and withdrawals', 'execution prices are recorded trade values', 'not current split-adjusted holdings', 'recorded currency', 'no conversion', 'currency_mismatch', 'Recorded currency differs', 'GBP', 'EUR', 'Instrument 2', 'Row 12', 'Row 10', '-1e-11', '0.0000123456789', '-100.125', '&lt;script&gt;source&lt;/script&gt;']) assert.ok(visible.includes(text), text);
  assert.ok(visible.indexOf('Row 12') < visible.indexOf('Row 10'), 'Keep the supplied transaction order');
  assert.ok(visible.includes('<td class="recorded-number">0</td>'));
  assert.match(html, /<details class="performance-methodology"><summary>About these records<\/summary>/);
  const detail = html.split('<summary>About these records</summary>')[1].split('</details>')[0];
  for (const text of ['Dates are recorded ledger dates', 'sorted by recorded date, then row ID, newest first', 'History does not require market prices or a successful valuation']) {
    assert.ok(detail.includes(text), text); assert.ok(!visible.includes(text), text);
  }
  assert.deepEqual(d, before); assert.equal(JSON.stringify(d), json);
});

test('ledger tables retain row identity, exact values and described scroll regions', () => {
  const data = fixture(); data.trades[0].source = 'source_identifier_'.repeat(20);
  data.trades[0].quantity = -1e-11;
  const before = structuredClone(data);
  const html = renderToStaticMarkup(createElement(page.TransactionsContent, { data }));
  assert.match(html, /<caption class="sr-only">Recorded security trades/);
  assert.match(html, /<caption class="sr-only">Recorded external cash flows/);
  assert.equal((html.match(/<th scope="row" class="table-date">/g) ?? []).length, 2);
  for (const role of ['table-identity', 'table-type', 'table-currency', 'table-metadata', 'recorded-number']) assert.ok(html.includes(`class="${role}"`));
  const hints = [...html.matchAll(/<p id="([^"]+)" class="table-scroll-hint">If columns extend/g)];
  assert.equal(hints.length, 2);
  assert.notEqual(hints[0][1], hints[1][1]);
  for (const [, id] of hints) assert.ok(html.includes(`aria-describedby="${id}"`));
  assert.ok(html.includes(data.trades[0].source)); assert.ok(html.includes('-1e-11'));
  assert.deepEqual(data, before);
  data.trades = []; data.external_cash_flows = [];
  assert.ok(!renderToStaticMarkup(createElement(page.TransactionsContent, { data })).includes('table-scroll-hint'));
});

test('runtime validation covers every nested field without assuming clean ledger data', () => {
  assert.equal(api.isTransactionsResponse(fixture()), true);
  const unusual = fixture(); unusual.trades[0].date = 'bad recorded date'; unusual.trades[0].ticker = null; unusual.trades[0].quantity = 0;
  assert.equal(api.isTransactionsResponse(unusual), true);
  for (const mutate of [d => delete d.trades[0].source, d => d.trades[0].price = null, d => d.trades[0].quantity = Infinity, d => d.trades[0].instrument_currency = 3, d => d.external_cash_flows[0].amount = '1', d => d.trades[0].issues[0].message = null, d => d.trades.push(d.trades[0]), d => d.external_cash_flows[0].id = 1.2]) {
    const d = fixture(); mutate(d); assert.equal(api.isTransactionsResponse(d), false);
  }
});
test('typed client uses configured portfolio, signal, and rejects mismatched or failed responses', async () => {
  const controller = new AbortController();
  globalThis.fetch = async (url, options) => { assert.equal(url, '/api/portfolios/7/transactions'); assert.ok(options.signal); assert.equal(options.headers.Accept, 'application/json'); return response(fixture()); };
  assert.deepEqual(await api.getTransactions(7, controller.signal), fixture());
  globalThis.fetch = async () => response(fixture());
  await assert.rejects(api.getTransactions(8, controller.signal), /mismatched/);
  globalThis.fetch = async () => response({ detail: 'Portfolio not found' }, 404);
  await assert.rejects(api.getTransactions(7, controller.signal), e => e.status === 404 && e.details.includes('Portfolio not found'));
  await assert.rejects(api.getTransactions(0, controller.signal), /positive integer/);
});
test('new loads abort old requests and suppress stale successes, failures and unmount completions', async () => {
  const pending = [];
  globalThis.fetch = (url, options) => new Promise((resolve, reject) => pending.push({ resolve, reject, signal: options.signal }));
  const loader = api.createTransactionsLoader(); const ready = [], failed = [];
  const first = loader.load(7, d => ready.push(d), e => failed.push(e));
  const second = loader.load(7, d => ready.push(d), e => failed.push(e));
  assert.equal(pending[0].signal.aborted, true);
  pending[1].resolve(response(fixture())); await second;
  pending[0].reject(new Error('stale failure')); await first;
  assert.equal(ready.length, 1); assert.equal(failed.length, 0);
  const third = loader.load(7, d => ready.push(d), e => failed.push(e)); loader.cancel();
  assert.equal(pending[2].signal.aborted, true); pending[2].resolve(response(fixture())); await third;
  assert.equal(ready.length, 1);
});
test('tables preserve currencies, precision, reasons, escape text and separate external flows', () => {
  const html = renderToStaticMarkup(createElement(page.TransactionsContent, { data: fixture() }));
  for (const text of ['Trades', 'External cash flows', '1e-11', '0.0000123456789', '100.125', 'GBP', 'EUR', 'Row 12', 'currency_mismatch', 'not external contributions', 'no conversion', '&lt;script&gt;source&lt;/script&gt;']) assert.ok(html.includes(text), text);
  assert.equal((html.match(/<table /g) || []).length, 2);
  assert.ok(!html.includes('<script>'));
  const empty = fixture(); empty.trades = []; empty.external_cash_flows = [];
  const emptyHtml = renderToStaticMarkup(createElement(page.TransactionsContent, { data: empty }));
  assert.ok(emptyHtml.includes('No security trades recorded.')); assert.ok(emptyHtml.includes('No external cash flows recorded.')); assert.ok(!emptyHtml.includes('Unable to load'));
  const zero = fixture(); zero.trades[0].quantity = 0; zero.external_cash_flows[0].amount = 0;
  assert.ok(renderToStaticMarkup(createElement(page.TransactionsContent, { data: zero })).includes('<td class="recorded-number">0</td>'));
});
