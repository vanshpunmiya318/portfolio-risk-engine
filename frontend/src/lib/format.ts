export function formatCurrency(value: number, currency: string): string {
  return new Intl.NumberFormat('en-GB', {
    style: 'currency', currency, currencyDisplay: 'symbol',
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  }).format(value);
}

export function formatValuationDate(date: string): string {
  // API dates are calendar dates, not instants in the viewer's timezone.
  return new Intl.DateTimeFormat('en-GB', {
    day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC',
  }).format(new Date(`${date}T00:00:00Z`));
}

export function formatOptionalCurrency(value: number | null, currency: string): string {
  return value === null ? 'Unavailable' : formatCurrency(value, currency);
}

export function formatWeight(value: number | null): string {
  return value === null ? 'Unavailable' : new Intl.NumberFormat('en-GB', {
    style: 'percent', minimumFractionDigits: 2, maximumFractionDigits: 2,
  }).format(value);
}

/**
 * Quantity display policy (the API provides finite JS numbers, not decimal scales):
 * - null stays unavailable; both signed zeros display as 0; integers are not rounded.
 * - fractional values round via Number(value.toPrecision(15)), trimming trailing zeros.
 *   This relative precision retains small fractions; it is not a decimal-place cap.
 * - choose scientific notation for a nonzero display magnitude below 1e-8 AFTER rounding.
 * - a changed numeric value is visibly approximate and exposes its full supplied value.
 * This is a display convention, not a claim to identify binary noise. Real precision
 * beyond 15 significant digits is ambiguous and must remain accessible in the UI/data.
 * `exact` means String(value), the round-trip representation of the supplied JS number,
 * not the original JSON decimal token or an arbitrary-precision backend quantity.
 * Only display text is shortened: callers must retain the original number for data
 * and downloads. Significant-digit rounding cannot turn a finite nonzero value into 0.
 */
const QUANTITY_SIGNIFICANT_DIGITS = 15;
const QUANTITY_SCIENTIFIC_THRESHOLD = 1e-8;

export function quantityPresentation(value: number | null): { display: string; exact: string | null; rounded: boolean } {
  if (value === null) return { display: 'Unavailable', exact: null, rounded: false };
  if (value === 0) return { display: '0', exact: '0', rounded: false };
  const integer = Number.isInteger(value);
  const displayed = integer ? value : Number(value.toPrecision(QUANTITY_SIGNIFICANT_DIGITS));
  const rounded = displayed !== value;
  const text = Math.abs(displayed) < QUANTITY_SCIENTIFIC_THRESHOLD
    ? displayed.toExponential().replace('e', 'E')
    : new Intl.NumberFormat('en-GB', integer ? { maximumFractionDigits: 0 } : { maximumSignificantDigits: QUANTITY_SIGNIFICANT_DIGITS }).format(displayed);
  return { display: `${rounded ? '\u2248' : ''}${text}`, exact: String(value), rounded };
}

export function formatQuantity(value: number | null): string {
  return quantityPresentation(value).display;
}


export type DisplayUnit = 'wealth_index' | 'fraction' | 'ratio' | 'percentage_points';
/** Presentation only: inputs and exact observation data are never rounded in place. */
export function formatDisplayValue(value: number | null, unit: DisplayUnit): string {
  if (value === null) return 'Unavailable';
  if (unit === 'fraction') return formatWeight(value === 0 ? 0 : value);
  const digits = unit === 'wealth_index' ? 4 : 2;
  const display = new Intl.NumberFormat('en-GB', { minimumFractionDigits: unit === 'wealth_index' ? 2 : digits, maximumFractionDigits: digits })
    .format(unit === 'percentage_points' ? value * 100 : value);
  if (unit === 'wealth_index') return `${display} (index)`;
  if (unit === 'ratio') return `${display} (ratio)`;
  return `${value > 0 ? '+' : ''}${display} pp`;
}
