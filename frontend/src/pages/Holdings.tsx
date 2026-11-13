import { useEffect, useState } from 'react';
import { getPortfolioHoldings } from '../api/portfolio';
import type { HoldingsResponse } from '../api/portfolio';
import { AllocationDonut } from '../components/AllocationDonut';
import { formatOptionalCurrency, formatQuantity, formatValuationDate, formatWeight } from '../lib/format';

type State = { status: 'loading' } | { status: 'error'; message: string } | { status: 'ready'; data: HoldingsResponse };
const gainClass = (value: number | null) => value === null || value === 0 ? undefined : value < 0 ? 'value-negative' : 'value-positive';
const priceLabels = { current: 'Exact-date price', carried_forward: 'Earlier price', stale: 'Stale · excluded', missing: 'Missing price', invalid: 'Invalid · excluded' };

export function Holdings({ portfolioId = 1 }: { portfolioId?: number }) {
  const [state, setState] = useState<State>({ status: 'loading' });
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setState({ status: 'loading' });
    getPortfolioHoldings(portfolioId, controller.signal).then((data) => {
      if (!controller.signal.aborted) setState({ status: 'ready', data });
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setState({ status: 'error', message: error instanceof Error
        ? error.message : 'Unable to retrieve holdings. Check the API connection and try again.' });
    });
    return () => controller.abort();
  }, [portfolioId, attempt]);
  const retry = () => setAttempt((value) => value + 1);

  return <>
    <section className="page-intro holdings-intro"><p className="eyebrow">Portfolio intelligence</p><h1>Holdings</h1>
      <p className="lede">Understand your invested positions, remaining cost basis and historical valuation.</p></section>
    <section aria-label="Holdings data" aria-busy={state.status === 'loading'}>
      {state.status === 'loading' && <div className="summary-state" role="status"><p className="eyebrow">Retrieving positions</p>
        <h2>Loading holdings…</h2><p>Checking portfolio values and price quality.</p></div>}
      {state.status === 'error' && <div className="summary-state" role="alert"><p className="eyebrow">Unable to load</p>
        <h2>Holdings unavailable</h2><p>{state.message}</p><button className="summary-button" onClick={retry}>Try again</button></div>}
      {state.status === 'ready' && <HoldingsContent data={state.data} retry={retry} />}
    </section>
  </>;
}

export function HoldingsContent({ data, retry }: { data: HoldingsResponse; retry: () => void }) {
  const date = data.effective_valuation_date;
  const metrics = [
    { label: 'Invested market value', value: formatOptionalCurrency(data.totals.market_value, data.currency), note: 'Excludes uninvested cash' },
    { label: 'Remaining cost basis', value: formatOptionalCurrency(data.totals.remaining_average_cost_basis, data.currency), note: 'Average-cost basis, not net investment' },
    { label: 'Unrealized gain / loss', value: formatOptionalCurrency(data.totals.unrealized_gain_loss, data.currency), note: 'Open positions only', className: gainClass(data.totals.unrealized_gain_loss) },
    { label: 'Open holdings', value: String(data.totals.holding_count), note: `${data.totals.valued_holding_count} with usable valuations` },
  ];
  return <>
    <div className="summary-heading"><div><p className="eyebrow">Historical positions</p><h2>{data.name}</h2></div>
      <div className="valuation-date"><span>Historical valuation cutoff</span>{date
        ? <time dateTime={date}>{formatValuationDate(date)}</time> : <strong>No valuation date available</strong>}</div></div>
    <p className="holdings-date-note">These are historical records, not today’s live market values. Price dates are listed for each holding.
      {' '}Maximum price age: {data.max_price_age_days} calendar days{data.max_price_age_days === 0 ? ' (exact-date prices required)' : ''}.</p>
    {!data.valuation_complete && <div className="quality-banner" role="status"><strong>Incomplete valuation</strong>
      <p>Some positions cannot be valued. Portfolio totals and weights may be unavailable; unavailable amounts are never treated as zero.</p></div>}
    {data.warnings.length > 0 && <section className="quality-warnings" aria-label="API data-quality warnings"><h3>Data-quality notes</h3>
      <ul>{data.warnings.map((warning, index) => <li key={`${warning.code}-${warning.ticker}-${index}`}>
        {warning.ticker && <strong>{warning.ticker}: </strong>}{warning.message} <span className="warning-code">({warning.code})</span></li>)}</ul></section>}
    <dl className="summary-metrics holdings-metrics">{metrics.map((metric) => <div key={metric.label} className="summary-metric">
      <dt>{metric.label}</dt><dd className={metric.className}>{metric.value}</dd><dd className="metric-note">{metric.note}</dd></div>)}</dl>
    {data.holdings.length === 0 ? <div className="summary-state holdings-empty" role="status"><h2>No open holdings</h2>
      <p>No invested positions were reported for this cutoff.</p><button className="summary-button" onClick={retry}>Check again</button></div> : <>
      <section className="holdings-panel holdings-table-panel" aria-labelledby="positions-title"><div className="panel-heading">
        <h2 id="positions-title">Open positions</h2><span className="panel-caption">Values in {data.currency}, except explicitly labelled prices</span></div>
        <p className="price-details-hint">Open a price status for its observation date and age.</p>
        <div className="holdings-table-scroll" role="region" aria-label="Holdings table, scroll horizontally for all columns" tabIndex={0}>
          <table className="holdings-table"><caption className="sr-only">Historical holdings, including price freshness and currency status. Unavailable values are not zero.</caption>
            <thead><tr>{['Instrument', 'Shares', 'Market price', 'Price freshness', 'Market value', 'Weight', 'Remaining basis', 'Unrealized gain / loss'].map((label) => <th key={label} scope="col">{label}</th>)}</tr></thead>
            <tbody>{data.holdings.map((holding) => <tr key={holding.ticker}>
              <th scope="row"><strong>{holding.ticker}</strong><span className="instrument-name">{holding.instrument_name}</span>
                {holding.currency_status === 'conflict' && <span className="holding-issue">Currency conflict · values excluded</span>}</th>
              <td>{formatQuantity(holding.quantity)}</td>
              <td>{formatOptionalCurrency(holding.market_price, holding.market_price_currency)}<span className="cell-note">{holding.market_price_currency}</span></td>
              <td><details className="price-details"><summary className={`price-status price-${holding.price_status}`}
                aria-label={`${holding.ticker} price freshness: ${priceLabels[holding.price_status]}`}>{priceLabels[holding.price_status]}</summary>
                <span className="cell-note">{holding.effective_price_date ? formatValuationDate(holding.effective_price_date) : 'No observation'}</span>
                {holding.price_age_days !== null && <span className="cell-note">{holding.price_age_days} calendar days old at cutoff</span>}</details></td>
              <td>{formatOptionalCurrency(holding.market_value, data.currency)}</td>
              <td>{formatWeight(holding.invested_weight)}</td>
              <td>{formatOptionalCurrency(holding.remaining_average_cost_basis, data.currency)}</td>
              <td className={gainClass(holding.unrealized_gain_loss)}>{formatOptionalCurrency(holding.unrealized_gain_loss, data.currency)}</td>
            </tr>)}</tbody>
          </table>
        </div></section>
      <AllocationDonut data={data} />
    </>}
    <p className="valuation-note">Weights exclude cash. Remaining average-cost basis is the cost of open positions, not net transaction investment.
      {' '}No currency conversions or missing-price estimates are made.</p>
  </>;
}
