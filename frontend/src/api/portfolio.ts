export interface PortfolioSummary {
  portfolio_id: number;
  name: string;
  currency: string;
  as_of_date: string;
  total_equity: number;
  net_contributions: number;
  profit_loss: number;
}

function isSummary(value: unknown): value is PortfolioSummary {
  if (typeof value !== 'object' || value === null) return false;
  const data = value as Record<string, unknown>;
  const date = data.as_of_date;
  return Number.isInteger(data.portfolio_id) &&
    typeof data.name === 'string' && data.name.trim().length > 0 &&
    typeof data.currency === 'string' && /^[A-Z]{3}$/.test(data.currency) &&
    typeof date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(date) &&
    Number.isFinite(Date.parse(`${date}T00:00:00Z`)) &&
    new Date(`${date}T00:00:00Z`).toISOString().slice(0, 10) === date &&
    ['total_equity', 'net_contributions', 'profit_loss'].every(
      (key) => typeof data[key] === 'number' && Number.isFinite(data[key]),
    );
}

export async function getPortfolioSummary(signal: AbortSignal): Promise<PortfolioSummary | null> {
  const response = await fetch('/api/portfolio/summary', {
    signal: AbortSignal.any([signal, AbortSignal.timeout(20_000)]),
    headers: { Accept: 'application/json' },
  });
  if (response.status === 204) return null;
  if (response.status === 404) {
    const body: unknown = await response.json().catch(() => null);
    if (typeof body === 'object' && body !== null && 'detail' in body &&
      ['No portfolio found', 'No market prices available'].includes(String(body.detail))) return null;
  }
  if (!response.ok) throw new Error(`Portfolio request failed (HTTP ${response.status}).`);
  const data: unknown = await response.json();
  if (data === null) return null;
  if (!isSummary(data)) throw new Error('The API returned an invalid portfolio summary.');
  return data;
}

export interface Holding {
  ticker: string;
  instrument_name: string;
  quantity: number | null;
  effective_price_date: string | null;
  price_age_days: number | null;
  price_status: 'current' | 'carried_forward' | 'stale' | 'missing' | 'invalid';
  market_price: number | null;
  market_price_currency: string;
  currency_status: 'consistent' | 'conflict';
  market_value: number | null;
  remaining_average_cost_basis: number | null;
  unrealized_gain_loss: number | null;
  invested_weight: number | null;
}

export interface HoldingsResponse {
  portfolio_id: number;
  name: string;
  currency: string;
  requested_valuation_date: string | null;
  effective_valuation_date: string | null;
  max_price_age_days: number;
  valuation_complete: boolean;
  weight_basis: 'invested_market_value_excluding_cash';
  weight_unit: 'fraction';
  cost_basis_method: 'remaining_average_cost_not_net_transaction_investment';
  holdings: Holding[];
  totals: {
    holding_count: number;
    valued_holding_count: number;
    market_value: number | null;
    remaining_average_cost_basis: number | null;
    unrealized_gain_loss: number | null;
  };
  warnings: { code: string; ticker: string | null; message: string }[];
}

const record = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null;
const nullableNumber = (value: unknown) => value === null || (typeof value === 'number' && Number.isFinite(value));
const text = (value: unknown): value is string => typeof value === 'string' && value.trim().length > 0;
const currencyCode = (value: unknown) => typeof value === 'string' && /^[A-Z]{3}$/.test(value);
const nonnegativeInteger = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
const calendarDate = (value: unknown) => value === null || (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) &&
  Number.isFinite(Date.parse(`${value}T00:00:00Z`)) && new Date(`${value}T00:00:00Z`).toISOString().slice(0, 10) === value);

function isHoldings(value: unknown): value is HoldingsResponse {
  if (!record(value) || !record(value.totals) || !Array.isArray(value.holdings) || !Array.isArray(value.warnings)) return false;
  const totals = value.totals;
  return nonnegativeInteger(value.portfolio_id) && Number(value.portfolio_id) > 0 && text(value.name) && currencyCode(value.currency) &&
    calendarDate(value.requested_valuation_date) && calendarDate(value.effective_valuation_date) &&
    nonnegativeInteger(value.max_price_age_days) && typeof value.valuation_complete === 'boolean' &&
    value.weight_basis === 'invested_market_value_excluding_cash' && value.weight_unit === 'fraction' &&
    value.cost_basis_method === 'remaining_average_cost_not_net_transaction_investment' &&
    nonnegativeInteger(value.totals.holding_count) && value.totals.holding_count === value.holdings.length &&
    nonnegativeInteger(value.totals.valued_holding_count) && value.totals.valued_holding_count <= value.holdings.length &&
    ['market_value', 'remaining_average_cost_basis', 'unrealized_gain_loss'].every((key) => nullableNumber(totals[key])) &&
    value.holdings.every((holding: unknown) => record(holding) && text(holding.ticker) && text(holding.instrument_name) &&
      currencyCode(holding.market_price_currency) && ['consistent', 'conflict'].includes(String(holding.currency_status)) &&
      calendarDate(holding.effective_price_date) && (holding.price_age_days === null || nonnegativeInteger(holding.price_age_days)) &&
      ['current', 'carried_forward', 'stale', 'missing', 'invalid'].includes(String(holding.price_status)) &&
      ['quantity', 'market_price', 'market_value', 'remaining_average_cost_basis', 'unrealized_gain_loss', 'invested_weight']
        .every((key) => nullableNumber(holding[key]))) &&
    value.warnings.every((warning: unknown) => record(warning) && text(warning.code) && text(warning.message) &&
      (warning.ticker === null || text(warning.ticker)));
}

export async function getPortfolioHoldings(portfolioId: number, signal: AbortSignal): Promise<HoldingsResponse> {
  if (!Number.isSafeInteger(portfolioId) || portfolioId <= 0) throw new Error('Configure a positive integer portfolio ID.');
  const response = await fetch(`/api/portfolios/${portfolioId}/holdings`, {
    signal: AbortSignal.any([signal, AbortSignal.timeout(20_000)]), headers: { Accept: 'application/json' },
  });
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    const detail = record(body) && text(body.detail) ? ` ${body.detail}` : '';
    throw new Error(`Holdings request failed (HTTP ${response.status}).${detail}`);
  }
  const data: unknown = await response.json().catch(() => null);
  if (!isHoldings(data) || data.portfolio_id !== portfolioId) throw new Error('The API returned invalid or mismatched holdings data.');
  return data;
}
