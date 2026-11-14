import { isCalendarDate } from '../api/performance';
import type { RiskResponse } from '../api/risk';
import type { TimeSeriesLine } from './performanceCharts';

/** Map the supplied drawdown observations without deriving or rescaling values. */
export function riskDrawdownLines(data: RiskResponse): TimeSeriesLine[] {
  return (['portfolio', 'benchmark'] as const).map(side => {
    const series = data.drawdown[side];
    const coverage = data[`${side}_coverage`];
    const intervals = data[`${side}_intervals`];
    const points = series.points;
    const unavailable = series.unavailable_reasons.map(reason => `${reason.message} (${reason.code})`);
    if (!coverage.calendar_verified || coverage.complete !== true || coverage.missing_sessions.length) {
      unavailable.push('The complete reporting window must have verified session coverage.');
    }
    if (!points.length) unavailable.push('No dated drawdown observations are available.');
    if (points.length && (points[0].date !== data.period.effective_baseline_date || points[0].value !== 0 || points.at(-1)?.date !== data.period.effective_end_date)) {
      unavailable.push('The drawdown series must include the actual zero-loss baseline and reporting end.');
    }
    // Do not use daily_eligible: that legacy flag can exclude valid one-interval charts.
    if (coverage.expected_sessions !== points.length || coverage.expected_intervals !== intervals.length ||
        coverage.interval_count !== intervals.length || intervals.length !== points.length - 1) {
      unavailable.push('Drawdown observations and return-interval coverage do not reconcile.');
    }
    if (series.unit !== 'fraction' || points.some((point, i) => !isCalendarDate(point.date) || !Number.isFinite(point.value) || point.value < 0 ||
        (i > 0 && (points[i - 1].date >= point.date || intervals[i - 1]?.start_date !== points[i - 1].date ||
          intervals[i - 1]?.end_date !== point.date || intervals[i - 1]?.trading_sessions !== 1 || !Number.isFinite(intervals[i - 1]?.return_value))))) {
      unavailable.push('Drawdown dates must match verified one-session intervals, with finite positive-loss magnitudes and no duplicates or gaps.');
    }
    if (side === 'benchmark' && data.benchmark_currency !== data.currency) {
      unavailable.push('Benchmark currency does not match the portfolio; no implicit FX comparison is shown.');
    }
    return { id: side, label: side === 'portfolio' ? 'Portfolio' : data.benchmark_ticker,
      points: unavailable.length ? [] : points, intervals, unavailable: [...new Set(unavailable)] };
  });
}
