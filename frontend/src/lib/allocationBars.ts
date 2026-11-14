import type { AllocationResponse } from '../api/allocation';
import { allocationComparison } from './allocationComparison';

export function allocationBars(data: AllocationResponse) {
  const comparison = allocationComparison(data);
  const reasons = [
    ...data.current.unavailable_reasons, ...data.reference.unavailable_reasons,
    ...(data.reference.snapshot?.unavailable_reasons ?? []), ...data.comparison_unavailable_reasons,
  ].map(reason => `${reason.message} (${reason.code})`);
  if (!comparison.comparisonValid) reasons.push('Both complete invested valuations and a valid historical starting reference are required.');
  const currentWeights = new Map(data.current.securities.map(row => [row.ticker, row.weight]));
  const referenceWeights = new Map(data.reference.snapshot?.securities.map(row => [row.ticker, row.weight]) ?? []);
  if (data.changes?.some(row => !Number.isFinite(row.current_weight) || !Number.isFinite(row.reference_weight) || !Number.isFinite(row.weight_change_fraction) ||
      row.current_weight !== (currentWeights.has(row.ticker) ? currentWeights.get(row.ticker) : 0) ||
      row.reference_weight !== (referenceWeights.has(row.ticker) ? referenceWeights.get(row.ticker) : 0))) {
    reasons.push('Comparison weights must be available and agree with both supplied snapshots; only absent positions can have explicit zero weights.');
  }
  const rows = comparison.rows;
  if (!rows.length) reasons.push('No securities are available for allocation comparison.');
  if (rows.some(row => row.currentWeight === null || row.referenceWeight === null || row.change === null ||
      !Number.isFinite(row.currentWeight) || !Number.isFinite(row.referenceWeight) || !Number.isFinite(row.change) ||
      row.currentWeight < 0 || row.currentWeight > 1 || row.referenceWeight < 0 || row.referenceWeight > 1)) {
    reasons.push('Every plotted weight must be a finite fraction between zero and one; missing values are not zero.');
  }
  // Validate totals only; never renormalize or derive replacement weights.
  if (!reasons.length) {
    for (const key of ['currentWeight', 'referenceWeight'] as const) {
      const total = rows.reduce((sum, row) => sum + row[key]!, 0);
      if (Math.abs(total - 1) > 1e-10) reasons.push('Complete invested weights must sum to one for both observations; chart withheld without renormalization.');
    }
  }
  return { rows: reasons.length ? [] : [...rows].sort((a, b) => b.currentWeight! - a.currentWeight! || (a.ticker < b.ticker ? -1 : a.ticker > b.ticker ? 1 : 0)),
    reasons: [...new Set(reasons)], scaleMaximum: 1 as const };
}

export const exactAllocationPercent = (value: number) => new Intl.NumberFormat('en-GB', { style: 'percent', maximumFractionDigits: 20 }).format(value);
export const exactAllocationChange = (value: number) => `${value > 0 ? '+' : ''}${new Intl.NumberFormat('en-GB', { maximumFractionDigits: 20 }).format(value * 100)} percentage points`;
