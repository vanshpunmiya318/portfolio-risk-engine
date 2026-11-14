import { isCalendarDate } from '../api/performance';
import type { ChartSeries, Coverage, ReturnInterval, PerformanceResponse } from '../api/performance';

export interface ChartDatum { date: string; value: number | null }
export interface TimeSeriesLine {
  id: string; label: string; points: ChartDatum[]; intervals: ReturnInterval[]; unavailable: string[];
}
export const dateNumber = (day: string) => Date.parse(`${day}T00:00:00Z`);

// Edges come from actual verified return intervals, never weekday/calendar guesses.
export function chartSegments(points: ChartDatum[], intervals: ReturnInterval[]): ChartDatum[][] {
  if (points.some((p, i) => !isCalendarDate(p.date) || (i > 0 && points[i - 1].date >= p.date))) return [];
  const edges = new Set(intervals.filter(i => i.start_date !== null && i.trading_sessions === 1 && Number.isFinite(i.return_value))
    .map(i => `${i.start_date}/${i.end_date}`));
  const segments: ChartDatum[][] = [];
  let current: ChartDatum[] = [];
  for (const point of points) {
    if (point.value === null || !Number.isFinite(point.value)) { current = []; continue; }
    const previous = current.at(-1);
    if (!previous || !edges.has(`${previous.date}/${point.date}`)) { current = []; segments.push(current); }
    current.push(point);
  }
  return segments;
}
function completeWealth(series: ChartSeries, coverage: Coverage, intervals: ReturnInterval[], start: string | null, end: string | null): string[] {
  const reasons = series.unavailable_reasons.map(r => `${r.message} (${r.code})`);
  if (!coverage.calendar_verified || !coverage.daily_eligible || coverage.complete !== true || coverage.missing_sessions.length > 0) reasons.push('Complete verified trading-session coverage is required.');
  if (series.unit !== 'wealth_index' || !start || !end || series.points.length === 0 || series.points[0]?.date !== start || series.points.at(-1)?.date !== end || series.points[0]?.value !== 1) reasons.push('A complete wealth series with a known 1.00 baseline is required.');
  if (intervals.length !== series.points.length - 1 || coverage.expected_intervals !== intervals.length || coverage.interval_count !== intervals.length || coverage.expected_sessions !== series.points.length) reasons.push('Wealth observations and coverage counts do not reconcile.');
  if (series.points.some((p, i, all) => !isCalendarDate(p.date) || !Number.isFinite(p.value) ||
      (i > 0 && (all[i - 1].date >= p.date || intervals[i - 1]?.start_date !== all[i - 1].date || intervals[i - 1]?.end_date !== p.date || intervals[i - 1]?.trading_sessions !== 1 || !Number.isFinite(intervals[i - 1]?.return_value))))) reasons.push('Chart dates must match consecutive verified return intervals without duplicates or gaps.');
  return [...new Set(reasons)];
}

export function performanceChartLines(data: PerformanceResponse) {
  const line = (side: 'portfolio' | 'benchmark'): TimeSeriesLine => {
    const series = data.wealth_index[side];
    const intervals = data[`${side}_intervals`];
    const unavailable = completeWealth(series, data[`${side}_coverage`], intervals, data.period.effective_baseline_date, data.period.effective_end_date);
    return { id: side, label: side === 'portfolio' ? 'Portfolio' : data.benchmark_ticker,
      points: unavailable.length ? [] : series.points, intervals, unavailable };
  };
  const portfolio = line('portfolio');
  const benchmark = line('benchmark');
  // Keep valid portfolio growth if benchmark comparison is not supported.
  if (!data.comparison_eligible && benchmark.unavailable.length === 0) {
    benchmark.points = []; benchmark.unavailable = ['Benchmark comparison requires matching verified intervals in the same currency.'];
  }
  const drawdown = deriveDrawdown(portfolio, data.portfolio_metrics.maximum_drawdown.value);
  return { growth: [portfolio, benchmark], drawdown: [drawdown] };
}

export function deriveDrawdown(wealth: TimeSeriesLine, expectedMaximum: number | null): TimeSeriesLine {
  const result: TimeSeriesLine = { ...wealth, id: 'drawdown', label: 'Portfolio drawdown', points: [], unavailable: [...wealth.unavailable] };
  if (result.unavailable.length || !wealth.points.length) return result;
  const segments = chartSegments(wealth.points, wealth.intervals);
  if (wealth.points[0].value !== 1 || segments.length !== 1 || segments[0].length !== wealth.points.length) {
    result.unavailable.push('Drawdown requires an uninterrupted normalized series including the 1.00 baseline.'); return result;
  }
  let peak = 1;
  let maximum = 0;
  for (const point of wealth.points) {
    if (point.value === null || !Number.isFinite(point.value)) {
      result.points = []; result.unavailable.push('Drawdown requires complete finite wealth observations.'); return result;
    }
    peak = Math.max(peak, point.value);
    // Backend analysis_adapters._Adapter.chart: -(wealth / wealth.cummax() - 1).
    const value = -(point.value / peak - 1);
    if (!Number.isFinite(value)) { result.points = []; result.unavailable.push('Drawdown is non-finite.'); return result; }
    maximum = Math.max(maximum, value);
    result.points.push({ date: point.date, value });
  }
  if (expectedMaximum === null || maximum !== expectedMaximum) {
    result.points = []; result.unavailable.push('Derived drawdown does not exactly reconcile with the backend maximum drawdown; chart withheld.');
  }
  return result;
}

export function observationIndex(key: string, current: number, count: number): number {
  if (count <= 0) return 0;
  if (key === 'Home') return 0;
  if (key === 'End') return count - 1;
  const change = key === 'ArrowRight' || key === 'ArrowUp' ? 1 : key === 'ArrowLeft' || key === 'ArrowDown' ? -1 : 0;
  return Math.max(0, Math.min(count - 1, current + change));
}
export function chartBounds(lines: TimeSeriesLine[], unit: 'wealth_index' | 'fraction') {
  const values = lines.flatMap(l => l.points.flatMap(p => p.value !== null && Number.isFinite(p.value) ? [p.value] : []));
  if (!values.length) return null;
  let minimum = Math.min(...values, unit === 'wealth_index' ? 1 : 0);
  let maximum = Math.max(...values, unit === 'wealth_index' ? 1 : 0);
  const padding = maximum === minimum ? (unit === 'wealth_index' ? .05 : .01) : (maximum - minimum) * .08;
  minimum = unit === 'fraction' ? Math.min(0, minimum) : minimum - padding;
  maximum += padding;
  return { minimum, maximum };
}
