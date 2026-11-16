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
    <p className="valuation-note transactions-scroll-note">Scroll tables horizontally to see all recorded fields. Keyboard users can focus a table and use the arrow keys.</p>
    <section className="holdings-panel"><h2 id="trades-title">Trades</h2>
      {data.trades.length === 0 ? <p role="status">No security trades recorded.</p> : <div className="holdings-table-scroll" role="region" aria-labelledby="trades-title" tabIndex={0}><table className="holdings-table transactions-table"><thead><tr>{['Recorded date / ID', 'Instrument', 'Type', 'Quantity', 'Price', 'Currency', 'Source / data quality'].map(h => <th scope="col" className={['Quantity', 'Price', 'Amount'].includes(h) ? 'recorded-number' : undefined} key={h}>{h}</th>)}</tr></thead><tbody>{data.trades.map(t => <tr key={t.id}>
        <td>{t.date}<span className="cell-note">Row {t.id}</span></td><td>{t.ticker ?? 'Missing instrument'}<span className="cell-note">{t.instrument_name ?? 'Metadata unavailable'} · Instrument {t.instrument_id}{t.instrument_currency !== null ? ` · ${t.instrument_currency}` : ''}</span></td><td>{t.transaction_type}</td><td className="recorded-number">{number(t.quantity)}</td><td className="recorded-number">{number(t.price)}</td><td>{t.currency}</td><td>{t.source || 'Source not supplied'}<Issues issues={t.issues} /></td>
      </tr>)}</tbody></table></div>}
    </section>
    <section className="holdings-panel"><h2 id="cash-flows-title">External cash flows</h2>
      {data.external_cash_flows.length === 0 ? <p role="status">No external cash flows recorded.</p> : <div className="holdings-table-scroll" role="region" aria-labelledby="cash-flows-title" tabIndex={0}><table className="holdings-table transactions-table"><thead><tr>{['Recorded date / ID', 'Type', 'Amount', 'Currency', 'Source / data quality'].map(h => <th scope="col" className={['Quantity', 'Price', 'Amount'].includes(h) ? 'recorded-number' : undefined} key={h}>{h}</th>)}</tr></thead><tbody>{data.external_cash_flows.map(c => <tr key={c.id}>
        <td>{c.date}<span className="cell-note">Row {c.id}</span></td><td>{c.flow_type}</td><td className="recorded-number">{number(c.amount)}</td><td>{c.currency}</td><td>{c.source || 'Source not supplied'}<Issues issues={c.issues} /></td>
      </tr>)}</tbody></table></div>}
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
