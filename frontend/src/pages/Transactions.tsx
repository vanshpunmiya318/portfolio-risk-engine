import { TableScroll } from '../components/TableScroll';
import { useEffect, useRef, useState } from 'react';
import { createTransactionsLoader, TransactionsRequestError } from '../api/transactions';
import type { TransactionsResponse, HistoryIssue } from '../api/transactions';

// Recorded numbers are displayed without rounding away small fractional quantities/prices.
const number = (value: number) => String(value);
function Issues({ issues }: { issues: HistoryIssue[] }) {
  return issues.length ? <ul className="transaction-issues">{issues.map((issue, index) => <li key={index}>{issue.message} <code>{issue.code}</code></li>)}</ul> : null;
}
export function TransactionsContent({ data }: { data: TransactionsResponse }) {
  return <>
    <p className="valuation-note">{data.name} · Portfolio currency: {data.currency}. Each row retains its recorded currency; no currency conversion is performed. Dates are recorded ledger dates. Sorted by recorded date, then row ID, newest first.</p>
    <p className="valuation-note">Security trades buy or sell investments; they are not external contributions. Deposits and withdrawals are shown separately. History does not require market prices or a successful valuation. Quantities and prices are recorded trade values, not current split-adjusted holdings.</p>
    <section className="holdings-panel"><h2 id="trades-title">Trades</h2>
      {data.trades.length === 0 ? <p role="status">No security trades recorded.</p> : <TableScroll label="Trades"><table className="holdings-table transactions-table trades-table"><caption className="sr-only">Recorded security trades, including ledger row IDs, currencies, sources and data-quality notes.</caption><thead><tr>{['Recorded date / ID', 'Instrument', 'Type', 'Quantity', 'Price', 'Currency', 'Source / data quality'].map(h => <th scope="col" className={['Quantity', 'Price', 'Amount'].includes(h) ? 'recorded-number' : h === 'Recorded date / ID' ? 'table-date' : h === 'Instrument' ? 'table-identity' : h === 'Source / data quality' ? 'table-metadata' : h === 'Type' ? 'table-type' : 'table-currency'} key={h}>{h}</th>)}</tr></thead><tbody>{data.trades.map(t => <tr key={t.id}>
        <th scope="row" className="table-date">{t.date}<span className="cell-note">Row {t.id}</span></th><td className="table-identity">{t.ticker ?? 'Missing instrument'}<span className="cell-note">{t.instrument_name ?? 'Metadata unavailable'} · Instrument {t.instrument_id}{t.instrument_currency !== null ? ` · ${t.instrument_currency}` : ''}</span></td><td className="table-type">{t.transaction_type}</td><td className="recorded-number">{number(t.quantity)}</td><td className="recorded-number">{number(t.price)}</td><td className="table-currency">{t.currency}</td><td className="table-metadata">{t.source || 'Source not supplied'}<Issues issues={t.issues} /></td>
      </tr>)}</tbody></table></TableScroll>}
    </section>
    <section className="holdings-panel"><h2 id="cash-flows-title">External cash flows</h2>
      {data.external_cash_flows.length === 0 ? <p role="status">No external cash flows recorded.</p> : <TableScroll label="External cash flows"><table className="holdings-table transactions-table cash-flows-table"><caption className="sr-only">Recorded external cash flows, including ledger row IDs, currencies, sources and data-quality notes.</caption><thead><tr>{['Recorded date / ID', 'Type', 'Amount', 'Currency', 'Source / data quality'].map(h => <th scope="col" className={['Quantity', 'Price', 'Amount'].includes(h) ? 'recorded-number' : h === 'Recorded date / ID' ? 'table-date' : h === 'Instrument' ? 'table-identity' : h === 'Source / data quality' ? 'table-metadata' : h === 'Type' ? 'table-type' : 'table-currency'} key={h}>{h}</th>)}</tr></thead><tbody>{data.external_cash_flows.map(c => <tr key={c.id}>
        <th scope="row" className="table-date">{c.date}<span className="cell-note">Row {c.id}</span></th><td className="table-type">{c.flow_type}</td><td className="recorded-number">{number(c.amount)}</td><td className="table-currency">{c.currency}</td><td className="table-metadata">{c.source || 'Source not supplied'}<Issues issues={c.issues} /></td>
      </tr>)}</tbody></table></TableScroll>}
    </section>
  </>;
}
export function Transactions({ portfolioId = 1 }: { portfolioId?: number }) {
  const [state, setState] = useState<{ status: 'loading' } | { status: 'error'; message: string; details: string[] } | { status: 'ready'; data: TransactionsResponse }>({ status: 'loading' });
  const [attempt, setAttempt] = useState(0);
  const loader = useRef<ReturnType<typeof createTransactionsLoader> | null>(null);
  if (!loader.current) loader.current = createTransactionsLoader();
  useEffect(() => {
    const requests = loader.current!; setState({ status: 'loading' });
    void requests.load(portfolioId, data => setState({ status: 'ready', data }), error => setState({ status: 'error', message: error instanceof Error ? error.message : 'Unable to load transactions.', details: error instanceof TransactionsRequestError ? error.details : [] }));
    return () => requests.cancel();
  }, [portfolioId, attempt]);
  return <>
    <section className="page-intro"><p className="eyebrow">Portfolio records</p><h1>Transactions</h1><p className="lede">Read-only security trades and external cash flows.</p></section>
    <section aria-label="Transactions history" aria-busy={state.status === 'loading'}>
      {state.status === 'loading' || (state.status === 'ready' && state.data.portfolio_id !== portfolioId) ? <p className="summary-state" role="status">Loading transaction history…</p> : state.status === 'error' ? <div className="summary-state" role="alert"><h2>Unable to load transaction history</h2><p>{state.message}</p>{state.details.map((detail, i) => <p key={i}>{detail}</p>)}<button className="summary-button" onClick={() => setAttempt(n => n + 1)}>Retry</button></div> : <TransactionsContent data={state.data} />}
    </section>
  </>;
}
