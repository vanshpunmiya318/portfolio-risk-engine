import { useEffect, useState } from 'react';
import { getPortfolioSummary } from '../api/portfolio';
import type { PortfolioSummary } from '../api/portfolio';
import { formatCurrency, formatValuationDate } from '../lib/format';

type SummaryState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'empty' }
  | { status: 'ready'; data: PortfolioSummary };

export function Overview({ portfolioId = 1, portfolioRevision }: { portfolioId?: number; portfolioRevision?: string }) {
  const [state, setState] = useState<SummaryState>({ status: 'loading' });
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setState({ status: 'loading' });
    getPortfolioSummary(controller.signal, portfolioId).then((data) => {
      if (!controller.signal.aborted) setState(data ? { status: 'ready', data } : { status: 'empty' });
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setState({
        status: 'error',
        message: error instanceof Error && error.message.startsWith('Portfolio request failed')
          ? error.message
          : 'Unable to load a valid summary. Check the API connection and try again.',
      });
    });
    return () => controller.abort();
  }, [portfolioId, portfolioRevision, attempt]);

  const retry = () => setAttempt((value) => value + 1);
  return <OverviewContent state={state} retry={retry} />;
}

export function OverviewContent({ state, retry }: { state: SummaryState; retry: () => void }) {
  return (
    <>
      <section className="page-intro">
        <p className="eyebrow">Portfolio intelligence</p>
        <h1>Portfolio overview</h1>
        <p className="lede">Portfolio value and invested capital, grounded in your historical portfolio data.</p>
      </section>
      <section className="summary-section overview-summary" aria-label="Portfolio summary" aria-busy={state.status === 'loading'}>
        {state.status === 'loading' && <div className="summary-state" role="status">
          <span className="eyebrow">Connecting to portfolio</span><h2>Loading your summary…</h2>
          <p>Retrieving the latest available historical valuation.</p>
        </div>}
        {state.status === 'error' && <div className="summary-state" role="alert">
          <span className="eyebrow">Unable to load</span><h2>Summary unavailable</h2>
          <p>{state.message}</p><button className="summary-button" onClick={retry}>Try again</button>
        </div>}
        {state.status === 'empty' && <div className="summary-state" role="status">
          <span className="eyebrow">No valuation available</span><h2>No portfolio summary yet</h2>
          <p>The API has no portfolio or market-price data to display.</p>
          <button className="summary-button" onClick={retry}>Check again</button>
        </div>}
        {state.status === 'ready' && <>
          <div className="summary-heading">
            <div><p className="eyebrow">Historical portfolio</p><h2><bdi>{state.data.name}</bdi></h2></div>
            <div className="valuation-date"><span>Historical valuation date</span>
              <time dateTime={state.data.as_of_date}>{formatValuationDate(state.data.as_of_date)}</time>
            </div>
          </div>
          <dl className="summary-metrics">
            <div className="summary-metric summary-metric-primary"><dt>Total equity</dt>
              <dd>{formatCurrency(state.data.total_equity, state.data.currency)}</dd>
              <dd className="metric-note">Holdings value plus cash balance</dd></div>
            <div className="summary-metric"><dt>Net contributions</dt>
              <dd>{formatCurrency(state.data.net_contributions, state.data.currency)}</dd>
              <dd className="metric-note">Deposits minus withdrawals</dd></div>
            <div className="summary-metric"><dt>Profit / loss</dt>
              <dd className={state.data.profit_loss < 0 ? 'value-negative' : state.data.profit_loss > 0 ? 'value-positive' : undefined}>
                {formatCurrency(state.data.profit_loss, state.data.currency)}</dd>
              <dd className="metric-note">Total equity minus net contributions</dd></div>
          </dl>
          <p className="valuation-note">All amounts in {state.data.currency}. Values are as of the historical valuation date above,
            not today’s account valuation or live market prices. Summary valuation uses the latest available historical prices without a maximum price-age restriction.</p>
        </>}
      </section>
      <footer className="page-footer"><span>Portfolio Risk Engine</span><span>Clarity before complexity.</span></footer>
    </>
  );
}
