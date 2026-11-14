import type { AllocationResponse } from '../api/allocation';

export function allocationComparison(data: AllocationResponse) {
  const current = data.current;
  const reference = data.reference.snapshot;
  const currentValid = current.valuation_complete && current.invested_value !== null && current.invested_value > 0;
  const referenceValid = reference !== null && reference.valuation_complete && reference.invested_value !== null && reference.invested_value > 0 && data.reference.unavailable_reasons.length === 0;
  const comparisonValid = currentValid && referenceValid && data.changes !== null && data.comparison_unavailable_reasons.length === 0;
  const now = new Map(current.securities.map(s => [s.ticker, s]));
  const then = new Map(reference?.securities.map(s => [s.ticker, s]) ?? []);
  const changes = new Map(comparisonValid ? data.changes!.map(c => [c.ticker, c]) : []);
  return { currentValid, referenceValid, comparisonValid, rows: [...new Set([...now.keys(), ...then.keys(), ...changes.keys()])].sort().map(ticker => {
    const n = now.get(ticker); const r = then.get(ticker); const c = changes.get(ticker);
    return { ticker, instrument: n?.instrument_name ?? r?.instrument_name ?? 'Name unavailable',
      currentWeight: currentValid ? c?.current_weight ?? n?.weight ?? null : null,
      referenceWeight: referenceValid ? c?.reference_weight ?? r?.weight ?? null : null,
      change: comparisonValid ? c?.weight_change_fraction ?? null : null,
      marketValue: n?.market_value ?? (comparisonValid && !n && c?.current_weight === 0 ? 0 : null),
    };
  }) };
}
export function formatAllocationChange(value: number | null): string {
  return value === null ? 'Unavailable' : `${value > 0 ? '+' : ''}${new Intl.NumberFormat('en-GB', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value * 100)} pp`;
}
