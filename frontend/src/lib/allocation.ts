import type { HoldingsResponse } from '../api/portfolio';

export interface AllocationSegment { ticker: string; weight: number; offset: number }
export type Allocation = { available: true; segments: AllocationSegment[] } | { available: false; reason: string };

export function buildAllocation(data: HoldingsResponse): Allocation {
  if (data.holdings.length === 0) return { available: false, reason: 'No invested holdings to chart.' };
  if (!data.valuation_complete) return { available: false, reason: 'Allocation is unavailable while the portfolio valuation is incomplete.' };
  if (data.holdings.some((h) => h.invested_weight === null || !Number.isFinite(h.invested_weight) || h.invested_weight < 0 || h.invested_weight > 1)) {
    return { available: false, reason: 'Complete, valid portfolio weights are not available.' };
  }
  const total = data.holdings.reduce((sum, h) => sum + h.invested_weight!, 0);
  if (Math.abs(total - 1) > 0.000001) return { available: false, reason: 'Reported weights do not sum to 100%. Allocation has not been rescaled.' };
  let offset = 0;
  return { available: true, segments: data.holdings.map((holding) => {
    const segment = { ticker: holding.ticker, weight: holding.invested_weight!, offset };
    offset += segment.weight;
    return segment;
  }) };
}
