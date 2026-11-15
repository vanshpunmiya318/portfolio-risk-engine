import { analysisValidation, errorDetails, isAnalysisResponse, isCalendarDate } from './performance';
import type { AnalysisMessage, AnalysisPeriod, MetricValue, PerformanceResponse } from './performance';
import type { RiskResponse } from './risk';
import { riskQuery } from './risk';
import { isAllocationResponse } from './allocation';
import type { AllocationResponse } from './allocation';
import { isHoldings } from './portfolio';
import type { HoldingsResponse } from './portfolio';

export interface ReportAmount { value: number | null; unavailable_reasons: AnalysisMessage[] }
export interface ReportResponse {
  status: 'ok' | 'partial' | 'unavailable'; portfolio_id: number; name: string; currency: string;
  period: AnalysisPeriod;
  endpoint: { valuation_date: string | null; currency: string; total_equity: ReportAmount; invested_value: ReportAmount;
    cash_balance: ReportAmount; net_contributions: ReportAmount; remaining_average_cost_basis: ReportAmount };
  performance: PerformanceResponse; risk: RiskResponse; holdings: HoldingsResponse | null; allocation: AllocationResponse | null;
  warnings: AnalysisMessage[]; unavailable_reasons: AnalysisMessage[];
  provenance: { report_methodology: 'validated_portfolio_report_v1'; schema_version: '1.0';
    snapshot_policy: 'one_caller_owned_read_transaction'; persisted_snapshot_id: null;
    reproducibility: string; point_in_time_cutoff: string | null; latest_portfolio_price_date: string | null;
    price_sources: string[]; calendar_provenance: string | null; classification_basis: string; section_dates: string };
}
export interface ReportQuery { start_date?: string; end_date?: string; benchmark: string; confidence: number }
export interface ReportRequest { id: number; query: ReportQuery }
export interface ReportArtifact { readonly id: number; readonly query: Readonly<ReportQuery>; readonly report: ReportResponse; readonly jsonText: string }
export interface MarkdownArtifact { text: string; filename: string }
export interface ReportDraft { start: string; end: string; benchmark: string; confidence: string }
export const initialReportDraft: ReportDraft = { start: '', end: '', benchmark: 'SPY', confidence: '.95' };
const { record, finite, message, ordered } = analysisValidation;
const text = (v: unknown): v is string => typeof v === 'string';
const date = (v: unknown) => v === null || isCalendarDate(v);
const messages = (v: unknown) => Array.isArray(v) && v.every(message);
const nullableFinite = (v: unknown) => v === null || finite(v);
const amount = (v: unknown) => record(v) && nullableFinite(v.value) && messages(v.unavailable_reasons) &&
  (v.value === null) === ((v.unavailable_reasons as unknown[]).length > 0);
// Reports follow MetricValue's actual unit union, rather than inferring units
// from names. Existing standalone page validators remain unchanged.
const metric = (v: unknown) => amount(v) && record(v) && ['fraction', 'ratio'].includes(String(v.unit)) &&
  Number.isSafeInteger(v.observations) && Number(v.observations) >= 0;
const onlyKeys = (v: unknown, keys: string[]) => record(v) && Object.keys(v).every(k => keys.includes(k));
const metrics = (v: unknown, keys: string[]) => record(v) && onlyKeys(v, keys) && keys.every(k => metric(v[k]));
const performanceKeys = ['cumulative_return', 'annualized_return', 'annualized_volatility', 'sharpe_ratio', 'maximum_drawdown'];
const riskKeys = ['annualized_volatility', 'sharpe_ratio', 'maximum_drawdown', 'beta', 'historical_var', 'historical_cvar'];
const chart = (v: unknown, unit: string) => record(v) && v.unit === unit && messages(v.unavailable_reasons) &&
  ordered(v.points, p => record(p) && isCalendarDate(p.date) && finite(p.value), 'date');
const pair = (v: unknown, unit: string) => record(v) && chart(v.portfolio, unit) && chart(v.benchmark, unit);

export function isReportResponse(v: unknown): v is ReportResponse {
  if (!record(v) || !record(v.period) || !record(v.endpoint) || !record(v.provenance) || !record(v.performance) || !record(v.risk)) return false;
  const e = v.endpoint, p = v.provenance, performance = v.performance, risk = v.risk;
  if (!['ok', 'partial', 'unavailable'].includes(String(v.status)) || !Number.isSafeInteger(v.portfolio_id) || Number(v.portfolio_id) <= 0 ||
      !text(v.name) || typeof v.currency !== 'string' || !/^[A-Z]{3}$/.test(v.currency) || !date(e.valuation_date) || e.currency !== v.currency ||
      !['total_equity', 'invested_value', 'cash_balance', 'net_contributions', 'remaining_average_cost_basis'].every(k => amount(e[k])) ||
      !messages(v.warnings) || !messages(v.unavailable_reasons)) return false;
  if (!isAnalysisResponse(performance) || !isAnalysisResponse(risk) ||
      !metrics(performance.portfolio_metrics, performanceKeys) || !metrics(performance.benchmark_metrics, performanceKeys) ||
      !metrics(risk.portfolio_metrics, riskKeys) || !metrics(risk.benchmark_metrics, riskKeys) ||
      !metrics(performance.comparison, ['excess_cumulative_return', 'excess_annualized_return']) ||
      !pair(performance.wealth_index, 'wealth_index') || !pair(risk.drawdown, 'fraction')) return false;
  for (const section of [performance, risk]) {
    const period = section.period;
    if (section.portfolio_id !== v.portfolio_id || section.currency !== v.currency || section.name !== v.name || !record(period) ||
        ['requested_start_date', 'requested_end_date', 'selected_start_date', 'selected_end_date', 'effective_baseline_date', 'first_return_end_date', 'effective_end_date']
          .some(key => period[key] !== (v.period as Record<string, unknown>)[key])) return false;
  }
  if (performance.benchmark_ticker !== risk.benchmark_ticker || performance.confidence !== risk.confidence) return false;
  for (const section of [performance, risk]) {
    if (!onlyKeys(section.methodology, ['calendar_source', 'price_age_days', 'external_flow_timing', 'portfolio_price_basis', 'benchmark_return_basis', 'return_unit', 'monetary_unit', 'annualization_sessions']) ||
        !onlyKeys(section.metric_conventions, ['risk_free_rate', 'annualization_sessions', 'volatility_ddof', 'maximum_drawdown_sign', 'drawdown_series_sign', 'var_method', 'cvar_method', 'tail_horizon', 'chart_policy'])) return false;
  }
  if (v.holdings !== null && (!isHoldings(v.holdings) || v.holdings.portfolio_id !== v.portfolio_id || v.holdings.currency !== v.currency ||
      v.holdings.effective_valuation_date !== e.valuation_date || v.holdings.requested_valuation_date !== e.valuation_date || v.holdings.max_price_age_days !== 0)) return false;
  if (v.allocation !== null && (!isAllocationResponse(v.allocation) || v.allocation.portfolio_id !== v.portfolio_id || v.allocation.currency !== v.currency ||
      v.allocation.effective_valuation_date !== e.valuation_date || v.allocation.requested_valuation_date !== e.valuation_date)) return false;
  return onlyKeys(p, ['report_methodology', 'schema_version', 'snapshot_policy', 'persisted_snapshot_id', 'reproducibility', 'point_in_time_cutoff', 'latest_portfolio_price_date', 'price_sources', 'calendar_provenance', 'classification_basis', 'section_dates']) &&
    p.schema_version === '1.0' && p.report_methodology === 'validated_portfolio_report_v1' &&
    p.snapshot_policy === 'one_caller_owned_read_transaction' &&
    // ReportProvenance explicitly declares persisted_snapshot_id: None in v1.0.
    p.persisted_snapshot_id === null && p.point_in_time_cutoff === e.valuation_date && date(p.point_in_time_cutoff) &&
    date(p.latest_portfolio_price_date) && Array.isArray(p.price_sources) && p.price_sources.every(text) &&
    (p.calendar_provenance === null || text(p.calendar_provenance)) &&
    ['reproducibility', 'classification_basis', 'section_dates'].every(k => text(p[k]));
}

export function reportQuery(query: ReportQuery): string {
  if (typeof query.benchmark !== 'string' || !query.benchmark.trim()) throw new Error('Benchmark must not be empty.');
  if (typeof query.confidence !== 'number') throw new Error('Choose a confidence level.');
  return riskQuery(query);
}
export function draftQuery(draft: ReportDraft): ReportQuery {
  const query = { ...(draft.start ? { start_date: draft.start } : {}), ...(draft.end ? { end_date: draft.end } : {}),
    benchmark: draft.benchmark.trim().toUpperCase(), confidence: Number(draft.confidence) };
  reportQuery(query);
  return query;
}
export function hasDraftChanges(draft: ReportDraft, query: ReportQuery): boolean {
  try { return reportQuery(draftQuery(draft)) !== reportQuery(query); } catch { return true; }
}
export class ReportRequestError extends Error {
  constructor(public status: number, public details: string[]) { super(`Report request failed (HTTP ${status}).`); }
}
async function responseText(response: Response): Promise<string> {
  const content = await response.text();
  if (!response.ok) {
    let body: unknown; try { body = JSON.parse(content); } catch { body = null; }
    throw new ReportRequestError(response.status, errorDetails(body));
  }
  return content;
}
export async function getPortfolioReport({ id, query }: ReportRequest, signal: AbortSignal): Promise<ReportArtifact> {
  if (!Number.isSafeInteger(id) || id <= 0) throw new Error('Configure a positive integer portfolio ID.');
  const applied = Object.freeze({ ...query });
  const response = await fetch(`/api/portfolios/${id}/report?${reportQuery(applied)}`, {
    signal: AbortSignal.any([signal, AbortSignal.timeout(60_000)]), headers: { Accept: 'application/json' },
  });
  const jsonText = await responseText(response);
  let report: unknown; try { report = JSON.parse(jsonText); } catch { throw new Error('The API returned invalid Report JSON.'); }
  if (!isReportResponse(report) || report.portfolio_id !== id || report.performance.benchmark_ticker !== applied.benchmark.trim().toUpperCase() ||
      report.risk.confidence !== applied.confidence || report.period.requested_start_date !== (applied.start_date ?? null) ||
      report.period.requested_end_date !== (applied.end_date ?? null) ||
      report.endpoint.valuation_date !== (applied.end_date ?? report.period.selected_end_date)) throw new Error('The API returned invalid or mismatched Report data.');
  return { id, query: applied, report, jsonText };
}
export async function getReportMarkdown(artifact: ReportArtifact, signal: AbortSignal): Promise<MarkdownArtifact> {
  const params = new URLSearchParams(reportQuery(artifact.query)); params.set('format', 'markdown');
  const response = await fetch(`/api/portfolios/${artifact.id}/report/export?${params}`, {
    signal: AbortSignal.any([signal, AbortSignal.timeout(60_000)]), headers: { Accept: 'text/markdown' },
  });
  const content = await responseText(response);
  if (response.headers.get('content-type')?.split(';')[0].trim() !== 'text/markdown') throw new Error('The API did not return a Markdown download.');
  const filename = response.headers.get('content-disposition')?.match(/filename="([^"]+)"/)?.[1];
  return { text: content, filename: filename && filename.startsWith(`portfolio-${artifact.id}-report-`) && /^portfolio-\d+-report-(?:auto|\d{4}-\d{2}-\d{2})-to-(?:auto|\d{4}-\d{2}-\d{2})-asof-(?:undated|\d{4}-\d{2}-\d{2})\.md$/.test(filename)
    ? filename : `portfolio-${artifact.id}-report-fresh.md` };
}
export function createLatestLoader<Q, T>(fetcher: (query: Q, signal: AbortSignal) => Promise<T>) {
  let identity = 0; let controller: AbortController | undefined;
  return {
    cancel() { identity++; controller?.abort(); },
    async load(query: Q, ready: (value: T) => void, failed: (error: unknown) => void) {
      controller?.abort(); const current = ++identity; controller = new AbortController(); const signal = controller.signal;
      try { const value = await fetcher(query, signal); if (current === identity && !signal.aborted) ready(value); }
      catch (error) { if (current === identity && !signal.aborted) failed(error); }
    },
  };
}
export const createReportLoader = () => createLatestLoader(getPortfolioReport);
export const createMarkdownLoader = () => createLatestLoader(getReportMarkdown);
export type { MetricValue };
