import { NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { ThemeToggle } from './theme/ThemeProvider';
import { Overview } from './pages/Overview';

const pages = [
  { path: '/overview', title: 'Overview' },
  { path: '/holdings', title: 'Holdings' },
  { path: '/transactions', title: 'Transactions' },
  { path: '/performance', title: 'Performance' },
  { path: '/risk-analytics', title: 'Risk Analytics' },
  { path: '/portfolio-allocation', title: 'Portfolio Allocation' },
  { path: '/reports', title: 'Reports' },
];

export function App() {
  const location = useLocation();
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
            <NavLink key={page.path} to={page.path}>
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
          <Routes>
            <Route path="/" element={<Navigate to="/overview" replace />} />
            <Route path="/overview" element={<Overview />} />
            {pages.slice(1).map((page) => (
              <Route key={page.path} path={page.path} element={
                <section className="page-intro"><p className="eyebrow">Workspace / Coming next</p>
                  <h1>{page.title}</h1><p className="lede">This page is reserved for the next stage of migration.</p>
                  <NavLink className="text-link" to="/overview">Return to overview →</NavLink></section>
              } />
            ))}
            <Route path="*" element={<section className="page-intro"><h1>Page not found</h1>
              <NavLink className="text-link" to="/overview">Return to overview →</NavLink></section>} />
          </Routes>
        </main>
      </div>
    </div>
  );
}
