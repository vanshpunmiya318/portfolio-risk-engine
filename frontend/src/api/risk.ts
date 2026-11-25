import { apiFetch } from './localSession.ts';

import { isAnalysisResponse, analysisValidation, performanceQuery, errorDetails, isCalendarDate } from './performance';
import type { AnalysisResponse, MetricValue, ChartSeries, PerformanceQuery } from './performance';

export interface RiskMetrics {
  annualized_volatility: MetricValue; beta: MetricValue; historical_var: MetricValue;
  historical_cvar: MetricValue; sharpe_ratio: MetricValue; maximum_drawdown: MetricValue;
}
export interface RiskResponse extends AnalysisResponse {
  portfolio_metrics: RiskMetrics; benchmark_metrics: RiskMetrics;
  drawdown: { portfolio: ChartSeries & { unit: 'fraction' }; benchmark: ChartSeries & { unit: 'fraction' } };
}
export interface RiskQuery extends PerformanceQuery { confidence?: number }
const { record, metric, ordered, finite, message } = analysisValidation;
const metrics = (v: unknown) => record(v) && ['annualized_volatility', 'historical_var', 'historical_cvar', 'maximum_drawdown'].every(k => metric(v[k], 'fraction')) &&
  ['beta', 'sharpe_ratio'].every(k => metric(v[k], 'ratio'));
const drawdown = (v: unknown) => record(v) && v.unit === 'fraction' && Array.isArray(v.unavailable_reasons) && v.unavailable_reasons.every(message) &&
  ordered(v.points, p => record(p) && isCalendarDate(p.date) && finite(p.value), 'date');
export function isRiskResponse(v: unknown): v is RiskResponse {
  return isAnalysisResponse(v) && record(v) && metrics(v.portfolio_metrics) && metrics(v.benchmark_metrics) &&
    record(v.drawdown) && drawdown(v.drawdown.portfolio) && drawdown(v.drawdown.benchmark);
}
export function riskQuery(query: RiskQuery): string {
  const params = new URLSearchParams(performanceQuery(query));
  if (query.confidence !== undefined) {
    if (!Number.isFinite(query.confidence) || query.confidence <= 0 || query.confidence >= 1) throw new Error('Confidence must be finite and strictly between 0 and 1.');
    params.set('confidence', String(query.confidence));
  }
  return params.toString();
}
export class RiskRequestError extends Error {
  status: number; details: string[];
  constructor(status: number, details: string[]) {
    super(`Risk request failed (HTTP ${status}).`); this.status = status; this.details = details;
  }
}
export async function getPortfolioRisk(id: number, query: RiskQuery, signal: AbortSignal): Promise<RiskResponse> {
  if (!Number.isSafeInteger(id) || id <= 0) throw new Error('Configure a positive integer portfolio ID.');
  const params = riskQuery(query);
  const response = await apiFetch(`/api/portfolios/${id}/risk${params ? `?${params}` : ''}`, {
    signal: AbortSignal.any([signal, AbortSignal.timeout(60_000)]), headers: { Accept: 'application/json' },
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new RiskRequestError(response.status, errorDetails(body));
  if (!isRiskResponse(body) || body.portfolio_id !== id) throw new Error('The API returned invalid or mismatched Risk data.');
  return body;
}
export function createRiskLoader() {
  let identity = 0;
  let controller: AbortController | undefined;
  return {
    cancel() { identity++; controller?.abort(); },
    async load(id: number, query: RiskQuery, ready: (data: RiskResponse) => void, failed: (error: unknown) => void) {
      controller?.abort(); const current = ++identity; controller = new AbortController(); const signal = controller.signal;
      try { const data = await getPortfolioRisk(id, query, signal); if (current === identity && !signal.aborted) ready(data); }
      catch (error) { if (current === identity && !signal.aborted) failed(error); }
    },
  };
}
