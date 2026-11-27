import { useRef, useState } from 'react';
import { usePortfolios } from './PortfolioProvider';
import type { Portfolio } from './api/portfolioManagement';

function PortfolioForm({ portfolio }: { portfolio?: Portfolio }) {
  const { directory, mutation, submit, canManage, pending, storageProblem } = usePortfolios();
  const [draft, setDraft] = useState<string | null>(null);
  const [currency, setCurrency] = useState('');
  const form = useRef<HTMLFormElement>(null);
  const name = draft ?? portfolio?.name ?? '';
  const busy = mutation.pending || !!pending || !!storageProblem || !canManage;
  const guidance = portfolio ? 'rename-portfolio-guidance' : 'create-portfolio-guidance';
  return <form ref={form} aria-label={portfolio ? 'Rename portfolio' : 'Create portfolio'} onSubmit={async event => {
    event.preventDefault();
    if (busy) return;
    const focused = document.activeElement;
    const saved = await submit(portfolio ? { name } : { name, currency }, portfolio?.id ?? null);
    if (saved) {
      setDraft(null);
      if (!portfolio) setCurrency('');
    }
    if (portfolio && document.activeElement === document.body && focused instanceof HTMLElement &&
        form.current?.contains(focused)) focused.focus();
  }}>
    <label>{portfolio ? 'New portfolio name' : 'Portfolio name'}
      <input required maxLength={256} dir="auto" value={name} onChange={event => setDraft(event.target.value)}
        readOnly={busy} aria-describedby={guidance} />
    </label>
    <p id={guidance}>Use 1 to 80 characters after whitespace normalization. Names must be unique, ignoring case.</p>
    {!portfolio && <label>Base currency
      <select required value={currency} onChange={event => setCurrency(event.target.value)} disabled={busy}>
        <option value="">Choose a currency</option>
        {directory?.supported_currencies.map(code => <option key={code}>{code}</option>)}
      </select>
    </label>}
    <button className="summary-button" aria-disabled={busy}>{portfolio ? 'Save name' : 'Create portfolio'}</button>
  </form>;
}

export function PortfolioControls() {
  const { directory, active, select, refresh, loading, error, invalidConfiguration, mutation, pending,
    reconcile, acknowledge, storageProblem } = usePortfolios();
  const first = directory?.portfolios.length === 0 && directory.write_availability === 'available';
  const forms = directory?.management_configured && directory.write_availability === 'available';
  return <section className="portfolio-controls" aria-label="Portfolio selection">
    {directory && <label className="active-portfolio">Active portfolio
      <select value={active?.id ?? ''} onChange={event => select(event.target.value ? Number(event.target.value) : null)}>
        <option value="">{directory.portfolios.length ? 'Choose a portfolio' : 'No selectable portfolios'}</option>
        {directory.portfolios.map(row => <option key={row.id} value={row.id}>
          {'#' + row.id + ' \u00b7 \u2068' + row.name.replace(/[\u202a-\u202e\u2066-\u2069]/g, '') + '\u2069 (' + row.currency + ')'}
        </option>)}
      </select>
    </label>}
    {loading && <p role="status">Loading portfolios&hellip;</p>}
    {error && <p role="alert">{error} {directory && 'The directory is stale. Changes are disabled until refresh succeeds.'}</p>}
    {invalidConfiguration && !active && <p role="alert">The configured portfolio is unavailable. Choose an existing portfolio or create one if management is enabled.</p>}
    {directory && <button className="summary-button" disabled={loading} onClick={() => void refresh()}>Refresh portfolios</button>}
    {!directory && error && <button className="summary-button" disabled={loading} onClick={() => void refresh()}>Refresh portfolios</button>}
    {!!directory?.unsupported.length && <details>
      <summary>Portfolios with unsupported IDs</summary>
      <ul>{directory.unsupported.map(row => <li key={row.id}><bdi>{row.id}</bdi> &middot; {row.reason === 'nonpositive'
        ? 'Portfolio IDs must be positive.' : 'This ID exceeds the exact integer range supported by the browser.'}</li>)}</ul>
    </details>}
    {storageProblem && <p role="alert">{storageProblem}</p>}
    {mutation.pending && <p role="status">Checking portfolio change&hellip;</p>}
    {mutation.error && <p role="alert">{mutation.error}</p>}
    {mutation.success && <p role="status">{mutation.success}</p>}
    {pending && <div className="portfolio-reconciliation">
      <p>{mutation.outcome === 'running' ? 'The change is still running.' : 'A previous change needs review.'}</p>
      <button className="summary-button" disabled={mutation.pending} onClick={() => void reconcile(false)}>Check change status</button>
      {(!mutation.outcome || mutation.outcome === 'prepared') && <button className="summary-button" disabled={mutation.pending}
        onClick={() => void reconcile(true)}>Close if unstarted</button>}
      {(mutation.outcome === 'unavailable' || mutation.outcome === 'unknown') &&
        <button className="summary-button" disabled={mutation.pending} onClick={acknowledge}>Acknowledge unresolved outcome</button>}
    </div>}
    {first && <div className="portfolio-management"><h2>{directory?.unsupported.length ? "Create a selectable portfolio" : "Create your first portfolio"}</h2><PortfolioForm /></div>}
    {forms && !first && <details className="portfolio-management">
      <summary>Manage portfolios</summary>
      <PortfolioForm />
      {active && <PortfolioForm key={active.id} portfolio={active} />}
    </details>}
    {directory && !forms && <p>{directory.management_configured
      ? 'Write availability could not be confirmed. Refresh portfolios before trying again.'
      : 'Read-only database. Creating or renaming a portfolio requires a writable managed database.'}</p>}
  </section>;
}
