# Portfolio Risk Engine frontend

React + TypeScript + Vite application backed by FastAPI.

## Development

Follow the [Windows local quickstart](../README.md#windows-local-quickstart) to install the backend
in `.venv`, run `npm.cmd ci`, and provision the database. From Windows CMD, start both servers with:

```bat
cd /d D:\portfolio-risk-engine
dev.cmd
```

Open **http://127.0.0.1:5173/holdings**. FastAPI runs at `http://127.0.0.1:8000`; Vite forwards `/api` requests to it. Keep the terminal open. Type `stop` then Enter to shut down both servers, or press Ctrl+C. Typing `stop` avoids CMD's possible batch-termination prompt.

The launcher waits for FastAPI's `/ready` to return HTTP 200 before starting Vite. `/health` only checks
process liveness. If startup fails because of the database, verify the file selected by
`RISKENGINE_DEMO=1`, `RISKENGINE_DB`, or the default `data/historical_portfolio.db`, in that order.
Run `dev.cmd` again after fixing the configuration or stopping a previous run.

The launcher uses `.venv\Scripts\python.exe`, Node/npm on PATH, and the existing `frontend/node_modules` installation. It does not install dependencies, activate a different environment, or change database selection. Install the project dependencies separately if they are missing. Run frontend-only commands from `frontend/`:

```sh
npm ci
npm run dev
npm run build
npm test
npm run preview
```

On Windows PowerShell with script execution restricted, use `npm.cmd` instead of `npm`.

The build runs strict TypeScript checking before bundling. Commit the lockfile; dependencies and build output are ignored.

`dev.cmd` checks both ports before starting, uses strict ports, and refuses a second runner. If either child fails, it stops the other. Normal shutdown requests graceful Uvicorn/Vite closure, with an owned-process-tree termination fallback after five seconds. It never terminates an unrelated server or silently switches ports.

If another application uses the defaults, either stop that application yourself or explicitly configure different ports in CMD:

```bat
set "RISKENGINE_API_PORT=8001"
set "RISKENGINE_VITE_PORT=5174"
dev.cmd
```

The launcher updates the in-memory Vite proxy to the configured API port. Ports must be different integers between 1024 and 65535. Clear those environment variables to restore defaults. The launcher creates an ignored lock at `tmp\portfolio-risk-dev.lock` and removes it after confirmed shutdown.

After a crash or a shutdown that requires manual inspection, verify that the launcher and its
backend and frontend processes have all exited and both configured ports are free before removing
the lock. The PID recorded in the lock identifies the launcher. The shutdown message identifies
any process tree whose termination the launcher could not confirm.

## Structure

- `src/App.tsx`: shared shell and seven React Router routes.
- `src/api/`: typed portfolio API clients.
- `src/lib/`: currency/date/weight formatting and allocation geometry.
- `src/components/AllocationDonut.tsx`: accessible SVG chart with a text legend; no chart dependency.
- `src/pages/`: route content.
- `src/theme/`: persisted light/dark/system preference, including live OS theme changes.
- `src/styles/tokens.css`: semantic colors, spacing, typography and shape tokens.
- `src/styles/base.css`: global primitives and accessibility defaults.
- `src/styles/shell.css`: responsive shell styles.

## Routes

- Overview — `/overview`
- Holdings — `/holdings`
- Transactions — `/transactions`
- Performance — `/performance`
- Risk Analytics — `/risk-analytics`
- Portfolio Allocation — `/portfolio-allocation`
- Reports — `/reports`

All seven pages use portfolio-specific APIs. For example, Overview fetches
`/api/portfolios/{portfolio_id}/summary`. The portfolio ID defaults to `1`. If the configured
database lacks that portfolio, set `VITE_PORTFOLIO_ID` to an existing ID in your environment or
an ignored `frontend/.env.local` file, then restart the launcher. Production Vite variables
are set at build time.

Holdings preserves nulls as "Unavailable", labels observed-price currencies and historical price dates, and shows all API warnings. The allocation chart uses reported fractional weights, excludes cash, and is withheld for incomplete, missing or inconsistent weights; it never rescales a partial allocation. The backend's default exact-date price policy is displayed; date and price-age controls are not yet exposed in this page.

`npm test` uses Node's built-in test runner and existing Vite/React packages, without additional dependencies. Tests require Node.js 22.18+ with native TypeScript support (tested on Node 26). They cover response validation, nulls, failures, formatting, chart geometry and server-rendered content; they do not replace browser interaction testing.

For frontend-only development, start FastAPI separately on `127.0.0.1:8000`, then use `npm.cmd run dev`. Use `dev.cmd` (or `npm.cmd run dev:all` from `frontend/`) to manage both together instead.

With the Windows quickstart dependencies installed, run `node --test scripts/dev.test.mjs` from the
repository root to test startup and cleanup with isolated databases and temporary ports.

Production hosting must serve `index.html` for client-side routes such as `/overview` (SPA fallback), and forward `/api` to FastAPI on the same origin. The development proxy is not a production deployment configuration. Authentication is deferred.
