import { apiFetch } from './localSession.ts';

import { analysisValidation, errorDetails, isCalendarDate } from './performance';

export interface AllocationIssue { code: string; message: string; scope: 'current' | 'reference' | 'target'; ticker: string | null }
export interface SecurityAllocation {
  ticker: string; instrument_name: string; classification: string; quantity: number | null;
  effective_price_date: string | null; price_status: string; market_value: number | null; weight: number | null;
}
export interface AllocationSnapshot {
  valuation_date: string | null; valuation_complete: boolean; invested_value: number | null; cash_balance: number | null;
  securities: SecurityAllocation[];
  classification_exposures: { classification: string; market_value: number; weight: number; holdings_count: number }[];
  concentration: { hhi: number | null; effective_holdings: number | null; largest_position_weight: number | null };
  unavailable_reasons: AllocationIssue[];
}
export interface AllocationChange { ticker: string; current_weight: number; reference_weight: number; weight_change_fraction: number }
export interface AllocationResponse {
  portfolio_id: number; name: string; currency: string; requested_valuation_date: string | null; effective_valuation_date: string | null;
  weight_basis: 'invested_holdings_excluding_cash'; weight_unit: 'fraction';
  classification_basis: 'current_instrument_metadata_not_look_through_or_historically_versioned'; max_price_age_days: 0;
  current: AllocationSnapshot;
  reference: { reference_date: string | null; methodology: 'first_recorded_buy_date_end_of_day_valuation';
    provenance: 'recorded_transactions_and_stored_exact_date_prices'; is_investment_policy_target: false;
    snapshot: AllocationSnapshot | null; unavailable_reasons: AllocationIssue[] };
  changes: AllocationChange[] | null; comparison_unavailable_reasons: AllocationIssue[];
  target_allocation: null; target_unavailable_reasons: AllocationIssue[]; warnings: AllocationIssue[];
}
const { record, finite } = analysisValidation;
const text = (v: unknown): v is string => typeof v === 'string' && v.trim().length > 0;
const count = (v: unknown): v is number => finite(v) && Number.isSafeInteger(v) && v >= 0;
const optionalNumber = (v: unknown) => v === null || finite(v);
const optionalDate = (v: unknown) => v === null || isCalendarDate(v);
const issues = (v: unknown) => Array.isArray(v) && v.every(i => record(i) && text(i.code) && text(i.message) &&
  ['current', 'reference', 'target'].includes(String(i.scope)) && (i.ticker === null || text(i.ticker)));
function snapshot(v: unknown): v is AllocationSnapshot {
  return record(v) && optionalDate(v.valuation_date) && typeof v.valuation_complete === 'boolean' && optionalNumber(v.invested_value) && optionalNumber(v.cash_balance) &&
    record(v.concentration) && ['hhi', 'effective_holdings', 'largest_position_weight'].every(k => optionalNumber((v.concentration as Record<string, unknown>)[k])) &&
    Array.isArray(v.securities) && v.securities.every(s => record(s) && ['ticker', 'instrument_name', 'classification', 'price_status'].every(k => text(s[k])) &&
      ['quantity', 'market_value', 'weight'].every(k => optionalNumber(s[k])) && optionalDate(s.effective_price_date)) &&
    new Set(v.securities.map(s => s.ticker)).size === v.securities.length &&
    Array.isArray(v.classification_exposures) && v.classification_exposures.every(e => record(e) && text(e.classification) && finite(e.market_value) && finite(e.weight) && count(e.holdings_count)) &&
    issues(v.unavailable_reasons);
}
export function isAllocationResponse(v: unknown): v is AllocationResponse {
  if (!record(v) || !record(v.reference)) return false;
  const r = v.reference;
  return count(v.portfolio_id) && v.portfolio_id > 0 && text(v.name) && typeof v.currency === 'string' && /^[A-Z]{3}$/.test(v.currency) &&
    optionalDate(v.requested_valuation_date) && optionalDate(v.effective_valuation_date) && v.weight_basis === 'invested_holdings_excluding_cash' &&
    v.weight_unit === 'fraction' && v.classification_basis === 'current_instrument_metadata_not_look_through_or_historically_versioned' && v.max_price_age_days === 0 && snapshot(v.current) &&
    v.current.valuation_date === v.effective_valuation_date && optionalDate(r.reference_date) && r.methodology === 'first_recorded_buy_date_end_of_day_valuation' &&
    r.provenance === 'recorded_transactions_and_stored_exact_date_prices' && r.is_investment_policy_target === false &&
    (r.snapshot === null || (snapshot(r.snapshot) && r.snapshot.valuation_date === r.reference_date)) && issues(r.unavailable_reasons) &&
    (v.changes === null || (Array.isArray(v.changes) && v.changes.every(c => record(c) && text(c.ticker) && finite(c.current_weight) && finite(c.reference_weight) && finite(c.weight_change_fraction)) &&
      new Set(v.changes.map(c => c.ticker)).size === v.changes.length)) && issues(v.comparison_unavailable_reasons) &&
    v.target_allocation === null && issues(v.target_unavailable_reasons) && issues(v.warnings);
}
export function allocationQuery(asOfDate?: string): string {
  if (asOfDate !== undefined && !isCalendarDate(asOfDate)) throw new Error('Use a valid YYYY-MM-DD valuation date.');
  return asOfDate === undefined ? '' : new URLSearchParams({ as_of_date: asOfDate }).toString();
}
export class AllocationRequestError extends Error {
  status: number; details: string[];
  constructor(status: number, details: string[]) { super(`Allocation request failed (HTTP ${status}).`); this.status = status; this.details = details; }
}
export async function getPortfolioAllocation(id: number, asOfDate: string | undefined, signal: AbortSignal): Promise<AllocationResponse> {
  if (!Number.isSafeInteger(id) || id <= 0) throw new Error('Configure a positive integer portfolio ID.');
  const params = allocationQuery(asOfDate);
  const response = await apiFetch(`/api/portfolios/${id}/allocation${params ? `?${params}` : ''}`, {
    signal: AbortSignal.any([signal, AbortSignal.timeout(60_000)]), headers: { Accept: 'application/json' },
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new AllocationRequestError(response.status, errorDetails(body));
  if (!isAllocationResponse(body) || body.portfolio_id !== id) throw new Error('The API returned invalid or mismatched Allocation data.');
  if (asOfDate !== undefined && (body.requested_valuation_date !== asOfDate || body.effective_valuation_date !== asOfDate)) throw new Error('The API returned a shifted historical valuation date.');
  return body;
}
export function createAllocationLoader() {
  let identity = 0; let controller: AbortController | undefined;
  return {
    cancel() { identity++; controller?.abort(); },
    async load(id: number, day: string | undefined, ready: (d: AllocationResponse) => void, failed: (e: unknown) => void) {
      controller?.abort(); const current = ++identity; controller = new AbortController(); const signal = controller.signal;
      try { const data = await getPortfolioAllocation(id, day, signal); if (current === identity && !signal.aborted) ready(data); }
      catch (error) { if (current === identity && !signal.aborted) failed(error); }
    },
  };
}
