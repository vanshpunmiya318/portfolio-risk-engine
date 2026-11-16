import { errorDetails } from './performance';
export interface HistoryIssue { code: string; message: string }
interface RecordedRow { id: number; date: string; currency: string; source: string; issues: HistoryIssue[] }
export interface TradeRow extends RecordedRow { transaction_type: string; instrument_id: number; ticker: string | null; instrument_name: string | null; instrument_currency: string | null; quantity: number; price: number }
export interface CashFlowRow extends RecordedRow { flow_type: string; amount: number }
export interface TransactionsResponse { portfolio_id: number; name: string; currency: string; trades: TradeRow[]; external_cash_flows: CashFlowRow[] }
const record = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);
const text = (v: unknown): v is string => typeof v === 'string';
const id = (v: unknown): v is number => typeof v === 'number' && Number.isSafeInteger(v);
const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const nullableText = (v: unknown) => v === null || text(v);
const row = (v: unknown): v is Record<string, unknown> => record(v) && id(v.id) && text(v.date) && text(v.currency) && text(v.source) && Array.isArray(v.issues) && v.issues.every(i => record(i) && text(i.code) && text(i.message));
export function isTransactionsResponse(v: unknown): v is TransactionsResponse {
  return record(v) && id(v.portfolio_id) && v.portfolio_id > 0 && text(v.name) && text(v.currency) &&
    Array.isArray(v.trades) && v.trades.every(t => row(t) && text(t.transaction_type) && id(t.instrument_id) && nullableText(t.ticker) && nullableText(t.instrument_name) && nullableText(t.instrument_currency) && finite(t.quantity) && finite(t.price)) &&
    new Set(v.trades.map(t => t.id)).size === v.trades.length &&
    Array.isArray(v.external_cash_flows) && v.external_cash_flows.every(c => row(c) && text(c.flow_type) && finite(c.amount)) && new Set(v.external_cash_flows.map(c => c.id)).size === v.external_cash_flows.length;
}
export class TransactionsRequestError extends Error {
  constructor(public status: number, public details: string[]) { super(`Transactions request failed (HTTP ${status}).`); }
}
export async function getTransactions(portfolioId: number, signal: AbortSignal): Promise<TransactionsResponse> {
  if (!Number.isSafeInteger(portfolioId) || portfolioId <= 0) throw new Error('Configure a positive integer portfolio ID.');
  const response = await fetch(`/api/portfolios/${portfolioId}/transactions`, { signal: AbortSignal.any([signal, AbortSignal.timeout(60_000)]), headers: { Accept: 'application/json' } });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new TransactionsRequestError(response.status, errorDetails(body));
  if (!isTransactionsResponse(body) || body.portfolio_id !== portfolioId) throw new Error('The API returned invalid or mismatched Transactions data.');
  return body;
}
export function createTransactionsLoader() {
  let identity = 0; let controller: AbortController | undefined;
  return {
    cancel() { identity++; controller?.abort(); },
    async load(portfolioId: number, ready: (d: TransactionsResponse) => void, failed: (e: unknown) => void) {
      controller?.abort(); const current = ++identity; controller = new AbortController(); const signal = controller.signal;
      try { const data = await getTransactions(portfolioId, signal); if (current === identity && !signal.aborted) ready(data); }
      catch (error) { if (current === identity && !signal.aborted) failed(error); }
    },
  };
}
