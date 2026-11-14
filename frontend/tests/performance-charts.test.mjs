import assert from 'node:assert/strict';
import { test, before, after } from 'node:test';
import { existsSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

let server, helpers, TimeSeriesChart;
before(async () => {
  server = await createServer({ configFile: false, plugins: [react()], server: { middlewareMode: true, ws: false, hmr: false, watch: null } });
  helpers = await server.ssrLoadModule('/src/lib/performanceCharts.ts');
  ({ TimeSeriesChart } = await server.ssrLoadModule('/src/components/TimeSeriesChart.tsx'));
});
after(async () => { await server?.close(); });
const days = ['2026-01-02', '2026-01-05', '2026-01-06', '2026-01-07'];
const points = (values = [1, .9, 1.1, .88]) => values.map((value, i) => ({ date: days[i], value }));
const intervals = () => days.slice(1).map((end_date, i) => ({ start_date: days[i], end_date, trading_sessions: 1, return_value: .01 }));
const line = () => ({ id: 'portfolio', label: 'Portfolio', points: points(), intervals: intervals(), unavailable: [] });
function response() {
  const coverage = { calendar_verified: true, daily_eligible: true, complete: true, missing_sessions: [], expected_sessions: 4, expected_intervals: 3, interval_count: 3 };
  return { benchmark_ticker: 'SPY', comparison_eligible: true, period: { effective_baseline_date: days[0], effective_end_date: days[3] },
    portfolio_coverage: { ...coverage }, benchmark_coverage: { ...coverage }, portfolio_intervals: intervals(), benchmark_intervals: intervals(),
    portfolio_metrics: { maximum_drawdown: { value: -( .88 / 1.1 - 1) } },
    wealth_index: { portfolio: { unit: 'wealth_index', points: points(), unavailable_reasons: [] }, benchmark: { unit: 'wealth_index', points: points(), unavailable_reasons: [] } } };
}
test('normalized growth and drawdown retain baseline and exact backend operation order', () => {
  const charts = helpers.performanceChartLines(response());
  assert.equal(charts.growth[0].points[0].value, 1);
  assert.equal(charts.growth[1].points[0].value, 1);
  assert.equal(charts.drawdown[0].points[0].value, -0, 'Retain the backend formula’s signed zero');
  const expected = [-(1 / 1 - 1), -(.9 / 1 - 1), -(1.1 / 1.1 - 1), -(.88 / 1.1 - 1)];
  assert.deepEqual(charts.drawdown[0].points.map(p => p.value), expected);
  assert.deepEqual(charts.drawdown[0].points.map(p => p.date), days);
  assert.equal(helpers.deriveDrawdown(line(), .9).points.length, 0, 'Mismatched backend maximum withholds chart');
});
test('single baseline, flat returns and first-period losses preserve mathematical drawdown', () => {
  const first = { ...line(), points: points([1]), intervals: [] };
  assert.equal(helpers.deriveDrawdown(first, 0).points[0].value, -0);
  const flat = { ...line(), points: points([1, 1, 1, 1]) };
  assert.deepEqual(helpers.deriveDrawdown(flat, 0).points.map(p => Math.abs(p.value)), [0, 0, 0, 0]);
  assert.equal(helpers.deriveDrawdown(line(), null).points.length, 0);
});
test('missing dates break lines using interval boundaries; verified weekends remain connected', () => {
  assert.equal(helpers.chartSegments(points(), intervals()).length, 1, 'Friday to Monday is an actual verified interval');
  const sparse = points().filter(p => p.date !== days[1]);
  assert.deepEqual(helpers.chartSegments(sparse, intervals()).map(s => s.length), [1, 2]);
  const unknown = intervals(); unknown[1].trading_sessions = null;
  assert.deepEqual(helpers.chartSegments(points(), unknown).map(s => s.length), [2, 2]);
  const multi = intervals(); multi[1].trading_sessions = 2;
  assert.equal(helpers.chartSegments(points(), multi).length, 2);
});
test('duplicate/invalid/reversed dates and unavailable observations never create bridges', () => {
  for (const p of [[...points(), points()[3]], points().reverse(), [{ date: '2026-02-30', value: 1 }]]) assert.deepEqual(helpers.chartSegments(p, intervals()), []);
  for (const value of [null, NaN, Infinity]) {
    const p = points(); p[1].value = value;
    assert.deepEqual(helpers.chartSegments(p, intervals()).map(s => s.length), [1, 2]);
    assert.equal(helpers.deriveDrawdown({ ...line(), points: p }, .2).points.length, 0);
  }
});
test('incomplete portfolio or benchmark windows are withheld independently', () => {
  for (const alter of [
    d => { d.benchmark_coverage.complete = false; },
    d => { d.benchmark_intervals.splice(1, 1); },
    d => { d.wealth_index.benchmark.points.splice(1, 1); },
    d => { d.wealth_index.benchmark.points[0].value = 2; },
    d => { d.wealth_index.benchmark.unavailable_reasons = [{ message: 'Benchmark missing', code: 'missing' }]; },
    d => { d.comparison_eligible = false; },
  ]) {
    const d = response(); alter(d); const charts = helpers.performanceChartLines(d);
    assert.equal(charts.growth[0].points.length, 4);
    assert.equal(charts.growth[1].points.length, 0);
    assert.ok(charts.growth[1].unavailable.length > 0);
    assert.equal(charts.drawdown[0].points.length, 4);
  }
  const d = response(); d.portfolio_coverage.calendar_verified = false;
  assert.equal(helpers.performanceChartLines(d).drawdown[0].points.length, 0);
});
test('keyboard navigation clamps observations and supports Home/End and arrow keys', () => {
  const next = helpers.observationIndex;
  assert.equal(next('Home', 3, 4), 0); assert.equal(next('End', 0, 4), 3);
  assert.equal(next('ArrowLeft', 0, 4), 0); assert.equal(next('ArrowRight', 3, 4), 3);
  assert.equal(next('ArrowUp', 1, 4), 2); assert.equal(next('ArrowDown', 2, 4), 1);
  assert.equal(next('Enter', 2, 4), 2); assert.equal(next('End', 0, 0), 0);
});
test('chart boundaries include baseline, zero drawdown and constant-series padding', () => {
  const wealth = helpers.chartBounds([line()], 'wealth_index');
  assert.ok(wealth.minimum < .88 && wealth.maximum > 1.1);
  const flat = helpers.chartBounds([{ ...line(), points: points([1, 1]) }], 'wealth_index');
  assert.ok(flat.minimum < 1 && flat.maximum > 1);
  const dd = helpers.chartBounds([{ ...line(), points: points([0, .2]) }], 'fraction');
  assert.equal(dd.minimum, 0); assert.ok(dd.maximum > .2);
  assert.equal(helpers.chartBounds([], 'wealth_index'), null);
});
test('SVG, keyboard selector, exact tooltips and table remain accessible without a benchmark', () => {
  const missing = { ...line(), id: 'benchmark', label: 'SPY', points: [], unavailable: ['Missing benchmark data'] };
  const html = renderToStaticMarkup(createElement(TimeSeriesChart, { title: 'Growth', description: 'Verified observations only', lines: [line(), missing], unit: 'wealth_index', start: days[0], end: days[3] }));
  for (const text of ['role="img"', 'type="range"', 'min="0"', 'max="3"', 'Home/End', '7 January 2026', '0.88', 'SPY unavailable', 'Unavailable for this date', 'scope="col"', 'scope="row"', 'series-0', 'series-1', 'aria-live="polite"']) assert.ok(html.includes(text), text);
  assert.equal((html.match(/class="series-line"/g) ?? []).length, 1);
  const empty = renderToStaticMarkup(createElement(TimeSeriesChart, { title: 'Growth', description: '', lines: [missing], unit: 'wealth_index', start: null, end: null }));
  assert.ok(!empty.includes('<svg')); assert.ok(!empty.includes('type="range"'));
});
test('all historical drawdown points equal the existing backend adapter', { skip: !existsSync('../data/historical_portfolio.db') }, () => {
  const python = process.platform === 'win32' ? '../.venv/Scripts/python.exe' : '../.venv/bin/python';
  const script = `import json,sqlite3
from pathlib import Path
from contextlib import closing
from riskengine.risk.analysis_service import prepare_analysis
from riskengine.risk.analysis_adapters import build_performance_response,build_risk_response
from riskengine.risk.trading_calendar import verified_nyse_calendar
with closing(sqlite3.connect(Path('../data/historical_portfolio.db').resolve().as_uri()+'?mode=ro',uri=True)) as c:
 c.execute('BEGIN')
 p=prepare_analysis(c,1,session_calendar=verified_nyse_calendar('2024-01-02','2026-11-06'))
 print(json.dumps({'performance':build_performance_response(p).model_dump(mode='json'),'drawdown':build_risk_response(p).model_dump(mode='json')['drawdown']['portfolio']['points']}))
`;
  const { performance, drawdown } = JSON.parse(execFileSync(python, ['-c', script], { encoding: 'utf8', maxBuffer: 4 * 1024 * 1024, timeout: 120_000 }));
  const actual = helpers.performanceChartLines(performance).drawdown[0];
  assert.equal(actual.points.length, 716); assert.equal(actual.points[0].date, '2024-01-02');
  assert.deepEqual(actual.points, drawdown);
  assert.equal(JSON.stringify(actual.points), JSON.stringify(drawdown));
  assert.equal(Math.max(...actual.points.map(p => p.value)), performance.portfolio_metrics.maximum_drawdown.value);
});
