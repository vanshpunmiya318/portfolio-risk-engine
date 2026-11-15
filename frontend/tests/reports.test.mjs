import assert from 'node:assert/strict';
import { test, before, after } from 'node:test';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { execFileSync } from 'node:child_process';

let server, api, page, downloads, fixtures;
const nativeFetch = globalThis.fetch;
before(async () => {
  server = await createServer({ configFile: false, plugins: [react()], server: { middlewareMode: true, hmr: false, ws: false, watch: null } });
  api = await server.ssrLoadModule('/src/api/reports.ts');
  page = await server.ssrLoadModule('/src/pages/Reports.tsx');
  downloads = await server.ssrLoadModule('/src/lib/reportDownloads.ts');
  const python = process.platform === 'win32' ? '../.venv/Scripts/python.exe' : '../.venv/bin/python';
  fixtures = JSON.parse(execFileSync(python, ['-B', '-c', `import json,sqlite3
from pathlib import Path
from riskengine.risk.report_service import get_report_response
c=sqlite3.connect(Path('../data/historical_portfolio.db').resolve().as_uri()+'?mode=ro',uri=True)
c.execute('BEGIN')
out={}
for name,start,end,benchmark in [('healthy','2026-10-23','2026-11-06','SPY'),('full','2024-01-02','2026-11-06','SPY'),('missing','2026-10-23','2026-11-06','ABSENT'),('weekend','2026-10-24','2026-11-07','SPY')]:
 out[name]=get_report_response(c,1,start,end,benchmark).model_dump(mode='json')
c.close()
print(json.dumps(out))`], { encoding: 'utf8', timeout: 120_000, maxBuffer: 4 * 1024 * 1024 }));
});
after(async () => { globalThis.fetch = nativeFetch; await server?.close(); });
const clone = () => structuredClone(fixtures.healthy);
const query = { start_date: '2026-10-23', end_date: '2026-11-06', benchmark: 'SPY', confidence: .95 };
const signal = () => new AbortController().signal;
const respond = (data, status = 200) => new Response(typeof data === 'string' ? data : JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
const artifact = (report = clone()) => ({ id: 1, query: { ...query }, report, jsonText: JSON.stringify(report, null, 2) + '\n' });

test('validates complete real short/full/partial/weekend reports without dropping arrays', () => {
  for (const data of Object.values(fixtures)) assert.equal(api.isReportResponse(data), true);
  assert.equal(fixtures.full.performance.valuations.length, 716);
  const reordered = clone(); reordered.period = Object.fromEntries(Object.entries(reordered.period).reverse());
  assert.equal(api.isReportResponse(reordered), true);
});

test('rejects malformed nested fields, reasons, provenance, identities and dates', () => {
  for (const change of [
    d => { delete d.endpoint.cash_balance; }, d => { d.endpoint.total_equity.value = Infinity; },
    d => { d.endpoint.invested_value.value = null; }, d => { d.endpoint.cash_balance.unavailable_reasons = [{}]; },
    d => { delete d.provenance.price_sources; }, d => { d.provenance.price_sources = [null]; },
    d => { d.provenance.persisted_snapshot_id = 'not-supported-by-schema-1.0'; }, d => { delete d.provenance.persisted_snapshot_id; },
    d => { d.provenance.schema_version = '2.0'; }, d => { d.provenance.point_in_time_cutoff = '2026-11-05'; },
    d => { d.risk.portfolio_id = 2; }, d => { d.performance.currency = 'EUR'; },
    d => { d.holdings.effective_valuation_date = '2026-11-05'; }, d => { d.allocation.current.valuation_date = '2026-11-05'; },
    d => { d.period.selected_start_date = '2026-02-30'; }, d => { d.risk.portfolio_metrics.beta.unit = 'percent'; },
    d => { d.performance.valuations[0].total_equity = NaN; }, d => { d.risk.drawdown.portfolio.points[0].value = Infinity; },
    d => { delete d.risk.methodology; }, d => { d.holdings.holdings[0].price_status = 'invented'; },
    d => { d.allocation.target_allocation = []; }, d => { d.allocation.reference.snapshot.securities[0].weight = 'bad'; },
    d => { d.performance.portfolio_metrics.unrecognized = {}; }, d => { d.performance.comparison.unrecognized = {}; },
    d => { d.provenance.unrecognized = {}; }, d => { d.risk.methodology.unrecognized = {}; },
    d => { d.performance.metric_conventions.unrecognized = {}; },
  ]) { const d = clone(); change(d); assert.equal(api.isReportResponse(d), false); }
});

test('draft controls are Apply-only values and encode all parameters explicitly', () => {
  assert.deepEqual(api.draftQuery(api.initialReportDraft), { benchmark: 'SPY', confidence: .95 });
  assert.equal(api.reportQuery(api.draftQuery(api.initialReportDraft)), 'benchmark=SPY&confidence=0.95');
  const draft = { start: '2026-10-23', end: '2026-11-06', benchmark: ' spy ', confidence: '.95' };
  assert.equal(api.hasDraftChanges(draft, query), false);
  draft.end = '2026-11-07'; assert.equal(api.hasDraftChanges(draft, query), true);
  assert.equal(query.end_date, '2026-11-06');
  assert.equal(api.reportQuery({ ...query, benchmark: 'A&B' }), 'start_date=2026-10-23&end_date=2026-11-06&benchmark=A%26B&confidence=0.95');
});

test('invalid controls never fetch and invalid applied echoes never become preview artifacts', async () => {
  globalThis.fetch = () => assert.fail('Invalid request must not fetch');
  for (const q of [{ ...query, confidence: NaN }, { ...query, confidence: 1 }, { ...query, benchmark: ' ' },
    { ...query, start_date: '2026-02-30' }, { ...query, start_date: '2027-01-01' }]) {
    await assert.rejects(api.getPortfolioReport({ id: 1, query: q }, signal()));
  }
  await assert.rejects(api.getPortfolioReport({ id: 0, query }, signal()));
  for (const change of [d => { d.portfolio_id = 2; }, d => { d.risk.confidence = .99; },
    d => { d.period.requested_end_date = null; }, d => { d.performance.benchmark_ticker = 'QQQ'; }]) {
    const d = clone(); change(d); globalThis.fetch = async () => respond(d);
    await assert.rejects(api.getPortfolioReport({ id: 1, query }, signal()), /invalid or mismatched/);
  }
});

test('retains exact validated JSON text and copies applied parameters away from mutable drafts', async () => {
  const d = clone(), raw = JSON.stringify(d, null, 3) + '\n', applied = { ...query };
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/portfolios/1/report?start_date=2026-10-23&end_date=2026-11-06&benchmark=SPY&confidence=0.95');
    assert.ok(options.signal instanceof AbortSignal); return respond(raw);
  };
  const result = await api.getPortfolioReport({ id: 1, query: applied }, signal());
  assert.equal(result.jsonText, raw); assert.deepEqual(result.report, JSON.parse(raw));
  applied.end_date = '2027-01-01'; assert.equal(result.query.end_date, query.end_date);
  globalThis.fetch = () => assert.fail('Preview JSON download must not fetch');
  const download = downloads.previewJsonDownload(result);
  assert.equal(await download.blob.text(), raw);
  assert.equal(download.blob.type, 'application/json');
  assert.equal(download.filename, 'portfolio-1-report-2026-10-23-to-2026-11-06-asof-2026-11-06.json');
});

test('runtime units, signs, zero, null and reasons survive summary rendering without mutation', () => {
  const d = clone();
  d.endpoint.cash_balance.value = 0;
  d.risk.portfolio_metrics.historical_var.value = -.0125;
  d.performance.portfolio_metrics.annualized_return.unit = 'ratio';
  d.performance.portfolio_metrics.annualized_return.value = 1.25;
  assert.equal(api.isReportResponse(d), true);
  assert.equal(page.reportMetricLabel(d.performance.portfolio_metrics.annualized_return), '1.25 (ratio)');
  assert.equal(page.reportMetricLabel(d.risk.portfolio_metrics.historical_var), '-1.25%');
  const before = structuredClone(d);
  const html = renderToStaticMarkup(createElement(page.ReportContent, { data: d }));
  assert.ok(html.includes('$0.00')); assert.ok(html.includes('-1.25%')); assert.ok(html.includes('1.25 (ratio)'));
  assert.deepEqual(d, before);
  const partial = renderToStaticMarkup(createElement(page.ReportContent, { data: fixtures.missing }));
  assert.ok(partial.includes('Partial report')); assert.ok(partial.includes('comparison_unavailable'));
  assert.ok(partial.includes('151,597.57')); assert.ok(partial.includes('Unavailable'));
});

test('nullable sections and genuine empty holdings remain different from failed requests', () => {
  const d = clone(); d.holdings = null; d.allocation = null; d.status = 'unavailable';
  let html = renderToStaticMarkup(createElement(page.ReportContent, { data: d }));
  assert.ok(html.includes('Financial results unavailable')); assert.ok(html.includes('Holdings unavailable'));
  assert.ok(!html.includes('No invested holdings.'));
  d.holdings = clone().holdings; d.holdings.holdings = []; d.holdings.totals.holding_count = 0; d.holdings.totals.valued_holding_count = 0;
  d.holdings.totals.market_value = 0; d.status = 'partial';
  html = renderToStaticMarkup(createElement(page.ReportContent, { data: d }));
  assert.ok(html.includes('No invested holdings.'));
});

test('dates, fresh-download disclosure, provenance and escaped backend text appear without chart observations', () => {
  const d = structuredClone(fixtures.weekend); d.name = '<script>unsafe</script>';
  const html = renderToStaticMarkup(createElement(page.ReportContent, { data: d }));
  assert.ok(html.includes('&lt;script&gt;unsafe&lt;/script&gt;')); assert.ok(!html.includes('<script>unsafe'));
  assert.ok(html.includes('7 November 2026') && html.includes('6 November 2026'));
  assert.ok(html.includes('no persisted snapshot')); assert.ok(html.includes('Dataset origin is not provided'));
  assert.ok(html.includes('Corporate-action validation is source-dependent'));
  assert.ok(html.includes('not an investment-policy target'));
  const full = renderToStaticMarkup(createElement(page.ReportContent, { data: fixtures.full }));
  assert.ok(!full.includes('<svg')); assert.ok(!full.includes('3 June 2025'));
  const shell = renderToStaticMarkup(createElement(page.Reports));
  assert.ok(shell.includes('Blank dates use automatic history boundaries'));
  assert.ok(shell.includes('Full history clears draft dates only'));
  assert.ok(shell.includes('may differ if stored data changed'));
  assert.equal((shell.match(/ disabled=""/g) ?? []).length, 2);
});

test('Markdown uses retained applied parameters, validates content and never replaces preview', async () => {
  const a = artifact(); const before = structuredClone(a);
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/portfolios/1/report/export?start_date=2026-10-23&end_date=2026-11-06&benchmark=SPY&confidence=0.95&format=markdown');
    assert.equal(options.headers.Accept, 'text/markdown');
    return new Response('# Fresh report', { headers: { 'Content-Type': 'text/markdown; charset=utf-8',
      'Content-Disposition': 'attachment; filename="portfolio-1-report-2026-10-23-to-2026-11-06-asof-2026-11-06.md"' } });
  };
  const result = await api.getReportMarkdown(a, signal());
  assert.equal(result.text, '# Fresh report'); assert.ok(result.filename.endsWith('.md')); assert.deepEqual(a, before);
  globalThis.fetch = async () => new Response('<html>wrong</html>', { headers: { 'Content-Type': 'text/html' } });
  await assert.rejects(api.getReportMarkdown(a, signal()), /did not return a Markdown/);
  globalThis.fetch = async () => new Response('# safe', { headers: { 'Content-Type': 'text/markdown', 'Content-Disposition': 'attachment; filename="../../bad.md"' } });
  assert.equal((await api.getReportMarkdown(a, signal())).filename, 'portfolio-1-report-fresh.md');
  assert.deepEqual(a, before);
});

test('automatic boundaries stay automatic for fresh Markdown instead of being replaced by preview cutoffs', async () => {
  const a = artifact(); a.query = { benchmark: 'SPY', confidence: .95 };
  globalThis.fetch = async url => {
    assert.equal(url, '/api/portfolios/1/report/export?benchmark=SPY&confidence=0.95&format=markdown');
    return new Response('# newer cutoff', { headers: { 'Content-Type': 'text/markdown' } });
  };
  await api.getReportMarkdown(a, signal());
});

test('structured HTTP errors, malformed JSON and transport errors preserve usable preview artifacts', async () => {
  const a = artifact();
  for (const get of [() => api.getReportMarkdown(a, signal()), () => api.getPortfolioReport({ id: 1, query }, signal())]) {
    globalThis.fetch = async () => respond({ detail: [{ code: 'corporate_action_conflict', message: 'Coverage unavailable.' }] }, 409);
    await assert.rejects(get(), error => error instanceof api.ReportRequestError && error.status === 409 && error.details[0].includes('Coverage unavailable'));
  }
  globalThis.fetch = async () => respond({ detail: [{ loc: ['query', 'confidence'], msg: 'Bad confidence' }] }, 422);
  await assert.rejects(api.getReportMarkdown(a, signal()), error => error.details[0].includes('query / confidence'));
  globalThis.fetch = async () => respond('not JSON');
  await assert.rejects(api.getPortfolioReport({ id: 1, query }, signal()), /invalid Report JSON/);
  globalThis.fetch = async () => { throw new TypeError('Network down'); };
  await assert.rejects(api.getReportMarkdown(a, signal()), /Network down/);
  assert.equal(a.report.endpoint.total_equity.value, fixtures.healthy.endpoint.total_equity.value);
});

test('both loaders abort and ignore late successes/failures even when transport ignores abort; retry recovers', async () => {
  for (const markdown of [false, true]) {
    const pending = [], ready = [], failed = [];
    globalThis.fetch = (url, options) => new Promise((resolve, reject) => pending.push({ resolve, reject, signal: options.signal }));
    const loader = markdown ? api.createMarkdownLoader() : api.createReportLoader();
    const input = markdown ? artifact() : { id: 1, query };
    const first = loader.load(input, x => ready.push(x), e => failed.push(e));
    const second = loader.load(input, x => ready.push(x), e => failed.push(e));
    assert.ok(pending[0].signal.aborted);
    pending[0].reject(new Error('stale error'));
    pending[1].resolve(markdown ? new Response('# current', { headers: { 'Content-Type': 'text/markdown' } }) : respond(clone()));
    await Promise.all([first, second]); assert.equal(ready.length, 1); assert.equal(failed.length, 0);
    const third = loader.load(input, x => ready.push(x), e => failed.push(e)); loader.cancel();
    pending[2].resolve(markdown ? new Response('# stale', { headers: { 'Content-Type': 'text/markdown' } }) : respond(clone()));
    await third; assert.equal(ready.length, 1);
    const fourth = loader.load(input, x => ready.push(x), e => failed.push(e)); pending[3].reject(new Error('retry me')); await fourth;
    assert.equal(failed.length, 1);
    const fifth = loader.load(input, x => ready.push(x), e => failed.push(e));
    pending[4].resolve(markdown ? new Response('# retry', { headers: { 'Content-Type': 'text/markdown' } }) : respond(clone()));
    await fifth; assert.equal(ready.length, 2);
  }
});

test('Blob download revokes object URLs and removes temporary anchors', async () => {
  const originalDocument = globalThis.document, create = URL.createObjectURL, revoke = URL.revokeObjectURL;
  const actions = [];
  const link = { remove: () => actions.push('remove'), click: () => actions.push('click') };
  globalThis.document = { createElement: () => link, body: { append: () => actions.push('append') } };
  URL.createObjectURL = () => 'blob:test'; URL.revokeObjectURL = value => actions.push(value);
  try {
    downloads.saveDownload(new Blob(['x']), 'safe.json');
    assert.equal(link.download, 'safe.json'); assert.equal(link.href, 'blob:test');
    await new Promise(resolve => setTimeout(resolve, 10));
    assert.deepEqual(actions, ['append', 'click', 'remove', 'blob:test']);
  } finally { globalThis.document = originalDocument; URL.createObjectURL = create; URL.revokeObjectURL = revoke; }
});
