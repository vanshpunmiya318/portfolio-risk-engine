export interface AnalysisMessage {
  code: string; message: string; scope: string; category: string;
  metric: string | null; start_date: string | null; end_date: string | null; ticker: string | null;
}
export interface MetricValue {
  value: number | null; unit: 'fraction' | 'ratio'; observations: number; unavailable_reasons: AnalysisMessage[];
}
export interface PerformanceMetrics {
  cumulative_return: MetricValue; annualized_return: MetricValue; annualized_volatility: MetricValue;
  sharpe_ratio: MetricValue; maximum_drawdown: MetricValue;
}
export interface AnalysisPeriod {
  requested_start_date: string | null; requested_end_date: string | null;
  selected_start_date: string | null; selected_end_date: string | null;
  effective_baseline_date: string | null; first_return_end_date: string | null; effective_end_date: string | null;
}
export interface Coverage {
  calendar_verified: boolean; expected_sessions: number | null; expected_intervals: number | null;
  valuation_count: number; valid_valuation_count: number; interval_count: number;
  missing_sessions: string[]; complete: boolean | null; daily_eligible: boolean;
}
export interface ValuationPoint {
  date: string; holdings_value: number | null; cash_balance: number | null;
  total_equity: number | null; net_contributions: number | null; complete: boolean;
}
export interface ReturnInterval {
  start_date: string | null; end_date: string; return_value: number; trading_sessions: number | null;
}
export interface ChartSeries {
  unit: 'wealth_index' | 'fraction'; points: { date: string; value: number }[]; unavailable_reasons: AnalysisMessage[];
}
export interface PerformanceResponse {
  status: 'ok' | 'partial' | 'unavailable'; portfolio_id: number; name: string; currency: string;
  benchmark_ticker: string; benchmark_currency: string | null; confidence: number;
  period: AnalysisPeriod; portfolio_coverage: Coverage; benchmark_coverage: Coverage; comparison_eligible: boolean;
  methodology: {
    calendar_source: string | null; price_age_days: number; external_flow_timing: string;
    portfolio_price_basis: string; benchmark_return_basis: string; return_unit: string;
    monetary_unit: string; annualization_sessions: number;
  };
  metric_conventions: {
    risk_free_rate: number; annualization_sessions: number; volatility_ddof: number;
    maximum_drawdown_sign: string; drawdown_series_sign: string; var_method: string;
    cvar_method: string; tail_horizon: string; chart_policy: string;
  };
  valuations: ValuationPoint[]; portfolio_intervals: ReturnInterval[]; benchmark_intervals: ReturnInterval[];
  warnings: AnalysisMessage[]; unavailable_reasons: AnalysisMessage[];
  portfolio_metrics: PerformanceMetrics; benchmark_metrics: PerformanceMetrics;
  comparison: { excess_cumulative_return: MetricValue; excess_annualized_return: MetricValue };
  wealth_index: { portfolio: ChartSeries; benchmark: ChartSeries };
}
export interface PerformanceQuery { start_date?: string; end_date?: string; benchmark?: string }
const record = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null;
const text = (v: unknown): v is string => typeof v === 'string' && v.trim().length > 0;
const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const count = (v: unknown): v is number => finite(v) && Number.isSafeInteger(v) && v >= 0;
const nullable = (check: (v: unknown) => boolean) => (v: unknown) => v === null || check(v);
const array = (v: unknown, check: (v: unknown) => boolean) => Array.isArray(v) && v.every(check);
export const isCalendarDate = (v: unknown): v is string => typeof v === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(v) &&
  Number.isFinite(Date.parse(`${v}T00:00:00Z`)) && new Date(`${v}T00:00:00Z`).toISOString().slice(0, 10) === v;
const currency = (v: unknown) => typeof v === 'string' && /^[A-Z]{3}$/.test(v);
const message = (v: unknown) => record(v) && ['code', 'message', 'scope', 'category'].every(k => text(v[k])) &&
  ['metric', 'ticker'].every(k => nullable(text)(v[k])) && ['start_date', 'end_date'].every(k => nullable(isCalendarDate)(v[k]));
const metric = (v: unknown, unit: string) => record(v) && nullable(finite)(v.value) && v.unit === unit && count(v.observations) &&
  array(v.unavailable_reasons, message) && (v.value === null) === ((v.unavailable_reasons as unknown[]).length > 0);
const metrics = (v: unknown) => record(v) && ['cumulative_return', 'annualized_return', 'annualized_volatility', 'maximum_drawdown']
  .every(k => metric(v[k], 'fraction')) && metric(v.sharpe_ratio, 'ratio');
const coverage = (v: unknown) => record(v) && typeof v.calendar_verified === 'boolean' && typeof v.daily_eligible === 'boolean' &&
  (v.complete === null || typeof v.complete === 'boolean') && ['expected_sessions', 'expected_intervals'].every(k => nullable(count)(v[k])) &&
  ['valuation_count', 'valid_valuation_count', 'interval_count'].every(k => count(v[k])) &&
  Number(v.valid_valuation_count) <= Number(v.valuation_count) && array(v.missing_sessions, isCalendarDate);
const ordered = (items: unknown, check: (v: unknown) => boolean, key: string) => array(items, check) &&
  (items as Record<string, unknown>[]).every((v, i, all) => i === 0 || String(all[i - 1][key]) < String(v[key]));
const interval = (v: unknown) => record(v) && nullable(isCalendarDate)(v.start_date) && isCalendarDate(v.end_date) &&
  (v.start_date === null || String(v.start_date) < v.end_date) && finite(v.return_value) && nullable(count)(v.trading_sessions);
const chart = (v: unknown) => record(v) && v.unit === 'wealth_index' && array(v.unavailable_reasons, message) &&
  ordered(v.points, p => record(p) && isCalendarDate(p.date) && finite(p.value), 'date');

export function isPerformanceResponse(v: unknown): v is PerformanceResponse {
  if (!record(v) || !record(v.period) || !record(v.methodology) || !record(v.metric_conventions) ||
      !record(v.comparison) || !record(v.wealth_index)) return false;
  return ['ok', 'partial', 'unavailable'].includes(String(v.status)) && count(v.portfolio_id) && v.portfolio_id > 0 &&
    text(v.name) && currency(v.currency) && text(v.benchmark_ticker) && nullable(currency)(v.benchmark_currency) &&
    finite(v.confidence) && v.confidence > 0 && v.confidence < 1 && typeof v.comparison_eligible === 'boolean' &&
    ['requested_start_date', 'requested_end_date', 'selected_start_date', 'selected_end_date', 'effective_baseline_date', 'first_return_end_date', 'effective_end_date']
      .every(k => nullable(isCalendarDate)((v.period as Record<string, unknown>)[k])) &&
    coverage(v.portfolio_coverage) && coverage(v.benchmark_coverage) &&
    nullable(text)(v.methodology.calendar_source) && count(v.methodology.price_age_days) && count(v.methodology.annualization_sessions) &&
    ['external_flow_timing', 'portfolio_price_basis', 'benchmark_return_basis', 'return_unit', 'monetary_unit'].every(k => text((v.methodology as Record<string, unknown>)[k])) &&
    finite(v.metric_conventions.risk_free_rate) && ['annualization_sessions', 'volatility_ddof'].every(k => count((v.metric_conventions as Record<string, unknown>)[k])) &&
    ['maximum_drawdown_sign', 'drawdown_series_sign', 'var_method', 'cvar_method', 'tail_horizon', 'chart_policy'].every(k => text((v.metric_conventions as Record<string, unknown>)[k])) &&
    ordered(v.valuations, p => record(p) && isCalendarDate(p.date) && typeof p.complete === 'boolean' &&
      ['holdings_value', 'cash_balance', 'total_equity', 'net_contributions'].every(k => nullable(finite)(p[k])), 'date') &&
    ordered(v.portfolio_intervals, interval, 'end_date') && ordered(v.benchmark_intervals, interval, 'end_date') &&
    array(v.warnings, message) && array(v.unavailable_reasons, message) && metrics(v.portfolio_metrics) && metrics(v.benchmark_metrics) &&
    metric(v.comparison.excess_cumulative_return, 'fraction') && metric(v.comparison.excess_annualized_return, 'fraction') &&
    chart(v.wealth_index.portfolio) && chart(v.wealth_index.benchmark);
}

export function performanceQuery(query: PerformanceQuery): string {
  for (const day of [query.start_date, query.end_date]) if (day !== undefined && !isCalendarDate(day)) throw new Error('Use valid YYYY-MM-DD dates.');
  if (query.start_date && query.end_date && query.start_date > query.end_date) throw new Error('Start date must not follow end date.');
  if (query.benchmark !== undefined && !query.benchmark.trim()) throw new Error('Benchmark must not be empty.');
  const params = new URLSearchParams();
  for (const key of ['start_date', 'end_date', 'benchmark'] as const) if (query[key] !== undefined) params.set(key, query[key]);
  return params.toString();
}
export class PerformanceRequestError extends Error {
  status: number;
  details: string[];
  constructor(status: number, details: string[]) {
    super(`Performance request failed (HTTP ${status}).`);
    this.status = status;
    this.details = details;
  }
}
function errorDetails(body: unknown): string[] {
  if (!record(body)) return [];
  if (text(body.detail)) return [body.detail];
  if (!Array.isArray(body.detail)) return [];
  return body.detail.flatMap(item => {
    if (!record(item)) return [];
    if (text(item.message)) return [`${text(item.code) ? `${item.code}: ` : ''}${item.message}`];
    if (text(item.msg)) return [`${Array.isArray(item.loc) ? `${item.loc.join(' / ')}: ` : ''}${item.msg}`];
    return [];
  });
}
export async function getPortfolioPerformance(portfolioId: number, query: PerformanceQuery, signal: AbortSignal): Promise<PerformanceResponse> {
  if (!Number.isSafeInteger(portfolioId) || portfolioId <= 0) throw new Error('Configure a positive integer portfolio ID.');
  const params = performanceQuery(query);
  const response = await fetch(`/api/portfolios/${portfolioId}/performance${params ? `?${params}` : ''}`, {
    signal: AbortSignal.any([signal, AbortSignal.timeout(60_000)]), headers: { Accept: 'application/json' },
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new PerformanceRequestError(response.status, errorDetails(body));
  if (!isPerformanceResponse(body) || body.portfolio_id !== portfolioId) throw new Error('The API returned invalid or mismatched Performance data.');
  return body;
}

// Request identity also guards against transports that resolve after abort.
export function createPerformanceLoader() {
  let identity = 0;
  let controller: AbortController | undefined;
  return {
    cancel() { identity++; controller?.abort(); },
    async load(id: number, query: PerformanceQuery, ready: (data: PerformanceResponse) => void, failed: (error: unknown) => void) {
      controller?.abort();
      const current = ++identity;
      controller = new AbortController();
      const signal = controller.signal;
      try {
        const data = await getPortfolioPerformance(id, query, signal);
        if (current === identity && !signal.aborted) ready(data);
      } catch (error) {
        if (current === identity && !signal.aborted) failed(error);
      }
    },
  };
}
