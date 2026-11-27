import { NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { ThemeToggle } from './theme/ThemeProvider';
import { Overview } from './pages/Overview';
import { Holdings } from './pages/Holdings';
import { Performance } from './pages/Performance';
import { RiskAnalytics } from './pages/RiskAnalytics';
import { PortfolioAllocation } from './pages/PortfolioAllocation';
import { Transactions } from './pages/Transactions';
import { Reports } from './pages/Reports';
import { PortfolioProvider, usePortfolios } from './PortfolioProvider';
import { PortfolioControls } from './PortfolioControls';

const pages = [
  { path: '/overview', title: 'Overview', Page: Overview },
  { path: '/holdings', title: 'Holdings', Page: Holdings },
  { path: '/transactions', title: 'Transactions', Page: Transactions },
  { path: '/performance', title: 'Performance', Page: Performance },
  { path: '/risk-analytics', title: 'Risk Analytics', Page: RiskAnalytics },
  { path: '/portfolio-allocation', title: 'Portfolio Allocation', Page: PortfolioAllocation },
  { path: '/reports', title: 'Reports', Page: Reports },
];

export function App() {
  return <PortfolioProvider configured={import.meta.env.VITE_PORTFOLIO_ID}><Workspace /></PortfolioProvider>;
}

function Workspace() {
  const location = useLocation();
  const { active, directory, loading, error } = usePortfolios();
  const title = pages.find((page) => page.path === location.pathname)?.title ?? 'Page not found';
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">Skip to content</a>
      <aside className="sidebar">
        <NavLink className="brand" to="/overview" aria-label="Portfolio Risk Engine home">
          <span className="brand-mark" aria-hidden="true">PR</span>
          <span>Portfolio<span className="brand-subtitle">Risk Engine</span></span>
        </NavLink>
        <p className="nav-label">Workspace</p>
        <nav aria-label="Main navigation" className="page-nav">
          {pages.map((page, index) => (
            <NavLink key={page.path} to={page.path} onFocus={event => event.currentTarget.scrollIntoView({ block: 'nearest', inline: 'nearest' })}>
              <span className="nav-number" aria-hidden="true">{String(index + 1).padStart(2, '0')}</span>
              {page.title}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-footer"><span className="eyebrow">Built for perspective</span>
          <p>A considered view of portfolio risk.</p></div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <nav className="breadcrumb" aria-label="Breadcrumb"><span>Workspace</span>
            <span aria-hidden="true">/</span><strong>{title}</strong></nav>
          <ThemeToggle />
        </header>
        <main id="main-content" tabIndex={-1}>
          <PortfolioControls />
          <Routes>
            <Route path="/" element={<Navigate to="/overview" replace />} />
            {pages.map(({ path, title: pageTitle, Page }) => <Route key={path} path={path} element={active
              ? <div hidden={!!error}>
                  <Page key={JSON.stringify([directory?.context, active.id])} portfolioId={active.id}
                    portfolioRevision={active.name} directoryTrusted={!error && !loading} />
                </div>
              : <section className="page-intro"><h1>{pageTitle}</h1>
                  {directory && <p>{directory.portfolios.length ? 'Select a portfolio to continue.'
                    : directory.write_availability === 'available' ? 'Create a portfolio to open this page.'
                    : 'This database contains no portfolios. Creation is unavailable in read-only mode.'}</p>}
                  {!directory && !loading && <p>Refresh the portfolio list to continue.</p>}
                </section>
            } />)}
            <Route path="*" element={<section className="page-intro"><h1>Page not found</h1>
              <NavLink className="text-link" to="/overview">Return to overview →</NavLink></section>} />
          </Routes>
        </main>
      </div>
    </div>
  );
}
