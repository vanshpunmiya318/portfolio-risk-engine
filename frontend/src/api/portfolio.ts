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
