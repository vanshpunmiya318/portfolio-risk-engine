import { quantityPresentation } from '../lib/format';

/** Native disclosure keeps any shortened precision available to keyboard/touch users. */
export function QuantityValue({ value }: { value: number | null }) {
  const quantity = quantityPresentation(value);
  if (!quantity.rounded) return <span>{quantity.display}</span>;
  return <details className="quantity-details">
    <summary aria-label={`Rounded quantity ${quantity.display}. Expand for full supplied precision.`}>{quantity.display}</summary>
    <span className="cell-note">Display rounded to 15 significant digits.</span>
    <span className="cell-note">Supplied quantity: {quantity.exact}</span>
  </details>;
}
