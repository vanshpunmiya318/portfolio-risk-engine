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

export function formatQuantity(value: number | null): string {
  return value === null ? 'Unavailable' : new Intl.NumberFormat('en-GB', { maximumFractionDigits: 8 }).format(value);
}
